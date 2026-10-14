//! A running sender moved to another wall by its controller
//! (`bitwall/moving.py`).
//!
//! A controller moves a sender it started by writing the new wall into the
//! sender's record, the file `BITWALL_RECORD` names, as `target`, and
//! sending the sender's process SIGUSR1: the pid alone, never its group.
//! The library catches the signal and moves every Wall and layer of this
//! process that says the controller's instance to the wall the record
//! names, so a sender built on them moves with all its state and needs no
//! code of its own.
//!
//! The handler only writes a byte down a pipe, which is all a signal
//! handler may safely do; a thread of its own reads it and makes the move,
//! one at a time. It is set by the first thing held with a record, and
//! only where nothing else has claimed SIGUSR1.
//!
//! Anything that can move implements `Retarget` and is held weakly with
//! `hold`, so a Wall or layer let go of is let go of here too.

use std::sync::{Arc, Mutex, OnceLock, Weak};

/// Something a controller may move: a Wall, a cells layer, a video layer.
pub trait Retarget: Send + Sync {
    /// Move to the wall `target` names (`host`, `host:<wall>`), keeping
    /// everything held.
    fn retarget(self: Arc<Self>, target: &str) -> Result<(), String>;
    /// The wall it is on now, for saying what could not be moved.
    fn label(&self) -> String;
}

static HELD: Mutex<Vec<Weak<dyn Retarget>>> = Mutex::new(Vec::new());
static RECORD: OnceLock<String> = OnceLock::new();
/// One move at a time: a second signal waits for the first to be done.
static MOVING: Mutex<()> = Mutex::new(());

/// Move `obj` with the rest when the controller says, reading the new wall
/// from `record` (the sender's `BITWALL_RECORD`). The first record given
/// is the one read, and sets the handler.
pub fn hold(obj: Weak<dyn Retarget>, record: Option<&str>) {
    {
        let mut held = HELD.lock().unwrap_or_else(|e| e.into_inner());
        held.retain(|w| w.strong_count() > 0);
        held.push(obj);
    }
    let Some(record) = record.filter(|r| !r.is_empty()) else { return };
    if RECORD.set(record.to_string()).is_err() {
        return;
    }
    install();
}

/// The record named in this process's environment, `BITWALL_RECORD`.
pub fn record_from_env() -> Option<String> {
    std::env::var("BITWALL_RECORD").ok().filter(|r| !r.is_empty())
}

/// Move everything held to the wall the record names now.
pub fn move_now() {
    let _one = MOVING.lock().unwrap_or_else(|e| e.into_inner());
    let Some(path) = RECORD.get() else {
        log("told to move, and there is no record to read");
        return;
    };
    let target = match std::fs::read_to_string(path) {
        Ok(text) => match record_target(&text) {
            Ok(Some(t)) if !t.is_empty() => t,
            Ok(_) => {
                log("told to move, and the record names no target");
                return;
            }
            Err(e) => {
                log(&format!("told to move, and the record could not be read: {e}"));
                return;
            }
        },
        Err(e) => {
            log(&format!("told to move, and the record could not be read: {e}"));
            return;
        }
    };
    let held: Vec<Arc<dyn Retarget>> = {
        let mut held = HELD.lock().unwrap_or_else(|e| e.into_inner());
        held.retain(|w| w.strong_count() > 0);
        held.iter().filter_map(Weak::upgrade).collect()
    };
    for obj in held {
        let was = obj.label();
        if let Err(e) = obj.retarget(&target) {
            log(&format!("{was}: could not move to {target}: {e}"));
        }
    }
}

fn log(text: &str) {
    eprintln!("bitwall: {text}");
}

// ---------------------------------------------------------- the signal

#[cfg(unix)]
mod sig {
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicI32, Ordering};
    use std::thread;

    #[cfg(any(target_os = "linux", target_os = "android"))]
    const SIGUSR1: i32 = 10;
    #[cfg(not(any(target_os = "linux", target_os = "android")))]
    const SIGUSR1: i32 = 30;
    const SIG_DFL: usize = 0;
    const SIG_ERR: usize = usize::MAX;

    unsafe extern "C" {
        fn signal(signum: i32, handler: usize) -> usize;
        fn write(fd: i32, buf: *const u8, n: usize) -> isize;
    }

    /// The pipe's end the handler writes to, -1 until there is one.
    static WAKE: AtomicI32 = AtomicI32::new(-1);
    static ENDS: OnceLock<UnixStream> = OnceLock::new();

    extern "C" fn on_usr1(_: i32) {
        let fd = WAKE.load(Ordering::SeqCst);
        if fd >= 0 {
            let b = 1u8;
            // SAFETY: write(2) is async-signal-safe, and the end is
            // non-blocking: a full pipe has a move coming already.
            unsafe { write(fd, &b, 1) };
        }
    }

    pub fn install() {
        let (tx, mut rx) = match UnixStream::pair() {
            Ok(p) => p,
            Err(e) => {
                super::log(&format!("no pipe for SIGUSR1 ({e}), so a controller cannot move this sender"));
                return;
            }
        };
        let _ = tx.set_nonblocking(true);
        WAKE.store(tx.as_raw_fd(), Ordering::SeqCst);
        let _ = ENDS.set(tx);
        let handler = on_usr1 as extern "C" fn(i32) as usize;
        // SAFETY: the handler only writes a byte to a descriptor.
        let prev = unsafe { signal(SIGUSR1, handler) };
        if prev != SIG_DFL {
            if prev != SIG_ERR {
                // SAFETY: putting back what was there.
                unsafe { signal(SIGUSR1, prev) };
            }
            super::log("SIGUSR1 is the sender's own, so a controller cannot move it");
            return;
        }
        let _ = thread::Builder::new().name("bitwall-move".into()).spawn(move || {
            let mut b = [0u8; 1];
            while matches!(rx.read(&mut b), Ok(1)) {
                super::move_now();
            }
        });
    }
}

#[cfg(unix)]
fn install() {
    sig::install();
}

#[cfg(not(unix))]
fn install() {
    log("this machine has no SIGUSR1, so a controller cannot move this sender");
}

// ---------------------------------------------------------- the record

/// `target` from a record's JSON: the string at that key of the top-level
/// object, None where it is absent or not a string.
pub fn record_target(text: &str) -> Result<Option<String>, String> {
    let mut p = Json { s: text.as_bytes(), i: 0 };
    p.ws();
    p.eat(b'{')?;
    let mut found = None;
    p.ws();
    if p.peek() == Some(b'}') {
        return Ok(None);
    }
    loop {
        p.ws();
        let key = p.string()?;
        p.ws();
        p.eat(b':')?;
        p.ws();
        if key == "target" && p.peek() == Some(b'"') {
            found = Some(p.string()?);
        } else {
            p.value()?;
            if key == "target" {
                found = None;
            }
        }
        p.ws();
        match p.next() {
            Some(b',') => continue,
            Some(b'}') => return Ok(found),
            _ => return Err("not a JSON object".into()),
        }
    }
}

struct Json<'a> {
    s: &'a [u8],
    i: usize,
}

impl Json<'_> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn next(&mut self) -> Option<u8> {
        let c = self.peek();
        self.i += 1;
        c
    }

    fn ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn eat(&mut self, c: u8) -> Result<(), String> {
        if self.next() == Some(c) { Ok(()) } else { Err(format!("expected {:?}", c as char)) }
    }

    fn string(&mut self) -> Result<String, String> {
        self.eat(b'"')?;
        let mut out: Vec<u16> = Vec::new();
        let mut bytes = Vec::new();
        let flush = |bytes: &mut Vec<u8>, out: &mut Vec<u16>| {
            out.extend(String::from_utf8_lossy(bytes).encode_utf16());
            bytes.clear();
        };
        loop {
            match self.next().ok_or("a string runs off the end")? {
                b'"' => break,
                b'\\' => {
                    flush(&mut bytes, &mut out);
                    let c = self.next().ok_or("a string runs off the end")?;
                    match c {
                        b'"' | b'\\' | b'/' => out.push(c as u16),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(10),
                        b'r' => out.push(13),
                        b't' => out.push(9),
                        b'u' => {
                            let hex = self.s.get(self.i..self.i + 4).ok_or("a short \\u")?;
                            let hex = std::str::from_utf8(hex).map_err(|_| "a bad \\u")?;
                            out.push(u16::from_str_radix(hex, 16).map_err(|_| "a bad \\u")?);
                            self.i += 4;
                        }
                        _ => return Err("a bad escape".into()),
                    }
                }
                c => bytes.push(c),
            }
        }
        flush(&mut bytes, &mut out);
        Ok(String::from_utf16_lossy(&out))
    }

    /// Skip one value of any kind.
    fn value(&mut self) -> Result<(), String> {
        self.ws();
        match self.peek().ok_or("a value runs off the end")? {
            b'"' => self.string().map(|_| ()),
            b'{' | b'[' => {
                let close = if self.next() == Some(b'{') { b'}' } else { b']' };
                self.ws();
                if self.peek() == Some(close) {
                    self.i += 1;
                    return Ok(());
                }
                loop {
                    self.ws();
                    if close == b'}' {
                        self.string()?;
                        self.ws();
                        self.eat(b':')?;
                    }
                    self.value()?;
                    self.ws();
                    match self.next() {
                        Some(b',') => continue,
                        Some(c) if c == close => return Ok(()),
                        _ => return Err("a bad list or object".into()),
                    }
                }
            }
            _ => {
                let start = self.i;
                while matches!(self.peek(), Some(c) if c.is_ascii_alphanumeric() || b"+-.".contains(&c)) {
                    self.i += 1;
                }
                if self.i == start { Err("not a value".into()) } else { Ok(()) }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_from_record() {
        let rec = r#"{"id": "mac-7f3a2c", "argv": ["a", {"x": [1, 2.5e3, null]}],
                      "wall": "pi.local:1", "target": "pi.local:1", "pid": 42, "moves": true}"#;
        assert_eq!(record_target(rec).unwrap().as_deref(), Some("pi.local:1"));
        assert_eq!(record_target(r#"{"target": "aé\"b"}"#).unwrap().as_deref(), Some("a\u{e9}\"b"));
        assert_eq!(record_target(r#"{"x": {"target": "no"}}"#).unwrap(), None);
        assert_eq!(record_target(r#"{"target": 3}"#).unwrap(), None);
        assert_eq!(record_target("{}").unwrap(), None);
        assert!(record_target("[1]").is_err());
        assert!(record_target(r#"{"target": "x""#).is_err());
    }

    struct Thing(Mutex<Vec<String>>);

    impl Retarget for Thing {
        fn retarget(self: Arc<Self>, target: &str) -> Result<(), String> {
            self.0.lock().unwrap().push(target.to_string());
            Ok(())
        }
        fn label(&self) -> String {
            "thing".into()
        }
    }

    #[test]
    fn held_weakly() {
        let a = Arc::new(Thing(Mutex::new(Vec::new())));
        let weak: Weak<dyn Retarget> = Arc::downgrade(&(a.clone() as Arc<dyn Retarget>));
        hold(weak, None);
        let alive = HELD.lock().unwrap().iter().filter(|w| w.strong_count() > 0).count();
        assert!(alive >= 1);
        drop(a);
        let alive = HELD.lock().unwrap().iter().filter(|w| w.strong_count() > 0).count();
        assert_eq!(alive, 0);
    }
}
