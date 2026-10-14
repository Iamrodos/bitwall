//! A wall's door and sessions, on this machine or over ssh
//! (`bitwall/session.py`).
//!
//! A wall is a directory under its host's run directory,
//! `<run>/bitwall/<wall>/`, holding a door and the sessions opened through
//! it (PROTOCOL.md). A caller writes one `open` line at the door, reads the
//! session's `out` once it appears, and writes frames and commands into its
//! `in`. On another host it does the same through ssh, with the same
//! commands the Python library runs: the opener, a few lines of Python on
//! the wall's host, knocks and copies `out` to ssh's stdout, and `exec cat
//! > in` carries what is written. A wall on this machine is opened in this
//! process, the opener's steps taken here, so a sender here starts nothing.
//!
//! ```no_run
//! use bitwall::session::{Session, Target};
//! let t = Target::parse("pi.local:1").unwrap();
//! let mut s = Session::open(&t, "sysinfo-4821", "layer take=1 type=cells").unwrap();
//! s.write(b"\x1b_bitwall report\x1b\\").unwrap();
//! let said = s.lines.recv().unwrap();
//! s.close(Some(b"\x1b_bitwall detach\x1b\\"));
//! ```
//!
//! What the wall says arrives on `lines` a line at a time, from a thread
//! reading `out`; a picture or a mirror frame's bytes on `blobs`. A `ping`
//! is answered on the reader's thread and never surfaces: every caller
//! answers it, and one that waited on its own loop to do so could be
//! ended for being slow. `Controller` is a session for a program that
//! talks to a wall rather than draws on it.

use std::collections::VecDeque;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

/// Targets that mean this machine. A wall here is reached by opening its
/// pipes rather than by sending anything anywhere.
pub const LOCAL: [&str; 5] = ["localhost", "127.0.0.1", "::1", "local", ""];

/// Where a host keeps its walls, as its own shell finds it. Asked of the
/// host rather than known here, so a caller never has to know what kind of
/// machine it is talking to: a Pi's session has XDG_RUNTIME_DIR, and a
/// Mac, which has none, keeps them in the home directory.
pub const RUN_HERE: &str = "${XDG_RUNTIME_DIR:-$HOME/.bitwall/run}";

/// What a Windows host's run directory begins with: its walls are named
/// pipes, reached through the relay, which this library does not speak.
pub const PIPES: &str = "//./pipe/";

/// How long a caller waits for its `out` to appear before the request is
/// taken as dropped: PROTOCOL.md's five seconds.
pub const OPEN_WAIT: Duration = Duration::from_secs(5);

/// How the opener says why it stopped before the session said anything:
/// no door, or nobody reading it.
pub const NO_WALL: i32 = 3;
/// No `out` within the wait: the request was dropped.
pub const DROPPED: i32 = 4;
/// `s/<name>/` was there before the knock: the name is taken.
pub const IN_USE: i32 = 5;

/// The most a line from a wall may be, as `bitwall/protocol.py` has it.
const MAX_MESSAGE: usize = 4096;

/// The far end of an open on another host, run there by `python3 -c`,
/// character for character the Python library's: it fails at once when
/// nothing reads the door, and when the name is already a session's, so it
/// never reads an older session's `out` as its own; writes the request in
/// one write; waits for the session's `out`; then copies everything the
/// wall says to stdout until `out` ends. Python rather than shell because
/// only it can open a fifo for writing without waiting on a reader that
/// may never come.
pub const OPENER: &str = r#"
import os, sys, time
wall, name, request = sys.argv[1:4]
out = os.path.join(wall, "s", name, "out")
if os.path.lexists(os.path.dirname(out)):
    sys.exit(5)
try:
    fd = os.open(os.path.join(wall, "door.in"), os.O_WRONLY | os.O_NONBLOCK)
except OSError:
    sys.exit(3)
os.write(fd, (request + "\n").encode())
os.close(fd)
until = time.monotonic() + 5.0
while not os.path.exists(out):
    if time.monotonic() >= until:
        sys.exit(4)
    time.sleep(0.02)
fd = os.open(out, os.O_RDONLY)
try:
    while True:
        data = os.read(fd, 65536)
        if not data:
            break
        while data:
            data = data[os.write(1, data):]
except BrokenPipeError:
    pass
"#;

/// A wall's name after the colon: one to thirty-two lower case letters,
/// digits, `_` and `-`, starting with a letter or digit. Lower case only,
/// since a Mac's filesystem would take two names differing in case for
/// one directory.
pub fn wall_token(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter()
            .all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_' || c == b'-')
}

/// A session's name, or an id: one to thirty-two of `[A-Za-z0-9_-]`.
pub fn session_token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

/// A controller's instance id: lower case letters, digits and `-`, one to
/// sixty-four, starting with a letter or digit.
pub fn instance_token(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
}

/// Where a wall is: `host:<wall>`, `host` alone being wall 0.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Target {
    pub host: String,
    pub wall: String,
}

impl Target {
    /// The host and wall a target names. `host:<wall>` is that wall on that
    /// host, a screen's number or a name, and `host` alone is wall 0. The
    /// wall is kept as written, so `001` is `001` and not `1`. A bare IPv6
    /// address is all colons, so it is taken whole, as wall 0.
    pub fn parse(target: &str) -> Result<Target, String> {
        if LOCAL.contains(&target) || target.matches(':').count() != 1 {
            return Ok(Target { host: target.to_string(), wall: "0".to_string() });
        }
        let (host, wall) = target.split_once(':').unwrap();
        if !wall_token(wall) {
            return Err(format!(
                "bitwall: {target:?}: the wall after ':' must be a number, or a name of \
                 lower case letters, digits, _ and -, starting with a letter or digit"
            ));
        }
        Ok(Target { host: host.to_string(), wall: wall.to_string() })
    }

    /// Whether it is on this machine (no ssh).
    pub fn local(&self) -> bool {
        LOCAL.contains(&self.host.as_str())
    }
}

impl fmt::Display for Target {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.wall == "0" { write!(f, "{}", self.host) } else { write!(f, "{}:{}", self.host, self.wall) }
    }
}

/// The program's name as a session's base: its executable's, or its
/// directory's where that is `main`, as the Python library takes it from
/// its script.
pub fn program_name() -> String {
    let path = std::env::args().next().unwrap_or_default();
    let p = Path::new(&path);
    let mut stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
    if matches!(stem.as_str(), "" | "-" | "main") && !path.is_empty() {
        stem = std::fs::canonicalize(p)
            .ok()
            .and_then(|a| a.parent().and_then(|d| d.file_name()).map(|d| d.to_string_lossy().into_owned()))
            .unwrap_or_default();
    }
    stem
}

/// A session name that cannot collide: `<base>-<pid>`, `-<tag>` after.
///
/// Anything in the base that a name may not hold becomes `-`, an empty
/// base is `sender`, and the base is cut so the whole fits the
/// thirty-two characters a name has.
pub fn session_name(base: &str, tag: Option<&str>) -> String {
    let clean: String = base
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' })
        .collect();
    let clean = clean.trim_matches('-');
    let clean = if clean.is_empty() { "sender" } else { clean };
    let tail = format!("-{}", std::process::id())
        + &tag.filter(|t| !t.is_empty()).map(|t| format!("-{t}")).unwrap_or_default();
    let keep = 32usize.saturating_sub(tail.len()).min(clean.len());
    format!("{}{}", &clean[..keep], tail)
}

/// A command carrying an id where it will be read as one. A metadata
/// value is the rest of the line, so `meta` takes it in front instead.
pub fn with_id(text: &str, id: &str) -> String {
    match text.split_once(' ') {
        Some(("meta", rest)) => format!("meta id={id} {rest}"),
        None if text == "meta" => format!("meta id={id}"),
        _ => format!("{text} id={id}"),
    }
}

/// A command as it travels in the stream: `ESC _ bitwall <text> ESC \`.
pub fn apc(text: &str) -> Vec<u8> {
    format!("\x1b_bitwall {text}\x1b\\").into_bytes()
}

/// A word made safe for a POSIX shell, as Python's `shlex.quote` makes it.
pub fn shell_quote(s: &str) -> String {
    if s.is_empty() {
        return "''".to_string();
    }
    if s.bytes().all(|c| c.is_ascii_alphanumeric() || b"@%+=:,./-_".contains(&c)) {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

/// The user a host is reached as: `BITWALL_PI_USER`, else this one, as
/// Python's `getpass.getuser` finds it.
fn default_user() -> String {
    for key in ["BITWALL_PI_USER", "LOGNAME", "USER", "LNAME", "USERNAME"] {
        if let Ok(v) = std::env::var(key) {
            if !v.is_empty() {
                return v;
            }
        }
    }
    Command::new("id")
        .arg("-un")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

/// The directory of wall `wall`, given its host's run directory.
pub fn wall_run(run: &str, wall: &str) -> String {
    format!("{}/bitwall/{}", run.trim_end_matches('/'), wall)
}

/// How a wall is reached: its host, the wall's directory there, and the
/// command lines that run things on it. Only the host is ever handed to
/// ssh; the wall is a directory on it.
pub struct Link {
    pub target: Target,
    pub user: String,
    /// The ssh ControlPath, one per wall so a film on one cannot queue
    /// another's frames behind it; `-raw` after it for the uncompressed
    /// master.
    pub cm: PathBuf,
    run: OnceLock<String>,
}

/// What keeps a writer's ssh honest: a connection that has died is found
/// within a minute rather than held for ever.
pub const ALIVE: [&str; 4] = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"];

impl Link {
    /// A link to `target`, as `user` (the default as Python's), its wall's
    /// directory `run` where given, else found inside `BITWALL_RUN`, else
    /// asked of the host when first needed.
    pub fn new(target: &Target, user: Option<&str>, run: Option<&str>) -> Link {
        let cell = OnceLock::new();
        if let Some(r) = run {
            let _ = cell.set(r.to_string());
        } else if let Ok(base) = std::env::var("BITWALL_RUN") {
            if !base.is_empty() {
                let _ = cell.set(wall_run(&base, &target.wall));
            }
        }
        let suffix = if target.wall != "0" { format!("-{}", target.wall) } else { String::new() };
        Link {
            target: target.clone(),
            user: user.map(str::to_string).unwrap_or_else(default_user),
            cm: std::env::temp_dir().join(format!("bitwall-cm-{}{}", target.host, suffix)),
            run: cell,
        }
    }

    pub fn local(&self) -> bool {
        self.target.local()
    }

    /// ssh's options for a connection compressed or not. A cell stream is
    /// escape sequences and repeated attributes, which compress about
    /// tenfold; pictures and video are compressed already. Each kind has a
    /// master of its own, since every session shares its master's
    /// compression.
    pub fn ssh_opts(&self, compress: bool) -> Vec<String> {
        let mut o: Vec<String> = Vec::new();
        if compress {
            o.push("-C".into());
        }
        for s in ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] {
            o.push(s.into());
        }
        o.push("-o".into());
        o.push(format!("ControlPath={}{}", self.cm.display(), if compress { "" } else { "-raw" }));
        for s in ["-o", "ControlMaster=auto", "-o", "ControlPersist=60"] {
            o.push(s.into());
        }
        o
    }

    /// How to run a shell command on the wall's host: `sh -c` here, ssh
    /// there.
    pub fn argv(&self, cmd: &str, extra: &[&str], compress: bool) -> Vec<String> {
        if self.local() {
            return vec!["sh".into(), "-c".into(), cmd.into()];
        }
        let mut v = vec!["ssh".to_string()];
        v.extend(self.ssh_opts(compress));
        v.extend(extra.iter().map(|s| s.to_string()));
        v.push(format!("{}@{}", self.user, self.target.host));
        v.push(cmd.into());
        v
    }

    /// The command that copies its stdin into the pipe at `path`.
    pub fn writer_argv(&self, path: &str, compress: bool) -> Vec<String> {
        self.argv(&format!("exec cat > {}", shell_quote(path)), &ALIVE, compress)
    }

    /// Run a command on the wall's host; what it printed, trimmed, or
    /// nothing if it failed or took longer than `timeout`.
    pub fn shell(&self, cmd: &str, timeout: Duration) -> String {
        let argv = self.argv(cmd, &[], true);
        let Ok(mut child) = Command::new(&argv[0])
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        else {
            return String::new();
        };
        let mut out = child.stdout.take().unwrap();
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            let mut s = String::new();
            let _ = out.read_to_string(&mut s);
            let _ = tx.send(s);
        });
        match rx.recv_timeout(timeout) {
            Ok(s) => {
                let _ = child.wait();
                s.trim().to_string()
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                String::new()
            }
        }
    }

    /// The wall's directory, the host asked where it keeps its walls the
    /// first time it is needed. A host that cannot say, or says something
    /// that is not a path, is an error rather than a guess; a Windows host
    /// is one, since its walls are named pipes behind the relay.
    pub fn run(&self) -> Result<String, String> {
        if let Some(r) = self.run.get() {
            return Ok(r.clone());
        }
        let base = if self.local() {
            match std::env::var("XDG_RUNTIME_DIR") {
                Ok(x) if !x.is_empty() => x,
                _ => format!("{}/.bitwall/run", std::env::var("HOME").unwrap_or_default()),
            }
        } else {
            self.shell(&format!("printf %s \"{RUN_HERE}\""), Duration::from_secs(15))
        };
        if !base.starts_with('/') {
            let what = if base.is_empty() {
                ": no answer".to_string()
            } else {
                format!(": it said {:?} (a Windows host? not yet reached from Rust)",
                        base.chars().take(80).collect::<String>())
            };
            return Err(format!("bitwall: could not ask {} where it keeps its walls{what}",
                               self.target.host));
        }
        let _ = self.run.set(wall_run(&base, &self.target.wall));
        Ok(self.run.get().unwrap().clone())
    }

    pub fn session_dir(&self, name: &str) -> Result<String, String> {
        Ok(format!("{}/s/{}", self.run()?, name))
    }
}

/// A picture or a mirror frame as a session hears it: the line that
/// announced it, and exactly the bytes it said would follow.
#[derive(Clone, Debug)]
pub struct Blob {
    pub said: String,
    pub data: Vec<u8>,
}

/// How a session stands.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum State {
    Opening,
    Open,
    /// The wall ended it, and said why (`closed <name> <reason>`).
    Closed(String),
    /// The wall said no (`error open <name> <reason>`).
    Refused(String),
    /// No `out` within the wait, or the name was already a session's.
    Dropped,
    /// Nothing reads the door.
    Absent,
    /// `out` ended with no `closed`: the wall, or the connection, has gone.
    Lost,
}

#[derive(Debug)]
pub enum OpenError {
    /// Nothing at that door: no wall, or no host.
    NoWall(String),
    /// The wall said no (`error open ...`).
    Refused(String),
    /// The door took the request and nothing came of it: the name was a
    /// session's still, or the wall dropped it. Worth asking again under
    /// another name.
    Dropped(String),
    Io(io::Error),
}

impl fmt::Display for OpenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OpenError::NoWall(s) | OpenError::Refused(s) | OpenError::Dropped(s) => f.write_str(s),
            OpenError::Io(e) => write!(f, "bitwall: {e}"),
        }
    }
}

impl std::error::Error for OpenError {}

impl From<io::Error> for OpenError {
    fn from(e: io::Error) -> OpenError {
        OpenError::Io(e)
    }
}

/// How much one write hands the pipe: small enough that a write given up
/// on stops soon, large enough that a frame is a few calls.
pub const CHUNK: usize = 65536;

/// A session's `in`, written whole or not at all by a thread of its own,
/// and shareable between threads: cloned, it is the same pipe, and writes
/// go in the order they were made, never interleaved.
///
/// A caller hands it bytes and waits as long as it chooses: until they are
/// all in the pipe, until `stop` is set, or until a timeout passes. What
/// it gives up on is dropped at the next chunk, so the pipe carries at
/// most one piece of a frame nobody waits for any more. The thread owns
/// the pipe and is the one that closes it, so a close never pulls it from
/// under a write, and a wall that has stopped reading holds up only that
/// thread.
#[derive(Clone)]
pub struct Writer {
    shared: Arc<PipeShared>,
}

struct Job {
    data: Vec<u8>,
    given_up: AtomicBool,
    /// Whether anybody waits to hear how it went.
    waited: bool,
}

#[derive(Default)]
struct PipeState {
    jobs: VecDeque<Arc<Job>>,
    /// The job being written.
    busy: Option<Arc<Job>>,
    /// Jobs finished, and whether each went whole.
    finished: Vec<(Arc<Job>, bool)>,
    attached: bool,
    closing: bool,
    closed: bool,
}

#[derive(Default)]
struct PipeShared {
    m: Mutex<PipeState>,
    cv: Condvar,
}

fn broken() -> io::Error {
    io::Error::new(io::ErrorKind::BrokenPipe, "bitwall: the session's in is closed")
}

impl Writer {
    fn empty() -> Writer {
        Writer { shared: Arc::new(PipeShared::default()) }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, PipeState> {
        self.shared.m.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Give the writer its pipe, and the thread that writes it.
    fn set(&self, pipe: Box<dyn Write + Send>) -> io::Result<()> {
        self.lock().attached = true;
        let shared = self.shared.clone();
        thread::Builder::new()
            .name("bitwall-in".into())
            .spawn(move || Writer::run(shared, pipe))
            .map(|_| ())
    }

    fn run(shared: Arc<PipeShared>, mut pipe: Box<dyn Write + Send>) {
        loop {
            let job = {
                let mut g = shared.m.lock().unwrap_or_else(|e| e.into_inner());
                loop {
                    if let Some(job) = g.jobs.pop_front() {
                        g.busy = Some(job.clone());
                        break Some(job);
                    }
                    if g.closing {
                        break None;
                    }
                    g = shared.cv.wait(g).unwrap_or_else(|e| e.into_inner());
                }
            };
            let Some(job) = job else { break };
            let mut ok = true;
            for piece in job.data.chunks(CHUNK) {
                if job.given_up.load(Ordering::SeqCst) {
                    break;
                }
                if pipe.write_all(piece).and_then(|_| pipe.flush()).is_err() {
                    ok = false;
                    break;
                }
            }
            let mut g = shared.m.lock().unwrap_or_else(|e| e.into_inner());
            g.busy = None;
            if job.waited && !job.given_up.load(Ordering::SeqCst) {
                g.finished.push((job, ok));
            }
            if !ok {
                // A broken pipe takes everything waiting with it.
                for j in std::mem::take(&mut g.jobs) {
                    if j.waited {
                        g.finished.push((j, false));
                    }
                }
                g.closing = true;
                g.closed = true;
                shared.cv.notify_all();
                return;
            }
            shared.cv.notify_all();
        }
        drop(pipe);
        let mut g = shared.m.lock().unwrap_or_else(|e| e.into_inner());
        g.closed = true;
        shared.cv.notify_all();
    }

    /// Queue `data` behind what is already queued, without waiting.
    fn post(&self, data: Vec<u8>, waited: bool) -> Option<Arc<Job>> {
        let mut g = self.lock();
        if !g.attached || g.closing || g.closed {
            return None;
        }
        let job = Arc::new(Job { data, given_up: AtomicBool::new(false), waited });
        g.jobs.push_back(job.clone());
        self.shared.cv.notify_all();
        Some(job)
    }

    /// Write all of `data`, blocking while the pipe is full. A pipe that
    /// has broken fails this write and every write after.
    pub fn write(&self, data: &[u8]) -> io::Result<()> {
        self.write_until(data, None, None)
    }

    /// Write all of `data`, waiting only until `stop` is set or `timeout`
    /// has passed: `Interrupted` or `TimedOut` then, and what was not yet
    /// written is dropped at the next chunk.
    pub fn write_until(&self, data: &[u8], stop: Option<&AtomicBool>, timeout: Option<Duration>) -> io::Result<()> {
        let Some(job) = self.post(data.to_vec(), true) else { return Err(broken()) };
        let until = timeout.map(|t| Instant::now() + t);
        let mut g = self.lock();
        loop {
            if let Some(i) = g.finished.iter().position(|(j, _)| Arc::ptr_eq(j, &job)) {
                let (_, ok) = g.finished.swap_remove(i);
                return if ok { Ok(()) } else { Err(broken()) };
            }
            // Abandoned while waiting, and no longer being written.
            if job.given_up.load(Ordering::SeqCst) && !g.busy.as_ref().is_some_and(|b| Arc::ptr_eq(b, &job)) {
                return Err(broken());
            }
            let why = if stop.is_some_and(|s| s.load(Ordering::SeqCst)) {
                Some(io::ErrorKind::Interrupted)
            } else if until.is_some_and(|u| Instant::now() >= u) {
                Some(io::ErrorKind::TimedOut)
            } else {
                None
            };
            if let Some(kind) = why {
                job.given_up.store(true, Ordering::SeqCst);
                g.jobs.retain(|j| !Arc::ptr_eq(j, &job));
                return Err(io::Error::new(kind, "bitwall: the write was given up"));
            }
            let step = until.map_or(Duration::from_millis(50), |u| {
                u.saturating_duration_since(Instant::now()).min(Duration::from_millis(50))
            });
            g = self.shared.cv.wait_timeout(g, step).unwrap_or_else(|e| e.into_inner()).0;
        }
    }

    /// Whether there is a pipe to write to.
    pub fn open(&self) -> bool {
        let g = self.lock();
        g.attached && !g.closing && !g.closed
    }

    /// Whether everything handed over is in the pipe already, so a frame
    /// written now goes at once rather than waiting behind another.
    pub fn idle(&self) -> bool {
        let g = self.lock();
        g.attached && !g.closed && g.jobs.is_empty() && g.busy.is_none()
    }

    /// End the pipe now: what is queued is dropped, and a write under way
    /// stops at its next chunk.
    fn abandon(&self) {
        let mut g = self.lock();
        g.closing = true;
        if let Some(job) = &g.busy {
            job.given_up.store(true, Ordering::SeqCst);
        }
        for j in std::mem::take(&mut g.jobs) {
            if j.waited {
                g.finished.push((j, false));
            }
        }
        self.shared.cv.notify_all();
    }

    /// End the pipe once what is queued has gone, waiting up to `timeout`
    /// for that; whether it has ended. Its reader sees end of file.
    fn close(&self, timeout: Duration) -> bool {
        let until = Instant::now() + timeout;
        let mut g = self.lock();
        g.closing = true;
        self.shared.cv.notify_all();
        if !g.attached {
            return true;
        }
        while !g.closed {
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            g = self.shared.cv.wait_timeout(g, left).unwrap_or_else(|e| e.into_inner()).0;
        }
        true
    }
}

/// What the reader tells the opener while the session is opening.
enum Event {
    Opened,
    Refused(String),
    /// `out` ended, or never came: the opener's exit code.
    Ended(Option<i32>),
}

struct Shared {
    state: Mutex<State>,
    /// Set once `out` has ended.
    eof: Mutex<bool>,
    eof_cv: Condvar,
    stopping: Mutex<bool>,
}

impl Shared {
    fn set_state(&self, s: State) {
        *self.state.lock().unwrap() = s;
    }

    fn wait_eof(&self, timeout: Duration) -> bool {
        let g = self.eof.lock().unwrap();
        let (g, _) = self.eof_cv.wait_timeout_while(g, timeout, |e| !*e).unwrap();
        *g
    }
}

/// An open session: `in` to write frames and commands to, and what the
/// wall says on `out`, a line at a time, until it ends.
pub struct Session {
    pub name: String,
    /// Every line the wall says on `out` but `ping`, without its newline,
    /// from the opening `hello` and `opened` on; the channel ends when
    /// `out` does.
    pub lines: Receiver<String>,
    /// The bytes after a `picture` or `mirror` line, which is on `lines`
    /// too.
    pub blobs: Receiver<Blob>,
    writer: Writer,
    shared: Arc<Shared>,
    /// The opener's ssh, where the wall is on another host.
    listener: Arc<Mutex<Option<Child>>>,
    /// The writer's ssh, likewise.
    carrier: Option<Child>,
    done: bool,
}

impl Session {
    /// Ask at `target`'s door for session `name`, `request` being the rest
    /// of the `open` line (`layer take=1 type=cells`, `controller`).
    pub fn open(target: &Target, name: &str, request: &str) -> Result<Session, OpenError> {
        Session::open_on(&Link::new(target, None, None), name, request)
    }

    /// The same, on a link made with a user or a run directory of its own.
    ///
    /// Waits for the wall's answer: `opened`, then the writer is started
    /// and the session returned; `error open`, `Refused`; no door,
    /// `NoWall`; no `out` within five seconds or the name a session's
    /// already, `Dropped`. A cells layer's ssh is compressed, a video
    /// layer's and a controller's not, as Python has them.
    pub fn open_on(link: &Link, name: &str, request: &str) -> Result<Session, OpenError> {
        if !session_token(name) {
            return Err(OpenError::Refused(format!("bitwall: {name:?} is not a session name")));
        }
        let run = link.run().map_err(OpenError::NoWall)?;
        let words: Vec<&str> = request.split_whitespace().collect();
        let role = words.first().copied().unwrap_or("");
        let kind = words.iter().find_map(|w| w.strip_prefix("type=")).unwrap_or("cells");
        let compress = role == "layer" && kind != "video";
        let line = format!("open {name} {}", words.join(" "));

        let (lines_tx, lines) = mpsc::channel();
        let (blobs_tx, blobs) = mpsc::channel();
        let (ev_tx, ev_rx) = mpsc::channel();
        let shared = Arc::new(Shared {
            state: Mutex::new(State::Opening),
            eof: Mutex::new(false),
            eof_cv: Condvar::new(),
            stopping: Mutex::new(false),
        });
        let writer = Writer::empty();
        let listener: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
        let mut reader = Reader {
            name: name.to_string(),
            lines: lines_tx,
            blobs: blobs_tx,
            events: ev_tx,
            shared: shared.clone(),
            writer: writer.clone(),
            listener: listener.clone(),
            opened: false,
        };

        if link.local() {
            let (run, name2) = (run.clone(), name.to_string());
            thread::Builder::new()
                .name(format!("bitwall-out-{name}"))
                .spawn(move || match knock_here(&run, &name2, &line) {
                    Ok(out) => reader.listen(out),
                    Err(code) => reader.end(Some(code)),
                })
                .map_err(OpenError::Io)?;
        } else {
            let cmd = format!(
                "exec python3 -c {} {} {} {}",
                shell_quote(OPENER),
                shell_quote(&run),
                shell_quote(name),
                shell_quote(&line)
            );
            let argv = link.argv(&cmd, &[], compress);
            // In a process group of its own, as the writer is: a Ctrl+C is
            // for the caller, which closes the session in good order.
            let mut child = Command::new(&argv[0])
                .args(&argv[1..])
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .process_group(0)
                .spawn()?;
            let out = child.stdout.take().unwrap();
            *listener.lock().unwrap() = Some(child);
            thread::Builder::new()
                .name(format!("bitwall-out-{name}"))
                .spawn(move || reader.listen(out))
                .map_err(OpenError::Io)?;
        }

        let mut session = Session {
            name: name.to_string(),
            lines,
            blobs,
            writer,
            shared,
            listener,
            carrier: None,
            done: false,
        };
        let target = link.target.to_string();
        match ev_rx.recv_timeout(OPEN_WAIT + Duration::from_secs(15)) {
            Ok(Event::Opened) => {
                let path = format!("{run}/s/{name}/in");
                let pipe: io::Result<Box<dyn Write + Send>> = if link.local() {
                    open_in_here(&path).map(|f| Box::new(f) as Box<dyn Write + Send>)
                } else {
                    let argv = link.writer_argv(&path, compress);
                    let spawned = Command::new(&argv[0])
                        .args(&argv[1..])
                        .stdin(Stdio::piped())
                        .stdout(Stdio::null())
                        .process_group(0)
                        .spawn();
                    spawned.map(|mut child| {
                        let stdin = child.stdin.take().unwrap();
                        session.carrier = Some(child);
                        Box::new(stdin) as Box<dyn Write + Send>
                    })
                };
                if let Err(e) = pipe.and_then(|p| session.writer.set(p)) {
                    session.kill();
                    return Err(OpenError::Io(e));
                }
                let mut st = session.shared.state.lock().unwrap();
                if *st == State::Opening {
                    *st = State::Open;
                }
                drop(st);
                Ok(session)
            }
            Ok(Event::Refused(why)) => {
                session.kill();
                Err(OpenError::Refused(format!("bitwall: {target}: refused: {why}")))
            }
            Ok(Event::Ended(code)) => {
                session.kill();
                Err(match code {
                    Some(NO_WALL) => OpenError::NoWall(format!("bitwall: {target}: no wall is there")),
                    Some(DROPPED) | Some(IN_USE) => {
                        OpenError::Dropped(format!("bitwall: {target}: the door did not answer"))
                    }
                    _ => OpenError::NoWall(format!("bitwall: {target}: could not open a session")),
                })
            }
            Err(_) => {
                session.kill();
                Err(OpenError::Dropped(format!("bitwall: {target}: the door did not answer")))
            }
        }
    }

    /// Write to `in`, all of it, blocking while the pipe is full.
    pub fn write(&mut self, data: &[u8]) -> io::Result<()> {
        self.writer.write(data)
    }

    /// Write to `in`, all of it, but wait only until `stop` is set or
    /// `timeout` has passed (`Interrupted`, `TimedOut`): a wall that has
    /// stopped reading holds a write that long and no longer.
    pub fn write_until(&mut self, data: &[u8], stop: Option<&AtomicBool>, timeout: Option<Duration>) -> io::Result<()> {
        self.writer.write_until(data, stop, timeout)
    }

    /// Whether `in` has taken everything written to it, so a frame written
    /// now goes at once rather than waiting behind another.
    pub fn ready(&self) -> bool {
        self.writer.idle()
    }

    /// The session's `in`, for another thread to write to as well.
    pub fn writer(&self) -> Writer {
        self.writer.clone()
    }

    /// How the session stands now.
    pub fn state(&self) -> State {
        self.shared.state.lock().unwrap().clone()
    }

    /// Say `last` (a command) if given, and end the session.
    ///
    /// `last` goes after everything else, `in` is let go so the wall sees
    /// its end, `out` is given two seconds to end with the wall's `closed`,
    /// and then the session's ends are let go. Bounded throughout, at
    /// about four seconds, so a caller stopping is not held up by a wall
    /// that is not answering.
    pub fn close(mut self, last: Option<&[u8]>) {
        self.close_within(last, Duration::from_secs(2));
    }

    fn close_within(&mut self, last: Option<&[u8]>, linger: Duration) {
        *self.shared.stopping.lock().unwrap() = true;
        if let Some(last) = last {
            let _ = self.writer.write_until(last, None, Some(linger / 4));
        }
        self.writer.close(linger / 4);
        if let Some(mut child) = self.carrier.take() {
            // It ends when its input does, once what it holds has reached
            // the wall; it is only killed when it will not.
            if !wait_child(&mut child, linger / 2) {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        self.shared.wait_eof(linger);
        self.end_listener();
        self.done = true;
    }

    /// Let go of both ends at once, as when the wall has gone.
    fn kill(&mut self) {
        *self.shared.stopping.lock().unwrap() = true;
        self.writer.abandon();
        if let Some(mut child) = self.carrier.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.end_listener();
        self.done = true;
    }

    fn end_listener(&mut self) {
        if let Some(mut child) = self.listener.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for Session {
    /// A session let go without `close` ends as a caller that dies does:
    /// its `in` ends, and the wall lets the session go.
    fn drop(&mut self) {
        if !self.done {
            self.kill();
        }
    }
}

/// Wait up to `timeout` for a child to end; whether it did.
fn wait_child(child: &mut Child, timeout: Duration) -> bool {
    let until = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if Instant::now() < until => thread::sleep(Duration::from_millis(20)),
            _ => return false,
        }
    }
}

// ----------------------------------------------------------- this machine

#[cfg(target_os = "linux")]
const O_NONBLOCK: i32 = 0o4000;
#[cfg(not(target_os = "linux"))]
const O_NONBLOCK: i32 = 0x0004;
const F_GETFL: i32 = 3;
const F_SETFL: i32 = 4;
const ENXIO: i32 = 6;

unsafe extern "C" {
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
}

/// A fifo opened for writing without waiting for a reader: fails at once,
/// ENXIO, when nobody reads it.
fn open_write_now(path: &str) -> io::Result<File> {
    OpenOptions::new().write(true).custom_flags(O_NONBLOCK).open(path)
}

/// The opener's steps, taken here: the name not a session's already, the
/// door read by somebody, the request in one write, `out` within five
/// seconds. `out` opened for reading, or the opener's exit code.
fn knock_here(run: &str, name: &str, request: &str) -> Result<File, i32> {
    let sdir = format!("{run}/s/{name}");
    if std::fs::symlink_metadata(&sdir).is_ok() {
        return Err(IN_USE);
    }
    let mut door = open_write_now(&format!("{run}/door.in")).map_err(|_| NO_WALL)?;
    door.write_all(format!("{request}\n").as_bytes()).map_err(|_| NO_WALL)?;
    drop(door);
    let out = format!("{sdir}/out");
    let until = Instant::now() + OPEN_WAIT;
    while !Path::new(&out).exists() {
        if Instant::now() >= until {
            return Err(DROPPED);
        }
        thread::sleep(Duration::from_millis(20));
    }
    File::open(&out).map_err(|_| NO_WALL)
}

/// A session's `in` here, opened for blocking writes. Opened without
/// waiting first, for up to two seconds while the wall has yet to read
/// it, so a wall that never does is an error rather than a hang.
fn open_in_here(path: &str) -> io::Result<File> {
    let until = Instant::now() + Duration::from_secs(2);
    let f = loop {
        match open_write_now(path) {
            Ok(f) => break f,
            Err(e) if e.raw_os_error() == Some(ENXIO) && Instant::now() < until => {
                thread::sleep(Duration::from_millis(20))
            }
            Err(e) => return Err(e),
        }
    };
    let fd = f.as_raw_fd();
    // SAFETY: fcntl on a descriptor this File owns, with plain int flags.
    unsafe {
        let flags = fcntl(fd, F_GETFL);
        if flags < 0 || fcntl(fd, F_SETFL, flags & !O_NONBLOCK) < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(f)
}

// -------------------------------------------------------------- listening

/// The thread reading `out`: whole lines to `lines`, a picture's bytes to
/// `blobs`, a `ping` answered, and what the session's own lines mean noted.
struct Reader {
    name: String,
    lines: Sender<String>,
    blobs: Sender<Blob>,
    events: Sender<Event>,
    shared: Arc<Shared>,
    writer: Writer,
    listener: Arc<Mutex<Option<Child>>>,
    opened: bool,
}

impl Reader {
    fn listen<R: Read>(&mut self, mut out: R) {
        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = vec![0u8; 65536];
        loop {
            let n = match out.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            };
            buf.extend_from_slice(&chunk[..n]);
            self.feed(&mut buf);
        }
        let code = {
            let mut g = self.listener.lock().unwrap();
            g.as_mut().and_then(|c| c.wait().ok()).and_then(|s| s.code())
        };
        self.end(if self.opened { None } else { code.or(Some(0)) });
    }

    /// Whole lines and blobs out of what has arrived so far, the rest left
    /// in `buf` for the next read.
    fn feed(&mut self, buf: &mut Vec<u8>) {
        let mut at = 0;
        loop {
            let Some(cut) = buf[at..].iter().position(|&b| b == b'\n') else {
                if buf.len() - at > MAX_MESSAGE * 16 {
                    at = buf.len() - MAX_MESSAGE;
                }
                break;
            };
            let line = String::from_utf8_lossy(&buf[at..at + cut]).into_owned();
            let rest = at + cut + 1;
            let words: Vec<&str> = line.split_whitespace().collect();
            let verb = words.first().copied().unwrap_or("");
            // A picture or a mirror frame says how many bytes follow; a line
            // that says none, `mirror <session> clear`, is only a line.
            if verb == "picture" || verb == "mirror" {
                let size = words[1..]
                    .iter()
                    .rev()
                    .filter(|w| !w.contains('='))
                    .find(|w| !w.is_empty() && w.len() <= 19 && w.bytes().all(|c| c.is_ascii_digit()))
                    .and_then(|w| w.parse::<usize>().ok());
                if let Some(size) = size {
                    if buf.len() - rest < size {
                        break; // the bytes are still coming
                    }
                    let data = buf[rest..rest + size].to_vec();
                    let _ = self.lines.send(line.clone());
                    let _ = self.blobs.send(Blob { said: line, data });
                    at = rest + size;
                    continue;
                }
            }
            at = rest;
            drop(words);
            self.heard(line);
        }
        buf.drain(..at);
    }

    fn heard(&mut self, line: String) {
        let words: Vec<&str> = line.split_whitespace().collect();
        let verb = words.first().copied().unwrap_or("");
        let mine = words.get(1) == Some(&self.name.as_str());
        match verb {
            "ping" => {
                // Queued, not waited for, so a frame being written cannot
                // hold up what the wall says meanwhile.
                let _ = self.writer.post(apc("pong"), false);
                return;
            }
            "opened" if mine && !self.opened => {
                self.opened = true;
                let _ = self.lines.send(line.clone());
                let _ = self.events.send(Event::Opened);
                return;
            }
            "error" if words.get(1) == Some(&"open") && words.get(2) == Some(&self.name.as_str()) => {
                let why = words.get(3).unwrap_or(&"?").to_string();
                self.shared.set_state(State::Refused(why.clone()));
                let _ = self.events.send(Event::Refused(why));
            }
            "closed" if mine => {
                self.shared.set_state(State::Closed(words.get(2).unwrap_or(&"?").to_string()));
            }
            _ => {}
        }
        drop(words);
        let _ = self.lines.send(line);
    }

    /// `out` has ended, or never came: say how the session went.
    fn end(&mut self, code: Option<i32>) {
        let stopping = *self.shared.stopping.lock().unwrap();
        {
            let mut st = self.shared.state.lock().unwrap();
            match *st {
                State::Opening if !stopping => {
                    *st = match code {
                        Some(NO_WALL) => State::Absent,
                        Some(DROPPED) | Some(IN_USE) => State::Dropped,
                        _ => State::Lost,
                    }
                }
                State::Open if !stopping => *st = State::Lost,
                _ => {}
            }
        }
        if !self.opened {
            let _ = self.events.send(Event::Ended(code));
        }
        *self.shared.eof.lock().unwrap() = true;
        self.shared.eof_cv.notify_all();
    }
}

// ------------------------------------------------------------- controller

/// Which Controller of this process each is, for its default name and its
/// ids.
static CONTROLLERS: AtomicUsize = AtomicUsize::new(1);

/// A controller session on a wall, for a program that talks to a wall
/// rather than draws on it.
///
/// ```no_run
/// use bitwall::session::{Controller, Target};
/// use std::time::Duration;
/// let mut c = Controller::open(&Target::parse("pi.local:1").unwrap(), None, None).unwrap();
/// for line in c.ask("stats 1 show=all", Duration::from_secs(3)) {
///     println!("{line}");
/// }
/// c.close();
/// ```
///
/// `open` waits for the wall's answer; a request the door drops is made
/// again under `<name>-r<k>`, a few times, since the name is most often a
/// session of this controller's own the wall has not yet let go of. Once
/// open it asks for the wall's report, which connects the session, and
/// keeps it as `report`. `ask` sends a command with an id and returns
/// every line that answered it; `read` is the next line nobody has taken.
/// The name is `ctl-<pid>` unless given, the second and later controllers
/// of one process `ctl-<pid>-2` and on.
pub struct Controller {
    pub session: Session,
    pub name: String,
    /// What the wall said of itself as the session opened.
    pub report: Vec<String>,
    heard: VecDeque<String>,
    n: u64,
    ids: String,
}

impl Controller {
    /// How many times a dropped request is made, and the wait between,
    /// about what a wall takes to let a name go.
    pub const TRIES: usize = 3;
    pub const RETRY: Duration = Duration::from_millis(500);
    /// How long the report asked for as the session opens is waited for.
    pub const REPORT_WAIT: Duration = Duration::from_secs(15);

    /// A controller session on `target`, `instance` said on the open so
    /// that the wall's `sessions` lists it.
    pub fn open(target: &Target, name: Option<&str>, instance: Option<&str>) -> Result<Controller, OpenError> {
        Controller::open_on(&Link::new(target, None, None), name, instance)
    }

    pub fn open_on(link: &Link, name: Option<&str>, instance: Option<&str>) -> Result<Controller, OpenError> {
        let seq = CONTROLLERS.fetch_add(1, Ordering::SeqCst);
        let seq_tag = seq.to_string();
        let asked = name
            .map(str::to_string)
            .unwrap_or_else(|| session_name("ctl", if seq > 1 { Some(seq_tag.as_str()) } else { None }));
        let request = match instance.filter(|i| instance_token(i)) {
            Some(i) => format!("controller instance={i}"),
            None => "controller".to_string(),
        };
        let mut last = None;
        for attempt in 0..Self::TRIES {
            let name = if attempt == 0 {
                asked.clone()
            } else {
                let tag = format!("-r{attempt}");
                format!("{}{}", &asked[..asked.len().min(32 - tag.len())], tag)
            };
            match Session::open_on(link, &name, &request) {
                Ok(session) => {
                    let mut c = Controller {
                        session,
                        name,
                        report: Vec::new(),
                        heard: VecDeque::new(),
                        n: 0,
                        ids: format!("c{}-{}-", std::process::id(), seq),
                    };
                    // The greeting at the door is the session's own business.
                    while let Ok(line) = c.session.lines.recv_timeout(Duration::from_millis(200)) {
                        let mut w = line.split_whitespace();
                        let verb = w.next();
                        if verb == Some("opened") && w.next() == Some(c.name.as_str()) {
                            break;
                        }
                        if verb != Some("hello") {
                            c.heard.push_back(line);
                        }
                    }
                    // The first write connects the session, and a wall
                    // removes one nobody writes to.
                    c.report = c.ask("report", Self::REPORT_WAIT);
                    return Ok(c);
                }
                Err(OpenError::Dropped(e)) => {
                    last = Some(OpenError::Dropped(e));
                    thread::sleep(Self::RETRY);
                }
                Err(e) => return Err(e),
            }
        }
        Err(last.unwrap())
    }

    /// One command, as PROTOCOL.md writes it: `font 20`.
    pub fn send(&mut self, text: &str) -> io::Result<()> {
        self.session.write(&apc(text))
    }

    /// The next line the wall said, or None when nothing came in time or
    /// the session has ended.
    pub fn read(&mut self, timeout: Duration) -> Option<String> {
        if let Some(l) = self.heard.pop_front() {
            return Some(l);
        }
        self.session.lines.recv_timeout(timeout).ok()
    }

    /// Send `text` with an id and collect what answered it. A report ends
    /// at its `area`, `sessions` at its count, `ping` at `pong`; any other
    /// answer is taken to be over once nothing more has come for 0.4
    /// seconds, or at `timeout`. Lines heard meanwhile that were not
    /// answers are kept for `read`.
    pub fn ask(&mut self, text: &str, timeout: Duration) -> Vec<String> {
        self.n += 1;
        let id = format!("{}{}", self.ids, self.n);
        let last = match text.split_whitespace().next().unwrap_or("") {
            "report" => Some("area"),
            "sessions" => Some("sessions"),
            "ping" => Some("pong"),
            _ => None,
        };
        let mut got = Vec::new();
        if self.send(&with_id(text, &id)).is_err() {
            return got;
        }
        let quiet = Duration::from_millis(400);
        let deadline = Instant::now() + timeout;
        let mut settle: Option<Instant> = None;
        let mut others = Vec::new();
        loop {
            let until = settle.map_or(deadline, |s| s.min(deadline));
            let left = until.saturating_duration_since(Instant::now());
            let line = match self.session.lines.recv_timeout(left) {
                Ok(l) => l,
                Err(RecvTimeoutError::Timeout) => break,
                Err(RecvTimeoutError::Disconnected) => break,
            };
            let mut words = line.split_whitespace();
            let verb = words.next().unwrap_or("");
            let said_id = words.find_map(|w| w.strip_prefix("id="));
            if said_id != Some(id.as_str()) || verb == ">" || verb == "<" {
                others.push(line);
                continue;
            }
            let end = last.is_some_and(|l| verb == l || verb == "error");
            got.push(line);
            if end {
                break;
            }
            settle = Some(Instant::now() + quiet);
        }
        self.heard.extend(others);
        got
    }

    /// Say `close`, and let the session go.
    pub fn close(self) {
        self.session.close(Some(&apc("close")));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn targets() {
        let t = |s: &str| Target::parse(s).unwrap();
        assert_eq!(t("pi.local"), Target { host: "pi.local".into(), wall: "0".into() });
        assert_eq!(t("pi.local:1"), Target { host: "pi.local".into(), wall: "1".into() });
        assert_eq!(t("pi.local:001").wall, "001");
        assert_eq!(t("localhost:web-kitchen").wall, "web-kitchen");
        assert_eq!(t("fe80::1"), Target { host: "fe80::1".into(), wall: "0".into() });
        assert_eq!(t("::1"), Target { host: "::1".into(), wall: "0".into() });
        assert_eq!(t(""), Target { host: "".into(), wall: "0".into() });
        assert!(t("localhost").local() && t("127.0.0.1:2").local() && t("").local());
        assert!(!t("pi.local").local());
        for bad in ["pi:", "pi:Kitchen", "pi:-a", "pi:a b", "pi:é", &format!("pi:{}", "a".repeat(33))] {
            assert!(Target::parse(bad).is_err(), "{bad}");
        }
        assert!(Target::parse(&format!("pi:{}", "a".repeat(32))).is_ok());
    }

    #[test]
    fn names() {
        let pid = std::process::id();
        assert_eq!(session_name("sysinfo", None), format!("sysinfo-{pid}"));
        assert_eq!(session_name("ctl", Some("2")), format!("ctl-{pid}-2"));
        assert_eq!(session_name("my sender!", None), format!("my-sender-{pid}"));
        assert_eq!(session_name("..", None), format!("sender-{pid}"));
        let long = session_name(&"x".repeat(40), Some("tag"));
        assert_eq!(long.len(), 32);
        assert!(long.ends_with(&format!("-{pid}-tag")) && session_token(&long));
    }

    #[test]
    fn ids_and_quoting() {
        assert_eq!(with_id("stats 1", "c1-1-1"), "stats 1 id=c1-1-1");
        assert_eq!(with_id("meta name=Galaga", "x"), "meta id=x name=Galaga");
        assert_eq!(shell_quote("/run/user/1000/bitwall/0"), "/run/user/1000/bitwall/0");
        assert_eq!(shell_quote(""), "''");
        assert_eq!(shell_quote("open a layer z=0"), "'open a layer z=0'");
        assert_eq!(shell_quote("it's"), "'it'\"'\"'s'");
    }

    /// A pipe nobody reads: every write waits for ever.
    struct Stuck;

    impl Write for Stuck {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            loop {
                thread::sleep(Duration::from_secs(60));
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn writes_given_up() {
        let w = Writer::empty();
        assert_eq!(w.write(b"x").unwrap_err().kind(), io::ErrorKind::BrokenPipe);
        w.set(Box::new(Stuck)).unwrap();
        let t = Instant::now();
        let e = w.write_until(b"frame", None, Some(Duration::from_millis(100))).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::TimedOut);
        assert!(t.elapsed() < Duration::from_millis(300));
        let stop = AtomicBool::new(true);
        let e = w.write_until(b"frame", Some(&stop), None).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::Interrupted);
        assert!(!w.idle());
        let t = Instant::now();
        assert!(!w.close(Duration::from_millis(100)));
        assert!(t.elapsed() < Duration::from_millis(300));
        assert_eq!(w.write(b"x").unwrap_err().kind(), io::ErrorKind::BrokenPipe);
    }

    #[test]
    fn writes_in_order() {
        let (r, wr) = std::io::pipe().unwrap();
        let w = Writer::empty();
        w.set(Box::new(wr)).unwrap();
        let w2 = w.clone();
        let t = thread::spawn(move || {
            for i in 0..100 {
                w2.write(format!("b{i};").as_bytes()).unwrap();
            }
        });
        for i in 0..100 {
            w.write(format!("a{i};").as_bytes()).unwrap();
        }
        t.join().unwrap();
        let _ = w.post(b"end".to_vec(), false);
        assert!(w.close(Duration::from_secs(1)));
        let mut got = String::new();
        let mut r = r;
        r.read_to_string(&mut got).unwrap();
        assert!(got.ends_with("end"));
        let parts: Vec<&str> = got.trim_end_matches("end").split_terminator(';').collect();
        assert_eq!(parts.len(), 200);
        let a: Vec<&&str> = parts.iter().filter(|p| p.starts_with('a')).collect();
        assert!(a.iter().enumerate().all(|(i, p)| **p == format!("a{i}")));
    }

    #[test]
    fn ssh_argv() {
        let t = Target::parse("pi.local:1").unwrap();
        let l = Link::new(&t, Some("pi"), Some("/run/user/1000/bitwall/1"));
        let cm = std::env::temp_dir().join("bitwall-cm-pi.local-1");
        assert_eq!(
            l.writer_argv("/run/user/1000/bitwall/1/s/x-1/in", true),
            [
                "ssh", "-C", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", &format!("ControlPath={}", cm.display()),
                "-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "pi@pi.local", "exec cat > /run/user/1000/bitwall/1/s/x-1/in",
            ]
        );
        let raw = l.argv("true", &[], false);
        assert_eq!(raw[1], "-o");
        assert!(raw.contains(&format!("ControlPath={}-raw", cm.display())));
        assert_eq!(l.run().unwrap(), "/run/user/1000/bitwall/1");
        let here = Link::new(&Target::parse("localhost").unwrap(), None, None);
        assert_eq!(here.argv("true", &[], true), ["sh", "-c", "true"]);
    }
}
