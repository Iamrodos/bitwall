//! Fifos, as bitwall/local.py and receivers/core.py keep them on a Mac or
//! Linux: a wall built under a `.` name and renamed into place, taking
//! over a wall whose door nobody reads; each fifo the wall reads held open
//! without a writer, so it sees its writer go, and read by a thread polling
//! it; `out` held open for reading and writing, so what is said before its
//! reader comes waits in it.

use super::{Piece, Slot, Wrote};
use std::ffi::CString;
use std::io;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

pub fn run_dir() -> String {
    match std::env::var("XDG_RUNTIME_DIR") {
        Ok(d) if !d.is_empty() => d,
        _ => format!("{}/.bitwall/run", std::env::var("HOME").unwrap_or_else(|_| ".".into())),
    }
}

fn c(path: &str) -> CString {
    CString::new(path).unwrap_or_default()
}

fn mkfifo(path: &str) -> io::Result<()> {
    if unsafe { libc::mkfifo(c(path).as_ptr(), 0o600) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn open(path: &str, flags: i32) -> io::Result<i32> {
    let fd = unsafe { libc::open(c(path).as_ptr(), flags | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(fd)
}

/// Whether something reads the door at `path`: a live wall's.
fn door_live(path: &str) -> bool {
    match open(path, libc::O_WRONLY | libc::O_NONBLOCK) {
        Ok(fd) => {
            unsafe { libc::close(fd) };
            true
        }
        Err(_) => false,
    }
}

/// A fifo the wall reads, held open without a writer.
pub struct Reader {
    slot: Arc<Slot>,
}

impl Reader {
    fn new(path: &str, door: bool) -> io::Result<Reader> {
        mkfifo(path)?;
        let fd = open(path, libc::O_RDONLY | libc::O_NONBLOCK)?;
        #[cfg(target_os = "linux")]
        if !door {
            // As F_SETPIPE_SZ gives a fifo on a Pi: how far a writer may
            // get ahead of the wall.
            unsafe { libc::fcntl(fd, 1031, 1 << 20) };
        }
        let slot = Arc::new(Slot::new());
        let s2 = slot.clone();
        thread::spawn(move || read_loop(fd, door, s2));
        Ok(Reader { slot })
    }

    pub fn take(&self) -> Option<Piece> {
        self.slot.take()
    }

    pub fn close(&self) {
        self.slot.close();
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.close();
    }
}

/// Polled rather than read blocking, so a reader closed is let go within
/// a tenth of a second. An empty read before any byte is a fifo nobody
/// has opened yet, not an end; after one, the writer has gone. A door
/// never ends: its callers come and go. A Mac's poll() does not wake for
/// a writer that was waiting on a full fifo and has written again, so
/// after a read the next poll waits a millisecond rather than a tenth of
/// a second: a frame bigger than the fifo would otherwise come a tenth of
/// a second a fifo's worth.
fn read_loop(fd: i32, door: bool, slot: Arc<Slot>) {
    let mut buf = vec![0u8; 1 << 20];
    let mut got = false;
    let mut hot = false;
    while !slot.closed() {
        let mut p = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
        let r = unsafe { libc::poll(&mut p, 1, if hot { 1 } else { 100 }) };
        hot = false;
        if slot.closed() {
            break;
        }
        // A Mac's poll() never says a fifo's writer has gone, so once one
        // has written, a quiet tenth of a second is a read as well: no
        // bytes and no writer is 0, a writer still there EAGAIN.
        if r <= 0 && !(got && r == 0) {
            continue;
        }
        let n = unsafe { libc::read(fd, buf.as_mut_ptr() as _, buf.len()) };
        if n > 0 {
            got = true;
            hot = true;
            slot.push(Some(buf[..n as usize].to_vec()), false);
        } else if n == 0 {
            if got && !door {
                slot.push(None, true);
                got = false;
            }
            thread::sleep(Duration::from_millis(20));
        } else {
            let e = io::Error::last_os_error().raw_os_error().unwrap_or(0);
            if e != libc::EAGAIN && e != libc::EINTR {
                break;
            }
        }
    }
    unsafe { libc::close(fd) };
}

pub struct Door {
    r: Reader,
}

impl Door {
    pub fn take(&self) -> Vec<u8> {
        let mut out = Vec::new();
        while let Some(Piece::Data(d)) = self.r.take() {
            out.extend_from_slice(&d);
        }
        out
    }
}

/// Claim `<run>/bitwall/<wall>`: built under a `.` name, a wall whose door
/// nobody reads taken over, renamed into place.
pub fn claim(run: &str, wall: &str) -> Result<(String, Door), String> {
    let walls = format!("{}/bitwall", run.trim_end_matches('/'));
    let err = |e: io::Error| format!("{walls}: {e}");
    std::fs::create_dir_all(&walls).map_err(err)?;
    let tmp = format!("{walls}/.{wall}-{}", std::process::id());
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(format!("{tmp}/s")).map_err(err)?;
    let r = Reader::new(&format!("{tmp}/door.in"), true).map_err(err)?;
    let fin = format!("{walls}/{wall}");
    if std::fs::symlink_metadata(&fin).is_ok() {
        if door_live(&format!("{fin}/door.in")) {
            r.close();
            let _ = std::fs::remove_dir_all(&tmp);
            return Err(format!("{fin}: its door is read"));
        }
        std::fs::remove_dir_all(&fin).map_err(err)?;
    }
    std::fs::rename(&tmp, &fin).map_err(err)?;
    Ok((fin, Door { r }))
}

/// A receiver that stops in good order removes the walls it made.
pub fn unclaim(dir: &str) {
    let _ = std::fs::remove_dir_all(dir);
}

pub struct Out {
    fd: i32,
}

impl Out {
    pub fn write(&mut self, data: &[u8]) -> Wrote {
        if self.fd < 0 {
            return Wrote::Gone;
        }
        let n = unsafe { libc::write(self.fd, data.as_ptr() as _, data.len()) };
        if n > 0 {
            Wrote::Took(n as usize)
        } else if n == 0 {
            Wrote::Full
        } else {
            match io::Error::last_os_error().raw_os_error().unwrap_or(0) {
                e if e == libc::EAGAIN || e == libc::EINTR => Wrote::Full,
                _ => Wrote::Gone,
            }
        }
    }

    /// Whether its reader has everything: nothing left in the fifo, which
    /// the wall holds open for reading itself.
    pub fn drained(&self) -> bool {
        if self.fd < 0 {
            return true;
        }
        let mut p = libc::pollfd { fd: self.fd, events: libc::POLLIN, revents: 0 };
        unsafe { libc::poll(&mut p, 1, 0) <= 0 }
    }

    pub fn close(&mut self) {
        if self.fd >= 0 {
            unsafe { libc::close(self.fd) };
            self.fd = -1;
        }
    }
}

impl Drop for Out {
    fn drop(&mut self) {
        self.close();
    }
}

/// A session's directory with its fifos, `out` made last.
pub fn session(dir: &str) -> io::Result<(Reader, Out)> {
    std::fs::create_dir(dir)?;
    let r = Reader::new(&format!("{dir}/in"), false)?;
    let out = format!("{dir}/out");
    mkfifo(&out)?;
    let fd = open(&out, libc::O_RDWR | libc::O_NONBLOCK)?;
    Ok((r, Out { fd }))
}

pub fn unsession(dir: &str) {
    let _ = std::fs::remove_dir_all(dir);
}
