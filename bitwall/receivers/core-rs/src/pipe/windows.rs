//! Named pipes, as bitwall/local.py makes them on Windows: each a pipe at
//! a wall's path under `//./pipe/bitwall-<user>`, refusing remote clients,
//! read overlapped by a thread of its own. The door keeps four instances,
//! the first made first of its name, which is the claim. `out` is written
//! without waiting (PIPE_NOWAIT). Every pipe is made for its user, the
//! administrators and the system, so a wall made from an elevated console
//! is reached from one that is not, and from ssh.

use super::{Piece, Slot, Wrote};
use std::io;
use std::sync::{Arc, OnceLock};
use std::thread;
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Security::Authorization::*;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::{ReadFile, WriteFile};
use windows_sys::Win32::System::Pipes::*;
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::System::IO::*;

const INBOUND: u32 = 0x1;
const OUTBOUND: u32 = 0x2;
const OVERLAPPED_FLAG: u32 = 0x4000_0000;
const FIRST: u32 = 0x0008_0000;
const NOWAIT: u32 = 0x1;
const LOCAL_ONLY: u32 = 0x8;
/// What a wall gives each pipe it reads, as a fifo on a Pi gets.
const IN_BUFFER: u32 = 1 << 20;
const OUT_BUFFER: u32 = 1 << 16;
const DOOR_INSTANCES: usize = 4;
const DOOR_MAX: usize = 4096;

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}

fn native(path: &str) -> String {
    path.replace('/', "\\")
}

fn invalid(h: HANDLE) -> bool {
    h == INVALID_HANDLE_VALUE || h.is_null()
}

/// The machine's walls are this user's: the user is in the name, since the
/// pipe namespace is shared by every user on the machine.
pub fn run_dir() -> String {
    let user = std::env::var("USERNAME").unwrap_or_else(|_| "user".into()).to_lowercase();
    format!("//./pipe/bitwall-{user}")
}

fn user_sid() -> Option<String> {
    unsafe {
        let mut token: HANDLE = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return None;
        }
        let mut need = 0u32;
        GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut need);
        let mut buf = vec![0u8; need as usize];
        let ok = GetTokenInformation(token, TokenUser, buf.as_mut_ptr() as _, need, &mut need);
        CloseHandle(token);
        if ok == 0 {
            return None;
        }
        let user = &*(buf.as_ptr() as *const TOKEN_USER);
        let mut text: *mut u16 = std::ptr::null_mut();
        if ConvertSidToStringSidW(user.User.Sid, &mut text) == 0 {
            return None;
        }
        let mut n = 0;
        while *text.add(n) != 0 {
            n += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, n));
        LocalFree(text as _);
        Some(sid)
    }
}

/// The security every pipe is made with, made once.
fn security() -> *const SECURITY_ATTRIBUTES {
    static SA: OnceLock<usize> = OnceLock::new();
    *SA.get_or_init(|| unsafe {
        let me = user_sid().map(|s| format!("(A;;GA;;;{s})")).unwrap_or_default();
        let sddl = wide(&format!("D:P(A;;GA;;;SY)(A;;GA;;;BA){me}"));
        let mut sd: PSECURITY_DESCRIPTOR = std::ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.as_ptr(), SDDL_REVISION_1, &mut sd, std::ptr::null_mut()) == 0 {
            return 0;
        }
        let sa = Box::new(SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd,
            bInheritHandle: 0,
        });
        Box::into_raw(sa) as usize
    }) as *const SECURITY_ATTRIBUTES
}

/// A handle that may cross to the reader's thread.
#[derive(Clone, Copy)]
struct H(usize);
unsafe impl Send for H {}
impl H {
    fn get(self) -> HANDLE {
        self.0 as HANDLE
    }
}

/// An event that closes itself when the last holder lets it go.
struct Event(H);
impl Event {
    fn new() -> Event {
        Event(H(unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) } as usize))
    }
}
impl Drop for Event {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0.get()) };
    }
}
unsafe impl Send for Event {}
unsafe impl Sync for Event {}

fn make_in(name: &[u16], buffer: u32, instances: u32, first: bool) -> HANDLE {
    unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            INBOUND | OVERLAPPED_FLAG | if first { FIRST } else { 0 },
            LOCAL_ONLY,
            instances,
            4096,
            buffer,
            0,
            security(),
        )
    }
}

/// A pipe the wall reads: `in`, or one of the door's instances.
pub struct Reader {
    slot: Arc<Slot>,
    stop: Arc<Event>,
}

impl Reader {
    fn new(path: &str, buffer: u32, instances: u32, whole: bool, first: bool) -> io::Result<Reader> {
        let name = wide(&native(path));
        let h = make_in(&name, buffer, instances, first);
        if invalid(h) {
            return Err(io::Error::last_os_error());
        }
        let slot = Arc::new(Slot::new());
        let stop = Arc::new(Event::new());
        let (s2, e2, h2) = (slot.clone(), stop.clone(), H(h as usize));
        thread::spawn(move || read_loop(h2, name, buffer, instances, whole, s2, e2));
        Ok(Reader { slot, stop })
    }

    pub fn take(&self) -> Option<Piece> {
        self.slot.take()
    }

    pub fn close(&self) {
        self.slot.close();
        unsafe { SetEvent(self.stop.0.get()) };
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.close();
    }
}

/// An overlapped call's result as an error code, 0 for done; given up when
/// the reader is closed.
unsafe fn finish(h: HANDLE, ok: BOOL, ov: *mut OVERLAPPED, ev: HANDLE, stop: HANDLE, n: &mut u32) -> u32 {
    if ok == 0 {
        let e = GetLastError();
        if e != ERROR_IO_PENDING {
            return e;
        }
        let both = [ev, stop];
        if WaitForMultipleObjects(2, both.as_ptr(), 0, INFINITE) == WAIT_OBJECT_0 + 1 {
            CancelIoEx(h, ov);
        }
    }
    if GetOverlappedResult(h, ov, n, 1) == 0 {
        GetLastError()
    } else {
        0
    }
}

fn read_loop(h0: H, name: Vec<u16>, buffer: u32, instances: u32, whole: bool, slot: Arc<Slot>, stop: Arc<Event>) {
    unsafe {
        let ev = Event::new();
        let mut ov: Box<OVERLAPPED> = Box::new(std::mem::zeroed());
        let mut buf = vec![0u8; 1 << 20];
        let mut h = h0.get();
        let arm = |ov: &mut OVERLAPPED| {
            ResetEvent(ev.0.get());
            *ov = std::mem::zeroed();
            ov.hEvent = ev.0.get();
        };
        let mut n = 0u32;
        while !slot.closed() {
            arm(&mut ov);
            let ok = ConnectNamedPipe(h, &mut *ov);
            let e = finish(h, ok, &mut *ov, ev.0.get(), stop.0.get(), &mut n);
            if slot.closed() {
                break;
            }
            // Connected, or came and went, its bytes still to read.
            if e != 0 && e != ERROR_PIPE_CONNECTED && e != ERROR_NO_DATA {
                break;
            }
            // The next writer's instance is there before this one goes,
            // so a writer that comes now is never told there is no pipe.
            let next = make_in(&name, buffer, instances, false);
            let mut held = Vec::new();
            while !slot.closed() {
                arm(&mut ov);
                let ok = ReadFile(h, buf.as_mut_ptr(), buf.len() as u32, std::ptr::null_mut(), &mut *ov);
                if finish(h, ok, &mut *ov, ev.0.get(), stop.0.get(), &mut n) != 0 {
                    break;
                }
                if n == 0 {
                    continue;
                }
                if !whole {
                    slot.push(Some(buf[..n as usize].to_vec()), false);
                } else if held.len() < DOOR_MAX {
                    held.extend_from_slice(&buf[..n as usize]);
                }
            }
            if !slot.closed() {
                if !whole {
                    slot.push(None, true);
                } else if !held.is_empty() {
                    slot.push(Some(held), false);
                }
            }
            CloseHandle(h);
            h = next;
            if invalid(h) {
                return;
            }
        }
        if !invalid(h) {
            CloseHandle(h);
        }
    }
}

/// A wall's door: four instances, so callers at once each have one, and
/// each caller's request handed on whole when it closes.
pub struct Door {
    ends: Vec<Reader>,
}

impl Door {
    /// Every request that has come, together.
    pub fn take(&self) -> Vec<u8> {
        let mut out = Vec::new();
        for e in &self.ends {
            while let Some(Piece::Data(d)) = e.take() {
                out.extend_from_slice(&d);
                out.push(b'\n');
            }
        }
        out
    }
}

/// Claim `<run>/bitwall/<wall>`: its door's first instance, which fails
/// while a live receiver holds the name. The wall's path, and its door.
pub fn claim(run: &str, wall: &str) -> Result<(String, Door), String> {
    let dir = format!("{}/bitwall/{}", run.trim_end_matches('/'), wall);
    let door = format!("{dir}/door.in");
    let mut ends = Vec::new();
    for i in 0..DOOR_INSTANCES {
        match Reader::new(&door, 4096, (DOOR_INSTANCES * 2) as u32, true, i == 0) {
            Ok(r) => ends.push(r),
            Err(e) if i == 0 => {
                let code = e.raw_os_error().unwrap_or(0) as u32;
                if code == ERROR_ACCESS_DENIED || code == ERROR_PIPE_BUSY {
                    return Err(format!("{dir}: its door is read"));
                }
                return Err(format!("{dir}: {e}"));
            }
            Err(e) => return Err(format!("{dir}: {e}")),
        }
    }
    Ok((dir, Door { ends }))
}

/// Nothing to take away: a wall's pipes go with its handles.
pub fn unclaim(_dir: &str) {}

/// A pipe the wall writes, `out`: written without waiting, a write that
/// does not fit taking nothing.
pub struct Out {
    h: H,
    connected: bool,
}

impl Out {
    pub fn write(&mut self, data: &[u8]) -> Wrote {
        if self.h.0 == 0 {
            return Wrote::Gone;
        }
        let mut n = 0u32;
        let ok = unsafe { WriteFile(self.h.get(), data.as_ptr(), data.len() as u32, &mut n, std::ptr::null_mut()) };
        if ok == 0 {
            return match unsafe { GetLastError() } {
                ERROR_PIPE_LISTENING => Wrote::Full,
                _ => Wrote::Gone,
            };
        }
        self.connected = true;
        if n == 0 && !data.is_empty() {
            Wrote::Full
        } else {
            Wrote::Took(n as usize)
        }
    }

    /// Whether what was written will reach a reader: on a pipe, what was
    /// written to one that has been read outlives the wall's handle.
    pub fn drained(&self) -> bool {
        self.connected
    }

    pub fn close(&mut self) {
        if self.h.0 != 0 {
            unsafe { CloseHandle(self.h.get()) };
            self.h = H(0);
        }
    }
}

impl Drop for Out {
    fn drop(&mut self) {
        self.close();
    }
}

/// A session's pipes at `<wall>/s/<name>`: `in`, then `out` last, so a
/// session whose `out` is there is whole.
pub fn session(dir: &str) -> io::Result<(Reader, Out)> {
    let r = Reader::new(&format!("{dir}/in"), IN_BUFFER, 2, false, false)?;
    let name = wide(&native(&format!("{dir}/out")));
    let h = unsafe { CreateNamedPipeW(name.as_ptr(), OUTBOUND, NOWAIT | LOCAL_ONLY, 1, OUT_BUFFER, 4096, 0, security()) };
    if invalid(h) {
        r.close();
        return Err(io::Error::last_os_error());
    }
    Ok((r, Out { h: H(h as usize), connected: false }))
}

/// Nothing to take away: a session's pipes go with its handles.
pub fn unsession(_dir: &str) {}
