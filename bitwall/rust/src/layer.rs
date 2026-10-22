//! A video layer: pictures in pixels over a wall's cells, and a stream the
//! wall decodes itself (`bitwall/video.py`).
//!
//! ```no_run
//! use bitwall::layer::{Hold, Layer, Place};
//! use std::time::Duration;
//! let layer = Layer::new("pi.local:1", Place::Above).unwrap();
//! layer.open(Duration::from_secs(15)).unwrap();
//! let rgba = vec![255u8; 64 * 64 * 4];
//! layer.frame(&bitwall::qoi::encode(&rgba, 64, 64), (40, 900), None, Hold::Ms(1000)).unwrap();
//! layer.clear();
//! layer.close();
//! ```
//!
//! A layer is a session of its own beside the base (PROTOCOL.md, "A video
//! layer"), so a sender draws over whatever the wall's base shows without
//! taking the base from it. It asks for a place, not a number: `above`, on
//! top of the layers placed from below; `top`, under those placed from the
//! top; or a number, that slot. The wall says the slot it gave, which `z`
//! holds, and `moved` when the stack moves it. `Place::from_env` is
//! `BITWALL_PLACE`, which a controller sets from its catalogue, or `above`.
//!
//! **A frame is QOI, PNG or JPEG**, read from its first bytes. QOI is what
//! every wall that takes video draws; `takes("jpeg")` says whether this
//! one draws the others (`images=` in its `caps`), and a photograph goes
//! as the JPEG it came as where it does. `size` is the box in the glass's
//! pixels the wall scales it to, its shape the sender's to keep.
//!
//! **The sender paces, and a full pipe drops a frame rather than queue
//! it.** `frame` sends only when the pipe to the wall has taken everything
//! before it, and says whether it did. A frame once begun goes whole,
//! since its count is what tells the wall where the next command starts.
//!
//! **A stream on `data`** is a film, or QOI images or JPEGs back to back,
//! that the wall decodes and plays out itself (PROTOCOL.md, "The data
//! pipe"): `Layer::new(t, Place::Z(0)).take().data("hevc").fps(24.0)
//! .start(ms)`, then `data_write` and `data_end`. It goes on a fifo of its
//! own through a writer of its own, so commands on `in` never wait behind
//! it, and a write waits for room rather than dropping, since a compressed
//! stream cannot lose bytes. Each writer is one stream: `data_end` ends it,
//! and the next write starts the next.
//!
//! **A layer that loses its wall opens again on its own**, as the base
//! does, from a thread of its own, and stays away when the wall displaced
//! or refused it. What the wall says about its glass, who is watching and
//! whether it wants a `repaint` is kept, for a sender to read.

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::moving::{self, Retarget};
use crate::protocol::{self, Message};
use crate::session::{self, apc, instance_token, Link, OpenError, Session, State, Target, Writer, CHUNK};

/// How long a layer waits, at most, between asking again for a wall that
/// is not there.
pub const RETRY_MOST: f64 = 5.0;

/// What a stream on `data` may be.
pub const CODECS: [&str; 4] = ["hevc", "h264", "qoi", "mjpeg"];

/// Where a stream's frames may sit on a glass of another shape.
pub const ALIGNS: [&str; 3] = ["top", "centre", "bottom"];

/// `box` as it goes down `in`: the box, or alone for the glass.
fn box_command(frame_box: Option<(i32, i32, u32, u32)>) -> String {
    match frame_box {
        Some((x, y, w, h)) => format!("box at={x},{y} size={w}x{h}"),
        None => "box".into(),
    }
}

/// How many of the wall's `error frame` lines are kept.
const ERRORS_KEPT: usize = 16;

/// Which layer of this process each is, so two never share a session name.
static LAYERS: AtomicUsize = AtomicUsize::new(0);

fn log(text: &str) {
    eprintln!("bitwall: {text}");
}

/// Where a layer asks to go.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Place {
    /// On top of the layers placed from below.
    Above,
    /// Under the layers placed from the top.
    Top,
    /// Exactly that slot; 0 is the base.
    Z(u32),
}

impl Place {
    /// `above`, `top` or a number.
    pub fn parse(text: &str) -> Result<Place, String> {
        match text {
            "above" => Ok(Place::Above),
            "top" => Ok(Place::Top),
            t if !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()) => t
                .parse()
                .map(Place::Z)
                .map_err(|_| format!("bitwall: a layer's place is above, top or a number, not {t:?}")),
            t => Err(format!("bitwall: a layer's place is above, top or a number, not {t:?}")),
        }
    }

    /// `BITWALL_PLACE`, as a controller sets it from its catalogue, or
    /// `above`. A sender placed `controller` holds no layer of its own:
    /// one it opens goes above.
    pub fn from_env() -> Result<Place, String> {
        match std::env::var("BITWALL_PLACE") {
            Ok(p) if !p.is_empty() && p != "controller" => Place::parse(&p),
            _ => Ok(Place::Above),
        }
    }

    pub(crate) fn word(&self) -> String {
        match self {
            Place::Above => "above".into(),
            Place::Top => "top".into(),
            Place::Z(n) => n.to_string(),
        }
    }
}

/// How long a frame stays up: so many ms, or until the next frame, a
/// `clear` or the session's end.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hold {
    Ms(u32),
    Still,
}

impl Default for Hold {
    fn default() -> Hold {
        Hold::Ms(1000)
    }
}

/// What a layer asks for, and what it keeps saying each time it opens.
struct Config {
    place: Place,
    take: bool,
    codec: Option<String>,
    fps: Option<f64>,
    start: Option<i64>,
    instance: Option<String>,
    align: String,
    /// The box (x, y, w, h) the stream's frames are fitted to, or None
    /// for the glass.
    frame_box: Option<(i32, i32, u32, u32)>,
    user: Option<String>,
    run: Option<String>,
    /// What the layer says it is, in the order first said.
    meta: Vec<(String, String)>,
    /// The `start` the next opening asks for, where the sender gave one.
    start_next: Option<i64>,
}

/// The session the layer has open, as frames and commands reach it.
struct Current {
    writer: Writer,
    name: String,
    /// Which opening it is: a data writer is for one session only.
    opening: u64,
}

#[derive(Default)]
struct St {
    z: Option<i64>,
    has_data: bool,
    cur: Option<Current>,
    said: HashMap<String, Message>,
    errors: VecDeque<String>,
    repaint: bool,
    stopped: Option<String>,
    frames_sent: u64,
    frames_dropped: u64,
    bytes_sent: u64,
    /// Whether `open` has been asked for since the last settling.
    settled: bool,
}

type Lost = Arc<dyn Fn() + Send + Sync>;

pub(crate) struct Inner {
    /// The wall as asked for, and as said in the log; changed only by a
    /// retarget.
    target: Mutex<Target>,
    label: Mutex<String>,
    /// Bumped by every retarget, so the link knows a session it holds or
    /// is opening is on the wall it has left.
    moves: AtomicU64,
    index: usize,
    link: Mutex<Option<Arc<Link>>>,
    cfg: Mutex<Config>,
    st: Mutex<St>,
    settled: Condvar,
    /// The session, held for `close`; the link thread reads its lines.
    session: Mutex<Option<Session>>,
    /// The writer on `data`, held through each whole write.
    data: Mutex<Option<DataPipe>>,
    when_lost: Mutex<Vec<Lost>>,
    stop: AtomicBool,
    started: AtomicBool,
}

/// A layer above (or at) a wall's base, held as a session of its own.
/// Cheap to clone; every clone is the same layer.
#[derive(Clone)]
pub struct Layer {
    inner: Arc<Inner>,
}

impl Layer {
    /// A layer on `target` (`host`, `host:<wall>`) at `place`, not yet
    /// asked for: say what else it is, then `open`.
    ///
    /// A layer a controller started carries its instance id, as a base
    /// does: `BITWALL_INSTANCE` is taken from the environment here unless
    /// `instance` gives one, so a process this one starts is not it.
    pub fn new(target: &str, place: Place) -> Result<Layer, String> {
        let t = Target::parse(target)?;
        let label = t.to_string();
        Ok(Layer {
            inner: Arc::new(Inner {
                target: Mutex::new(t),
                label: Mutex::new(label),
                moves: AtomicU64::new(0),
                index: LAYERS.fetch_add(1, Ordering::SeqCst),
                link: Mutex::new(None),
                cfg: Mutex::new(Config {
                    place,
                    take: false,
                    codec: None,
                    fps: None,
                    start: None,
                    instance: None,
                    align: "centre".into(),
                    frame_box: None,
                    user: None,
                    run: None,
                    meta: Vec::new(),
                    start_next: None,
                }),
                st: Mutex::new(St::default()),
                settled: Condvar::new(),
                session: Mutex::new(None),
                data: Mutex::new(None),
                when_lost: Mutex::new(Vec::new()),
                stop: AtomicBool::new(false),
                started: AtomicBool::new(false),
            }),
        })
    }

    fn cfg(&self) -> MutexGuard<'_, Config> {
        self.inner.cfg.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn st(&self) -> MutexGuard<'_, St> {
        self.inner.st.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// The wall it is on, as the log says it.
    fn label(&self) -> String {
        self.inner.label()
    }

    fn moves(&self) -> u64 {
        self.inner.moves.load(Ordering::SeqCst)
    }

    // ------------------------------------------------------ asking

    /// Take the base from whoever holds it: for `Place::Z(0)` only.
    pub fn take(self) -> Layer {
        self.cfg().take = true;
        self
    }

    /// Carry a stream on `data` in `codec`: hevc, h264, qoi or mjpeg.
    pub fn data(self, codec: &str) -> Layer {
        self.cfg().codec = Some(codec.to_string());
        self
    }

    /// The stream's frames a second: the wall plays it out on its clock.
    pub fn fps(self, fps: f64) -> Layer {
        self.cfg().fps = Some(fps);
        self
    }

    /// The Unix time in ms the first stream's first frame is due at.
    pub fn start(self, ms: i64) -> Layer {
        self.cfg().start = Some(ms);
        self
    }

    /// The controller's instance id this layer runs under.
    pub fn instance(self, id: &str) -> Layer {
        self.cfg().instance = Some(id.to_string());
        self
    }

    /// The user the wall's host is reached as.
    pub fn user(self, user: &str) -> Layer {
        self.cfg().user = Some(user.to_string());
        self
    }

    /// The wall's directory on its host, where it is known.
    pub fn run(self, dir: &str) -> Layer {
        self.cfg().run = Some(dir.to_string());
        self
    }

    /// The `open` request's words after `layer`, rendered as Python
    /// renders them, sorted; and what this opening is to be called.
    fn request(&self, opens: u32) -> Result<String, String> {
        let z = self.st().z;
        let cfg = self.cfg();
        // Asked again, a layer placed from below asks for the slot it had,
        // which puts it back in its place in the stack whoever comes back
        // first; one placed `top` asks for the top again.
        let again = z.is_some_and(|z| z > 0) && cfg.place != Place::Top;
        let mut opts: Vec<(String, String)> = vec![
            ("z".into(), if again { z.unwrap().to_string() } else { cfg.place.word() }),
            ("type".into(), "video".into()),
        ];
        if cfg.take {
            opts.push(("take".into(), "1".into()));
        }
        if let Some(codec) = &cfg.codec {
            opts.push(("data".into(), "1".into()));
            opts.push(("codec".into(), codec.clone()));
            if let Some(fps) = cfg.fps {
                opts.push(("fps".into(), fmt_g(fps)));
            }
        }
        // `start` is the first stream's of the session it opens with. The
        // wall counts a new session's frames from 0 again, so the first
        // `start`, said again, would put every frame in the past: a layer
        // asked for again starts when its first frame is decoded, or when
        // the sender says with `start_at`.
        let start = cfg.start_next.or(if opens == 0 { cfg.start } else { None });
        if let (Some(s), Some(_)) = (start, &cfg.codec) {
            opts.push(("start".into(), s.to_string()));
        }
        if let Some(i) = &cfg.instance {
            opts.push(("instance".into(), i.clone()));
        }
        let pairs: Vec<(&str, &str)> = opts.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        protocol::render("layer", &[], &pairs)
    }

    /// Check what was asked, as Python checks it on making a layer.
    fn check(&self) -> Result<(), String> {
        let c = self.cfg();
        if c.take && c.place != Place::Z(0) {
            return Err("bitwall: only the base is taken; take is for Place::Z(0)".into());
        }
        if let Some(codec) = &c.codec {
            if !CODECS.contains(&codec.as_str()) {
                return Err(format!("bitwall: a stream's codec is one of {}, not {codec:?}", CODECS.join(", ")));
            }
        } else if c.fps.is_some() || c.start.is_some() {
            return Err("bitwall: fps and start are for a stream, data(codec)".into());
        }
        if c.start.is_some() && c.fps.is_none() {
            return Err("bitwall: start needs fps".into());
        }
        if let Some(f) = c.fps {
            if !(f.is_finite() && f > 0.0) {
                return Err(format!("bitwall: fps is a number above 0, not {f}"));
            }
        }
        Ok(())
    }

    // ------------------------------------------------------ opening

    /// Ask the wall for the layer, and wait up to `wait` for it. Err only
    /// where what was asked cannot be asked; a wall that is not there yet
    /// is asked again until it is, as `open_now` and `stopped` say.
    pub fn open(&self, wait: Duration) -> Result<&Layer, String> {
        if !self.inner.started.swap(true, Ordering::SeqCst) {
            if let Err(e) = self.check() {
                self.inner.started.store(false, Ordering::SeqCst);
                return Err(e);
            }
            {
                let mut c = self.cfg();
                if c.instance.is_none() {
                    let env = std::env::var("BITWALL_INSTANCE").ok();
                    if env.is_some() {
                        // SAFETY: as `attach` takes it: once, while the
                        // sender is starting.
                        unsafe { std::env::remove_var("BITWALL_INSTANCE") };
                    }
                    c.instance = env;
                }
                c.instance = c.instance.take().filter(|i| instance_token(i));
                if let Some(i) = c.instance.clone() {
                    if !c.meta.iter().any(|(k, _)| k == "instance") {
                        c.meta.insert(0, ("instance".into(), i));
                    }
                }
                let target = self.inner.target.lock().unwrap().clone();
                let link = Link::new(&target, c.user.as_deref(), c.run.as_deref());
                *self.inner.link.lock().unwrap() = Some(Arc::new(link));
                // One a controller started may be moved by it.
                if c.instance.is_some() {
                    let weak: Weak<dyn Retarget> = Arc::downgrade(&(self.inner.clone() as Arc<dyn Retarget>));
                    moving::hold(weak, moving::record_from_env().as_deref());
                }
            }
            self.st().settled = false;
            let me = self.clone();
            thread::Builder::new()
                .name("bitwall-layer".into())
                .spawn(move || me.link_loop())
                .map_err(|e| format!("bitwall: {e}"))?;
        }
        let until = Instant::now() + wait;
        let mut s = self.st();
        while !s.settled {
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            s = self.inner.settled.wait_timeout(s, left).unwrap_or_else(|e| e.into_inner()).0;
        }
        Ok(self)
    }

    fn settle(&self) {
        self.st().settled = true;
        self.inner.settled.notify_all();
    }

    fn stopping(&self) -> bool {
        self.inner.stop.load(Ordering::SeqCst)
    }

    /// Wait before asking again, but not past a move.
    fn pause(&self, secs: f64, moves: u64) {
        let end = Instant::now() + Duration::from_secs_f64(secs);
        while Instant::now() < end && !self.stopping() && self.moves() == moves {
            thread::sleep(Duration::from_millis(20));
        }
    }

    /// Ask for the layer, listen to it, and ask again whenever the wall
    /// goes, until closed, displaced or refused.
    fn link_loop(&self) {
        let mut opens = 0u32;
        let mut opening = 0u64;
        let mut wait = 0.25f64;
        let mut lost = false;
        loop {
            if self.stopping() {
                return;
            }
            let request = match self.request(opens) {
                Ok(r) => r,
                Err(e) => {
                    self.stay_out(&format!("cannot ask for the layer: {e}"));
                    return;
                }
            };
            // `l<k>` for which layer of this process it is, so it never has
            // the name of the process's base, and `r<k>` for each time it
            // asks again.
            let tag = format!("l{}", self.inner.index) + &if opens > 0 { format!("-r{opens}") } else { String::new() };
            let name = session::session_name(&session::program_name(), Some(&tag));
            opens += 1;
            self.st().has_data = false;
            let moves = self.moves();
            let link = self.inner.link.lock().unwrap().clone().expect("a link once opened");
            let opened = Session::open_on(&link, &name, &request);
            if self.moves() != moves {
                // Moved while asking: what was opened is on the old wall.
                if let Ok(sess) = opened {
                    sess.close(Some(apc("close").as_slice()));
                }
                continue;
            }
            let why = match opened {
                Ok(mut sess) => {
                    opening += 1;
                    let (_, dummy) = mpsc::channel();
                    let lines = std::mem::replace(&mut sess.lines, dummy);
                    let writer = sess.writer();
                    {
                        let mut slot = self.inner.session.lock().unwrap_or_else(|e| e.into_inner());
                        if self.stopping() || self.moves() != moves {
                            drop(slot);
                            sess.close(Some(apc("close").as_slice()));
                            if self.stopping() {
                                return;
                            }
                            continue;
                        }
                        *slot = Some(sess);
                    }
                    wait = 0.25;
                    // What the session says until it ends, or the layer
                    // moves: nothing the old wall says after that is about
                    // the new one.
                    loop {
                        match lines.recv_timeout(Duration::from_millis(100)) {
                            Ok(line) if self.moves() == moves => {
                                self.heard(&line, &writer, &name, opening, &mut lost)
                            }
                            Ok(_) => break,
                            Err(mpsc::RecvTimeoutError::Timeout) => {
                                if self.stopping() || self.moves() != moves {
                                    break;
                                }
                            }
                            Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        }
                    }
                    // The session has ended.
                    if self.stopping() {
                        return;
                    }
                    if self.moves() != moves {
                        // The retarget let the old session go; ask the new
                        // wall at once.
                        lost = true;
                        self.fire_when_lost();
                        continue;
                    }
                    self.st().cur = None;
                    let sess = self.inner.session.lock().unwrap_or_else(|e| e.into_inner()).take();
                    if self.stopping() {
                        return;
                    }
                    let state = sess.as_ref().map(Session::state).unwrap_or(State::Lost);
                    drop(sess);
                    match state {
                        State::Refused(why) => {
                            self.stay_out(&format!("the wall refused the layer: {why}"));
                            return;
                        }
                        State::Closed(why) if why == "displaced" => {
                            self.stay_out("another layer has taken its place");
                            return;
                        }
                        State::Closed(_) => "closed",
                        _ => "lost",
                    }
                }
                Err(OpenError::Refused(why)) => {
                    let why = why.rsplit("refused: ").next().unwrap_or(&why).to_string();
                    self.stay_out(&format!("the wall refused the layer: {why}"));
                    return;
                }
                Err(OpenError::NoWall(_)) => "absent",
                Err(OpenError::Dropped(_)) => "dropped",
                Err(OpenError::Io(_)) => "lost",
            };
            if self.stopping() {
                return;
            }
            self.settle();
            if !lost {
                lost = true;
                log(&format!("{}: layer {why}; opening it again when the wall is back", self.label()));
                self.fire_when_lost();
                // Asked again at once; only a wall still not there waits.
                continue;
            }
            self.pause(wait, moves);
            wait = (wait * 2.0).min(RETRY_MOST);
        }
    }

    fn fire_when_lost(&self) {
        let fns: Vec<Lost> = self.inner.when_lost.lock().unwrap().clone();
        for f in fns {
            if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f())).is_err() {
                log(&format!("{}: when_lost panicked", self.label()));
            }
        }
    }

    /// One line the wall said on the layer's `out`.
    fn heard(&self, line: &str, writer: &Writer, name: &str, opening: u64, lost: &mut bool) {
        let Some(said) = protocol::parse(line) else { return };
        match said.name.as_str() {
            "opened" => {
                let z = said.get("z").and_then(protocol::number);
                let has_data = said.args.iter().skip(1).any(|a| a == "data");
                let (align, frame_box, meta) = {
                    let mut c = self.cfg();
                    // A `start_at` is spent once an opening has it.
                    c.start_next = None;
                    (c.align.clone(), c.frame_box, c.meta.clone())
                };
                let _ = writer.write(&apc("report"));
                if align != "centre" {
                    let _ = writer.write(&apc(&format!("align {align}")));
                }
                if frame_box.is_some() {
                    let _ = writer.write(&apc(&box_command(frame_box)));
                }
                for (key, value) in &meta {
                    let _ = writer.write(&apc(&format!("meta set {key} {value}")));
                    if key == "instance" {
                        let _ = writer.write(&apc("meta hide instance"));
                    }
                }
                {
                    let mut s = self.st();
                    s.z = z;
                    s.has_data = has_data;
                    s.cur = Some(Current { writer: writer.clone(), name: name.to_string(), opening });
                }
                if std::mem::take(lost) {
                    log(&format!("{}: layer open again", self.label()));
                }
                self.settle();
            }
            "moved" => {
                // A layer beneath went, or one came in under it: the stack
                // moved it, and its slot is now this.
                if let Some(z) = said.get("z").and_then(protocol::number) {
                    self.st().z = Some(z);
                }
            }
            "repaint" => self.st().repaint = true,
            "error" if said.args.first().map(String::as_str) == Some("frame") => {
                let mut s = self.st();
                if s.errors.len() >= ERRORS_KEPT {
                    s.errors.pop_front();
                }
                s.errors.push_back(line.to_string());
            }
            "area" | "screen" | "caps" | "watching" => {
                self.st().said.insert(said.name.clone(), said);
            }
            _ => {}
        }
    }

    /// Stop for good: the wall is somebody else's, or will not have us.
    fn stay_out(&self, why: &str) {
        log(&format!("{}: {why}; stopping", self.label()));
        {
            let mut s = self.st();
            s.stopped = Some(why.to_string());
            s.cur = None;
        }
        if let Some(s) = self.inner.session.lock().unwrap_or_else(|e| e.into_inner()).take() {
            s.close(None);
        }
        self.settle();
    }

    /// Move to the wall `target` names, at the place first asked for, with
    /// the same meta and instance, and ask the sender for a frame that
    /// stands on its own (`repaint_asked`). The old wall is told `close`
    /// and is free at once. A stream on `data` is ended, not carried: its
    /// start is a time on the old wall's clock, and a sender playing one is
    /// not one a controller moves. A controller moves a sender this way
    /// through `moving`; a sender may call it itself.
    pub fn retarget(&self, target: &str) -> Result<(), String> {
        if let Some(why) = self.stopped() {
            return Err(format!("this layer has left {} for good: {why}", self.label()));
        }
        if self.stopping() {
            return Ok(());
        }
        let to = Target::parse(target)?;
        let was = self.inner.target.lock().unwrap().clone();
        if to == was {
            return Ok(());
        }
        let pipe = self.inner.data.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(p) = pipe {
            p.end(Duration::ZERO);
        }
        let old = {
            let mut slot = self.inner.session.lock().unwrap_or_else(|e| e.into_inner());
            let old = slot.take();
            let user = self.cfg().user.clone();
            *self.inner.link.lock().unwrap() = Some(Arc::new(Link::new(&to, user.as_deref(), None)));
            *self.inner.target.lock().unwrap() = to.clone();
            *self.inner.label.lock().unwrap() = to.to_string();
            let mut s = self.st();
            s.said.clear();
            s.z = None;
            s.cur = None;
            s.has_data = false;
            s.repaint = true;
            self.inner.moves.fetch_add(1, Ordering::SeqCst);
            old
        };
        log(&format!("moving from {was} to {to}"));
        if let Some(sess) = old {
            sess.close(Some(apc("close").as_slice()));
        }
        Ok(())
    }

    // ------------------------------------------------------ drawing

    /// Whether the wall holds the layer for us at this moment.
    pub fn open_now(&self) -> bool {
        self.st().cur.as_ref().is_some_and(|c| c.writer.open())
    }

    /// Show one image, QOI, PNG or JPEG, its top left at `at` in the
    /// glass's pixels, scaled to `size` (w, h) where given, held as `hold`
    /// says. Ok(false) when it was dropped: no layer open, a pipe still
    /// busy with the frame before, or a codec the wall does not draw
    /// (`takes`). Err where `data` is not a whole picture.
    pub fn frame(&self, data: &[u8], at: (i64, i64), size: Option<(u32, u32)>, hold: Hold) -> Result<bool, String> {
        let codec = protocol::image_of(data);
        let Some((w, h)) = codec.and_then(|c| protocol::image_size(c, data)) else {
            return Err("bitwall: a frame is a whole QOI, PNG or JPEG image".into());
        };
        let codec = codec.unwrap();
        let hold = match hold {
            Hold::Still => "still".to_string(),
            Hold::Ms(0) => return Err("bitwall: hold is a number of ms above 0, or still".into()),
            Hold::Ms(ms) => ms.to_string(),
        };
        let writer = {
            let mut s = self.st();
            let ok = s.cur.as_ref().filter(|c| c.writer.open() && c.writer.idle()).map(|c| c.writer.clone());
            match ok {
                Some(wr) if takes_in(s.said.get("caps"), codec) => wr,
                _ => {
                    s.frames_dropped += 1;
                    return Ok(false);
                }
            }
        };
        let mut out = frame_head(codec, (w, h), at, size, &hold, data.len());
        let head = out.len();
        out.extend_from_slice(data);
        let sent = writer.write_until(&out, Some(&self.inner.stop), None).is_ok();
        let mut s = self.st();
        if sent {
            s.frames_sent += 1;
            s.bytes_sent += (head + data.len()) as u64;
        } else {
            s.frames_dropped += 1;
        }
        Ok(sent)
    }

    /// Take the picture down; the layer stays.
    pub fn clear(&self) -> bool {
        self.command("clear")
    }

    /// A command on `in`, `ESC _ bitwall <text> ESC \`; false where no
    /// layer is open to take it.
    fn command(&self, text: &str) -> bool {
        let w = self.st().cur.as_ref().map(|c| c.writer.clone());
        w.is_some_and(|w| w.write_until(&apc(text), Some(&self.inner.stop), None).is_ok())
    }

    /// Where the stream on `data` sits on a glass of another shape, from
    /// its next frame: "top", "centre" or "bottom", said again whenever
    /// the layer opens.
    pub fn align(&self, place: &str) -> Result<(), String> {
        if !ALIGNS.contains(&place) {
            return Err(format!("bitwall: align is one of {}, not {place:?}", ALIGNS.join(", ")));
        }
        self.cfg().align = place.to_string();
        if self.open_now() {
            self.command(&format!("align {place}"));
        }
        Ok(())
    }

    /// Fit the stream on `data` to the box w by h at x, y in the glass's
    /// pixels, from its next frame, or to the whole glass again with
    /// None; said again whenever the layer opens. Its frames keep their
    /// shape within the box, `align` placing them, and the layer is
    /// transparent outside it. A wall whose `caps` do not say `box` fills
    /// the glass.
    pub fn set_box(&self, frame_box: Option<(i32, i32, u32, u32)>) -> Result<(), String> {
        if let Some((x, y, w, h)) = frame_box {
            if x.unsigned_abs() > 65535 || y.unsigned_abs() > 65535 || !(1..=4096).contains(&w)
                || !(1..=4096).contains(&h)
            {
                return Err(format!("bitwall: a box is at most 65535 off the glass and \
                                    1..4096 each way, not {:?}", (x, y, w, h)));
            }
        }
        self.cfg().frame_box = frame_box;
        if self.open_now() {
            self.command(&box_command(frame_box));
        }
        Ok(())
    }

    /// Say what this layer is, as a sender's meta says it: `name`,
    /// `description`, or a key of its own; said again whenever it opens.
    pub fn meta(&self, key: &str, value: &str) {
        let value = value.split_whitespace().collect::<Vec<_>>().join(" ");
        {
            let mut c = self.cfg();
            match c.meta.iter_mut().find(|(k, _)| k == key) {
                Some(kv) => kv.1 = value.clone(),
                None => c.meta.push((key.to_string(), value.clone())),
            }
        }
        if self.open_now() {
            self.command(&format!("meta set {key} {value}"));
        }
    }

    /// `name` and `description` in one.
    pub fn describe(&self, name: &str, description: &str) {
        self.meta("name", name);
        self.meta("description", description);
    }

    /// Call `f` when the wall goes and the layer will be asked for again
    /// once it is back. What the layer held is gone with it, and a stream
    /// on `data` starts again from its first frame: a sender that times
    /// its stream itself starts its count again, and may say when with
    /// `start_at`.
    pub fn when_lost<F: Fn() + Send + Sync + 'static>(&self, f: F) {
        self.inner.when_lost.lock().unwrap().push(Arc::new(f));
    }

    /// The Unix time in ms the stream's first frame is due at, on the
    /// next opening of a layer with `fps`: for a sender that keeps its
    /// own clock. Without it, a layer asked for again starts when its
    /// first frame is decoded.
    pub fn start_at(&self, ms: i64) -> Result<(), String> {
        let mut c = self.cfg();
        if c.fps.is_none() || c.codec.is_none() {
            return Err("bitwall: start_at is for a stream with fps".into());
        }
        c.start_next = Some(ms);
        Ok(())
    }

    // ------------------------------------------------------ data

    /// The path of the layer's `data` fifo on its host, for a caller who
    /// pipes a stream straight in; None while no session is open.
    pub fn data_path(&self) -> Option<String> {
        let name = self.st().cur.as_ref().map(|c| c.name.clone())?;
        let link = self.inner.link.lock().unwrap().clone()?;
        link.session_dir(&name).ok().map(|d| format!("{d}/data"))
    }

    /// Put `chunk` whole on the layer's `data`, waiting for room as long
    /// as it takes, or `timeout`; how many bytes went, or None when there
    /// is no stream to write to (no `data`, the writer or the layer gone,
    /// or the layer closing).
    pub fn data_write(&self, chunk: &[u8], timeout: Option<Duration>) -> Option<usize> {
        let mut slot = self.inner.data.lock().unwrap_or_else(|e| e.into_inner());
        let (opening, name) = {
            let s = self.st();
            match s.cur.as_ref() {
                Some(c) if s.has_data && c.writer.open() => (c.opening, c.name.clone()),
                _ => return None,
            }
        };
        if slot.as_ref().is_some_and(|p| p.opening != opening) {
            // The writer of a session the wall has lost: what it was handed
            // would be lost, and a stream that has lost its first frame,
            // the one with its headers, decodes nothing until its next
            // keyframe and has every frame counted that many late on a
            // timed layer.
            slot.take().unwrap().kill();
        }
        if slot.as_mut().is_none_or(|p| !p.alive()) {
            if let Some(old) = slot.take() {
                old.kill();
            }
            let link = self.inner.link.lock().unwrap().clone()?;
            let path = format!("{}/data", link.session_dir(&name).ok()?);
            *slot = Some(DataPipe::open(&link, &path, opening).ok()?);
        }
        let sent = slot.as_ref().unwrap().write(chunk, &self.inner.stop, timeout)?;
        self.st().bytes_sent += sent as u64;
        Some(sent)
    }

    /// End the stream on `data`: its writer closes, the wall plays out
    /// what it has and clears the layer, and the layer stays for the next.
    /// Waits up to `wait` for the writer to be done.
    pub fn data_end(&self, wait: Duration) {
        let pipe = self.inner.data.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(p) = pipe {
            p.end(wait);
        }
    }

    // ------------------------------------------------------ what it says

    /// The slot the wall gave, once it has said; None before.
    pub fn z(&self) -> Option<i64> {
        self.st().z
    }

    /// Whether the wall said `data` on `opened`: its fifo is there.
    pub fn has_data(&self) -> bool {
        self.st().has_data
    }

    /// Why the layer has left the wall for good, or None.
    pub fn stopped(&self) -> Option<String> {
        self.st().stopped.clone()
    }

    /// The last of `area`, `screen`, `caps` or `watching` the wall said.
    pub fn said(&self, name: &str) -> Option<Message> {
        self.st().said.get(name).cloned()
    }

    /// The glass, (width, height) in pixels, as the wall last said.
    pub fn pixels(&self) -> Option<(u32, u32)> {
        let s = self.st();
        let (w, h) = s.said.get("screen")?.get("pixels")?.split_once('x')?;
        let digits = |t: &str| !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit());
        if digits(w) && digits(h) { Some((w.parse().ok()?, h.parse().ok()?)) } else { None }
    }

    /// How many are watching, as the wall last said; None before it has.
    pub fn watching(&self) -> Option<i64> {
        self.st().said.get("watching")?.args.first().and_then(|a| protocol::number(a))
    }

    /// Whether the wall has asked for a frame that stands on its own since
    /// this was last asked: its layer was taken down under it.
    pub fn repaint_asked(&self) -> bool {
        std::mem::take(&mut self.st().repaint)
    }

    /// Whether the wall draws a frame in `codec`, `qoi`, `png` or `jpeg`,
    /// as its `caps` says in `images=`. A wall that says no `images` takes
    /// QOI alone; one that has not said its caps yet is taken at its word
    /// for QOI and not for the rest.
    pub fn takes(&self, codec: &str) -> bool {
        takes_in(self.st().said.get("caps"), codec)
    }

    /// The wall's `error frame` lines, newest last, the last sixteen.
    pub fn errors(&self) -> Vec<String> {
        self.st().errors.iter().cloned().collect()
    }

    /// (frames sent, frames dropped, bytes sent on `in` and `data`).
    pub fn sent(&self) -> (u64, u64, u64) {
        let s = self.st();
        (s.frames_sent, s.frames_dropped, s.bytes_sent)
    }

    // ------------------------------------------------------ ending

    /// Leave the wall: the layer is cleared and the session ends.
    ///
    /// Stopping comes first: a `data_write` waiting on a wall too busy to
    /// read holds the data lock, and gives it up only once it sees the
    /// stop.
    pub fn close(&self) {
        self.inner.stop.store(true, Ordering::SeqCst);
        self.data_end(Duration::from_secs(1));
        self.st().cur = None;
        let sess = self.inner.session.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(s) = sess {
            s.close(Some(apc("close").as_slice()));
        }
        self.settle();
    }
}

impl Inner {
    fn label(&self) -> String {
        self.label.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

impl Retarget for Inner {
    fn retarget(self: Arc<Self>, target: &str) -> Result<(), String> {
        Layer { inner: self }.retarget(target)
    }

    fn label(&self) -> String {
        Inner::label(self)
    }
}

/// `codec` in the `images=` of `caps`, or QOI where it says none.
fn takes_in(caps: Option<&Message>, codec: &str) -> bool {
    match caps.and_then(|c| c.get("images")).filter(|i| !i.is_empty()) {
        Some(images) => images.split(',').any(|i| i == codec),
        None => codec == "qoi",
    }
}

/// The command before a frame's bytes, as Python's `Layer.frame` says it.
pub fn frame_head(codec: &str, (w, h): (u32, u32), at: (i64, i64), size: Option<(u32, u32)>, hold: &str, len: usize) -> Vec<u8> {
    let bx = size.map(|(bw, bh)| format!(" size={bw}x{bh}")).unwrap_or_default();
    apc(&format!("frame {codec} {w}x{h} at={},{}{bx} hold={hold} {len}", at.0, at.1))
}

/// A number as Python's `f"{x:g}"` writes it: six significant digits,
/// trailing zeros dropped, exponent form outside 1e-4 to 1e6.
pub fn fmt_g(x: f64) -> String {
    if x == 0.0 {
        return "0".into();
    }
    if !x.is_finite() {
        return if x.is_nan() { "nan".into() } else if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    // The exponent as rounded to six digits, so 999999.5 is 1e+06.
    let sci = format!("{:.5e}", x);
    let (mant, exp) = sci.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let trim = |s: &str| -> String {
        if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_string() } else { s.to_string() }
    };
    if !(-4..6).contains(&exp) {
        let sign = if exp < 0 { '-' } else { '+' };
        return format!("{}e{sign}{:02}", trim(mant), exp.abs());
    }
    trim(&format!("{:.*}", (5 - exp).max(0) as usize, x))
}

// ---------------------------------------------------------- the data pipe

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

/// A fifo here opened for blocking writes, trying for up to two seconds
/// while nobody reads it yet, as Python's `_local.open_write` does.
fn open_fifo(path: &str) -> io::Result<File> {
    let until = Instant::now() + Duration::from_secs(2);
    let f = loop {
        match OpenOptions::new().write(true).custom_flags(O_NONBLOCK).open(path) {
            Ok(f) => break f,
            Err(e) if e.raw_os_error() == Some(ENXIO) && Instant::now() < until => {
                thread::sleep(Duration::from_millis(20))
            }
            Err(e) => return Err(e),
        }
    };
    // SAFETY: fcntl on a descriptor this File owns, with plain int flags.
    unsafe {
        let fd = f.as_raw_fd();
        let flags = fcntl(fd, F_GETFL);
        if flags < 0 || fcntl(fd, F_SETFL, flags & !O_NONBLOCK) < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(f)
}

/// One write handed to the data pipe's thread.
struct Job {
    data: Vec<u8>,
    sent: AtomicUsize,
    given_up: AtomicBool,
    /// None while it is going; then whether it went whole.
    done: Mutex<Option<bool>>,
    cv: Condvar,
}

impl Job {
    fn finish(&self, ok: bool) {
        *self.done.lock().unwrap() = Some(ok);
        self.cv.notify_all();
    }
}

/// A layer's `data`: the fifo itself for a wall here, `cat` over ssh for
/// one there, written whole by a thread of its own, which owns the pipe
/// and closes it once it has been handed everything.
struct DataPipe {
    opening: u64,
    jobs: Option<mpsc::Sender<Arc<Job>>>,
    broken: Arc<AtomicBool>,
    child: Option<Child>,
    thread: Option<JoinHandle<()>>,
}

impl DataPipe {
    fn open(link: &Link, path: &str, opening: u64) -> io::Result<DataPipe> {
        let (pipe, child): (Box<dyn Write + Send>, Option<Child>) = if link.local() {
            (Box::new(open_fifo(path)?), None)
        } else {
            // Uncompressed: video is compressed already. In a process group
            // of its own, so a signal to the sender leaves it to finish.
            let argv = link.writer_argv(path, false);
            let mut child = Command::new(&argv[0])
                .args(&argv[1..])
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .process_group(0)
                .spawn()?;
            (Box::new(child.stdin.take().unwrap()), Some(child))
        };
        let (tx, rx) = mpsc::channel::<Arc<Job>>();
        let broken = Arc::new(AtomicBool::new(false));
        let b = broken.clone();
        let thread = thread::Builder::new().name("bitwall-data".into()).spawn(move || {
            let mut pipe = pipe;
            for job in rx {
                if b.load(Ordering::SeqCst) {
                    job.finish(false);
                    continue;
                }
                let mut ok = true;
                for piece in job.data.chunks(CHUNK) {
                    if job.given_up.load(Ordering::SeqCst) {
                        break;
                    }
                    if pipe.write_all(piece).and_then(|_| pipe.flush()).is_err() {
                        b.store(true, Ordering::SeqCst);
                        ok = false;
                        break;
                    }
                    job.sent.fetch_add(piece.len(), Ordering::SeqCst);
                }
                job.finish(ok);
            }
            drop(pipe);
        })?;
        Ok(DataPipe { opening, jobs: Some(tx), broken, child, thread: Some(thread) })
    }

    fn alive(&mut self) -> bool {
        !self.broken.load(Ordering::SeqCst)
            && self.child.as_mut().is_none_or(|c| matches!(c.try_wait(), Ok(None)))
    }

    /// Hand over `data` and wait until it is in the pipe, `stop` is set,
    /// or `timeout` passes: all of it, None, or as much as went.
    fn write(&self, data: &[u8], stop: &AtomicBool, timeout: Option<Duration>) -> Option<usize> {
        let job = Arc::new(Job {
            data: data.to_vec(),
            sent: AtomicUsize::new(0),
            given_up: AtomicBool::new(false),
            done: Mutex::new(None),
            cv: Condvar::new(),
        });
        self.jobs.as_ref()?.send(job.clone()).ok()?;
        let until = timeout.map(|t| Instant::now() + t);
        let mut g = job.done.lock().unwrap();
        loop {
            if let Some(ok) = *g {
                let sent = job.sent.load(Ordering::SeqCst);
                return if ok && sent > 0 { Some(sent) } else { None };
            }
            if stop.load(Ordering::SeqCst) {
                job.given_up.store(true, Ordering::SeqCst);
                return None;
            }
            if until.is_some_and(|u| Instant::now() >= u) {
                job.given_up.store(true, Ordering::SeqCst);
                let sent = job.sent.load(Ordering::SeqCst);
                return (sent > 0).then_some(sent);
            }
            g = job.cv.wait_timeout(g, Duration::from_millis(50)).unwrap().0;
        }
    }

    /// Close once what it was handed has gone, and wait up to `wait` for
    /// the writer to end; killed where it will not.
    fn end(mut self, wait: Duration) {
        self.jobs = None;
        let until = Instant::now() + wait;
        if let Some(mut child) = self.child.take() {
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if Instant::now() < until => thread::sleep(Duration::from_millis(20)),
                    _ => {
                        let _ = child.kill();
                        let _ = child.wait();
                        break;
                    }
                }
            }
        }
        if let Some(t) = self.thread.take() {
            while !t.is_finished() && Instant::now() < until {
                thread::sleep(Duration::from_millis(10));
            }
            if t.is_finished() {
                let _ = t.join();
            }
        }
    }

    /// Let go at once, as when the session it was for has gone.
    fn kill(mut self) {
        self.broken.store(true, Ordering::SeqCst);
        self.jobs = None;
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_are_python_s() {
        let l = Layer::new("localhost", Place::Above).unwrap();
        assert_eq!(format!("open n {}", l.request(0).unwrap()), "open n layer type=video z=above");
        let l = Layer::new("localhost", Place::Z(0))
            .unwrap()
            .take()
            .data("hevc")
            .fps(23.976)
            .start(1790000000000)
            .instance("mac-7f3a2c");
        assert!(l.check().is_ok());
        assert_eq!(
            format!("open n {}", l.request(0).unwrap()),
            "open n layer codec=hevc data=1 fps=23.976 instance=mac-7f3a2c start=1790000000000 take=1 type=video z=0"
        );
        // Asked again: the first start is not said again, a start_at is.
        assert!(!l.request(1).unwrap().contains("start="));
        l.start_at(5).unwrap();
        assert!(l.request(1).unwrap().contains("start=5"));
    }

    #[test]
    fn asked_again_a_layer_asks_for_its_slot() {
        let l = Layer::new("localhost", Place::Above).unwrap();
        l.st().z = Some(3);
        assert!(l.request(1).unwrap().ends_with("z=3"));
        let t = Layer::new("localhost", Place::Top).unwrap();
        t.st().z = Some(7);
        assert!(t.request(1).unwrap().ends_with("z=top"));
    }

    #[test]
    fn what_cannot_be_asked_is_refused() {
        assert!(Layer::new("localhost", Place::Above).unwrap().take().check().is_err());
        assert!(Layer::new("localhost", Place::Above).unwrap().data("vp9").check().is_err());
        assert!(Layer::new("localhost", Place::Above).unwrap().fps(24.0).check().is_err());
        assert!(Layer::new("localhost", Place::Above).unwrap().data("hevc").start(1).check().is_err());
        assert!(Place::parse("middle").is_err());
        assert_eq!(Place::parse("4"), Ok(Place::Z(4)));
    }

    #[test]
    fn a_frame_is_said_as_python_says_it() {
        assert_eq!(
            frame_head("qoi", (64, 32), (40, 900), None, "1000", 1234),
            b"\x1b_bitwall frame qoi 64x32 at=40,900 hold=1000 1234\x1b\\".to_vec()
        );
        assert_eq!(
            frame_head("jpeg", (640, 480), (0, 0), Some((320, 240)), "still", 9),
            b"\x1b_bitwall frame jpeg 640x480 at=0,0 size=320x240 hold=still 9\x1b\\".to_vec()
        );
    }

    #[test]
    fn numbers_as_python_writes_them() {
        for (x, want) in [
            (25.0, "25"),
            (23.976, "23.976"),
            (29.97, "29.97"),
            (0.5, "0.5"),
            (1e7, "1e+07"),
            (1234567.0, "1.23457e+06"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
        ] {
            assert_eq!(fmt_g(x), want);
        }
    }

    #[test]
    fn takes_reads_images() {
        assert!(takes_in(None, "qoi"));
        assert!(!takes_in(None, "jpeg"));
        let caps = protocol::parse("caps types=cells,video images=qoi,png,jpeg").unwrap();
        assert!(takes_in(Some(&caps), "jpeg"));
        let caps = protocol::parse("caps images=qoi").unwrap();
        assert!(!takes_in(Some(&caps), "png"));
    }
}
