//! A wall: its door, its sessions and every command, as
//! bitwall/receivers/core.py keeps a wall for the web receiver, which is
//! the reference for anything PROTOCOL.md leaves open. The glass is the
//! receiver's (`Glass`): it says the area a font or a grid comes to, and
//! whether it has pixels. Cells layers stack over the base on every glass;
//! a glass of pixels takes video layers too, each holding its latest
//! frame as it came, and is pictured and mirrored by a look at it. A glass
//! with none, a terminal, refuses `type=video` and says its glass is not
//! offered.

use crate::glass::{Ask, Glass};
use crate::grid::{Grid, Said};
use crate::pipe::{self, Piece, Wrote};
use crate::proto::{self, Message};
use crate::sys;
use crate::video::{self, Frame, Head};
use std::collections::VecDeque;
use std::time::Instant;

/// Lines that say a current value: merged into the latest when a session's
/// `out` backs up, never dropped.
const STATE: [&str; 7] = ["area", "watching", "accepts", "display", "power", "presence", "screen"];
/// The most a session's queue holds while a picture waits in it.
const QUEUE_LIMIT: usize = 8 << 20;
pub const LAYERS: usize = 8;
const CONTROLLERS: usize = 8;
/// With no base, how long the status page shows before the wall dozes.
const DOZE: f64 = 300.0;
pub const MEASURES: [&str; 12] = ["wall", "grid", "fps", "in", "out", "frame", "draw", "sender", "pong", "display", "cpu", "render"];
const POINTS: [&str; 9] = ["n", "ne", "e", "se", "s", "sw", "w", "nw", "c"];
const CAPS: &str = "area font grid meta blank want input presence repaint watching ping detach close screen picture mirror stats sessions follow measures clear";

/// PROTOCOL.md's clocks, in seconds, all scaled together by
/// BITWALL_CLOCK_SCALE as the receiver core's are.
pub struct Timings {
    quiet: f64,
    pong: f64,
    unwatched: f64,
    unwritten: f64,
    keep: f64,
}

impl Timings {
    pub fn new() -> Timings {
        let k: f64 = std::env::var("BITWALL_CLOCK_SCALE").ok().and_then(|v| v.parse().ok()).unwrap_or(1.0);
        Timings { quiet: 5.0 * k, pong: 2.0 * k, unwatched: 60.0 * k, unwritten: 10.0 * k, keep: 10.0 * k }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Role {
    Layer,
    Controller,
}

#[derive(Clone, Copy, PartialEq)]
enum St {
    Open,
    Refused,
    Closed,
}

#[derive(Clone, Copy, PartialEq)]
enum Follow {
    Off,
    Said,
    In,
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    Keep,
    State,
    Event,
    Blob,
}

struct Item {
    kind: Kind,
    key: String,
    data: Vec<u8>,
    at: usize,
}

type Looks = Vec<Vec<(u32, i64, i64, bool)>>;

struct Mirror {
    target: String,
    every: f64,
    due: Instant,
    was: Option<Looks>,
    /// The glass's: the most it may be, and the look last sent.
    size: Option<(u32, u32)>,
    sent: Option<Vec<u8>>,
}

/// What a receiver draws, bottom to top: the base's grid or a cells
/// layer's, or a video layer's frame, None while it is transparent.
pub enum Surface<'a> {
    Cells(&'a Grid),
    Video(Option<&'a Frame>),
}

/// One layer of the stack: its slot, its session's name and what it shows.
pub struct Layer<'a> {
    pub z: usize,
    pub name: &'a str,
    pub surface: Surface<'a>,
    /// The font its cells are drawn at: the wall's, or a cells layer's
    /// own where it asked one.
    pub font: u32,
}

pub struct Session {
    pub name: String,
    path: String,
    role: Option<Role>,
    pub z: Option<usize>,
    from_top: bool,
    instance: Option<String>,
    reader: Option<pipe::Reader>,
    out: Option<pipe::Out>,
    st: St,
    ended_at: Instant,
    opened_at: Instant,
    connected: bool,
    last_in: Instant,
    pinged_at: Option<Instant>,
    awaiting: bool,
    answered: bool,
    pings: u32,
    pong_ms: Option<f64>,
    /// key, value, when set, hidden
    meta: Vec<(String, String, Instant, bool)>,
    /// touch, keyboard, buttons
    wants: [bool; 3],
    follow: Follow,
    measures: f64,
    measure_due: Option<Instant>,
    measure_mark: (Instant, u64, u64, f64),
    mirrors: Vec<Mirror>,
    in_bytes: u64,
    out_bytes: u64,
    queue: VecDeque<Item>,
    queued: usize,
    dropped: u64,
    dropped_all: u64,
    frame_count: u64,
    rates_at: Instant,
    rates_mark: (u64, u64, u64),
    r_in: f64,
    r_out: f64,
    r_fps: f64,
    /// Its stream read: the base's grid and a cells layer's, at the wall's
    /// area; a controller's, no cells at all, for its commands.
    pub grid: Grid,
    /// A cells layer above the base: what it asked of a grid of its own,
    /// None to follow the base's, and the area it has from it.
    own: Option<Ask>,
    own_area: (usize, usize, u32),
    kitty_used: bool,
    /// A video layer: its stream is frames, not cells.
    pub video: bool,
    /// A video layer's bytes not yet read, the payload it waits for, and
    /// whether that is to be let go, and the line it came with.
    vbuf: Vec<u8>,
    vwant: usize,
    skip: bool,
    head: Option<Head>,
    /// The frame a video layer shows, None while it is transparent.
    pub frame: Option<Frame>,
}

impl Session {
    fn new(name: &str, path: String, now: Instant) -> Session {
        Session {
            name: name.to_string(),
            path,
            role: None,
            z: None,
            from_top: false,
            instance: None,
            reader: None,
            out: None,
            st: St::Refused,
            ended_at: now,
            opened_at: now,
            connected: false,
            last_in: now,
            pinged_at: None,
            awaiting: false,
            answered: false,
            pings: 0,
            pong_ms: None,
            meta: Vec::new(),
            wants: [false; 3],
            follow: Follow::Off,
            measures: 0.0,
            measure_due: None,
            measure_mark: (now, 0, 0, 0.0),
            mirrors: Vec::new(),
            in_bytes: 0,
            out_bytes: 0,
            queue: VecDeque::new(),
            queued: 0,
            dropped: 0,
            dropped_all: 0,
            frame_count: 0,
            rates_at: now,
            rates_mark: (0, 0, 0),
            r_in: -1.0,
            r_out: -1.0,
            r_fps: -1.0,
            grid: Grid::new(0, 0, false),
            own: None,
            own_area: (0, 0, 0),
            kitty_used: false,
            video: false,
            vbuf: Vec::new(),
            vwant: 0,
            skip: false,
            head: None,
            frame: None,
        }
    }

    fn open(&self) -> bool {
        self.st == St::Open
    }

    fn layer(&self) -> bool {
        self.role == Some(Role::Layer)
    }
}

/// What a picture or a mirror asked for, checked.
struct Capture {
    who: Option<usize>,
    every: Option<f64>,
    off: bool,
    size: Option<(u32, u32)>,
}

pub struct Stats {
    pub on: bool,
    pub at: String,
    pub size: i64,
    pub show: String,
}

pub struct Wall {
    pub name: String,
    pub dir: String,
    door: pipe::Door,
    t: Timings,
    pub sessions: Vec<Session>,
    pub cols: usize,
    pub rows: usize,
    pub font: u32,
    /// What the area was last asked to be, worked out again when the glass
    /// changes.
    request: Ask,
    /// The glass's pixels as last said, None for a glass with none.
    pixels: Option<(u32, u32)>,
    /// Frames put up on video layers, counted for the next frame's `seq`.
    frames_up: u64,
    present: bool,
    feeder: Option<String>,
    /// The presence this wall follows, where it was given one: said on
    /// its `presence` line, so a controller feeding that name finds it.
    presence: Option<String>,
    fed_at: Instant,
    blank: bool,
    /// Whether the base has painted: until then, and after it goes, the
    /// status page, and after DOZE the wall dozes.
    pub connected: bool,
    pub dozing: bool,
    last_data: Instant,
    pub last_seen: Option<String>,
    pub stats: Stats,
    kitty: bool,
    receiver: String,
    resets: VecDeque<Instant>,
    in_total: u64,
    out_total: u64,
    /// Kitty strings from the base for the terminal, and where.
    pub kitty_out: Vec<(Vec<u8>, usize, usize)>,
    /// Something for the glass to show: a frame closed, or the wall's own
    /// state changed.
    pub frame_closed: bool,
    pub changed: bool,
    frame_ms: Option<f64>,
    pub draw_ms: Option<f64>,
    overlay_mark: Option<(Instant, u64, u64, f64)>,
    pub overlay_lines: Vec<String>,
    overlay_due: Instant,
}

impl Wall {
    /// A wall on the door claimed for it, its area what `font` comes to on
    /// the glass; `receiver` is the line its report says of the program.
    pub fn new(name: &str, dir: String, door: pipe::Door, glass: &dyn Glass, font: u32, receiver: String) -> Wall {
        let now = Instant::now();
        let request = Ask::Font(font);
        let (cols, rows, font) = glass.area(request);
        Wall {
            name: name.to_string(),
            dir,
            door,
            t: Timings::new(),
            sessions: Vec::new(),
            cols,
            rows,
            font,
            request,
            pixels: glass.pixels(),
            frames_up: 0,
            present: true,
            feeder: None,
            presence: None,
            fed_at: now,
            blank: false,
            connected: false,
            dozing: false,
            last_data: now,
            last_seen: None,
            stats: Stats { on: false, at: "ne".into(), size: 14, show: "fps".into() },
            kitty: glass.kitty(),
            receiver,
            resets: VecDeque::new(),
            in_total: 0,
            out_total: 0,
            kitty_out: Vec::new(),
            frame_closed: false,
            changed: true,
            frame_ms: None,
            draw_ms: None,
            overlay_mark: None,
            overlay_lines: Vec::new(),
            overlay_due: now,
        }
    }

    // ------------------------------------------------------------ finding

    pub fn base(&self) -> Option<usize> {
        self.sessions.iter().position(|s| s.open() && s.layer() && s.z == Some(0))
    }

    fn above(&self, z: usize) -> Option<usize> {
        self.sessions.iter().position(|s| s.open() && s.layer() && s.z == Some(z))
    }

    fn by_name(&self, name: &str) -> Option<usize> {
        self.sessions.iter().position(|s| s.open() && s.name == name)
    }

    /// The cells layers above the base, bottom to top.
    pub fn layers(&self) -> Vec<usize> {
        (1..LAYERS).filter_map(|z| self.above(z)).filter(|&j| !self.sessions[j].video).collect()
    }

    /// Every layer there is to draw, bottom to top: the base's grid once it
    /// has painted, then each layer above it, a cells layer's grid or a
    /// video layer's frame. The wall's dark, its status page and its
    /// overlay are the receiver's to draw (`dark`, `connected`,
    /// `overlay_lines`).
    pub fn stack(&self) -> Vec<Layer<'_>> {
        let mut out = Vec::new();
        if let Some(b) = self.base().filter(|_| self.connected) {
            out.push(Layer { z: 0, name: &self.sessions[b].name, surface: Surface::Cells(&self.sessions[b].grid), font: self.font });
        }
        for z in 1..LAYERS {
            if let Some(j) = self.above(z) {
                let s = &self.sessions[j];
                let surface = if s.video { Surface::Video(s.frame.as_ref()) } else { Surface::Cells(&s.grid) };
                let font = if s.own.is_some() { s.own_area.2 } else { self.font };
                out.push(Layer { z, name: &s.name, surface, font });
            }
        }
        out
    }

    fn place(s: &Session) -> String {
        s.z.map(|z| z.to_string()).unwrap_or_else(|| "-".into())
    }

    fn kind_of(s: &Session) -> &'static str {
        if !s.layer() {
            "-"
        } else if s.video {
            "video"
        } else {
            "cells"
        }
    }

    // ------------------------------------------------------------ serving

    /// What has come since last asked: requests at the door, each
    /// session's bytes, and the clocks.
    pub fn serve(&mut self, now: Instant, g: &mut dyn Glass) {
        let door = self.door.take();
        if !door.is_empty() {
            for line in String::from_utf8_lossy(&door).split('\n') {
                self.request(line, now);
            }
        }
        for i in 0..self.sessions.len() {
            // A few pieces a turn each, so one busy writer starves nobody.
            for _ in 0..16 {
                let piece = match &self.sessions[i].reader {
                    Some(r) if self.sessions[i].open() => r.take(),
                    _ => None,
                };
                match piece {
                    None => break,
                    Some(Piece::Eof) => {
                        if self.sessions[i].connected {
                            self.end(i, "eof");
                        }
                        break;
                    }
                    Some(Piece::Data(d)) => self.read_in(i, &d, now, g),
                }
            }
        }
        self.tick(now, g);
    }

    fn request(&mut self, line: &str, now: Instant) {
        let words: Vec<&str> = line.split_whitespace().collect();
        if words.len() < 2 || words[0] != "open" || !proto::is_name(words[1]) || words[1] == "glass" {
            return;
        }
        let name = words[1];
        let role = words.get(2).copied();
        let mut opts: Vec<(&str, &str)> = Vec::new();
        for w in words.iter().skip(3) {
            match w.split_once('=') {
                Some((k, v)) if !k.is_empty() && !v.is_empty() => opts.push((k, v)),
                _ => return,
            }
        }
        // A name whose session is there, however the filesystem compares
        // names, or whose pipes are still going, is in use.
        if self.sessions.iter().any(|s| s.name.eq_ignore_ascii_case(name)) {
            return;
        }
        let path = format!("{}/s/{}", self.dir, name);
        let (reader, out) = match pipe::session(&path) {
            Ok(p) => p,
            Err(_) => return,
        };
        let mut s = Session::new(name, path, now);
        s.reader = Some(reader);
        s.out = Some(out);
        self.sessions.push(s);
        let i = self.sessions.len() - 1;
        self.say_kind(i, "hello v1 line", Some(Kind::Keep));
        if let Some(reason) = self.admit(i, role, &opts) {
            let s = &mut self.sessions[i];
            s.st = St::Refused;
            s.ended_at = now;
            if let Some(r) = s.reader.take() {
                r.close();
            }
            self.say(i, &format!("error open {name} {reason}"));
            return;
        }
        let s = &mut self.sessions[i];
        s.st = St::Open;
        if s.layer() && !s.video {
            s.grid = Grid::new(self.cols, self.rows, s.z != Some(0));
            s.own = None;
        }
        if s.layer() {
            self.changed = true;
        }
        let opened = if s.layer() { format!("opened {name} z={}", s.z.unwrap()) } else { format!("opened {name}") };
        self.say(i, &opened);
        let s = &self.sessions[i];
        let line = format!(
            "session opened {name} {} {} {}{}",
            if s.layer() { "layer" } else { "controller" },
            Wall::place(s),
            Wall::kind_of(s),
            s.instance.as_ref().map(|v| format!(" instance={v}")).unwrap_or_default()
        );
        self.tell_controllers(&line, Some(i), None, "");
    }

    /// Take the request, or the word that refuses it. The base is displaced
    /// where `take=1` asks; a layer above it is placed in one of two stacks
    /// with no gaps, and never taken from its holder.
    fn admit(&mut self, i: usize, role: Option<&str>, opts: &[(&str, &str)]) -> Option<&'static str> {
        match role {
            Some("controller") => {
                self.sessions[i].role = Some(Role::Controller);
                for (k, v) in opts {
                    if *k != "instance" || !proto::is_instance(v) {
                        return Some("no-such-key");
                    }
                    self.sessions[i].instance = Some(v.to_string());
                }
                let n = self.sessions.iter().filter(|s| s.open() && s.role == Some(Role::Controller)).count();
                if n >= CONTROLLERS {
                    return Some("full");
                }
                return None;
            }
            Some("layer") => {}
            _ => return Some("no-such-role"),
        }
        self.sessions[i].role = Some(Role::Layer);
        let (mut z, mut place, mut take) = (0usize, None::<&str>, false);
        for (k, v) in opts {
            match *k {
                "z" => {
                    if *v == "above" || *v == "top" {
                        place = Some(v);
                    } else if v.len() <= 4 && v.bytes().all(|b| b.is_ascii_digit()) {
                        z = v.parse().unwrap();
                        place = None;
                    } else {
                        return Some("no-such-key");
                    }
                }
                "type" => {
                    if *v == "video" && self.pixels.is_some() {
                        self.sessions[i].video = true;
                    } else if *v != "cells" {
                        return Some("no-such-type");
                    }
                }
                "take" => {
                    if *v != "1" {
                        return Some("no-such-key");
                    }
                    take = true;
                }
                "instance" => {
                    if !proto::is_instance(v) {
                        return Some("no-such-key");
                    }
                    self.sessions[i].instance = Some(v.to_string());
                }
                _ => return Some("no-such-key"),
            }
        }
        if z >= LAYERS {
            return Some("full");
        }
        if place.is_none() && z == 0 && self.sessions[i].video {
            // A video base is a film on `data`, which this wall does not take.
            return Some("no-such-type");
        }
        if place.is_some() || z > 0 {
            if take {
                return Some("no-such-key");
            }
            let held: Vec<(usize, bool)> = (1..LAYERS).filter_map(|k| self.above(k).map(|j| (k, self.sessions[j].from_top))).collect();
            let lo = held.iter().filter(|(_, top)| !top).count();
            let hi = held.iter().filter(|(_, top)| *top).count();
            if lo + hi >= LAYERS - 1 {
                return Some("full");
            }
            if place == Some("top") {
                z = LAYERS - 1 - hi;
                self.sessions[i].from_top = true;
            } else {
                if place == Some("above") || z > lo + 1 {
                    z = lo + 1;
                }
                let mut k = lo;
                while k >= z && k >= 1 {
                    self.move_layer(k, k + 1);
                    k -= 1;
                }
            }
            self.sessions[i].z = Some(z);
            return None;
        }
        self.sessions[i].z = Some(0);
        if let Some(holder) = self.base() {
            if holder != i {
                if !take {
                    self.sessions[i].z = None;
                    return Some("busy");
                }
                self.end(holder, "displaced");
            }
        }
        None
    }

    fn read_in(&mut self, i: usize, data: &[u8], now: Instant, g: &mut dyn Glass) {
        let s = &mut self.sessions[i];
        s.in_bytes += data.len() as u64;
        self.in_total += data.len() as u64;
        s.last_in = now;
        let first = !s.connected;
        s.connected = true;
        let is_base = s.z == Some(0) && s.layer();
        if first {
            self.say(i, "repaint");
            if is_base {
                self.base_connected();
            }
        }
        if is_base {
            self.last_data = now;
        }
        if self.sessions[i].video {
            return self.read_video(i, data, now, g);
        }
        let kitty = is_base && self.kitty;
        let mut said = Vec::new();
        let started = Instant::now();
        let s = &mut self.sessions[i];
        let before = s.grid.resets;
        s.grid.feed(data, kitty, &mut said);
        let frames = s.grid.resets - before;
        if s.layer() && frames > 0 {
            s.frame_count += frames as u64;
            self.frame_closed = true;
            if is_base {
                let ms = started.elapsed().as_secs_f64() * 1000.0 / frames as f64;
                self.frame_ms = Some(self.frame_ms.map(|v| v * 0.8 + ms * 0.2).unwrap_or(ms));
                for _ in 0..frames {
                    self.resets.push_back(now);
                }
            }
        }
        for m in said {
            if !self.sessions[i].open() {
                break;
            }
            match m {
                Said::Command(body) => {
                    if let Some(msg) = Message::parse(&body) {
                        self.command(i, msg, now, g);
                    }
                }
                Said::Kitty(bytes, x, y) => {
                    self.sessions[i].kitty_used = true;
                    self.kitty_out.push((bytes, x, y));
                }
            }
        }
    }

    /// The base's first byte: the wall has a sender, and the status page and
    /// any doze go.
    fn base_connected(&mut self) {
        let was = (self.display(), self.watching());
        self.connected = true;
        self.dozing = false;
        self.changed = true;
        self.dark_changed(was, None, "");
    }

    /// A command from a session, followed to the controllers that asked,
    /// but for a video frame's line, which is framing, as its bytes are.
    fn command(&mut self, i: usize, m: Message, now: Instant, g: &mut dyn Glass) {
        for j in 0..self.sessions.len() {
            if m.name == "frame" {
                break;
            }
            if j != i && self.sessions[j].open() && self.sessions[j].follow == Follow::In {
                let line = format!("< {} {}", self.sessions[i].name, m.raw);
                self.say(j, &line);
            }
        }
        self.obey(i, &m, now, g);
    }

    /// A video layer's bytes: commands as they come, each frame's payload
    /// gathered whole, and anything else let go.
    fn read_video(&mut self, i: usize, data: &[u8], now: Instant, g: &mut dyn Glass) {
        let mut b = std::mem::take(&mut self.sessions[i].vbuf);
        b.extend_from_slice(data);
        let mut p = 0;
        while p < b.len() && self.sessions[i].open() {
            let want = self.sessions[i].vwant;
            if want > 0 {
                let have = b.len() - p;
                if self.sessions[i].skip {
                    let k = have.min(want);
                    p += k;
                    self.sessions[i].vwant -= k;
                    continue;
                }
                if have < want {
                    break;
                }
                self.video_frame(i, &b[p..p + want], now);
                p += want;
                self.sessions[i].vwant = 0;
                continue;
            }
            let Some(esc) = find(&b[p..], b"\x1b_").map(|k| p + k) else {
                p = if b.ends_with(b"\x1b") { b.len() - 1 } else { b.len() };
                break;
            };
            let Some(end) = find(&b[esc + 2..], b"\x1b\\").map(|k| esc + 2 + k) else {
                if b.len() - esc > proto::MAX_MESSAGE + 2 {
                    p = esc + 2;
                    continue;
                }
                p = esc;
                break;
            };
            let body = String::from_utf8_lossy(&b[esc + 2..end]).into_owned();
            p = end + 2;
            if let Some(rest) = body.strip_prefix("bitwall ") {
                if let Some(m) = Message::parse(rest) {
                    self.command(i, m, now, g);
                }
            }
        }
        b.drain(..p);
        self.sessions[i].vbuf = b;
    }

    /// One frame's payload, whole: put up if its header and end are what
    /// its line said, or refused with the line's id. A mirror of the layer
    /// is passed its bytes as they came.
    fn video_frame(&mut self, i: usize, data: &[u8], now: Instant) {
        let Some(h) = self.sessions[i].head.take() else { return };
        if video::image_size(h.codec, data) != Some((h.w, h.h)) {
            self.say(i, &format!("error frame {}: does not decode{}", h.codec.name(), h.tag));
            return;
        }
        self.frames_up += 1;
        let f = Frame { codec: h.codec, w: h.w, h: h.h, x: h.x, y: h.y, size: h.size, hold: h.hold, data: data.to_vec(), came: now, seq: self.frames_up };
        let header = format!(
            "mirror {} {} hold={} {}",
            self.sessions[i].name,
            f.words(),
            f.hold.map(|v| v.to_string()).unwrap_or_else(|| "still".into()),
            data.len()
        );
        let s = &mut self.sessions[i];
        s.frame_count += 1;
        s.frame = Some(f);
        self.resets.push_back(now);
        self.frame_closed = true;
        self.changed = true;
        let name = self.sessions[i].name.clone();
        for c in 0..self.sessions.len() {
            let x = &self.sessions[c];
            let Some(k) = x.mirrors.iter().position(|m| m.target == name) else { continue };
            if !x.open() || now < x.mirrors[k].due || !x.queue.is_empty() {
                continue;
            }
            let every = std::time::Duration::from_secs_f64(x.mirrors[k].every);
            self.sessions[c].mirrors[k].due = now + every;
            self.blob(c, &header, data);
        }
    }

    /// A video layer's frame taken down, and every mirror of it told.
    fn take_down(&mut self, i: usize) {
        if self.sessions[i].frame.take().is_none() {
            return;
        }
        self.changed = true;
        let name = self.sessions[i].name.clone();
        for c in 0..self.sessions.len() {
            if self.sessions[c].open() && self.sessions[c].mirrors.iter().any(|m| m.target == name) {
                self.say_kind(c, &format!("mirror {name} clear"), Some(Kind::Keep));
            }
        }
    }

    // ------------------------------------------------------------ saying

    fn say(&mut self, i: usize, line: &str) {
        self.say_kind(i, line, None);
    }

    /// One line to a session, queued when its `out` is full, followed to
    /// the controllers that asked.
    fn say_kind(&mut self, i: usize, line: &str, kind: Option<Kind>) {
        let first = line.split(' ').next().unwrap_or("");
        let kind = kind.unwrap_or(if first == "opened" || first == "closed" || line.starts_with("error open ") {
            Kind::Keep
        } else if STATE.contains(&first) && !line.contains(" id=") {
            Kind::State
        } else {
            Kind::Event
        });
        if first != "measure" && first != "dropped" && !line.starts_with("> ") && !line.starts_with("< ") {
            for j in 0..self.sessions.len() {
                let c = &self.sessions[j];
                if j != i && c.open() && (c.follow == Follow::Said || c.follow == Follow::In) {
                    let tagged = format!("> {} {}", self.sessions[i].name, line);
                    self.say_kind(j, &tagged, None);
                }
            }
        }
        let mut data = line.as_bytes().to_vec();
        data.push(b'\n');
        self.put(i, Item { kind, key: first.to_string(), data, at: 0 });
    }

    /// A picture or a mirror frame: a header line and its bytes, which
    /// wait in the queue rather than being dropped, so long as it has room.
    fn blob(&mut self, i: usize, header: &str, data: &[u8]) -> bool {
        let mut all = format!("{header}\n").into_bytes();
        all.extend_from_slice(data);
        if self.sessions[i].queued + all.len() > QUEUE_LIMIT {
            return false;
        }
        let key = header.split(' ').next().unwrap_or("").to_string();
        self.put(i, Item { kind: Kind::Blob, key, data: all, at: 0 });
        true
    }

    fn put(&mut self, i: usize, item: Item) {
        let s = &mut self.sessions[i];
        if s.out.is_none() {
            return;
        }
        if !s.queue.is_empty() {
            let waiting = s.queue.iter().any(|x| x.kind == Kind::Blob);
            if item.kind == Kind::Event && !waiting {
                s.dropped += 1;
                s.dropped_all += 1;
                return;
            }
            if item.kind == Kind::State && !waiting {
                if let Some(x) = s.queue.iter_mut().find(|x| x.kind == Kind::State && x.key == item.key && x.at == 0) {
                    s.queued = s.queued - x.data.len() + item.data.len();
                    x.data = item.data;
                    return;
                }
            }
            if item.kind == Kind::Event && s.queued + item.data.len() > QUEUE_LIMIT {
                s.dropped += 1;
                s.dropped_all += 1;
                return;
            }
        }
        s.queued += item.data.len();
        s.queue.push_back(item);
        self.flush(i);
    }

    /// Bytes into `out` without blocking; how many went.
    fn write(&mut self, i: usize, data: &[u8]) -> usize {
        let s = &mut self.sessions[i];
        let Some(out) = s.out.as_mut() else { return 0 };
        let mut sent = 0;
        while sent < data.len() {
            let end = (sent + 65536).min(data.len());
            match out.write(&data[sent..end]) {
                Wrote::Took(n) if n > 0 => sent += n,
                _ => break,
            }
        }
        s.out_bytes += sent as u64;
        self.out_total += sent as u64;
        sent
    }

    fn flush(&mut self, i: usize) {
        while !self.sessions[i].queue.is_empty() {
            let (dropped, at) = {
                let s = &self.sessions[i];
                (s.dropped, s.queue[0].at)
            };
            if dropped > 0 && at == 0 {
                let note = format!("dropped {dropped}\n");
                if self.write(i, note.as_bytes()) < note.len() {
                    return;
                }
                self.sessions[i].dropped = 0;
            }
            let rest = {
                let item = &self.sessions[i].queue[0];
                item.data[item.at..].to_vec()
            };
            let n = self.write(i, &rest);
            let s = &mut self.sessions[i];
            s.queue[0].at += n;
            s.queued -= n;
            if n < rest.len() {
                return;
            }
            s.queue.pop_front();
        }
    }

    fn tell_controllers(&mut self, line: &str, but: Option<usize>, cause: Option<usize>, tag: &str) {
        for j in 0..self.sessions.len() {
            let c = &self.sessions[j];
            if c.open() && c.role == Some(Role::Controller) && Some(j) != but {
                let l = if Some(j) == cause { format!("{line}{tag}") } else { line.to_string() };
                self.say(j, &l);
            }
        }
    }

    /// To the base and every layer above it.
    fn tell_layers(&mut self, line: &str, cause: Option<usize>, tag: &str) {
        for j in 0..self.sessions.len() {
            if self.sessions[j].open() && self.sessions[j].layer() {
                let l = if Some(j) == cause { format!("{line}{tag}") } else { line.to_string() };
                self.say(j, &l);
            }
        }
    }

    /// To the base and every cells layer that follows the base's grid:
    /// what the cells' area is. A video layer is pixels over pixels and
    /// hears nothing of it; a layer with a grid of its own hears its own
    /// (`refit_layer`).
    fn tell_cells(&mut self, line: &str, cause: Option<usize>, tag: &str) {
        for j in 0..self.sessions.len() {
            let s = &self.sessions[j];
            if s.open() && s.layer() && !s.video && s.own.is_none() {
                let l = if Some(j) == cause { format!("{line}{tag}") } else { line.to_string() };
                self.say(j, &l);
            }
        }
    }

    // ------------------------------------------------------------- state

    pub fn display(&self) -> &'static str {
        if self.blank {
            "blank"
        } else if !self.present {
            "away"
        } else if self.dozing {
            "doze"
        } else {
            "lit"
        }
    }

    pub fn dark(&self) -> bool {
        self.blank || !self.present || self.dozing
    }

    /// A terminal is a screen in front of somebody: 1 unless the wall is
    /// dark by a blank or presence, as a Pi says it.
    fn watching(&self) -> u8 {
        if self.blank || !self.present {
            0
        } else {
            1
        }
    }

    fn area_line(&self) -> String {
        format!("area {}x{} {}", self.cols, self.rows, self.font)
    }

    /// Follow the presence called `name`, as a receiver's `--presence`
    /// says: until a controller feeds it, the wall counts as watched.
    pub fn follow_presence(&mut self, name: &str) {
        self.presence = Some(name.to_string());
    }

    fn presence_line(&self) -> String {
        let mut line = format!("presence {}", if self.present { 1 } else { 0 });
        if let Some(n) = &self.presence {
            line += &format!(" name={n}");
        }
        if let Some(f) = &self.feeder {
            line += &format!(" from={f} age={}", self.fed_at.elapsed().as_secs());
        }
        line
    }

    fn stats_line(&self) -> String {
        let st = &self.stats;
        format!("stats {} at={} size={} show={}", st.on as u8, st.at, st.size, st.show)
    }

    /// What a cell can be, as the glass says it: its colour and rate, and
    /// where it has pixels, how many and a cell's at the wall's font.
    fn screen_line(&self, g: &dyn Glass) -> String {
        self.screen_line_at(g, self.font)
    }

    /// The screen line with a cell at `font`: a cells layer's own.
    fn screen_line_at(&self, g: &dyn Glass, font: u32) -> String {
        let mut line = format!("screen colour={} rate={}", g.colour(), g.rate());
        if let Some((w, h)) = self.pixels {
            line += &format!(" pixels={w}x{h}");
            if let Some((cw, ch)) = g.cell(font) {
                line += &format!(" cell={cw}x{ch}");
            }
        }
        if let Some(gl) = g.glyphs() {
            line += &format!(" glyphs={gl}");
        }
        line
    }

    fn accepts_line(&self, g: &dyn Glass) -> String {
        let words = g.accepts().trim();
        if words.is_empty() {
            "accepts".into()
        } else {
            format!("accepts {words}")
        }
    }

    /// What it speaks: a glass of pixels takes video layers, their frames
    /// in any of `images`, and no stream on `data`.
    fn caps_line(&self) -> String {
        let kitty = if self.kitty { " kitty" } else { "" };
        if self.pixels.is_none() {
            return format!("caps {CAPS}{kitty} layers={LAYERS} types=cells above=cells controllers={CONTROLLERS}");
        }
        format!(
            "caps {}{kitty} layers={LAYERS} types=cells,video above=video,cells codecs=qoi images={} controllers={CONTROLLERS}",
            CAPS.replace(" clear", " frame clear"),
            video::IMAGES.join(",")
        )
    }

    /// The words of `measures`: the wall's own, then the glass's.
    fn measure_names(g: &dyn Glass) -> Vec<String> {
        let mut out: Vec<String> = MEASURES.iter().map(|m| m.to_string()).collect();
        for (k, _) in g.measures() {
            if !out.iter().any(|m| m == k) {
                out.push(k.to_string());
            }
        }
        out
    }

    fn dark_changed(&mut self, was: (&'static str, u8), cause: Option<usize>, tag: &str) {
        let (d, w) = (self.display(), self.watching());
        if d != was.0 {
            self.tell_controllers(&format!("display {d}"), None, cause, tag);
        }
        if w != was.1 {
            if w == 1 {
                // Back in view: a base told nobody was watching stopped
                // drawing, and its silence is not held against it.
                if let Some(b) = self.base() {
                    self.sessions[b].last_in = Instant::now();
                    self.sessions[b].awaiting = false;
                }
            }
            self.tell_layers(&format!("watching {w}"), cause, tag);
        }
        self.changed = true;
    }

    // ---------------------------------------------------------- the area

    /// The glass changed, once the change has held: its area worked out
    /// again for what was last asked, and said to every cells session with
    /// the screen line and `repaint`, and to every controller. New pixels
    /// are a new glass: each video layer is taken down and told the screen
    /// and `repaint`, as a new mode is on a Pi.
    pub fn glass_changed(&mut self, g: &mut dyn Glass) {
        let pixels = g.pixels();
        let new_glass = pixels != self.pixels;
        self.pixels = pixels;
        if new_glass {
            for i in 0..self.sessions.len() {
                if self.sessions[i].open() && self.sessions[i].video {
                    self.take_down(i);
                }
            }
        }
        let (cols, rows, font) = g.area(self.request);
        if (cols, rows, font) != (self.cols, self.rows, self.font) {
            self.set_area(cols, rows, font, g, None, "");
        } else if new_glass {
            let screen = self.screen_line(g);
            self.tell_cells(&screen, None, "");
        }
        if new_glass {
            let screen = self.screen_line(g);
            for i in 0..self.sessions.len() {
                if self.sessions[i].open() && self.sessions[i].video {
                    self.say(i, &screen);
                    self.say(i, "repaint");
                }
            }
        }
        for i in 0..self.sessions.len() {
            if self.sessions[i].open() && self.sessions[i].own.is_some() {
                self.refit_layer(i, g, None, "", new_glass);
            }
        }
        self.changed = true;
    }

    /// A cells layer's own grid worked out again for what it asked: made
    /// that size, and said to it -- its area, `cause`'s id, its screen and
    /// `repaint` -- where the area changed, or `always`. A font is the
    /// cells the glass has at it; a grid is exactly its cells, at the font
    /// that holds them.
    fn refit_layer(&mut self, i: usize, g: &dyn Glass, cause: Option<usize>, tag: &str, always: bool) {
        let Some(ask) = self.sessions[i].own else {
            return;
        };
        let (cols, rows, font) = g.area(ask);
        let area = match ask {
            Ask::Grid(c, r) => (c, r, font),
            Ask::Font(_) => (cols, rows, font),
        };
        let s = &mut self.sessions[i];
        let was = s.own_area;
        s.own_area = area;
        if (area.0, area.1) != (s.grid.cols, s.grid.rows) {
            s.grid.resize(area.0, area.1);
            for m in s.mirrors.iter_mut() {
                m.was = None;
            }
        }
        if area != was || always {
            let t = if cause == Some(i) { tag } else { "" };
            self.say(i, &format!("area {}x{} {}{t}", area.0, area.1, area.2));
            let screen = self.screen_line_at(g, area.2);
            self.say(i, &screen);
            self.say(i, "repaint");
            self.changed = true;
        }
    }

    /// A cells layer gives up a grid of its own: the base's again, said to
    /// it with its screen and `repaint`.
    fn follow_base(&mut self, i: usize, g: &dyn Glass, tag: &str) {
        let (cols, rows) = (self.cols, self.rows);
        let s = &mut self.sessions[i];
        let had = s.own.take().is_some();
        if (s.grid.cols, s.grid.rows) != (cols, rows) {
            s.grid.resize(cols, rows);
        }
        let (area, screen) = (self.area_line(), self.screen_line(g));
        self.say(i, &format!("{area}{tag}"));
        if had {
            self.say(i, &screen);
            self.say(i, "repaint");
            self.changed = true;
        }
    }

    /// A new area: every cells grid made that size, and said, to every
    /// cells session with the screen line, and `repaint` where the cells
    /// changed, and to every controller; `cause` hears it with its id.
    fn set_area(&mut self, cols: usize, rows: usize, font: u32, g: &dyn Glass, cause: Option<usize>, tag: &str) {
        let resized = (cols, rows) != (self.cols, self.rows);
        self.cols = cols;
        self.rows = rows;
        self.font = font;
        if resized {
            for s in self.sessions.iter_mut() {
                if s.open() && s.layer() && !s.video && s.own.is_none() {
                    s.grid.resize(cols, rows);
                }
                for m in s.mirrors.iter_mut() {
                    m.was = None;
                }
            }
        }
        let (area, screen) = (self.area_line(), self.screen_line(g));
        self.tell_cells(&area, cause, tag);
        self.tell_cells(&screen, None, "");
        if resized {
            self.tell_cells("repaint", None, "");
        }
        self.tell_controllers(&area, None, cause, tag);
        self.changed = true;
    }

    /// A line to the base on its `out`, as the glass has one to say: a
    /// refusal of something the base's stream carried that the glass reads
    /// itself, such as a kitty picture.
    pub fn tell_base(&mut self, line: &str) {
        if let Some(b) = self.base() {
            self.say(b, line);
        }
    }

    /// What the base asked to be given.
    pub fn wants(&self) -> [bool; 3] {
        self.base().map(|b| self.sessions[b].wants).unwrap_or([false; 3])
    }

    /// A viewer's key, taken as a controller's `stats`, and every
    /// controller told.
    pub fn toggle_stats(&mut self) {
        self.stats.on = !self.stats.on;
        let line = self.stats_line();
        self.tell_controllers(&line, None, None, "");
        self.overlay_due = Instant::now();
        self.changed = true;
    }

    /// Something a viewer did, said to the base if it asked for it.
    pub fn press(&mut self, kind: &str, opts: &str) {
        let want = match kind {
            "tap" => 0,
            "key" => 1,
            _ => 2,
        };
        if let Some(b) = self.base() {
            if self.sessions[b].wants[want] {
                self.say(b, &format!("input {kind} {opts}"));
            }
        }
    }

    // ---------------------------------------------------------- commands

    fn obey(&mut self, i: usize, m: &Message, now: Instant, g: &mut dyn Glass) {
        let name = m.name.as_str();
        let id = m.get("id");
        if let Some(id) = id {
            if !proto::is_name(id) {
                self.say(i, &format!("error {name} id: not a token"));
                return;
            }
        }
        let tag = id.map(|v| format!(" id={v}")).unwrap_or_default();
        let s = &self.sessions[i];
        let who = if s.role == Some(Role::Controller) {
            "controller"
        } else if s.video {
            "video"
        } else if s.z == Some(0) {
            "base"
        } else {
            "layer"
        };
        let may: &[&str] = match name {
            "font" | "grid" => &["base", "layer", "controller"],
            "blank" => &["base", "controller"],
            "want" => &["base", "layer", "video"],
            "frame" | "align" => &["video"],
            "clear" => &["video", "layer"],
            "presence" | "stats" | "follow" | "measures" => &["controller"],
            "picture" | "mirror" | "report" | "sessions" | "ping" | "pong" | "meta" | "detach" | "close" => {
                &["base", "layer", "video", "controller"]
            }
            _ => {
                if id.is_some() {
                    self.say(i, &format!("error {name} unknown command{tag}"));
                }
                return;
            }
        };
        if !may.contains(&who) {
            self.say(i, &format!("error {name} not-for-role{tag}"));
            return;
        }
        match name {
            "report" => self.cmd_report(i, &tag, g),
            "sessions" => self.cmd_sessions(i, &tag),
            "ping" => self.say(i, &format!("pong{tag}")),
            "pong" => {
                let s = &mut self.sessions[i];
                if s.awaiting {
                    s.awaiting = false;
                    s.answered = true;
                    s.pong_ms = s.pinged_at.map(|p| now.duration_since(p).as_secs_f64() * 1000.0);
                }
            }
            "close" | "detach" => self.end(i, "asked"),
            "font" => self.cmd_font(i, m, &tag, g),
            "grid" => self.cmd_grid(i, m, &tag, g),
            "blank" => self.cmd_blank(i, m, &tag),
            "want" => self.cmd_want(i, m, &tag),
            "presence" => self.cmd_presence(i, m, &tag, now),
            "stats" => self.cmd_stats(i, m, &tag, g),
            "follow" => self.cmd_follow(i, m, &tag),
            "measures" => self.cmd_measures(i, m, &tag, now),
            "meta" => self.cmd_meta(i, m, &tag, now),
            "picture" => self.cmd_picture(i, m, &tag, g),
            "mirror" => self.cmd_mirror(i, m, &tag, now),
            "frame" => self.cmd_frame(i, m, &tag),
            "clear" if self.sessions[i].video => self.take_down(i),
            "clear" => {
                self.sessions[i].grid.wipe();
                self.changed = true;
            }
            _ => {}
        }
    }

    /// `frame ...`: what follows, and whether it is taken. The bytes are
    /// read next whatever is wrong with the line, so long as it says how
    /// many.
    fn cmd_frame(&mut self, i: usize, m: &Message, tag: &str) {
        let words: Vec<&str> = m.raw.split_whitespace().filter(|w| !w.starts_with("id=")).collect();
        let (count, head) = video::read_line(&words, tag);
        let s = &mut self.sessions[i];
        s.vwant = count;
        match head {
            Ok(h) => {
                s.skip = false;
                s.head = Some(h);
            }
            Err(why) => {
                s.skip = true;
                s.head = None;
                self.say(i, &format!("error frame {why}{tag}"));
            }
        }
    }

    fn cmd_report(&mut self, i: usize, tag: &str, g: &dyn Glass) {
        let mut lines = vec!["hello v1 line".to_string(), self.receiver.clone(), self.caps_line()];
        lines.push(format!("measures {}", Wall::measure_names(g).join(" ")));
        lines.push(if self.pixels.is_some() { "pictures qoi grid" } else { "pictures grid" }.into());
        let own = self.sessions[i].own.map(|_| self.sessions[i].own_area);
        lines.push(match own {
            Some((_, _, font)) => self.screen_line_at(g, font),
            None => self.screen_line(g),
        });
        lines.push(self.accepts_line(g));
        let now = Instant::now();
        if let Some(b) = self.base() {
            lines.extend(Wall::meta_lines(&self.sessions[b], now));
        }
        // Each layer above the base says its own, tagged with its session.
        for j in (1..LAYERS).filter_map(|z| self.above(z)).collect::<Vec<_>>() {
            for l in Wall::meta_lines(&self.sessions[j], now) {
                lines.push(format!("meta {} {}", self.sessions[j].name, &l[5..]));
            }
        }
        lines.push(self.stats_line());
        lines.push(self.presence_line());
        lines.push(format!("display {}", self.display()));
        lines.push(format!("watching {}", self.watching()));
        lines.push(match own {
            Some((c, r, f)) => format!("area {c}x{r} {f}"),
            None => self.area_line(),
        });
        for l in lines {
            self.say(i, &format!("{l}{tag}"));
        }
    }

    fn cmd_sessions(&mut self, i: usize, tag: &str) {
        let live: Vec<String> = self
            .sessions
            .iter()
            .filter(|x| x.open())
            .map(|x| {
                format!(
                    "session {} {} {} {}{}",
                    x.name,
                    if x.layer() { "layer" } else { "controller" },
                    Wall::place(x),
                    Wall::kind_of(x),
                    x.instance.as_ref().map(|v| format!(" instance={v}")).unwrap_or_default()
                )
            })
            .collect();
        let n = live.len();
        for l in live {
            self.say(i, &format!("{l}{tag}"));
        }
        self.say(i, &format!("sessions {n}{tag}"));
    }

    /// `font <size>`: the area that font comes to on the glass, said to
    /// every cells session as an answer always is, the asker's with its
    /// id. A glass that cannot change its cell answers with the area there
    /// is; a cells layer above the base has the base's grid, and is
    /// answered with it.
    fn cmd_font(&mut self, i: usize, m: &Message, tag: &str, g: &dyn Glass) {
        let size = m.args.iter().find_map(|w| proto::number(w.strip_prefix("n=").unwrap_or(w)).filter(|n| (1..=400).contains(n)));
        let base_word = m.args.first().map(|a| a == "base").unwrap_or(false);
        if size.is_none() && !base_word {
            if !tag.is_empty() {
                self.say(i, &format!("error font no size{tag}"));
            }
            return;
        }
        let (base, cells_layer) = (self.sessions[i].z == Some(0) || !self.sessions[i].layer(), !self.sessions[i].video);
        match size {
            Some(n) if base => self.ask_area(i, Ask::Font(n as u32), tag, g),
            Some(n) if cells_layer => {
                self.sessions[i].own = Some(Ask::Font(n as u32));
                self.refit_layer(i, g, Some(i), tag, true);
            }
            None if !base && cells_layer => self.follow_base(i, g, tag),
            _ => self.answer_area(i, tag, g),
        }
    }

    /// What was asked of the area, by the base or a controller: the area
    /// it comes to put up where it is new, and answered either way.
    fn ask_area(&mut self, i: usize, ask: Ask, tag: &str, g: &dyn Glass) {
        self.request = ask;
        let (cols, rows, font) = g.area(ask);
        if (cols, rows, font) == (self.cols, self.rows, self.font) {
            return self.answer_area(i, tag, g);
        }
        self.set_area(cols, rows, font, g, Some(i), tag);
    }

    fn cmd_grid(&mut self, i: usize, m: &Message, tag: &str, g: &dyn Glass) {
        let arg = m.args.first().map(String::as_str).unwrap_or("");
        if arg == "base" && self.sessions[i].z != Some(0) && self.sessions[i].layer() {
            if !self.sessions[i].video {
                return self.follow_base(i, g, tag);
            }
            return self.answer_area(i, tag, g);
        }
        let got = arg.split_once('x').and_then(|(c, r)| {
            let ok = |v: &str| v.len() <= 4 && !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit());
            if ok(c) && ok(r) {
                Some((c.parse::<usize>().unwrap(), r.parse::<usize>().unwrap()))
            } else {
                None
            }
        });
        match got {
            Some((c, r)) if c >= 1 && r >= 1 => {
                let (gc, gr, _) = g.area(Ask::Grid(c, r));
                if c > gc || r > gr {
                    self.say(i, &format!("error grid {c}x{r}: too many cells for this screen{tag}"));
                    return;
                }
                if self.sessions[i].z == Some(0) || !self.sessions[i].layer() {
                    self.ask_area(i, Ask::Grid(c, r), tag, g);
                } else if !self.sessions[i].video {
                    self.sessions[i].own = Some(Ask::Grid(c, r));
                    self.refit_layer(i, g, Some(i), tag, true);
                } else {
                    self.answer_area(i, tag, g);
                }
            }
            _ => self.say(i, &format!("error grid {}: not <cols>x<rows>{tag}", proto::word(arg))),
        }
    }

    fn answer_area(&mut self, i: usize, tag: &str, g: &dyn Glass) {
        let (area, screen) = (self.area_line(), self.screen_line(g));
        self.tell_cells(&area, Some(i), tag);
        self.tell_cells(&screen, None, "");
        if !self.sessions[i].layer() {
            self.say(i, &format!("{area}{tag}"));
        }
    }

    fn cmd_blank(&mut self, i: usize, m: &Message, tag: &str) {
        let Some(v) = m.args.first() else {
            return self.say(i, &format!("error blank no value{tag}"));
        };
        if v != "0" && v != "1" {
            return self.say(i, &format!("error blank {}: not 0 or 1{tag}", proto::word(v)));
        }
        let was = (self.display(), self.watching());
        self.blank = v == "1";
        self.dark_changed(was, Some(i), tag);
    }

    fn cmd_want(&mut self, i: usize, m: &Message, tag: &str) {
        for (k, v) in &m.opts {
            if k == "id" {
                continue;
            }
            let slot = match k.as_str() {
                "touch" => 0,
                "keyboard" => 1,
                "buttons" => 2,
                _ => {
                    self.say(i, &format!("error want {k}: unknown option{tag}"));
                    continue;
                }
            };
            if v != "0" && v != "1" {
                self.say(i, &format!("error want {k}={}: not 0 or 1{tag}", proto::word(v)));
            } else {
                self.sessions[i].wants[slot] = v == "1";
            }
        }
        self.changed = true;
    }

    fn cmd_presence(&mut self, i: usize, m: &Message, tag: &str, now: Instant) {
        let Some(v) = m.args.first() else {
            return self.say(i, &format!("error presence no value{tag}"));
        };
        if v != "0" && v != "1" {
            return self.say(i, &format!("error presence {}: not 0 or 1{tag}", proto::word(v)));
        }
        let Some(feeder) = m.get("from") else {
            return self.say(i, &format!("error presence no from{tag}"));
        };
        if !proto::is_name(feeder) {
            return self.say(i, &format!("error presence from={feeder}: not a token{tag}"));
        }
        if let Some((k, _)) = m.opts.iter().find(|(k, _)| k != "from" && k != "id") {
            return self.say(i, &format!("error presence {k}: unknown option{tag}"));
        }
        let was = (self.display(), self.watching());
        let was_present = self.present;
        self.present = v == "1";
        self.feeder = Some(feeder.to_string());
        self.fed_at = now;
        let line = self.presence_line();
        self.say(i, &format!("{line}{tag}"));
        if self.present != was_present {
            self.tell_controllers(&line, Some(i), None, "");
        }
        self.dark_changed(was, Some(i), tag);
    }

    fn cmd_stats(&mut self, i: usize, m: &Message, tag: &str, g: &dyn Glass) {
        let was = self.stats_line();
        let Some(v) = m.args.first() else {
            return self.say(i, &format!("error stats no value{tag}"));
        };
        if v != "0" && v != "1" {
            return self.say(i, &format!("error stats {}: not 0 or 1{tag}", proto::word(v)));
        }
        self.stats.on = v == "1";
        for (k, v) in &m.opts {
            match k.as_str() {
                "id" => {}
                "at" => {
                    if POINTS.contains(&v.as_str()) {
                        self.stats.at = v.clone();
                    } else {
                        self.say(i, &format!("error stats at={}: unknown point{tag}", proto::word(v)));
                    }
                }
                "size" => match proto::number(v) {
                    Some(n) => self.stats.size = n.clamp(8, 40),
                    None => self.say(i, &format!("error stats size={}: not a number{tag}", proto::word(v))),
                },
                "show" => {
                    let known = Wall::measure_names(g);
                    let names: Vec<&str> = if v == "all" {
                        known.iter().map(String::as_str).collect()
                    } else {
                        v.split(',').filter(|x| known.iter().any(|k| k == x)).collect()
                    };
                    if names.is_empty() {
                        self.say(i, &format!("error stats show={}: none known{tag}", proto::word(v)));
                    } else {
                        self.stats.show = names.join(",");
                    }
                }
                _ => self.say(i, &format!("error stats {k}: unknown option{tag}")),
            }
        }
        let line = self.stats_line();
        self.say(i, &format!("{line}{tag}"));
        if line != was {
            self.tell_controllers(&line, Some(i), None, "");
        }
        self.overlay_due = Instant::now();
        self.changed = true;
    }

    fn cmd_follow(&mut self, i: usize, m: &Message, tag: &str) {
        let Some(v) = m.args.first() else {
            return self.say(i, &format!("error follow no value{tag}"));
        };
        self.sessions[i].follow = match v.as_str() {
            "said" => Follow::Said,
            "in" => Follow::In,
            "off" => Follow::Off,
            _ => return self.say(i, &format!("error follow {}: unknown option{tag}", proto::word(v))),
        };
    }

    fn cmd_measures(&mut self, i: usize, m: &Message, tag: &str, now: Instant) {
        let Some(v) = m.args.first() else {
            return self.say(i, &format!("error measures no value{tag}"));
        };
        let Some(every) = proto::number(v) else {
            return self.say(i, &format!("error measures {}: not a number{tag}", proto::word(v)));
        };
        let (it, ot) = (self.in_total, self.out_total);
        let s = &mut self.sessions[i];
        if every == 0 {
            s.measures = 0.0;
            s.measure_due = None;
            return;
        }
        s.measures = every.clamp(200, 60000) as f64 / 1000.0;
        s.measure_due = Some(now + std::time::Duration::from_secs_f64(s.measures));
        s.measure_mark = (now, it, ot, sys::cpu_seconds());
    }

    fn cmd_meta(&mut self, i: usize, m: &Message, tag: &str, now: Instant) {
        let words: Vec<&str> = m.raw.split_whitespace().filter(|w| !w.starts_with("id=")).collect();
        let verb = words.get(1).copied();
        let rest = if words.len() > 2 { &words[2..] } else { &[][..] };
        match verb {
            Some("report") => {
                for l in Wall::meta_lines(&self.sessions[i], now) {
                    self.say(i, &format!("{l}{tag}"));
                }
                return;
            }
            Some("clear") => {
                if let Some(k) = rest.first() {
                    self.sessions[i].meta.retain(|(x, ..)| x != k);
                    self.say(i, &format!("meta clear {k}{tag}"));
                } else {
                    self.sessions[i].meta.clear();
                    self.say(i, &format!("meta clear{tag}"));
                }
                return;
            }
            Some("set") | Some("hide") | Some("show") => {}
            _ => return self.say(i, &format!("error meta {}: unknown verb{tag}", verb.unwrap_or("no verb"))),
        }
        let verb = verb.unwrap();
        let Some(key) = rest.first() else {
            return self.say(i, &format!("error meta {verb} no key{tag}"));
        };
        if !proto::is_meta_key(key) {
            return self.say(i, &format!("error meta {verb} {key}: not a key{tag}"));
        }
        let s = &mut self.sessions[i];
        if verb != "set" {
            let Some(entry) = s.meta.iter_mut().find(|(k, ..)| k == key) else {
                return self.say(i, &format!("error meta {verb} {key}: not set{tag}"));
            };
            entry.3 = verb == "hide";
            return self.say(i, &format!("meta {verb} {key}{tag}"));
        }
        let value = rest[1..].join(" ");
        if value.is_empty() {
            return self.say(i, &format!("error meta set {key}: no value{tag}"));
        }
        if value.len() > 256 || value.chars().any(|c| c.is_control()) {
            return self.say(i, &format!("error meta set {key}: too long{tag}"));
        }
        let had = s.meta.iter().position(|(k, ..)| k == key);
        if had.is_none() && s.meta.len() >= 32 {
            return self.say(i, &format!("error meta set {key}: too many keys{tag}"));
        }
        let size: usize = s.meta.iter().filter(|(k, ..)| k != key).map(|(k, v, ..)| k.len() + v.len()).sum();
        if size + key.len() + value.len() > 4096 {
            return self.say(i, &format!("error meta set {key}: too much{tag}"));
        }
        match had {
            Some(p) => {
                s.meta[p].1 = value.clone();
                s.meta[p].2 = now;
            }
            None => s.meta.push((key.to_string(), value.clone(), now, false)),
        }
        self.changed = true;
        self.say(i, &format!("meta set {key} {value}{tag}"));
    }

    fn meta_lines(s: &Session, now: Instant) -> Vec<String> {
        let mut out = Vec::new();
        for (k, v, at, hidden) in &s.meta {
            out.push(format!("meta set {k} {v}"));
            if *hidden {
                out.push(format!("meta hide {k}"));
            }
            out.push(format!("meta age {k} {}", now.duration_since(*at).as_secs()));
        }
        out
    }

    /// A picture's or a mirror's target and options, each checked: the
    /// session, or None for the glass, `every`, whether it is `off`, and
    /// the most a look at the glass may be; or the error said. A glass
    /// with no pixels is not offered.
    fn capture(&mut self, i: usize, m: &Message, tag: &str, cmd: &str) -> Option<Capture> {
        let Some(target) = m.args.first().cloned() else {
            self.say(i, &format!("error {cmd} no target{tag}"));
            return None;
        };
        let mirror = cmd == "mirror";
        if mirror && m.args.len() == 2 && m.args[1] == "off" && m.opts.iter().all(|(k, _)| k == "id") {
            return Some(Capture { who: None, every: None, off: true, size: None });
        }
        let mut who = None;
        if target != "glass" {
            match self.by_name(&target) {
                None => {
                    self.say(i, &format!("error {cmd} {target}: no such session{tag}"));
                    return None;
                }
                Some(j) if !self.sessions[j].layer() => {
                    self.say(i, &format!("error {cmd} {target}: not a layer{tag}"));
                    return None;
                }
                Some(j) => who = Some(j),
            }
        }
        if let Some(w) = m.args.get(1) {
            self.say(i, &format!("error {cmd} {w}: unknown option{tag}"));
            return None;
        }
        let pixels = self.pixels.is_some();
        let (mut every, mut size, mut format, mut quality) = (None, None, None, None);
        for (k, v) in &m.opts {
            match k.as_str() {
                "id" => {}
                "every" if mirror => match proto::number(v) {
                    Some(n) => every = Some(n.clamp(50, 60000) as f64 / 1000.0),
                    None => {
                        self.say(i, &format!("error {cmd} every={}: not a number{tag}", proto::word(v)));
                        return None;
                    }
                },
                "size" | "quality" if who.is_none() && !pixels => {
                    self.say(i, &format!("error {cmd} glass: not offered{tag}"));
                    return None;
                }
                "size" | "format" | "quality" if !pixels => {
                    self.say(i, &format!("error {cmd} {target} {k}={}: only for the glass{tag}", proto::word(v)));
                    return None;
                }
                "size" => match video::side_pair(v) {
                    Some(p) => size = Some(p),
                    None => {
                        self.say(i, &format!("error {cmd} size={}: bad size{tag}", proto::word(v)));
                        return None;
                    }
                },
                "format" => {
                    if v != "qoi" {
                        self.say(i, &format!("error {cmd} format={}: not offered{tag}", proto::word(v)));
                        return None;
                    }
                    format = Some(v.clone());
                }
                "quality" => {
                    if !(v.len() <= 3 && v.bytes().all(|b| b.is_ascii_digit()) && (1..=100).contains(&v.parse::<u32>().unwrap_or(0))) {
                        self.say(i, &format!("error {cmd} quality={}: bad quality{tag}", proto::word(v)));
                        return None;
                    }
                    quality = Some(v.clone());
                }
                _ => {
                    self.say(i, &format!("error {cmd} {k}={}: unknown option{tag}", proto::word(v)));
                    return None;
                }
            }
        }
        if who.is_some() && (size.is_some() || format.is_some() || quality.is_some()) {
            let said = match (size, &format, &quality) {
                (Some((w, h)), _, _) => format!("size={w}x{h}"),
                (_, Some(f), _) => format!("format={f}"),
                (_, _, Some(q)) => format!("quality={q}"),
                _ => unreachable!(),
            };
            self.say(i, &format!("error {cmd} {target} {said}: only for the glass{tag}"));
            return None;
        }
        if let Some(q) = quality {
            self.say(i, &format!("error {cmd} quality={q}: only for jpeg{tag}"));
            return None;
        }
        if who.is_none() && !pixels {
            // The glass is the receiver's, and this wall cannot see its pixels.
            self.say(i, &format!("error {cmd} glass: not offered{tag}"));
            return None;
        }
        Some(Capture { who, every, off: false, size })
    }

    /// The glass's size fitted inside `size` with its shape kept, and never
    /// larger than the glass; the glass's own without one.
    fn fit(&self, size: Option<(u32, u32)>) -> (u32, u32) {
        let (gw, gh) = self.pixels.unwrap_or((1, 1));
        let Some((mut w, mut h)) = size else { return (gw, gh) };
        if w >= gw && h >= gh {
            return (gw, gh);
        }
        if gw as u64 * h as u64 <= gh as u64 * w as u64 {
            w = ((gw as u64 * h as u64 + gh as u64 / 2) / gh as u64) as u32;
        } else {
            h = ((gh as u64 * w as u64 + gw as u64 / 2) / gw as u64) as u32;
        }
        (w.max(1), h.max(1))
    }

    /// A look at the glass at the size asked, as a QOI image: its size and
    /// bytes, or None where the glass gave none.
    fn look(&self, g: &mut dyn Glass, size: Option<(u32, u32)>) -> Option<(u32, u32, Vec<u8>)> {
        let (w, h) = self.fit(size);
        let px = g.look(self, w, h)?;
        if px.len() != (w * h * 4) as usize {
            return None;
        }
        Some((w, h, video::qoi_encode(w, h, &px)))
    }

    fn cmd_picture(&mut self, i: usize, m: &Message, tag: &str, g: &mut dyn Glass) {
        let Some(c) = self.capture(i, m, tag, "picture") else { return };
        let target = c.who.map(|j| self.sessions[j].name.clone()).unwrap_or_else(|| "glass".into());
        if self.sessions[i].queued >= QUEUE_LIMIT {
            return self.say(i, &format!("error picture {target}: too far behind{tag}"));
        }
        let Some(j) = c.who else {
            let Some((w, h, data)) = self.look(g, c.size) else {
                return self.say(i, &format!("error picture glass: not offered{tag}"));
            };
            if !self.blob(i, &format!("picture glass qoi {w}x{h} {}{tag}", data.len()), &data) {
                self.say(i, &format!("error picture glass: too far behind{tag}"));
            }
            return;
        };
        if self.sessions[j].video {
            let Some(f) = &self.sessions[j].frame else {
                return self.say(i, &format!("error picture {target}: nothing shown{tag}"));
            };
            let (header, data) = (format!("picture {target} {} {}{tag}", f.words(), f.data.len()), f.data.clone());
            if !self.blob(i, &header, &data) {
                self.say(i, &format!("error picture {target}: too far behind{tag}"));
            }
            return;
        }
        let (data, cols, rows) = {
            let g = &self.sessions[j].grid;
            let data = if self.sessions[j].z == Some(0) {
                let (d, _) = g.picture(None);
                if d.is_empty() {
                    b"\x1b[0m".to_vec()
                } else {
                    d
                }
            } else {
                g.layer_picture()
            };
            (data, g.cols, g.rows)
        };
        if !self.blob(i, &format!("picture {target} grid {cols}x{rows} {}{tag}", data.len()), &data) {
            self.say(i, &format!("error picture {target}: too far behind{tag}"));
        }
    }

    fn cmd_mirror(&mut self, i: usize, m: &Message, tag: &str, now: Instant) {
        let Some(c) = self.capture(i, m, tag, "mirror") else { return };
        let target = m.args[0].clone();
        if c.off {
            self.sessions[i].mirrors.retain(|x| x.target != target);
            return;
        }
        let video = c.who.map(|j| self.sessions[j].video).unwrap_or(false);
        if let Some(j) = c.who {
            if !video && self.sessions[j].z != Some(0) {
                return self.say(i, &format!("error mirror {target}: not offered{tag}"));
            }
        }
        let running: usize = self.sessions.iter().filter(|x| x.open()).map(|x| x.mirrors.len()).sum();
        let mine = self.sessions[i].mirrors.len();
        let again = self.sessions[i].mirrors.iter().any(|x| x.target == target);
        if !again && (mine >= 2 || running >= 4) {
            let n = if mine >= 2 { 2 } else { 4 };
            return self.say(i, &format!("error mirror {target}: {n} mirrors already{tag}"));
        }
        // A video layer's frames go as they come; the rest are looked at.
        let every = c.every.unwrap_or(if video { 0.0 } else { 0.1 });
        let s = &mut self.sessions[i];
        s.mirrors.retain(|x| x.target != target);
        s.mirrors.push(Mirror { target, every, due: now, was: None, size: c.size, sent: None });
    }

    // ------------------------------------------------------------ clocks

    fn tick(&mut self, now: Instant, g: &mut dyn Glass) {
        while self.resets.front().map(|r| now.duration_since(*r).as_secs_f64() > 60.0).unwrap_or(false) {
            self.resets.pop_front();
        }
        // No frame within its hold: the layer is cleared, and stays open.
        for i in 0..self.sessions.len() {
            let gone = match &self.sessions[i].frame {
                Some(f) => f.hold.map(|h| now.duration_since(f.came).as_millis() >= h as u128).unwrap_or(false),
                None => false,
            };
            if gone {
                self.take_down(i);
            }
        }
        let mut gone = Vec::new();
        for i in 0..self.sessions.len() {
            self.flush(i);
            let s = &self.sessions[i];
            if s.open() {
                if !s.connected && now.duration_since(s.opened_at).as_secs_f64() >= self.t.unwritten {
                    self.end(i, "timeout");
                    continue;
                }
                self.rates(i, now);
                if self.sessions[i].connected {
                    self.ping_clock(i, now);
                }
                if self.sessions[i].open() {
                    self.mirror_frames(i, now, g);
                }
                let s = &self.sessions[i];
                if s.open() && s.role == Some(Role::Controller) {
                    if let Some(due) = s.measure_due {
                        if now >= due {
                            let line = self.measure_line(i, now, g);
                            self.say(i, &line);
                            for x in 0..self.sessions.len() {
                                if self.sessions[x].open() {
                                    let l = self.session_measure(x, now, g);
                                    self.say(i, &l);
                                }
                            }
                            let s = &mut self.sessions[i];
                            s.measure_due = Some(now + std::time::Duration::from_secs_f64(s.measures));
                        }
                    }
                }
            } else {
                let drained = s.queue.is_empty() && s.out.as_ref().map(|o| o.drained()).unwrap_or(true);
                if drained || now.duration_since(s.ended_at).as_secs_f64() >= self.t.keep {
                    gone.push(i);
                }
            }
        }
        for i in gone.into_iter().rev() {
            let mut s = self.sessions.remove(i);
            if let Some(mut o) = s.out.take() {
                o.close();
            }
            pipe::unsession(&s.path);
        }
        if !self.connected && !self.dozing && now.duration_since(self.last_data).as_secs_f64() >= DOZE {
            let was = (self.display(), self.watching());
            self.dozing = true;
            self.dark_changed(was, None, "");
        }
        if self.stats.on && now >= self.overlay_due {
            self.overlay_due = now + std::time::Duration::from_millis(500);
            self.overlay_lines = self.overlay(now, g);
            self.changed = true;
        }
    }

    /// A session's rates, worked out once a second from its counts.
    fn rates(&mut self, i: usize, now: Instant) {
        let x = &mut self.sessions[i];
        let dt = now.duration_since(x.rates_at).as_secs_f64();
        if dt < 1.0 {
            return;
        }
        let (a, b, c) = x.rates_mark;
        x.r_in = (x.in_bytes - a) as f64 / dt;
        x.r_out = (x.out_bytes - b) as f64 / dt;
        x.r_fps = (x.frame_count - c) as f64 / dt;
        x.rates_mark = (x.in_bytes, x.out_bytes, x.frame_count);
        x.rates_at = now;
    }

    fn ping_clock(&mut self, i: usize, now: Instant) {
        let watching = self.watching() > 0;
        let t = &self.t;
        let s = &mut self.sessions[i];
        if s.awaiting {
            if now.duration_since(s.pinged_at.unwrap_or(now)).as_secs_f64() >= t.pong {
                if s.answered || s.role == Some(Role::Controller) {
                    self.end(i, "timeout");
                } else {
                    s.awaiting = false;
                }
            }
            return;
        }
        let since_from = s.pinged_at.map(|p| p.max(s.last_in)).unwrap_or(s.last_in);
        let since = now.duration_since(since_from).as_secs_f64();
        let due = if watching {
            since >= t.quiet
        } else if s.answered {
            since >= t.unwatched
        } else {
            s.pings == 0 && since >= t.quiet
        };
        if due {
            s.awaiting = true;
            s.pinged_at = Some(now);
            s.pings += 1;
            self.say(i, "ping");
        }
    }

    /// A session's mirrors that are due: the glass sent when it has
    /// changed, the base's grid whole and then what changed, never sent
    /// while its reader still has a frame waiting. A video layer's go as
    /// its frames come; a mirror of a session that has gone stops.
    fn mirror_frames(&mut self, i: usize, now: Instant, g: &mut dyn Glass) {
        let mut k = 0;
        while k < self.sessions[i].mirrors.len() {
            let target = self.sessions[i].mirrors[k].target.clone();
            let glass = target == "glass" && self.pixels.is_some();
            let found = self.by_name(&target).filter(|&j| self.sessions[j].z == Some(0) || self.sessions[j].video);
            if found.is_none() && !glass {
                self.sessions[i].mirrors.remove(k);
                continue;
            }
            let busy = !self.sessions[i].queue.is_empty();
            if found.map(|j| self.sessions[j].video).unwrap_or(false) || now < self.sessions[i].mirrors[k].due || busy {
                k += 1;
                continue;
            }
            let m = &mut self.sessions[i].mirrors[k];
            let every = std::time::Duration::from_secs_f64(m.every);
            m.due = if now.duration_since(m.due) < every { m.due + every } else { now + every };
            if glass {
                let size = m.size;
                if let Some((w, h, data)) = self.look(g, size) {
                    if self.sessions[i].mirrors[k].sent.as_ref() != Some(&data) && self.blob(i, &format!("mirror glass qoi {w}x{h} {}", data.len()), &data) {
                        self.sessions[i].mirrors[k].sent = Some(data);
                    }
                }
                k += 1;
                continue;
            }
            let j = found.unwrap();
            let was = self.sessions[i].mirrors[k].was.take();
            let g = &self.sessions[j].grid;
            let (mut data, rows) = g.picture(was.as_ref());
            let header = if was.is_none() {
                if data.is_empty() {
                    data = b"\x1b[0m".to_vec();
                }
                format!("mirror {target} grid {}x{} {}", g.cols, g.rows, data.len())
            } else if !data.is_empty() {
                format!("mirror {target} diff {}", data.len())
            } else {
                self.sessions[i].mirrors[k].was = was;
                k += 1;
                continue;
            };
            let sent = self.blob(i, &header, &data);
            self.sessions[i].mirrors[k].was = if sent { Some(rows) } else { was };
            k += 1;
        }
    }

    fn fps_since(&self, then: Instant, now: Instant) -> f64 {
        let n = self.resets.iter().filter(|r| **r >= then).count();
        n as f64 / now.duration_since(then).as_secs_f64().max(1e-6)
    }

    /// The wall's own line: the glass and what it costs.
    fn measure_line(&mut self, i: usize, now: Instant, g: &dyn Glass) -> String {
        let (then, in_then, out_then, cpu_then) = self.sessions[i].measure_mark;
        let elapsed = now.duration_since(then).as_secs_f64().max(1e-6);
        let cpu = sys::cpu_seconds();
        self.sessions[i].measure_mark = (now, self.in_total, self.out_total, cpu);
        let ms = |v: Option<f64>| v.map(|v| format!("{v:.2}")).unwrap_or_else(|| "-".into());
        let mut line = format!(
            "measure wall={} grid={}x{} font={} fps={:.1} in={:.0} out={:.0} frame={} draw={} display={} cpu={:.0} render={}",
            self.name,
            self.cols,
            self.rows,
            self.font,
            self.fps_since(then, now),
            (self.in_total - in_then) as f64 / elapsed,
            (self.out_total - out_then) as f64 / elapsed,
            ms(self.frame_ms),
            ms(self.draw_ms),
            self.display(),
            100.0 * (cpu - cpu_then) / elapsed,
            g.render()
        );
        for (k, v) in g.measures() {
            if !MEASURES.contains(&k) {
                line += &format!(" {k}={}", proto::word(&v));
            }
        }
        line
    }

    /// A line for session `x`: its place, and its rates over the last whole
    /// second, `-` until there is one.
    fn session_measure(&self, x: usize, now: Instant, g: &dyn Glass) -> String {
        let s = &self.sessions[x];
        let place = if s.layer() { format!(" z={} type={}", s.z.unwrap(), Wall::kind_of(s)) } else { String::new() };
        let fps = if s.layer() && s.r_fps >= 0.0 { format!("{:.1}", s.r_fps) } else { "-".into() };
        let rate = |v: f64| if s.r_in >= 0.0 { format!("{v:.0}") } else { "-".into() };
        format!(
            "measure wall={} session={} role={}{place} fps={fps} in={} out={} dropped={} queue={} quiet={:.1} pong={}",
            self.name,
            s.name,
            if s.layer() { "layer" } else { "controller" },
            rate(s.r_in),
            rate(s.r_out),
            s.dropped_all,
            s.queued,
            now.duration_since(s.last_in).as_secs_f64(),
            s.pong_ms.map(|v| format!("{v:.0}")).unwrap_or_else(|| "-".into())
        ) + &if s.video {
            format!(" decode={}", s.z.and_then(|z| g.decode_ms(z)).map(|v| format!("{v:.2}")).unwrap_or_else(|| "-".into()))
        } else {
            String::new()
        }
    }

    /// The overlay as the painter draws it: a line a measure chosen by
    /// `show`, and under them a row a session, top layer first.
    fn overlay(&mut self, now: Instant, g: &dyn Glass) -> Vec<String> {
        let cpu = sys::cpu_seconds();
        let own = g.measures();
        let mark = self.overlay_mark.replace((now, self.in_total, self.out_total, cpu));
        let val = |k: &str| -> String {
            let dt = mark.map(|m| now.duration_since(m.0).as_secs_f64()).unwrap_or(0.0);
            match k {
                "wall" => self.name.clone(),
                "grid" => format!("{}x{} @{}", self.cols, self.rows, self.font),
                "fps" => format!("{:.1}", self.fps_since(now.checked_sub(std::time::Duration::from_secs(1)).unwrap_or(now), now)),
                "in" if dt > 0.0 => format!("{}/s", human((self.in_total - mark.unwrap().1) as f64 / dt)),
                "out" if dt > 0.0 => format!("{}/s", human((self.out_total - mark.unwrap().2) as f64 / dt)),
                "cpu" if dt > 0.0 => format!("{:.0}%", 100.0 * (cpu - mark.unwrap().3) / dt),
                "frame" => self.frame_ms.map(|v| format!("{v:.2}ms")).unwrap_or_else(|| "-".into()),
                "draw" => self.draw_ms.map(|v| format!("{v:.2}ms")).unwrap_or_else(|| "-".into()),
                "sender" => format!("{} {:.0}s", if self.connected { "yes" } else { "no" }, now.duration_since(self.last_data).as_secs_f64()),
                "pong" => self
                    .base()
                    .and_then(|b| self.sessions[b].pong_ms)
                    .map(|v| format!("{v:.0}ms"))
                    .unwrap_or_else(|| "-".into()),
                "display" => self.display().into(),
                "render" => g.render().into(),
                _ => own.iter().find(|(n, _)| *n == k).map(|(_, v)| v.clone()).unwrap_or_else(|| "-".into()),
            }
        };
        let show: Vec<String> = self.stats.show.split(',').map(String::from).collect();
        let mut lines: Vec<String> = show.iter().map(|k| format!("{k:<6} {}", val(k))).collect();
        let mut order: Vec<usize> = (0..self.sessions.len()).filter(|&x| self.sessions[x].open()).collect();
        order.sort_by_key(|&x| {
            let s = &self.sessions[x];
            (!s.layer(), std::cmp::Reverse(s.z.unwrap_or(0)))
        });
        if !order.is_empty() {
            lines.push(String::new());
        }
        for x in order {
            let s = &self.sessions[x];
            let place = if s.layer() { format!("{} {}", s.z.unwrap(), Wall::kind_of(s)) } else { "controller".into() };
            let fps = if s.layer() && s.r_fps >= 0.0 { format!("{:.0}fps", s.r_fps) } else { "-".into() };
            let rate = if s.r_in >= 0.0 { format!("{}/s", human(s.r_in)) } else { "-".into() };
            let name: String = s.name.chars().take(18).collect();
            lines.push(format!("{name:<18} {place:<10} {fps:>6} {rate:>9} {} dropped", s.dropped_all));
        }
        lines
    }

    // ------------------------------------------------------------ ending

    fn end(&mut self, i: usize, reason: &str) {
        if !self.sessions[i].open() {
            return;
        }
        let now = Instant::now();
        let name = self.sessions[i].name.clone();
        self.say(i, &format!("closed {name} {reason}"));
        let s = &mut self.sessions[i];
        s.st = St::Closed;
        s.ended_at = now;
        if let Some(r) = s.reader.take() {
            r.close();
        }
        s.mirrors.clear();
        s.measure_due = None;
        let z = s.z;
        let from_top = s.from_top;
        let kitty_used = s.kitty_used;
        if z == Some(0) && s.layer() {
            if kitty_used {
                // Its pictures go with it, as every ending leaves a layer
                // transparent.
                self.kitty_out.push((b"\x1b_Ga=d,d=A,q=2\x1b\\".to_vec(), 0, 0));
            }
            self.connected = false;
            self.last_data = now;
            self.last_seen = Some(sys::clock(true));
        } else if let Some(z) = z.filter(|z| *z > 0) {
            // No gap is left: the rest of its stack closes up behind it.
            let mut k = z as isize + if from_top { -1 } else { 1 };
            while k >= 1 && (k as usize) < LAYERS {
                match self.above(k as usize) {
                    Some(j) if self.sessions[j].from_top == from_top => {
                        let to = if from_top { k + 1 } else { k - 1 };
                        self.move_layer(k as usize, to as usize);
                    }
                    _ => break,
                }
                k += if from_top { -1 } else { 1 };
            }
        }
        // Every ending leaves a video layer transparent.
        self.take_down(i);
        self.sessions[i].vbuf = Vec::new();
        self.changed = true;
        self.tell_controllers(&format!("session closed {name} {reason}"), Some(i), None, "");
    }

    /// The layer at slot `frm` moved to the free slot `to`, and told so, as
    /// every controller is.
    fn move_layer(&mut self, frm: usize, to: usize) {
        let Some(j) = self.above(frm) else { return };
        self.sessions[j].z = Some(to);
        let name = self.sessions[j].name.clone();
        self.say(j, &format!("moved {name} z={to}"));
        self.tell_controllers(&format!("session moved {name} {to}"), None, None, "");
        self.changed = true;
    }

    /// Stop in good order: every session closed, nothing said, and the wall
    /// taken away.
    pub fn stop(&mut self) {
        for s in self.sessions.iter_mut() {
            if let Some(r) = s.reader.take() {
                r.close();
            }
            if let Some(mut o) = s.out.take() {
                o.close();
            }
        }
        self.sessions.clear();
        pipe::unclaim(&self.dir);
    }
}

/// Bytes as the painter says them: 6.9MB, 2.1KB, 312B.
pub fn human(v: f64) -> String {
    if v >= 1e6 {
        format!("{:.1}MB", v / 1e6)
    } else if v >= 1e3 {
        format!("{:.1}KB", v / 1e3)
    } else {
        format!("{v:.0}B")
    }
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}
