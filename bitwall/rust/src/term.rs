//! A sender's picture in the terminal it was started from, with no wall:
//! the Rust `pane-<name>` (AGENTS.md, *Writing a game*), for running and
//! tuning a sender at a desk.
//!
//! ```no_run
//! use bitwall::frame::Frame;
//! use bitwall::sender::{self, Options};
//! use bitwall::term;
//!
//! let args = term::option(Options::new("dots", "the smallest thing that draws"))
//!     .clamped("fps", 12.0, 1.0, 60.0, "frames a second at most")
//!     .parse_env();
//! let source = |cols: usize, rows: usize| Some(Frame::new(cols, rows));
//! if term::wanted(&args) {
//!     term::main(source, sender::period(args.number("fps")));
//! }
//! let target = args.target_or_exit();
//! sender::run(&target, sender::period(args.number("fps")), |w, _stop| w.set_source(source));
//! ```
//!
//! The terminal is a wall that is always there and whose area is the
//! tty's: each frame is asked of the source at the tty's size, diffed
//! against the last with `frame::serialise`, the same bytes a wall would
//! get, and written to stdout in one write. A change of size, or SIGWINCH,
//! clears the screen and sends the whole frame. The picture is drawn on the
//! alternate screen with the cursor hidden and autowrap off, since the
//! grid is exactly as wide as the screen and a last cell with wrap on
//! pushes the cursor onto the next line. Echo and line mode are off so a
//! `q` quits at once and keys typed do not land on the picture; Ctrl-C and
//! SIGTERM stop it as they stop a sender. The terminal is put back on
//! every way out but SIGKILL: a return, a signal, a panic.
//!
//! The colours are what the terminal says it draws (`depth_from`):
//! `COLORTERM=truecolor` or `24bit` is truecolour, a `TERM` ending
//! `256color` the 256 palette, anything else the 16 colours.
//!
//! A sender's log goes to stdout too, so a line logged while the picture
//! is up lands on it; `2>` keeps `warn!` apart, and a sender in the
//! terminal should log before `run` and after it.
//!
//! `sender.rs` could take this in later as `sender::run_or_term(&args,
//! min_frame, source, body)`: `--term` declared by `Options::new` for every
//! sender, and `run` given the source as well as the body. Until then a
//! sender declares the flag with `option` and chooses with `wanted`.

use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, Once};
use std::time::{Duration, Instant};

use crate::frame::{serialise_into, Depth, Frame};
use crate::sender::{Args, Options};
use crate::wall::{stop_on_signals, Source};

/// What `run` writes to take the terminal over: the alternate screen,
/// the cursor hidden, autowrap off, a reset, a clear.
pub const ENTER: &[u8] = b"\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[0m\x1b[2J";
/// What `run` writes to give the terminal back: a reset, autowrap and the
/// cursor on, the main screen again.
pub const LEAVE: &[u8] = b"\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l";
/// What goes before a full frame: the screen cleared.
pub const CLEAR: &[u8] = b"\x1b[2J";

// ------------------------------------------------------------- the depth

/// The colours a terminal draws, from its `COLORTERM` and `TERM`.
pub fn depth_from(colorterm: Option<&str>, term: Option<&str>) -> Depth {
    let ct = colorterm.unwrap_or("").to_ascii_lowercase();
    let t = term.unwrap_or("").to_ascii_lowercase();
    if ct == "truecolor" || ct == "24bit" || t.ends_with("-direct") {
        Depth::True
    } else if t.ends_with("256color") || t.contains("256color") {
        Depth::Xterm256
    } else {
        Depth::Ansi16
    }
}

/// The colours this process's terminal draws.
pub fn depth() -> Depth {
    let ct = std::env::var("COLORTERM").ok();
    let t = std::env::var("TERM").ok();
    depth_from(ct.as_deref(), t.as_deref())
}

// ----------------------------------------------------------- the options

/// `opts` with `--term` as well: run in this terminal, with no wall.
pub fn option(opts: Options) -> Options {
    opts.switch("term", false, "run in this terminal instead of on a wall")
}

/// Whether `--term` was given; the options must have come through
/// `option`.
pub fn wanted(args: &Args) -> bool {
    args.on("term")
}

// ------------------------------------------------------------ the painter

/// Frames to bytes for one screen: each frame fitted to the area, diffed
/// against the last, and written to `out` in one `write_all`, a full
/// repaint after a clear wherever the area has changed or one is asked.
pub struct Painter<W: Write> {
    out: W,
    depth: Depth,
    prev: Option<Frame>,
    buf: Vec<u8>,
}

impl<W: Write> Painter<W> {
    pub fn new(out: W, depth: Depth) -> Painter<W> {
        Painter { out, depth, prev: None, buf: Vec::new() }
    }

    /// The writer, to look at what was written.
    pub fn out(&mut self) -> &mut W {
        &mut self.out
    }

    /// Ask `source` for a frame `cols` by `rows` and paint it: in full,
    /// after a clear, where `full` or the area is not the last frame's;
    /// otherwise only what changed. A source with nothing new has the last
    /// frame painted again where a full one is due. The bytes written.
    pub fn paint<S: Source + ?Sized>(
        &mut self,
        source: &mut S,
        cols: usize,
        rows: usize,
        full: bool,
    ) -> io::Result<usize> {
        let resized = self.prev.as_ref().is_none_or(|p| p.cols != cols || p.rows != rows);
        let full = full || resized;
        let cur = match source.render(cols, rows) {
            Some(f) => fit(f, cols, rows),
            None if full => match self.prev.take() {
                Some(p) => fit(p, cols, rows),
                None => return Ok(0),
            },
            None => return Ok(0),
        };
        self.buf.clear();
        if full {
            self.buf.extend_from_slice(CLEAR);
        }
        serialise_into(&mut self.buf, &cur, self.prev.as_ref(), full, self.depth);
        self.prev = Some(cur);
        if self.buf.is_empty() {
            return Ok(0);
        }
        self.out.write_all(&self.buf)?;
        self.out.flush()?;
        Ok(self.buf.len())
    }
}

/// A frame clipped or filled out to the area, as a wall takes one.
fn fit(f: Frame, cols: usize, rows: usize) -> Frame {
    if f.cols == cols && f.rows == rows {
        return f;
    }
    let mut out = Frame::new(cols, rows);
    let w = cols.min(f.cols);
    for y in 0..rows.min(f.rows) {
        let (src, dst) = (y * f.cols, y * cols);
        out.ch[dst..dst + w].copy_from_slice(&f.ch[src..src + w]);
        out.fg[dst..dst + w].copy_from_slice(&f.fg[src..src + w]);
        out.bg[dst..dst + w].copy_from_slice(&f.bg[src..src + w]);
        out.attrs[dst..dst + w].copy_from_slice(&f.attrs[src..src + w]);
    }
    out
}

// ------------------------------------------------------------ the tty

#[cfg(unix)]
mod sys {
    use std::ffi::{c_int, c_ulong, c_void};

    #[repr(C)]
    #[derive(Default)]
    pub struct Winsize {
        pub rows: u16,
        pub cols: u16,
        pub xpixel: u16,
        pub ypixel: u16,
    }

    /// A struct termios, held as bytes: larger than any libc's, and only
    /// its local flags are touched, at their place on each system.
    #[repr(C, align(8))]
    #[derive(Clone, Copy)]
    pub struct Termios(pub [u8; 256]);

    #[cfg(target_os = "macos")]
    pub const TIOCGWINSZ: c_ulong = 0x4008_7468;
    #[cfg(not(target_os = "macos"))]
    pub const TIOCGWINSZ: c_ulong = 0x5413;

    pub const SIGWINCH: c_int = 28;

    #[cfg(target_os = "macos")]
    mod lflag {
        pub const AT: usize = 24; // after three unsigned longs
        pub const ICANON: u64 = 0x100;
        pub const ECHO: u64 = 0x8;
    }
    #[cfg(not(target_os = "macos"))]
    mod lflag {
        pub const AT: usize = 12; // after three unsigned ints
        pub const ICANON: u64 = 0x2;
        pub const ECHO: u64 = 0x8;
    }

    unsafe extern "C" {
        fn ioctl(fd: c_int, request: c_ulong, ...) -> c_int;
        fn tcgetattr(fd: c_int, t: *mut Termios) -> c_int;
        fn tcsetattr(fd: c_int, action: c_int, t: *const Termios) -> c_int;
        fn isatty(fd: c_int) -> c_int;
        fn signal(signum: c_int, handler: usize) -> usize;
        fn write(fd: c_int, buf: *const c_void, n: usize) -> isize;
    }

    pub fn size(fd: c_int) -> Option<(usize, usize)> {
        let mut ws = Winsize::default();
        // SAFETY: TIOCGWINSZ fills one struct winsize, which ws is.
        let r = unsafe { ioctl(fd, TIOCGWINSZ, &mut ws as *mut Winsize) };
        (r == 0 && ws.cols > 0 && ws.rows > 0).then_some((ws.cols as usize, ws.rows as usize))
    }

    pub fn tty(fd: c_int) -> bool {
        // SAFETY: isatty only reads its argument.
        unsafe { isatty(fd) == 1 }
    }

    /// The terminal's settings now, and them with echo and line mode off.
    pub fn quiet(fd: c_int) -> Option<(Termios, Termios)> {
        let mut t = Termios([0; 256]);
        // SAFETY: t is larger than the struct termios tcgetattr fills.
        if unsafe { tcgetattr(fd, &mut t) } != 0 {
            return None;
        }
        let saved = t;
        #[cfg(target_os = "macos")]
        {
            let mut f = u64::from_ne_bytes(t.0[lflag::AT..lflag::AT + 8].try_into().unwrap());
            f &= !(lflag::ICANON | lflag::ECHO);
            t.0[lflag::AT..lflag::AT + 8].copy_from_slice(&f.to_ne_bytes());
        }
        #[cfg(not(target_os = "macos"))]
        {
            let mut f = u32::from_ne_bytes(t.0[lflag::AT..lflag::AT + 4].try_into().unwrap());
            f &= !((lflag::ICANON | lflag::ECHO) as u32);
            t.0[lflag::AT..lflag::AT + 4].copy_from_slice(&f.to_ne_bytes());
        }
        set(fd, &t);
        Some((saved, t))
    }

    pub fn set(fd: c_int, t: &Termios) {
        // SAFETY: t came from tcgetattr; TCSANOW is 0 everywhere.
        unsafe {
            tcsetattr(fd, 0, t);
        }
    }

    pub fn on_signal(signum: c_int, handler: extern "C" fn(c_int)) {
        // SAFETY: the handlers given only store to atomics.
        unsafe {
            signal(signum, handler as usize);
        }
    }

    /// All of `data` to stdout, straight to the descriptor, in as few
    /// writes as the tty takes: no buffer in between to split a frame or
    /// to be held locked by a thread that panicked.
    pub fn out(data: &[u8]) -> std::io::Result<()> {
        let mut rest = data;
        while !rest.is_empty() {
            // SAFETY: rest is a live slice of rest.len() bytes.
            let n = unsafe { write(1, rest.as_ptr() as *const c_void, rest.len()) };
            if n < 0 {
                let e = std::io::Error::last_os_error();
                if e.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(e);
            }
            rest = &rest[n as usize..];
        }
        Ok(())
    }
}

/// The terminal's size, cols by rows: the tty's own, else `COLUMNS` and
/// `LINES`, else 80x24.
pub fn size() -> (usize, usize) {
    #[cfg(unix)]
    for fd in [1, 0, 2] {
        if let Some(s) = sys::size(fd) {
            return s;
        }
    }
    let env = |k: &str| std::env::var(k).ok().and_then(|v| v.parse::<usize>().ok()).filter(|&v| v > 0);
    (env("COLUMNS").unwrap_or(80), env("LINES").unwrap_or(24))
}

/// Stdout written straight to its descriptor, each `write_all` whole.
struct Stdout;

impl Write for Stdout {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.write_all(buf)?;
        Ok(buf.len())
    }
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        #[cfg(unix)]
        return sys::out(buf);
        #[cfg(not(unix))]
        {
            let mut o = io::stdout().lock();
            o.write_all(buf)?;
            o.flush()
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

static WINCH: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
/// The terminal as it was, while it is ours: put back once, by whichever
/// way out comes first.
static TAKEN: Mutex<Option<Taken>> = Mutex::new(None);

struct Taken {
    #[cfg(unix)]
    termios: Option<sys::Termios>,
}

#[cfg(unix)]
extern "C" fn on_winch(_: i32) {
    WINCH.store(true, Ordering::SeqCst);
}

/// Take the terminal: the alternate screen, the cursor hidden, echo and
/// line mode off, and a panic put back first.
fn take() -> io::Result<()> {
    static HOOK: Once = Once::new();
    HOOK.call_once(|| {
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            give_back();
            prev(info);
        }));
    });
    #[cfg(unix)]
    let termios = if sys::tty(0) { sys::quiet(0).map(|(saved, _)| saved) } else { None };
    *TAKEN.lock().unwrap_or_else(|e| e.into_inner()) = Some(Taken {
        #[cfg(unix)]
        termios,
    });
    #[cfg(unix)]
    sys::on_signal(sys::SIGWINCH, on_winch);
    Stdout.write_all(ENTER)
}

/// Give the terminal back as it was taken, if it is still taken.
fn give_back() {
    let taken = TAKEN.lock().unwrap_or_else(|e| e.into_inner()).take();
    if let Some(_t) = taken {
        let _ = Stdout.write_all(LEAVE);
        #[cfg(unix)]
        if let Some(t) = &_t.termios {
            sys::set(0, t);
        }
    }
}

/// `q` on the keyboard, read from a tty on stdin, as a stop.
fn listen_for_q() {
    static STARTED: Once = Once::new();
    #[cfg(unix)]
    if !sys::tty(0) {
        return;
    }
    STARTED.call_once(|| {
        std::thread::spawn(|| {
            use std::io::Read;
            let mut b = [0u8; 64];
            let mut stdin = io::stdin();
            while let Ok(n) = stdin.read(&mut b) {
                if n == 0 {
                    return;
                }
                if b[..n].iter().any(|&c| c == b'q' || c == b'Q') {
                    QUIT.store(true, Ordering::SeqCst);
                }
            }
        });
    });
}

// ---------------------------------------------------------------- the run

/// Run `source` in this terminal, a frame every `min_frame` at most, until
/// `stop` is set, SIGINT or SIGTERM comes, or `q` is pressed; then give
/// the terminal back. Errors are stdout's: a terminal gone away.
pub fn run<S: Source>(mut source: S, min_frame: Duration, stop: &AtomicBool) -> io::Result<()> {
    let signalled = stop_on_signals();
    QUIT.store(false, Ordering::SeqCst);
    take()?;
    listen_for_q();
    let mut painter = Painter::new(Stdout, depth());
    let going = || !stop.load(Ordering::SeqCst) && !signalled.load(Ordering::SeqCst) && !QUIT.load(Ordering::SeqCst);
    let mut result = Ok(());
    let mut full = true;
    while going() {
        let due = Instant::now() + min_frame;
        if WINCH.swap(false, Ordering::SeqCst) {
            full = true;
        }
        let (cols, rows) = size();
        if let Err(e) = painter.paint(&mut source, cols, rows, full) {
            result = Err(e);
            break;
        }
        full = false;
        // Sleep to the next frame, waking for a resize or a stop.
        while going() && !WINCH.load(Ordering::SeqCst) {
            let left = due.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            std::thread::sleep(left.min(Duration::from_millis(10)));
        }
    }
    give_back();
    result
}

/// A sender's whole life in the terminal: `run` until it is time to go,
/// then exit, 0 or 1 where the terminal went away.
pub fn main<S: Source>(source: S, min_frame: Duration) -> ! {
    let stop = AtomicBool::new(false);
    let code = match run(source, min_frame, &stop) {
        Ok(()) => 0,
        Err(e) => {
            crate::warn!("terminal: {e}");
            1
        }
    };
    std::process::exit(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fit_clips_and_fills() {
        let mut f = Frame::new(3, 1);
        f.text(0, 0, "abc", 0x123456, crate::frame::DEFAULT, 0);
        let g = fit(f, 2, 2);
        assert_eq!(g.ch, vec!['a' as u32, 'b' as u32, ' ' as u32, ' ' as u32]);
        assert_eq!(g.fg[0], 0x123456);
    }
}
