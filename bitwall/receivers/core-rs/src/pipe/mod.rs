//! A wall's pipes on the machine it runs on: fifos on a Mac or Linux, named
//! pipes under `//./pipe/bitwall-<user>` on Windows (WINDOWS.md), behind one
//! interface, as bitwall/local.py is for Python.
//!
//! Each pipe the wall reads has a thread reading it, which hands over one
//! piece at a time and reads the next only once the wall has taken it, so a
//! wall that stops reading fills the pipe and its writer waits. Every
//! reader rings one bell when something arrives, which is what the wall's
//! loop waits on.

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex};
use std::time::Duration;

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use self::windows::*;

/// What a reader hands over: bytes, or its writer's end of file.
pub enum Piece {
    Data(Vec<u8>),
    Eof,
}

/// What a write to `out` came to.
pub enum Wrote {
    Took(usize),
    /// Nothing taken: the pipe is full, or its reader has not come.
    Full,
    /// Its reader came and went.
    Gone,
}

struct Bell {
    rung: Mutex<bool>,
    cv: Condvar,
}

static BELL: Bell = Bell { rung: Mutex::new(false), cv: Condvar::new() };

/// Wake the wall's loop.
pub fn ring() {
    let mut r = BELL.rung.lock().unwrap();
    *r = true;
    BELL.cv.notify_all();
}

/// Wait until something rings, or `t` has gone.
pub fn wait(t: Duration) {
    let r = BELL.rung.lock().unwrap();
    let (mut r, _) = BELL.cv.wait_timeout_while(r, t, |rung| !*rung).unwrap();
    *r = false;
}

/// Where a reader's thread leaves what it read: one piece in hand.
pub(crate) struct Slot {
    m: Mutex<SlotState>,
    cv: Condvar,
}

struct SlotState {
    pieces: VecDeque<Vec<u8>>,
    eofs: usize,
    closed: bool,
}

impl Slot {
    pub(crate) fn new() -> Slot {
        Slot { m: Mutex::new(SlotState { pieces: VecDeque::new(), eofs: 0, closed: false }), cv: Condvar::new() }
    }

    pub(crate) fn push(&self, data: Option<Vec<u8>>, eof: bool) {
        let mut s = self.m.lock().unwrap();
        while !s.pieces.is_empty() && !s.closed {
            s = self.cv.wait_timeout(s, Duration::from_millis(500)).unwrap().0;
        }
        if s.closed {
            return;
        }
        if let Some(d) = data {
            s.pieces.push_back(d);
        }
        if eof {
            s.eofs += 1;
        }
        drop(s);
        ring();
    }

    pub(crate) fn take(&self) -> Option<Piece> {
        let mut s = self.m.lock().unwrap();
        let got = if let Some(p) = s.pieces.pop_front() {
            Some(Piece::Data(p))
        } else if s.eofs > 0 {
            s.eofs -= 1;
            Some(Piece::Eof)
        } else {
            None
        };
        if got.is_some() {
            self.cv.notify_all();
        }
        got
    }

    pub(crate) fn close(&self) {
        self.m.lock().unwrap().closed = true;
        self.cv.notify_all();
    }

    pub(crate) fn closed(&self) -> bool {
        self.m.lock().unwrap().closed
    }
}
