//! The base: a sender's grid on a wall, its render loop and what the wall
//! says to it (`bitwall/__init__.py`, `Wall`).
//!
//! Three threads, as the Python library has them. The link thread knocks
//! at the door for the base, reads what the wall says, and knocks again
//! whenever the wall goes away, until the sender detaches or another
//! sender displaces it. The render loop asks the source for a frame at
//! most once a period and hands it to the writer, replacing any the
//! writer has not taken. The writer diffs it against what the wall last
//! got and writes it.
//!
//! Locks are taken in one order: the session (the write lock), then the
//! state; the source's lock never while holding the state's.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::frame::{self, Depth, Frame};
pub use crate::layer::Place;
use crate::moving::{self, Retarget};
use crate::protocol::{self, Message};
use crate::session::{OpenError, Session, Target};

/// The largest area a wall may say it has: a grid as large as a 5K wall.
pub const MAX_COLS: usize = 2000;
pub const MAX_ROWS: usize = 1000;
pub const MAX_CELLS: usize = 1_000_000;
pub const MAX_FONT: u32 = 400;
const MAX_WATCHING: i64 = 10_000;
const MAX_RATE: i64 = 240;
const META_KEY: &str = "abcdefghijklmnopqrstuvwxyz0123456789._-";
const META_KEY_MOST: usize = 32;
const META_VALUE: usize = 256;
const CAP_NAMES: [&str; 3] = ["touch", "keyboard", "buttons"];
/// A source with nothing left of its period is polled this often.
const POLL: Duration = Duration::from_millis(2);
/// Pass-through bytes held while there is no session to write them to.
const THROUGH_HELD: usize = 32 << 20;
/// How long `set_font_size` and `grid` wait for the wall to say it is there.
const SETTLE_WAIT: Duration = Duration::from_secs(8);
const APC: &str = "\x1b_";
const ST: &str = "\x1b\\";
const MARK: &str = "bitwall";

/// What a wall shows: asked for a frame `cols` by `rows`, the frame, or
/// None where nothing has changed since the last one it gave. A frame of
/// another size is clipped to the area, or filled out with blanks.
pub trait Source: Send {
    fn render(&mut self, cols: usize, rows: usize) -> Option<Frame>;
}

impl<F> Source for F
where
    F: FnMut(usize, usize) -> Option<Frame> + Send,
{
    fn render(&mut self, cols: usize, rows: usize) -> Option<Frame> {
        self(cols, rows)
    }
}

/// What a Wall's session is on its wall: the base, or a cells layer placed
/// among the layers above it. One engine draws both.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    Base,
    Cells(Place),
}

type Hook = Arc<dyn Fn() + Send + Sync>;

/// Which Wall of this process each is, so two never ask for one name.
static WALLS: Mutex<u32> = Mutex::new(0);

/// A frame on its way to the writer.
struct Offer {
    grid: Arc<Frame>,
    full: bool,
    epoch: u64,
    src: u64,
}

/// Everything the threads share, under one lock.
struct State {
    cols: usize,
    rows: usize,
    /// Which source is current: bumped by every `set_source`.
    src: u64,
    has_src: bool,
    /// The frame the wall holds, as far as a diff may assume.
    prev: Option<Arc<Frame>>,
    force: bool,
    epoch: u64,
    hold: u32,
    blanked: bool,
    watching: Option<i64>,
    min_frame: f64,
    asked_frame: f64,
    depth_want: Option<Depth>,
    said: HashMap<String, Message>,
    heard: VecDeque<Message>,
    wanted: Vec<(String, bool)>,
    meta: Vec<(String, String)>,
    hidden: Vec<String>,
    asked_size: Option<(String, String)>,
    talks: bool,
    pending: Vec<String>,
    displaced: bool,
    stopped: Option<String>,
    screen_late: bool,
    next: Option<Offer>,
    through: Vec<Vec<u8>>,
    bytes_sent: u64,
    frames_sent: u64,
    /// The place a cells layer was given, from `opened` and `moved`.
    z: Option<i64>,
    /// Bumped by every retarget, so the link knows a session it holds or
    /// is opening is on the wall it has left.
    moves: u64,
    /// The wall moved from, for the link to say so.
    moved_from: Option<String>,
    /// While the old wall's session is being let go of; the new one is
    /// opened after.
    closing: bool,
}

impl State {
    fn new(min_frame: f64) -> State {
        State {
            cols: 120,
            rows: 30,
            src: 0,
            has_src: false,
            prev: None,
            force: false,
            epoch: 0,
            hold: 0,
            blanked: false,
            watching: None,
            min_frame,
            asked_frame: min_frame,
            depth_want: None,
            said: HashMap::new(),
            heard: VecDeque::new(),
            wanted: Vec::new(),
            meta: Vec::new(),
            hidden: Vec::new(),
            asked_size: None,
            talks: false,
            pending: Vec::new(),
            displaced: false,
            stopped: None,
            screen_late: false,
            next: None,
            through: Vec::new(),
            bytes_sent: 0,
            frames_sent: 0,
            z: None,
            moves: 0,
            moved_from: None,
            closing: false,
        }
    }

    /// Nothing the wall holds of ours can be diffed against.
    fn invalidate(&mut self) {
        self.prev = None;
        self.force = true;
        self.epoch += 1;
    }

    /// Follow an area the wall has said.
    fn adopt(&mut self, area: Option<(usize, usize)>) {
        let Some((cols, rows)) = area else { return };
        if (cols, rows) == (self.cols, self.rows) {
            return;
        }
        self.cols = cols;
        self.rows = rows;
        self.invalidate();
    }

    /// (cols, rows, font) as the wall last said its area, the font as it
    /// said it or "?".
    fn report(&self) -> Option<(usize, usize, String)> {
        let said = self.said.get("area")?;
        let (cols, rows) = area_from(said)?;
        let font = said.args.get(1).map(String::as_str).unwrap_or("?");
        let ok = number(font).is_some_and(|n| (1..=MAX_FONT as i64).contains(&n));
        Some((cols, rows, if ok { font.to_string() } else { "?".to_string() }))
    }

    fn watching_said(&self) -> Option<i64> {
        self.said.get("watching").and_then(watching_of)
    }

    fn depth(&self) -> Depth {
        if let Some(d) = self.depth_want {
            return d;
        }
        self.said.get("screen").map(depth_of).unwrap_or(Depth::True)
    }

    fn stale(&self, f: &Offer) -> bool {
        self.epoch != f.epoch || self.hold > 0 || self.blanked || self.src != f.src
    }
}

pub(crate) struct Inner {
    /// The wall as asked for; changed only by a retarget.
    target: Mutex<Target>,
    kind: Kind,
    seq: u32,
    instance: Option<String>,
    resync: f64,
    state: Mutex<State>,
    /// Woken when the writer has something to take, or it is time to stop.
    ready: Condvar,
    /// The base as we hold it, None between one and the next. Held
    /// around every whole write, so a command never lands inside a frame.
    session: Mutex<Option<Session>>,
    source: Mutex<Option<Box<dyn Source>>>,
    stop: AtomicBool,
    broken: AtomicBool,
    threads: Mutex<Vec<JoinHandle<()>>>,
    when_lost: Mutex<Vec<Hook>>,
    before_leaving: Mutex<Vec<Hook>>,
}

/// A live connection to one wall: the base session on it. Cheap to clone;
/// every clone is the same Wall.
#[derive(Clone)]
pub struct Wall {
    pub(crate) inner: Arc<Inner>,
}

/// Open the base on `target` (`host`, `host:<wall>`), a frame at most
/// every 50 ms.
pub fn attach(target: &str) -> Result<Wall, String> {
    attach_with(target, Duration::from_millis(50))
}

/// Open the base, a frame at most every `min_frame`. A wall's own `rate`
/// can only slow it further.
pub fn attach_with(target: &str, min_frame: Duration) -> Result<Wall, String> {
    let target = Target::parse(target)?;
    let w = Wall::new(target, take_instance(), min_frame.as_secs_f64(), Kind::Base);
    w.start();
    Ok(w)
}

/// The instance id a controller started this sender under, taken from
/// `BITWALL_INSTANCE` by the first Wall or layer to ask: it goes on the
/// `open` and in the meta, off the glass. Taken, not read, so a process
/// this one starts is not this instance, and a second Wall attached on its
/// own is not either; a layer made from a Wall says the Wall's.
pub fn take_instance() -> Option<String> {
    static TAKEN: Mutex<()> = Mutex::new(());
    let _one = TAKEN.lock().unwrap_or_else(|e| e.into_inner());
    let instance = std::env::var("BITWALL_INSTANCE").ok();
    if instance.is_some() {
        // SAFETY: called while the sender is starting, before it has
        // threads of its own that read the environment.
        unsafe { std::env::remove_var("BITWALL_INSTANCE") };
    }
    instance.filter(|i| is_instance(i))
}

impl Wall {
    pub(crate) fn new(target: Target, instance: Option<String>, min_frame: f64, kind: Kind) -> Wall {
        let seq = {
            let mut n = WALLS.lock().unwrap();
            *n += 1;
            *n
        };
        Wall {
            inner: Arc::new(Inner {
                target: Mutex::new(target),
                kind,
                seq,
                instance,
                resync: 30.0,
                state: Mutex::new(State::new(min_frame)),
                ready: Condvar::new(),
                session: Mutex::new(None),
                source: Mutex::new(None),
                stop: AtomicBool::new(false),
                broken: AtomicBool::new(false),
                threads: Mutex::new(Vec::new()),
                when_lost: Mutex::new(Vec::new()),
                before_leaving: Mutex::new(Vec::new()),
            }),
        }
    }

    pub(crate) fn start(&self) {
        // One a controller started may be moved by it.
        if self.inner.instance.is_some() {
            let weak: Weak<dyn Retarget> = Arc::downgrade(&(self.inner.clone() as Arc<dyn Retarget>));
            moving::hold(weak, moving::record_from_env().as_deref());
        }
        let link = {
            let w = self.clone();
            thread::Builder::new()
                .name("bitwall-link".into())
                .spawn(move || w.link_loop())
                .expect("a thread")
        };
        drop(link); // it ends on its own once stopped; never joined
        let render = {
            let w = self.clone();
            thread::Builder::new()
                .name("bitwall-render".into())
                .spawn(move || w.render_loop())
                .expect("a thread")
        };
        let writer = {
            let w = self.clone();
            thread::Builder::new()
                .name("bitwall-writer".into())
                .spawn(move || w.write_loop())
                .expect("a thread")
        };
        *self.inner.threads.lock().unwrap() = vec![render, writer];
        self.say_instance();
    }

    fn st(&self) -> MutexGuard<'_, State> {
        self.inner.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn sess(&self) -> MutexGuard<'_, Option<Session>> {
        self.inner.session.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn stopping(&self) -> bool {
        self.inner.stop.load(Ordering::SeqCst)
    }

    // ------------------------------------------------------------- api

    /// The wall it is on, `host` or `host:<wall>`: as given, or as a
    /// move last named it.
    pub fn target(&self) -> Target {
        self.inner.target.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// The instance id a controller started this sender under, for a
    /// layer of the same sender to say too.
    pub fn instance(&self) -> Option<&str> {
        self.inner.instance.as_deref()
    }

    /// The area the wall says it has, in cells. Always ask; never assume.
    pub fn area(&self) -> (usize, usize) {
        let s = self.st();
        (s.cols, s.rows)
    }

    /// Choose what the wall shows, and repaint it in full. The old source
    /// is dropped once the render loop is not using it.
    pub fn set_source<S: Source + 'static>(&self, source: S) {
        let mut slot = self.inner.source.lock().unwrap_or_else(|e| e.into_inner());
        let old = slot.replace(Box::new(source));
        {
            let mut s = self.st();
            s.src += 1;
            s.has_src = true;
            s.invalidate();
        }
        drop(slot);
        drop(old);
    }

    /// Ask for a font size and wait, up to eight seconds, for the wall to
    /// say it is drawing at it. The area that comes back is the wall's.
    pub fn set_font_size(&self, size: u32) -> Result<(), String> {
        self.set_font_size_with(size, SETTLE_WAIT)
    }

    /// `set_font_size`, waiting at most `wait` (zero: not at all).
    pub fn set_font_size_with(&self, size: u32, wait: Duration) -> Result<(), String> {
        if !(1..=MAX_FONT).contains(&size) {
            return Err(format!("font size must be 1..{MAX_FONT}, not {size}"));
        }
        let font = size.to_string();
        self.st().asked_size = Some(("font".into(), font.clone()));
        self.say("font", &[&font], &[]);
        let deadline = Instant::now() + wait;
        while Instant::now() < deadline {
            let mut s = self.st();
            if let Some((c, r, f)) = s.report() {
                if f == font {
                    if (c, r) != (s.cols, s.rows) {
                        s.cols = c;
                        s.rows = r;
                        s.invalidate();
                    }
                    return Ok(());
                }
            }
            drop(s);
            thread::sleep(Duration::from_millis(150));
        }
        Ok(())
    }

    /// Ask for a number of cells, and return the area there is after
    /// waiting up to eight seconds for it.
    pub fn grid(&self, cols: usize, rows: usize) -> Result<(usize, usize), String> {
        self.grid_with(cols, rows, SETTLE_WAIT)
    }

    pub fn grid_with(&self, cols: usize, rows: usize, wait: Duration) -> Result<(usize, usize), String> {
        if !(1..=MAX_COLS).contains(&cols) || !(1..=MAX_ROWS).contains(&rows) {
            return Err("a grid is cols and rows, both positive".into());
        }
        let size = format!("{cols}x{rows}");
        self.st().asked_size = Some(("grid".into(), size.clone()));
        self.say("grid", &[&size], &[]);
        let deadline = Instant::now() + wait;
        while Instant::now() < deadline {
            if let Some((c, r, _)) = self.st().report() {
                if (c, r) == (cols, rows) {
                    break;
                }
            }
            thread::sleep(Duration::from_millis(150));
        }
        let mut s = self.st();
        if let Some((c, r, _)) = s.report() {
            if (c, r) != (s.cols, s.rows) {
                s.cols = c;
                s.rows = r;
                s.invalidate();
            }
        }
        Ok((s.cols, s.rows))
    }

    /// Settle a new area and a new source without painting in between:
    /// the render loop stands still while `f` runs, and one full repaint
    /// goes out when it returns.
    pub fn transition<R>(&self, f: impl FnOnce(&Wall) -> R) -> R {
        let _held = self.begin();
        f(self)
    }

    /// The start of a transition, ended when the guard is dropped.
    pub fn begin(&self) -> Transition {
        self.st().hold += 1;
        Transition { wall: self.clone() }
    }

    /// Say what this sender is: `name`, `description`, or a key of its
    /// own; None takes the key away. Said again whenever a session opens.
    pub fn meta(&self, key: &str, value: Option<&str>) -> Result<(), String> {
        let key = key.replace('_', ".");
        if !is_meta_key(&key) {
            return Err(format!("not a metadata key: {key:?}"));
        }
        match value {
            None => {
                {
                    let mut s = self.st();
                    s.meta.retain(|(k, _)| *k != key);
                    s.hidden.retain(|k| *k != key);
                }
                self.say_meta("clear", Some(&key), None);
            }
            Some(v) => {
                let text = plain(v);
                {
                    let mut s = self.st();
                    match s.meta.iter_mut().find(|(k, _)| *k == key) {
                        Some(kv) => kv.1 = text.clone(),
                        None => s.meta.push((key.clone(), text.clone())),
                    }
                }
                self.say_meta("set", Some(&key), Some(&text));
            }
        }
        Ok(())
    }

    /// `name` and `description` in one.
    pub fn describe(&self, name: &str, description: &str) -> Result<(), String> {
        self.meta("name", Some(name))?;
        self.meta("description", Some(description))
    }

    /// Keep a key off the glass (`hide`), or put it back.
    pub fn meta_hide(&self, key: &str, hide: bool) {
        {
            let mut s = self.st();
            if hide {
                if !s.hidden.iter().any(|k| k == key) {
                    s.hidden.push(key.to_string());
                }
            } else {
                s.hidden.retain(|k| k != key);
            }
        }
        self.say_meta(if hide { "hide" } else { "show" }, Some(key), None);
    }

    /// How many are looking, or None where the wall does not count.
    pub fn watching(&self) -> Option<i64> {
        self.st().watching_said()
    }

    /// The colour frames are sent in: the screen's, unless `use_depth`.
    pub fn depth(&self) -> Depth {
        self.st().depth()
    }

    /// Send in this colour whatever the screen says, and repaint.
    pub fn use_depth(&self, depth: Depth) {
        let mut s = self.st();
        s.depth_want = Some(depth);
        s.prev = None;
        s.force = true;
    }

    /// The font size the wall says its area is drawn at.
    pub fn font_size(&self) -> Option<u32> {
        let s = self.st();
        let said = s.said.get("area")?;
        number(said.args.get(1)?)
            .filter(|n| (1..=1000).contains(n))
            .map(|n| n as u32)
    }

    /// The last thing the wall said of a kind (`area`, `caps`, `screen`,
    /// `accepts`, `watching`).
    pub fn said(&self, name: &str) -> Option<Message> {
        self.st().said.get(name).cloned()
    }

    /// What the wall says a viewer may give it: `touch` and `keyboard` as
    /// 1 or 0, `buttons` as how many.
    pub fn capabilities(&self) -> HashMap<String, i64> {
        let s = self.st();
        let mut out = HashMap::new();
        let Some(told) = s.said.get("accepts") else { return out };
        for a in &told.args {
            if CAP_NAMES.contains(&a.as_str()) {
                out.insert(a.clone(), 1);
            }
        }
        for name in CAP_NAMES {
            if let Some(n) = opt_int(told, name, 0, 64) {
                out.insert(name.to_string(), if name == "buttons" { n } else { (n != 0) as i64 });
            }
        }
        out
    }

    /// Whether the wall's `caps` says `word`.
    pub fn offers(&self, word: &str) -> bool {
        let s = self.st();
        s.said.get("caps").is_some_and(|c| {
            c.args.iter().any(|a| a == word) || c.opts.iter().any(|(k, _)| k == word)
        })
    }

    /// Whether the screen draws every character of `text`. A wall that
    /// has not said is a yes; it is waited for once, two seconds.
    pub fn can(&self, text: &str) -> bool {
        let spans = self.glyphs();
        covers(&spans, text)
    }

    /// Code points the screen says it draws, as (low, high); empty where
    /// it has not said.
    pub fn glyphs(&self) -> Vec<(u32, u32)> {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            {
                let mut s = self.st();
                if let Some(said) = s.said.get("screen") {
                    return ranges(get_opt(said, "glyphs").unwrap_or(""));
                }
                if s.screen_late || Instant::now() >= deadline {
                    s.screen_late = true;
                    return Vec::new();
                }
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    /// Ask the wall to pass on inputs: `want(&[("touch", true)])`.
    pub fn want(&self, kinds: &[(&str, bool)]) {
        let vals: Vec<(String, String)> =
            kinds.iter().map(|(k, v)| (k.to_string(), (*v as i32).to_string())).collect();
        {
            let mut s = self.st();
            for (k, v) in kinds {
                if CAP_NAMES.contains(k) {
                    match s.wanted.iter_mut().find(|(n, _)| n == k) {
                        Some(kv) => kv.1 = *v,
                        None => s.wanted.push((k.to_string(), *v)),
                    }
                }
            }
        }
        let opts: Vec<(&str, &str)> = vals.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        self.say("want", &[], &opts);
    }

    /// The next press the wall reported of a kind this sender wants, once:
    /// `input key key=a`, `input tap cell=3,4 area=174,42`, `input button n=1`.
    pub fn input(&self) -> Option<Message> {
        let mut s = self.st();
        while let Some(said) = s.heard.pop_front() {
            let of = match said.args.first().map(String::as_str) {
                Some("tap") => "touch",
                Some("key") => "keyboard",
                Some("button") => "buttons",
                _ => continue,
            };
            if s.wanted.iter().any(|(k, v)| k == of && *v) {
                return Some(said);
            }
        }
        None
    }

    /// Black the wall and stop sending.
    pub fn blank(&self) {
        self.st().blanked = true;
        self.say("blank", &["1"], &[]);
    }

    /// Undo a blank this sender asked for, and repaint in full.
    pub fn unblank(&self) {
        let mine = {
            let mut s = self.st();
            let mine = s.blanked;
            s.blanked = false;
            s.prev = None;
            s.force = true;
            mine
        };
        if mine {
            self.say("blank", &["0"], &[]);
        }
    }

    /// Bytes for the wall's terminal, untouched (kitty graphics): in the
    /// order given, between frames, never dropped.
    pub fn passthrough(&self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        self.st().through.push(data.to_vec());
        self.inner.ready.notify_all();
    }

    /// Ask the wall to say everything it knows.
    pub fn ask(&self) {
        self.say("report", &[], &[]);
    }

    /// Whether the wall has opened our session.
    pub fn talking(&self) -> bool {
        self.st().talks
    }

    /// Why this sender has left the wall for good, or None while it is on
    /// it or waiting to be: displaced by another, or refused the base.
    pub fn stopped(&self) -> Option<String> {
        self.st().stopped.clone()
    }

    /// The frame period now: what was asked, or the screen's, if slower.
    pub fn min_frame(&self) -> Duration {
        Duration::from_secs_f64(self.st().min_frame)
    }

    /// Bytes and frames written, for measuring.
    pub fn sent(&self) -> (u64, u64) {
        let s = self.st();
        (s.bytes_sent, s.frames_sent)
    }

    /// Leave the wall: stop drawing, drop the source, and say `detach` as
    /// the last thing on the session, so the wall is ready for the next
    /// sender at once.
    pub fn detach(&self) {
        self.call(&self.inner.before_leaving);
        let end = Instant::now() + Duration::from_millis(300);
        while !self.st().through.is_empty() && Instant::now() < end {
            thread::sleep(Duration::from_millis(10));
        }
        self.inner.stop.store(true, Ordering::SeqCst);
        {
            let _s = self.st();
            self.inner.ready.notify_all();
        }
        let threads: Vec<_> = self.inner.threads.lock().unwrap().drain(..).collect();
        let end = Instant::now() + Duration::from_millis(300);
        for t in threads {
            while !t.is_finished() && Instant::now() < end {
                thread::sleep(Duration::from_millis(5));
            }
            if t.is_finished() {
                let _ = t.join();
            }
        }
        drop(self.inner.source.lock().unwrap_or_else(|e| e.into_inner()).take());
        let sess = self.sess().take();
        if let Some(sess) = sess {
            sess.close(Some(apc("detach").as_bytes()));
        }
    }

    /// Move to the wall `target` names, keeping everything this sender
    /// holds: its source, meta, instance, font or grid, wants and blank.
    ///
    /// What `before_leaving` draws off is taken off the old wall, which is
    /// told `detach` and is free at once; the new wall is attached to as a
    /// wall that came back is, told who we are and what we asked for, and
    /// painted whole. Everything the old wall said of itself -- its area,
    /// screen, caps, who is watching -- is forgotten, since the new one
    /// says its own. A cells layer asks for its place there afresh. A
    /// controller moves a sender this way through `moving`; a sender may
    /// call it itself.
    pub fn retarget(&self, target: &str) -> Result<(), String> {
        if let Some(why) = self.stopped() {
            return Err(format!("this sender has left {} for good: {why}", target_label(&self.target())));
        }
        if self.stopping() {
            return Ok(());
        }
        let to = Target::parse(target)?;
        let was = self.target();
        if to == was {
            return Ok(());
        }
        self.call(&self.inner.before_leaving);
        let end = Instant::now() + Duration::from_millis(300);
        while !self.st().through.is_empty() && Instant::now() < end {
            thread::sleep(Duration::from_millis(10));
        }
        let old = {
            let mut slot = self.sess();
            let old = slot.take();
            *self.inner.target.lock().unwrap_or_else(|e| e.into_inner()) = to.clone();
            let mut s = self.st();
            s.said.clear();
            s.screen_late = false;
            s.displaced = false;
            s.min_frame = s.asked_frame;
            s.talks = false;
            s.z = None;
            s.moves += 1;
            s.moved_from = Some(target_label(&was));
            s.closing = true;
            s.invalidate();
            old
        };
        if let Some(sess) = old {
            sess.close(Some(apc("detach").as_bytes()));
        }
        log(&format!("moving from {} to {}", target_label(&was), target_label(&to)));
        self.st().closing = false;
        Ok(())
    }

    /// Call `f` when the wall goes and this sender will attach again once
    /// it is back, or is moved: the wall it comes back to may hold nothing
    /// sent beside the cells, so pictures are sent again. Called before
    /// the new session is asked for.
    pub fn when_lost(&self, f: impl Fn() + Send + Sync + 'static) {
        self.inner.when_lost.lock().unwrap_or_else(|e| e.into_inner()).push(Arc::new(f));
    }

    /// Call `f` as `detach` or a move starts, while the session can still
    /// carry what it sends: pictures taken off a terminal that would keep
    /// them after the sender goes.
    pub fn before_leaving(&self, f: impl Fn() + Send + Sync + 'static) {
        self.inner.before_leaving.lock().unwrap_or_else(|e| e.into_inner()).push(Arc::new(f));
    }

    fn call(&self, hooks: &Mutex<Vec<Hook>>) {
        let hooks: Vec<Hook> = hooks.lock().unwrap_or_else(|e| e.into_inner()).clone();
        for f in hooks {
            f();
        }
    }

    /// The frame period this sender asked for, in seconds.
    pub(crate) fn asked_frame(&self) -> f64 {
        self.st().asked_frame
    }

    /// The place a cells layer was given.
    pub(crate) fn z_given(&self) -> Option<i64> {
        self.st().z
    }

    /// A cells layer gives up a font or grid of its own and follows the
    /// base's grid again.
    pub(crate) fn follow_base(&self) {
        self.st().asked_size = Some(("font".into(), "base".into()));
        self.say("font", &["base"], &[]);
    }

    /// The `open` request after the name: the base, taken from whoever
    /// holds it, or a cells layer at its place. Asked again, a layer placed
    /// from below asks for the slot it had, so the stack comes back in its
    /// order; one placed `top`, the top.
    fn request(&self) -> Result<String, String> {
        let instance = self.inner.instance.as_deref();
        match &self.inner.kind {
            Kind::Base => base_request(instance),
            Kind::Cells(place) => {
                let z = self.st().z;
                let z = match z {
                    Some(z) if z > 0 && *place != Place::Top => z.to_string(),
                    _ => place.word(),
                };
                let mut opts = vec![("type", "cells"), ("z", z.as_str())];
                if let Some(i) = instance {
                    opts.insert(0, ("instance", i));
                }
                protocol::render("layer", &[], &opts)
            }
        }
    }

    // ------------------------------------------------------- speaking

    /// A request, rendered; nothing where it cannot be said in words.
    fn say(&self, name: &str, args: &[&str], opts: &[(&str, &str)]) -> bool {
        match protocol::render(name, args, opts) {
            Ok(text) => self.send(text),
            Err(_) => false,
        }
    }

    /// A request, or held until the wall has opened the session.
    fn send(&self, text: String) -> bool {
        {
            let mut s = self.st();
            if !s.talks {
                s.pending.push(text);
                return true;
            }
        }
        self.write_command(&text)
    }

    fn write_command(&self, text: &str) -> bool {
        let mut sess = self.sess();
        match sess.as_mut() {
            Some(s) => s.write(apc(text).as_bytes()).is_ok(),
            None => true,
        }
    }

    /// One metadata statement, built rather than rendered: its value is
    /// the rest of the line, spaces and all.
    fn say_meta(&self, verb: &str, key: Option<&str>, value: Option<&str>) -> bool {
        self.send(meta_line(verb, key, value))
    }

    fn say_instance(&self) {
        if let Some(i) = self.inner.instance.clone() {
            let _ = self.meta("instance", Some(&i));
            self.meta_hide("instance", true);
        }
    }

    /// The next session's name: `<program>-<pid>`, then which Wall of
    /// this process it is when not the first, and `r<k>` for the k-th
    /// time the base is asked for again.
    fn name(&self, opens: u32) -> String {
        let mut parts = Vec::new();
        if self.inner.seq > 1 {
            parts.push(self.inner.seq.to_string());
        }
        if opens > 0 {
            parts.push(format!("r{opens}"));
        }
        let tag = parts.join("-");
        crate::session::session_name(&program_name(), if tag.is_empty() { None } else { Some(&tag) })
    }

    // --------------------------------------------------- the link

    /// Knock for the session, listen to it, and knock again whenever the
    /// wall goes or the sender is moved, until detached, displaced or
    /// refused.
    fn link_loop(&self) {
        let mut opens = 0u32;
        let mut wait = 0.25f64;
        let mut lost: Option<String> = None;
        loop {
            if self.stopping() {
                return;
            }
            // The old wall is let go of before the new one is asked.
            let end = Instant::now() + Duration::from_secs(5);
            while self.st().closing && Instant::now() < end && !self.stopping() {
                thread::sleep(Duration::from_millis(20));
            }
            let moves = {
                let mut s = self.st();
                s.invalidate();
                s.moves
            };
            let target = self.target();
            let label = target_label(&target);
            let name = self.name(opens);
            opens += 1;
            let request = match self.request() {
                Ok(r) => r,
                Err(e) => {
                    self.stay_out(&label, &format!("cannot ask for the session: {e}"));
                    return;
                }
            };
            let opened = Session::open(&target, &name, &request);
            if self.st().moves != moves {
                // Moved while asking: what was opened is on the old wall.
                if let Ok(sess) = opened {
                    sess.close(Some(apc("detach").as_bytes()));
                }
                continue;
            }
            let mut sess = match opened {
                Ok(s) => s,
                Err(OpenError::Refused(why)) => {
                    let what = if self.inner.kind == Kind::Base { "the base" } else { "the layer" };
                    self.stay_out(&label, &format!("the wall refused {what}: {why}"));
                    return;
                }
                Err(e) => {
                    if lost.is_none() {
                        let why = match e {
                            OpenError::NoWall(w) | OpenError::Dropped(w) => w,
                            OpenError::Io(e) => e.to_string(),
                            OpenError::Refused(_) => unreachable!(),
                        };
                        log(&format!("{label}: {why}; attaching again when it is back"));
                        lost = Some(why);
                    }
                    self.pause(wait, moves);
                    wait = (wait * 2.0).min(5.0);
                    continue;
                }
            };
            wait = 0.25;
            let (_, dummy) = mpsc::channel();
            let lines = std::mem::replace(&mut sess.lines, dummy);
            {
                let mut slot = self.sess();
                if self.stopping() || self.st().moves != moves {
                    drop(slot);
                    sess.close(Some(apc("detach").as_bytes()));
                    if self.stopping() {
                        return;
                    }
                    continue;
                }
                *slot = Some(sess);
                self.opened(&mut slot);
            }
            if lost.take().is_some() {
                log(&format!("{label}: attached again"));
            }
            // What the session says until it ends, or the sender moves:
            // nothing the old wall says after that is about the new one.
            loop {
                match lines.recv_timeout(Duration::from_millis(100)) {
                    Ok(line) => {
                        if self.st().moves != moves {
                            break;
                        }
                        if let Some(said) = protocol::parse(&line) {
                            self.told(said);
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if self.stopping() || self.st().moves != moves {
                            break;
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }
            if self.stopping() {
                return;
            }
            let moved = self.st().moves != moves;
            let end = Instant::now() + Duration::from_secs(5);
            while moved && self.st().closing && Instant::now() < end && !self.stopping() {
                thread::sleep(Duration::from_millis(20));
            }
            if !moved && self.st().displaced {
                self.stay_out(&label, "another sender has this wall");
                return;
            }
            let why = match self.st().moved_from.take() {
                Some(was) if moved => format!("moved from {was}"),
                _ => "its session ended".to_string(),
            };
            log(&format!("{}: {why}; attaching again when it is back", target_label(&self.target())));
            lost = Some(why);
            self.call(&self.inner.when_lost);
            if !moved {
                let mut slot = self.sess();
                if self.st().moves == moves {
                    if let Some(s) = slot.take() {
                        drop(slot);
                        s.close(None);
                    }
                }
            }
            let mut s = self.st();
            if !moved {
                s.talks = false;
            }
            s.said.remove("watching");
            s.watching = None;
        }
    }

    /// The session is ours: the report first, which connects it, then
    /// what was asked before it opened, then who we are and what we
    /// want, since a wall that has just opened a session knows nothing
    /// of us. All under the write lock, so nothing comes between.
    fn opened(&self, slot: &mut Option<Session>) {
        let (pending, meta, hidden, asked, wanted, blanked) = {
            let mut s = self.st();
            s.talks = true;
            s.watching = None;
            (
                std::mem::take(&mut s.pending),
                s.meta.clone(),
                s.hidden.clone(),
                s.asked_size.clone(),
                s.wanted.clone(),
                s.blanked,
            )
        };
        let mut texts = vec!["report".to_string()];
        texts.extend(pending);
        texts.extend(resay(&meta, &hidden, asked.as_ref(), &wanted, blanked));
        if let Some(sess) = slot.as_mut() {
            for t in texts {
                if sess.write(apc(&t).as_bytes()).is_err() {
                    break;
                }
            }
        }
    }

    /// Wait before knocking again, but not past a move.
    fn pause(&self, secs: f64, moves: u64) {
        let end = Instant::now() + Duration::from_secs_f64(secs);
        while Instant::now() < end && !self.stopping() && self.st().moves == moves {
            thread::sleep(Duration::from_millis(20));
        }
    }

    /// Stop for good: the wall is somebody else's, or will not have us.
    fn stay_out(&self, label: &str, why: &str) {
        log(&format!("{label}: {why}; stopping"));
        self.st().stopped = Some(why.to_string());
        if let Some(s) = self.sess().take() {
            s.close(None);
        }
        self.inner.broken.store(true, Ordering::SeqCst);
        let _s = self.st();
        self.inner.ready.notify_all();
    }

    /// One thing the wall said.
    fn told(&self, said: Message) {
        if matches!(self.inner.kind, Kind::Cells(_)) && matches!(said.name.as_str(), "opened" | "moved") {
            if let Some(z) = get_opt(&said, "z").and_then(number).filter(|z| (0..=64).contains(z)) {
                self.st().z = Some(z);
            }
            if said.name == "moved" {
                return;
            }
        }
        match said.name.as_str() {
            "ping" => {
                // A quiet sender and a gone one look alike to the wall
                // until this is answered.
                self.say("pong", &[], &[]);
            }
            "input" => {
                let mut s = self.st();
                if s.heard.len() >= 64 {
                    s.heard.pop_front();
                }
                s.heard.push_back(said);
            }
            _ => self.st().apply(said),
        }
    }

    // --------------------------------------------------- the loops

    /// Ask the source for frames and hand them to the writer.
    fn render_loop(&self) {
        let mut last_frame: Option<Instant> = None;
        let mut last_full: Option<Instant> = None;
        let mut built: Option<(Arc<Frame>, u64)> = None;
        while !self.stopping() && !self.inner.broken.load(Ordering::SeqCst) {
            let (src, idle, min_frame) = {
                let s = self.st();
                (s.src, !s.has_src || s.blanked || s.hold > 0 || s.watching == Some(0), s.min_frame)
            };
            let period = Duration::from_secs_f64(min_frame);
            if idle {
                thread::sleep(period);
                continue;
            }
            let left = last_frame.map(|t| (t + period).saturating_duration_since(Instant::now()));
            thread::sleep(match left {
                Some(l) if !l.is_zero() => l,
                _ => POLL,
            });
            let now = Instant::now();
            let (cols, rows, forced, epoch) = {
                let mut s = self.st();
                if s.src != src || s.blanked || s.hold > 0 {
                    continue;
                }
                let forced = s.force;
                s.force = false;
                (s.cols, s.rows, forced, s.epoch)
            };
            let prev = built.as_ref().filter(|(_, g)| *g == epoch).map(|(f, _)| f.clone());
            if !forced && last_frame.is_some_and(|t| now.duration_since(t) < period) {
                continue;
            }
            // Due a period after the last was due, unless that is a whole
            // period gone or more: then from now.
            last_frame = Some(match last_frame {
                Some(t) if t + period <= now && now < t + period * 2 => t + period,
                _ => now,
            });
            let got = {
                let mut slot = self.inner.source.lock().unwrap_or_else(|e| e.into_inner());
                if self.st().src != src {
                    continue;
                }
                match slot.as_mut() {
                    Some(source) => source.render(cols, rows),
                    None => continue,
                }
            };
            let full = forced
                || prev.is_none()
                || last_full.is_none_or(|t| now.duration_since(t).as_secs_f64() >= self.inner.resync);
            let grid = match got {
                None => match (&prev, full) {
                    (Some(p), true) => p.clone(),
                    _ => continue,
                },
                Some(f) => Arc::new(fit(f, cols, rows)),
            };
            if full || prev.as_deref() != Some(&*grid) {
                {
                    let mut s = self.st();
                    let full = full || s.next.as_ref().is_some_and(|n| n.full);
                    s.next = Some(Offer { grid: grid.clone(), full, epoch, src });
                    self.inner.ready.notify_all();
                }
                built = Some((grid, epoch));
                if full {
                    last_full = Some(now);
                }
            }
        }
    }

    /// Write the newest frame, diffed against what the wall last got.
    fn write_loop(&self) {
        while !self.stopping() {
            let (offer, through) = {
                let mut s = self.st();
                while s.next.is_none() && s.through.is_empty() && !self.stopping() {
                    s = self.inner.ready.wait(s).unwrap_or_else(|e| e.into_inner());
                }
                (s.next.take(), std::mem::take(&mut s.through))
            };
            if !through.is_empty() {
                let mut slot = self.sess();
                match slot.as_mut() {
                    Some(sess) => {
                        let data = through.concat();
                        if sess.write(&data).is_ok() {
                            let mut s = self.st();
                            s.bytes_sent += data.len() as u64;
                            s.frames_sent += 1;
                        }
                    }
                    None => {
                        // Kept for the next session, the newest only.
                        drop(slot);
                        let mut s = self.st();
                        let mut held = through;
                        held.append(&mut s.through);
                        while held.len() > 1 && held.iter().map(Vec::len).sum::<usize>() > THROUGH_HELD {
                            held.remove(0);
                        }
                        s.through = held;
                        drop(s);
                        if offer.is_none() {
                            thread::sleep(Duration::from_millis(100));
                            continue;
                        }
                    }
                }
            }
            let Some(f) = offer else { continue };
            let (prev, depth) = {
                let s = self.st();
                if s.stale(&f) {
                    continue;
                }
                (s.prev.clone(), s.depth())
            };
            let full = f.full || prev.is_none();
            let data = frame::serialise(&f.grid, prev.as_deref(), full, depth);
            if data.is_empty() {
                continue;
            }
            let mut slot = self.sess();
            let Some(sess) = slot.as_mut() else { continue };
            {
                // What it was built for has to be true still as it goes.
                let s = self.st();
                if s.stale(&f) || !same(&s.prev, &prev) {
                    continue;
                }
            }
            if sess.write(&data).is_err() {
                // Nobody reading: the link notices the session end.
                continue;
            }
            drop(slot);
            let mut s = self.st();
            s.bytes_sent += data.len() as u64;
            s.frames_sent += 1;
            if s.src == f.src && s.epoch == f.epoch && same(&s.prev, &prev) {
                s.prev = Some(f.grid);
            }
        }
    }
}

impl State {
    /// What the wall said, kept and acted on.
    fn apply(&mut self, said: Message) {
        match said.name.as_str() {
            "closed" => {
                if said.args.get(1).map(String::as_str) == Some("displaced") {
                    self.displaced = true;
                }
            }
            "screen" => {
                if let Some(rate) = opt_int(&said, "rate", 1, MAX_RATE) {
                    self.min_frame = self.asked_frame.max(1.0 / rate as f64);
                }
                self.said.insert("screen".into(), said);
            }
            "area" => {
                let area = area_from(&said);
                self.said.insert("area".into(), said);
                self.adopt(area);
            }
            "watching" => {
                self.said.insert("watching".into(), said);
                let was = self.watching;
                self.watching = self.watching_said();
                if was == Some(0) && self.watching != Some(0) {
                    self.invalidate();
                }
            }
            "caps" | "accepts" => {
                self.said.insert(said.name.clone(), said);
            }
            "repaint" => {
                self.prev = None;
                self.force = true;
            }
            _ => {}
        }
    }
}

impl Retarget for Inner {
    fn retarget(self: Arc<Self>, target: &str) -> Result<(), String> {
        Wall { inner: self }.retarget(target)
    }

    fn label(&self) -> String {
        target_label(&self.target.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

/// A transition under way; the render loop stands still until it drops,
/// and then repaints in full.
pub struct Transition {
    wall: Wall,
}

impl Drop for Transition {
    fn drop(&mut self) {
        let mut s = self.wall.st();
        s.hold -= 1;
        if s.hold == 0 {
            s.invalidate();
        }
    }
}

// ---------------------------------------------------------- signals

static STOP: OnceLock<Arc<AtomicBool>> = OnceLock::new();

#[cfg(unix)]
unsafe extern "C" {
    fn signal(signum: i32, handler: usize) -> usize;
}

#[cfg(unix)]
extern "C" fn on_signal(_: i32) {
    if let Some(stop) = STOP.get() {
        stop.store(true, Ordering::SeqCst);
    }
}

/// A flag set on SIGINT or SIGTERM, as a controller stops a sender: a
/// main loop waits on it, stops drawing, and calls `detach()` last.
pub fn stop_on_signals() -> Arc<AtomicBool> {
    let stop = STOP.get_or_init(|| Arc::new(AtomicBool::new(false))).clone();
    #[cfg(unix)]
    {
        const SIGINT: i32 = 2;
        const SIGTERM: i32 = 15;
        let handler = on_signal as extern "C" fn(i32) as usize;
        // SAFETY: the handler only stores to an atomic.
        unsafe {
            signal(SIGINT, handler);
            signal(SIGTERM, handler);
        }
    }
    stop
}

/// Wait until `stop` is set.
pub fn wait_for(stop: &AtomicBool) {
    while !stop.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(50));
    }
}

// ---------------------------------------------------------- helpers

fn apc(text: &str) -> String {
    format!("{APC}{MARK} {text}{ST}")
}

fn log(text: &str) {
    eprintln!("bitwall: {text}");
}

fn target_label(t: &Target) -> String {
    if t.wall.is_empty() || t.wall == "0" {
        t.host.clone()
    } else {
        format!("{}:{}", t.host, t.wall)
    }
}

/// The base as it is asked for: `layer [instance=..] take=1 type=cells z=0`,
/// rendered as Python renders it, its options sorted.
fn base_request(instance: Option<&str>) -> Result<String, String> {
    let mut opts = vec![("take", "1"), ("type", "cells"), ("z", "0")];
    if let Some(i) = instance {
        opts.insert(0, ("instance", i));
    }
    protocol::render("layer", &[], &opts)
}

fn meta_line(verb: &str, key: Option<&str>, value: Option<&str>) -> String {
    let mut text = format!("meta {verb}");
    if let Some(k) = key {
        text.push(' ');
        text.push_str(k);
    }
    if let Some(v) = value {
        text.push(' ');
        text.push_str(v);
    }
    text
}

/// What a newly opened session is told again, after what was pending.
fn resay(
    meta: &[(String, String)],
    hidden: &[String],
    asked: Option<&(String, String)>,
    wanted: &[(String, bool)],
    blanked: bool,
) -> Vec<String> {
    let mut out = Vec::new();
    for (k, v) in meta {
        out.push(meta_line("set", Some(k), Some(v)));
    }
    for k in hidden {
        out.push(meta_line("hide", Some(k), None));
    }
    if let Some((name, arg)) = asked {
        if let Ok(t) = protocol::render(name, &[arg], &[]) {
            out.push(t);
        }
    }
    if !wanted.is_empty() {
        let vals: Vec<(String, String)> =
            wanted.iter().map(|(k, v)| (k.clone(), (*v as i32).to_string())).collect();
        let opts: Vec<(&str, &str)> = vals.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        if let Ok(t) = protocol::render("want", &[], &opts) {
            out.push(t);
        }
    }
    if blanked {
        if let Ok(t) = protocol::render("blank", &["1"], &[]) {
            out.push(t);
        }
    }
    out
}

/// The program's name, as Python takes it: the script's stem, or its
/// directory's where that is `main`.
fn program_name() -> String {
    let path = std::env::args().next().unwrap_or_default();
    let p = std::path::Path::new(&path);
    let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
    if matches!(stem.as_str(), "" | "-" | "main") && !path.is_empty() {
        if let Some(dir) = std::fs::canonicalize(p).ok().and_then(|a| {
            a.parent().and_then(|d| d.file_name()).and_then(|n| n.to_str()).map(String::from)
        }) {
            return dir;
        }
    }
    stem
}

fn is_instance(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn is_meta_key(key: &str) -> bool {
    (1..=META_KEY_MOST).contains(&key.len()) && key.chars().all(|c| META_KEY.contains(c))
}

/// Text a command can carry: printable, one line.
fn plain(text: &str) -> String {
    let s: String = text
        .chars()
        .map(|c| if c.is_control() || (c.is_whitespace() && c != ' ') { ' ' } else { c })
        .collect();
    s.trim().chars().take(META_VALUE).collect()
}

/// A word as an integer, as `protocol.number` takes one: `-?[0-9]{1,19}`.
fn number(text: &str) -> Option<i64> {
    let digits = text.strip_prefix('-').unwrap_or(text);
    if digits.is_empty() || digits.len() > 19 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

fn get_opt<'a>(said: &'a Message, key: &str) -> Option<&'a str> {
    said.opts.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

fn opt_int(said: &Message, key: &str, low: i64, high: i64) -> Option<i64> {
    get_opt(said, key).and_then(number).filter(|n| (low..=high).contains(n))
}

/// (cols, rows) out of `area 174x42 14`, within the bounds.
fn area_from(said: &Message) -> Option<(usize, usize)> {
    let (c, r) = said.args.first()?.split_once('x')?;
    let cols = number(c).filter(|n| (1..=MAX_COLS as i64).contains(n))? as usize;
    let rows = number(r).filter(|n| (1..=MAX_ROWS as i64).contains(n))? as usize;
    if cols * rows > MAX_CELLS {
        return None;
    }
    Some((cols, rows))
}

/// `watching 1` and `watching n=1` alike.
fn watching_of(said: &Message) -> Option<i64> {
    let bare = said
        .args
        .first()
        .and_then(|a| number(a))
        .filter(|n| (0..=MAX_WATCHING).contains(n));
    opt_int(said, "n", 0, MAX_WATCHING).or(bare)
}

fn depth_of(said: &Message) -> Depth {
    let told = get_opt(said, "colour").or(said.args.first().map(String::as_str));
    Depth::from_name(told.unwrap_or("true"))
}

/// `U+2580-259F,U+2800` as ranges.
fn ranges(text: &str) -> Vec<(u32, u32)> {
    let mut out = Vec::new();
    for part in text.split(',').take(32) {
        let part = part.trim();
        let Some(hex) = part.strip_prefix("U+").or_else(|| part.strip_prefix("u+")) else {
            continue;
        };
        let (lo, hi) = match hex.split_once('-') {
            Some((a, b)) => (a, Some(b)),
            None => (hex, None),
        };
        let parse = |h: &str| {
            if (1..=6).contains(&h.len()) && h.bytes().all(|b| b.is_ascii_hexdigit()) {
                u32::from_str_radix(h, 16).ok()
            } else {
                None
            }
        };
        let Some(low) = parse(lo) else { continue };
        let high = match hi {
            Some(h) => match parse(h) {
                Some(v) => v,
                None => continue,
            },
            None => low,
        };
        if low > high || high > 0x10FFFF {
            continue;
        }
        out.push((low, high));
    }
    out
}

fn covers(spans: &[(u32, u32)], text: &str) -> bool {
    spans.is_empty()
        || text.chars().all(|c| spans.iter().any(|(l, h)| (*l..=*h).contains(&(c as u32))))
}

fn same(a: &Option<Arc<Frame>>, b: &Option<Arc<Frame>>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => Arc::ptr_eq(x, y),
        _ => false,
    }
}

/// A frame clipped or filled out to the area.
fn fit(f: Frame, cols: usize, rows: usize) -> Frame {
    if f.cols == cols && f.rows == rows {
        return f;
    }
    let n = cols * rows;
    let mut out = Frame {
        cols,
        rows,
        ch: vec![' ' as u32; n],
        fg: vec![frame::DEFAULT; n],
        bg: vec![frame::DEFAULT; n],
        attrs: vec![0; n],
    };
    for y in 0..rows.min(f.rows) {
        let w = cols.min(f.cols);
        let (src, dst) = (y * f.cols, y * cols);
        out.ch[dst..dst + w].copy_from_slice(&f.ch[src..src + w]);
        out.fg[dst..dst + w].copy_from_slice(&f.fg[src..src + w]);
        out.bg[dst..dst + w].copy_from_slice(&f.bg[src..src + w]);
        out.attrs[dst..dst + w].copy_from_slice(&f.attrs[src..src + w]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(name: &str, args: &[&str], opts: &[(&str, &str)]) -> Message {
        Message {
            name: name.into(),
            args: args.iter().map(|s| s.to_string()).collect(),
            opts: opts.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
        }
    }

    fn wall() -> Wall {
        Wall::new(Target { host: "localhost".into(), wall: "0".into() }, None, 0.05, Kind::Base)
    }

    #[test]
    fn area_bounds() {
        assert_eq!(area_from(&msg("area", &["174x42", "14"], &[])), Some((174, 42)));
        assert_eq!(area_from(&msg("area", &["2000x500"], &[])), Some((2000, 500)));
        assert_eq!(area_from(&msg("area", &["2001x10"], &[])), None);
        assert_eq!(area_from(&msg("area", &["10x1001"], &[])), None);
        assert_eq!(area_from(&msg("area", &["2000x1000"], &[])), None); // 2M cells
        assert_eq!(area_from(&msg("area", &["0x10"], &[])), None);
        assert_eq!(area_from(&msg("area", &["-3x10"], &[])), None);
        assert_eq!(area_from(&msg("area", &["10"], &[])), None);
        assert_eq!(area_from(&msg("area", &[], &[])), None);
    }

    #[test]
    fn numbers_as_python() {
        assert_eq!(number("42"), Some(42));
        assert_eq!(number("-1"), Some(-1));
        assert_eq!(number("+1"), None);
        assert_eq!(number("\u{b2}"), None);
        assert_eq!(number(""), None);
        assert_eq!(number("12345678901234567890"), None);
    }

    #[test]
    fn area_said_is_adopted_and_repaints() {
        let w = wall();
        let epoch = w.st().epoch;
        w.told(msg("area", &["80x24", "14"], &[]));
        assert_eq!(w.area(), (80, 24));
        let s = w.st();
        assert!(s.force && s.epoch > epoch && s.prev.is_none());
        assert_eq!(s.report(), Some((80, 24, "14".into())));
        drop(s);
        w.st().force = false;
        w.told(msg("area", &["80x24", "14"], &[]));
        assert!(!w.st().force, "the same area is not a change");
        w.told(msg("area", &["3000x24"], &[]));
        assert_eq!(w.area(), (80, 24));
    }

    #[test]
    fn watching_stops_and_resumes_in_full() {
        let w = wall();
        assert_eq!(w.watching(), None);
        w.told(msg("watching", &["0"], &[]));
        assert_eq!(w.watching(), Some(0));
        w.st().force = false;
        let epoch = w.st().epoch;
        w.told(msg("watching", &[], &[("n", "2")]));
        assert_eq!(w.watching(), Some(2));
        let s = w.st();
        assert!(s.force && s.epoch == epoch + 1);
    }

    #[test]
    fn screen_rate_only_slows() {
        let w = wall();
        w.told(msg("screen", &[], &[("rate", "10"), ("colour", "256")]));
        assert!((w.min_frame().as_secs_f64() - 0.1).abs() < 1e-9);
        assert_eq!(w.depth(), Depth::Xterm256);
        w.told(msg("screen", &[], &[("rate", "60")]));
        assert!((w.min_frame().as_secs_f64() - 0.05).abs() < 1e-9);
        assert_eq!(w.depth(), Depth::True);
    }

    #[test]
    fn repaint_and_displaced() {
        let w = wall();
        w.st().force = false;
        w.told(msg("repaint", &[], &[]));
        assert!(w.st().force);
        w.told(msg("closed", &["x-1", "displaced"], &[]));
        assert!(w.st().displaced);
    }

    #[test]
    fn transition_holds_then_repaints() {
        let w = wall();
        let epoch = w.st().epoch;
        w.transition(|w| {
            assert_eq!(w.st().hold, 1);
            let _inner = w.begin();
            assert_eq!(w.st().hold, 2);
        });
        let s = w.st();
        assert_eq!(s.hold, 0);
        assert_eq!(s.epoch, epoch + 1, "one repaint, when the outer ends");
    }

    #[test]
    fn meta_held_until_opened() {
        let w = wall();
        w.meta("name", Some("Galaga\tarcade")).unwrap();
        w.meta_hide("name", true);
        assert!(w.meta("Bad Key", Some("x")).is_err());
        let s = w.st();
        assert_eq!(s.pending, vec!["meta set name Galaga arcade", "meta hide name"]);
        assert_eq!(resay(&s.meta, &s.hidden, None, &[], false),
                   vec!["meta set name Galaga arcade", "meta hide name"]);
    }

    #[test]
    fn input_only_what_was_wanted() {
        let w = wall();
        w.told(msg("input", &["key"], &[("key", "a")]));
        w.st().wanted.push(("touch".into(), true));
        w.told(msg("input", &["tap"], &[("cell", "1,2")]));
        let got = w.input().unwrap();
        assert_eq!(got.args[0], "tap");
        assert!(w.input().is_none());
    }

    #[test]
    fn glyphs_and_caps() {
        let w = wall();
        w.told(msg("screen", &[], &[("glyphs", "U+20-7E,U+2580-259F")]));
        assert!(w.can("ab\u{2580}"));
        assert!(!w.can("\u{25e2}"));
        w.told(msg("caps", &["kitty"], &[("cells", "1")]));
        assert!(w.offers("kitty") && w.offers("cells") && !w.offers("stream"));
        w.told(msg("accepts", &["touch"], &[("buttons", "2")]));
        let c = w.capabilities();
        assert_eq!(c.get("touch"), Some(&1));
        assert_eq!(c.get("buttons"), Some(&2));
    }

    #[test]
    fn fit_clips_and_fills() {
        let f = Frame { cols: 2, rows: 1, ch: vec![65, 66], fg: vec![1, 2], bg: vec![3, 4], attrs: vec![0, 1] };
        let g = fit(f, 3, 2);
        assert_eq!(g.ch, vec![65, 66, 32, 32, 32, 32]);
        assert_eq!(g.fg[2], frame::DEFAULT);
        let h = fit(g, 1, 1);
        assert_eq!(h.ch, vec![65]);
    }

    fn layer(place: Place) -> Wall {
        Wall::new(Target { host: "localhost".into(), wall: "0".into() }, Some("mac-1".into()), 0.05,
                  Kind::Cells(place))
    }

    #[test]
    fn requests_as_python() {
        assert_eq!(wall().request().unwrap(), "layer take=1 type=cells z=0");
        let l = layer(Place::Above);
        assert_eq!(l.request().unwrap(), "layer instance=mac-1 type=cells z=above");
        // Asked again, it asks for the slot it was given, or moved to.
        l.told(msg("opened", &["band-1"], &[("z", "2")]));
        assert_eq!(l.z_given(), Some(2));
        assert_eq!(l.request().unwrap(), "layer instance=mac-1 type=cells z=2");
        l.told(msg("moved", &["band-1"], &[("z", "3")]));
        assert_eq!(l.request().unwrap(), "layer instance=mac-1 type=cells z=3");
        let t = layer(Place::Top);
        t.told(msg("opened", &["band-1"], &[("z", "5")]));
        assert_eq!(t.request().unwrap(), "layer instance=mac-1 type=cells z=top");
        assert_eq!(layer(Place::Z(4)).request().unwrap(), "layer instance=mac-1 type=cells z=4");
    }

    #[test]
    fn follow_base_is_said_again() {
        let l = layer(Place::Above);
        l.follow_base();
        let s = l.st();
        assert_eq!(s.pending, vec!["font base"]);
        assert_eq!(resay(&[], &[], s.asked_size.as_ref(), &[], false), vec!["font base"]);
    }

    #[test]
    fn retarget_forgets_the_old_wall() {
        let l = layer(Place::Above);
        l.told(msg("opened", &["band-1"], &[("z", "2")]));
        l.told(msg("area", &["80x24", "14"], &[]));
        l.told(msg("screen", &[], &[("rate", "10")]));
        l.st().displaced = true;
        l.meta("name", Some("Band")).unwrap();
        l.retarget("localhost").unwrap();
        assert_eq!(l.st().moves, 0, "the same wall is no move");
        l.retarget("localhost:1").unwrap();
        assert_eq!(l.target(), Target { host: "localhost".into(), wall: "1".into() });
        let s = l.st();
        assert_eq!(s.moves, 1);
        assert!(s.said.is_empty() && !s.displaced && !s.talks && s.force);
        assert_eq!(s.z, None, "a layer asks for its place afresh");
        assert!((s.min_frame - 0.05).abs() < 1e-9);
        assert_eq!(s.meta, vec![("name".to_string(), "Band".to_string())]);
        drop(s);
        assert_eq!(l.request().unwrap(), "layer instance=mac-1 type=cells z=above");
        l.st().stopped = Some("refused".into());
        assert!(l.retarget("localhost:2").is_err());
    }

    #[test]
    fn instance_ids() {
        assert!(is_instance("mac-7f3a2c"));
        assert!(!is_instance("-x"));
        assert!(!is_instance("Mac"));
        assert!(!is_instance(""));
    }
}
