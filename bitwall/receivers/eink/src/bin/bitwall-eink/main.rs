//! bitwall-eink: a wall on electronic paper. The receiver core
//! (`receivers/core-rs`) keeps the door, the sessions and every command;
//! this is its glass: the panel, turned as it hangs, with a cell from the
//! font at the size asked. Whenever a frame closes or the layers change,
//! everything the wall holds is handed to `Eink` as one scene -- the
//! base's cells with its kitty pictures, cells layers, and video layers'
//! frames decoded to pictures -- and `Eink` composes it in sixteen greys
//! and pushes to the panel when its policy says.
//!
//!     bitwall-eink [--font <n>] [--rotate 0|90|180|270] [--mirror]
//!                  [--vcom <volts>] [--presence <name>] [--panel it8951|file:DIR] [--glass WxH]
//!                  [--paper|--screen] [wall]          wall `paper` unless named
//!     bitwall-eink --cell <font>                       a cell's pixels at a font
//!
//! `--panel file:DIR` writes every push to DIR as a PNG and a line of log,
//! the panel `--glass` pixels (1872x1404 unless given), and is the panel
//! on a machine with no IT8951; a line `glass WxH` on its stdin gives it
//! another size. BITWALL_RUN puts the walls elsewhere. SIGTERM or SIGINT
//! ends it, the picture left on the panel.

mod kitty;

use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::io::{self, BufRead};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use bitwall_eink::geom::Rect;
use bitwall_eink::scene::BOLD;
use bitwall_eink::tune::{FONT, FONT_MAX, FONT_MIN};
use bitwall_eink::{Cell, Cells, Eink, FilePanel, Layer, Mode, Options, Panel, Picture, Pushed, Scene, Show};
use bitwall_receiver::{decode, pipe, sys, Ask, Glass, Surface, Wall};

use kitty::Kitty;

/// The IT8951 panel's own size, for a file panel standing in for it.
const PANEL: (u32, u32) = (1872, 1404);

static STOP: AtomicBool = AtomicBool::new(false);

fn now_s() -> String {
    sys::clock(true)
}

macro_rules! log {
    ($($a:tt)*) => {{
        use std::io::Write;
        println!("{} {}", now_s(), format!($($a)*));
        let _ = std::io::stdout().flush();
    }};
}

fn fail(msg: &str) -> ! {
    eprintln!("bitwall-eink: {msg}");
    std::process::exit(1);
}

/// Where pushes go: a directory of PNGs, or the IT8951 on a Pi's SPI.
enum AnyPanel {
    File(FilePanel),
    #[cfg(all(target_os = "linux", feature = "it8951"))]
    It(bitwall_eink::panel::it8951::It8951),
}

impl Panel for AnyPanel {
    fn size(&self) -> (u32, u32) {
        match self {
            AnyPanel::File(p) => p.size(),
            #[cfg(all(target_os = "linux", feature = "it8951"))]
            AnyPanel::It(p) => p.size(),
        }
    }

    fn clear(&mut self) -> io::Result<()> {
        match self {
            AnyPanel::File(p) => p.clear(),
            #[cfg(all(target_os = "linux", feature = "it8951"))]
            AnyPanel::It(p) => p.clear(),
        }
    }

    fn push(&mut self, r: &Rect, grey: &[u8], mode: Mode) -> io::Result<()> {
        match self {
            AnyPanel::File(p) => p.push(r, grey, mode),
            #[cfg(all(target_os = "linux", feature = "it8951"))]
            AnyPanel::It(p) => p.push(r, grey, mode),
        }
    }

    fn sleep(&mut self) -> io::Result<()> {
        match self {
            AnyPanel::File(p) => p.sleep(),
            #[cfg(all(target_os = "linux", feature = "it8951"))]
            AnyPanel::It(p) => p.sleep(),
        }
    }
}

/// The spans `glyphs=` names, each with a code point the face is asked
/// for, as the painter asks its font, or one drawn as a shape; then those
/// drawn as shapes, asked of their first code point. The Nerd Font icons
/// are the sets Nerd Fonts patches in, each asked of one icon.
const ASK: [(&str, u32); 18] = [
    ("U+0020-007E", 0x41),
    ("U+2190-21FF", 0x2192),
    ("U+2500-257F", 0x2500),
    ("U+2580-259F", 0x2588),
    ("U+25A0-25FF", 0x25CF),
    ("U+2800-28FF", 0x2801),
    ("U+FF61-FF9F", 0xFF66),
    ("U+E0A0-E0A3,U+E0B0-E0D7", 0xE0B0), // Powerline
    ("U+E300-E3E3", 0xE30D),             // Weather
    ("U+EA60-EC1E", 0xEA60),             // Codicons
    ("U+F000-F2E0", 0xF05A),             // Font Awesome
    ("U+F400-F533", 0xF400),             // Octicons
    ("U+F0001-F1AF0", 0xF0369),          // Material Design
    ("U+1CD00-1CDE5", 0x1CD00),
    ("U+1CEA0-1CEAF", 0x1CEA0),
    ("U+1FB00-1FB3B", 0x1FB00),
    ("U+1FB70-1FB8B", 0x1FB70),
    ("U+1FBE4-1FBE7", 0x1FBE4),
];

/// Spans of CSS `unicode-range` as one value: sorted, those that meet
/// joined and in the fewest digits, since a value on the wire is at most
/// 256 bytes and 32 ranges (PROTOCOL.md).
fn spelled(spans: &[&str]) -> String {
    let hex = |t: &str| u32::from_str_radix(t, 16).ok();
    let mut got: Vec<(u32, u32)> = spans
        .iter()
        .flat_map(|s| s.split(','))
        .filter_map(|p| {
            let p = p.strip_prefix("U+")?;
            match p.split_once('-') {
                Some((lo, hi)) => Some((hex(lo)?, hex(hi)?)),
                None => hex(p).map(|c| (c, c)),
            }
        })
        .collect();
    got.sort();
    let mut out: Vec<(u32, u32)> = Vec::new();
    for (lo, hi) in got {
        match out.last_mut() {
            Some(last) if lo <= last.1 + 1 => last.1 = last.1.max(hi),
            _ => out.push((lo, hi)),
        }
    }
    out.iter()
        .map(|&(lo, hi)| if lo == hi { format!("U+{lo:X}") } else { format!("U+{lo:X}-{hi:X}") })
        .collect::<Vec<_>>()
        .join(",")
}

/// The panel as the wall's glass.
struct Paper {
    eink: Eink<AnyPanel>,
    options: Options,
    render: &'static str,
    glyphs: String,
    host: String,
    addr: String,
    kitty: Kitty,
    /// Each video layer's frame decoded, by its `seq`, kept while shown.
    decoded: HashMap<u64, (Arc<Vec<u8>>, u32, u32)>,
    decode_ms: HashMap<usize, f64>,
    /// The last push: how long it took, and what it was.
    push_ms: Option<f64>,
    refresh: Option<String>,
    pushes: u64,
}

fn temp() -> Option<f64> {
    let s = std::fs::read_to_string("/sys/class/thermal/thermal_zone0/temp").ok()?;
    Some(s.trim().parse::<f64>().ok()? / 1000.0)
}

fn id_of(name: &str) -> u64 {
    let mut h = DefaultHasher::new();
    name.hash(&mut h);
    h.finish() | 1
}

impl Glass for Paper {
    fn area(&self, ask: Ask) -> (usize, usize, u32) {
        let at = |f: u32| {
            let (c, r) = self.eink.area(f);
            (c, r, f)
        };
        match ask {
            Ask::Font(f) => at(f.clamp(FONT_MIN, FONT_MAX)),
            Ask::Grid(c, r) => at(self.eink.font_for(c, r).unwrap_or(FONT_MIN)),
        }
    }

    fn pixels(&self) -> Option<(u32, u32)> {
        Some(self.eink.glass())
    }

    fn cell(&self, font: u32) -> Option<(u32, u32)> {
        Some(self.eink.cell(font))
    }

    fn colour(&self) -> &str {
        "grey"
    }

    fn rate(&self) -> u32 {
        1
    }

    fn glyphs(&self) -> Option<String> {
        Some(self.glyphs.clone())
    }

    fn render(&self) -> &str {
        self.render
    }

    fn kitty(&self) -> bool {
        true
    }

    fn measures(&self) -> Vec<(&'static str, String)> {
        vec![
            ("push", self.push_ms.map(|v| format!("{v:.0}ms")).unwrap_or_else(|| "-".into())),
            ("refresh", self.refresh.clone().unwrap_or_else(|| "-".into())),
            ("temp", temp().map(|t| format!("{t:.1}C")).unwrap_or_else(|| "-".into())),
        ]
    }

    fn decode_ms(&self, z: usize) -> Option<f64> {
        self.decode_ms.get(&z).copied()
    }

    /// The glass as composed, which is what the panel shows or is about to:
    /// greys as RGBA, scaled to `w` by `h`.
    fn look(&mut self, wall: &Wall, w: u32, h: u32) -> Option<Vec<u8>> {
        let scene = self.scene(wall);
        self.eink.show(&scene, Instant::now());
        let (gw, gh) = self.eink.glass();
        let g = &self.eink.comp.glass;
        let mut out = vec![255u8; (w * h * 4) as usize];
        for y in 0..h {
            let sy = (y as u64 * gh as u64 / h as u64) as u32;
            for x in 0..w {
                let sx = (x as u64 * gw as u64 / w as u64) as u32;
                let v = g[(sy * gw + sx) as usize];
                let d = ((y * w + x) * 4) as usize;
                out[d..d + 3].copy_from_slice(&[v, v, v]);
            }
        }
        Some(out)
    }
}

impl Paper {
    /// A grid as the core keeps it, as the compositor takes it.
    fn cells(font: u32, g: &bitwall_receiver::grid::Grid) -> Cells {
        Cells {
            font,
            cols: g.cols,
            rows: g.rows,
            cells: g.cells.iter().map(|c| Cell { ch: c.ch, fg: c.fg, bg: c.bg, at: c.at }).collect(),
            pictures: Vec::new(),
        }
    }

    /// Where a grid at `font` starts on the glass, centred as the
    /// compositor centres it.
    fn origin(&self, font: u32, cols: usize, rows: usize) -> (u32, u32) {
        let (cw, ch) = self.eink.cell(font);
        let (gw, gh) = self.eink.glass();
        (gw.saturating_sub(cols as u32 * cw) / 2, gh.saturating_sub(rows as u32 * ch) / 2)
    }

    /// Everything the wall holds, as one scene.
    fn scene(&mut self, wall: &Wall) -> Scene {
        let font = wall.font;
        let mut scene = Scene::default();
        let mut live = Vec::new();
        for layer in wall.stack() {
            match layer.surface {
                Surface::Cells(g) if layer.z == 0 => {
                    let mut c = Self::cells(font, g);
                    c.pictures = self.kitty.pictures(self.eink.cell(font), self.origin(font, c.cols, c.rows));
                    scene.base = Some(c);
                }
                Surface::Cells(g) => scene.layers.push(Layer { id: id_of(layer.name), show: Show::Cells(Self::cells(font, g)) }),
                Surface::Video(Some(f)) => {
                    live.push(f.seq);
                    if !self.decoded.contains_key(&f.seq) {
                        let t = Instant::now();
                        match decode::rgba(f) {
                            Ok(px) => {
                                self.decoded.insert(f.seq, (Arc::new(px.px), px.w, px.h));
                            }
                            Err(e) => log!("layer {} frame {}: {e}", layer.name, f.seq),
                        }
                        self.decode_ms.insert(layer.z, t.elapsed().as_secs_f64() * 1000.0);
                    }
                    if let Some((px, w, h)) = self.decoded.get(&f.seq) {
                        let (x, y, bw, bh) = f.placed();
                        let p = Picture { rgba: px.clone(), w: *w, h: *h, at: (x, y), size: (bw, bh) };
                        scene.layers.push(Layer { id: id_of(layer.name), show: Show::Picture(p) });
                    }
                }
                Surface::Video(None) => {}
            }
        }
        self.decoded.retain(|seq, _| live.contains(seq));
        if scene.base.is_none() {
            scene.base = Some(self.status(wall));
        }
        if wall.stats.on && wall.connected && !wall.overlay_lines.is_empty() {
            let c = Self::panel(wall.cols, wall.rows, font, &wall.overlay_lines, &wall.stats.at);
            scene.layers.push(Layer { id: 0, show: Show::Cells(c) });
        }
        scene
    }

    /// The status page, while no sender is on the wall, as the painter's:
    /// what this display is, where to send to, its area, and when a sender
    /// was last seen. No clock, so it stands still on the paper.
    fn status(&self, wall: &Wall) -> Cells {
        let (cols, rows) = (wall.cols, wall.rows);
        let mut c = Cells::new(wall.font, cols, rows, Cell::BLANK);
        let x0 = cols.saturating_sub(60) / 2 + 3;
        let mut y = rows.saturating_sub(10) / 2;
        let x = x0 + 2;
        c.text(x, y, "bitwall", -1, -1, BOLD);
        c.text(x + 10, y, "no sender", 0x808080, -1, 0);
        y += 2;
        let send = format!("{}:{}", self.host.to_lowercase(), wall.name);
        let seen = wall.last_seen.clone().unwrap_or_else(|| "never this run".into());
        let pairs = [
            ("this display", format!("{}  {}", self.host, self.addr)),
            ("wall", wall.name.clone()),
            ("send to", send),
            ("font size", wall.font.to_string()),
            ("area", format!("{}x{} cells", cols, rows)),
            ("last seen", seen),
        ];
        for (k, v) in pairs {
            c.text(x0, y, &format!("{k:<14}"), 0x808080, -1, 0);
            c.text(x0 + 15, y, &v, -1, -1, 0);
            y += 1;
        }
        c
    }

    /// Lines in a box at a point of the grid, a cell in from the edge: the
    /// stats overlay, as a cells layer of its own.
    fn panel(cols: usize, rows: usize, font: u32, lines: &[String], at: &str) -> Cells {
        let mut c = Cells::new(font, cols, rows, Cell::UNDRAWN);
        let w = (lines.iter().map(|l| l.chars().count()).max().unwrap_or(0) + 2).min(cols);
        let h = (lines.len() + 2).min(rows);
        let x = if at.contains('w') {
            1
        } else if at.contains('e') {
            cols.saturating_sub(w + 1)
        } else {
            cols.saturating_sub(w) / 2
        };
        let y = if at.starts_with('n') {
            1
        } else if at.starts_with('s') {
            rows.saturating_sub(h + 1)
        } else {
            rows.saturating_sub(h) / 2
        };
        let ground = 0x101418;
        for dy in 0..h {
            for dx in 0..w {
                c.set(x + dx, y + dy, Cell::new(' ', -1, ground, 0));
            }
        }
        for (k, l) in lines.iter().enumerate().take(h.saturating_sub(2)) {
            let l: String = l.chars().take(w - 2).collect();
            c.text(x + 1, y + 1 + k, &l, 0xd8dee9, ground, 0);
        }
        c
    }

    /// A push done: kept for the measures, and said in the log when it was
    /// a clean one.
    fn pushed(&mut self, p: &Pushed) {
        self.pushes += 1;
        self.push_ms = Some(p.secs * 1000.0);
        let mut du = 0;
        let mut gc = 0;
        for (_, m) in &p.boxes {
            match m {
                Mode::Du => du += 1,
                _ => gc += 1,
            }
        }
        let what = if p.clean {
            "clean".to_string()
        } else {
            let mut w = Vec::new();
            if gc > 0 {
                w.push(format!("{gc}xGC16"));
            }
            if du > 0 {
                w.push(format!("{du}xDU"));
            }
            w.join(",")
        };
        if p.clean {
            log!("pushed clean in {:.0} ms", p.secs * 1000.0);
        }
        self.refresh = Some(what);
    }
}

fn size(v: &str) -> Option<(u32, u32)> {
    let (w, h) = v.split_once('x')?;
    let (w, h): (u32, u32) = (w.parse().ok()?, h.parse().ok()?);
    (w >= 16 && h >= 16 && w <= 8192 && h <= 8192).then_some((w, h))
}

fn wall_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && !s.starts_with(['_', '-'])
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

#[cfg(unix)]
fn on_signals() {
    extern "C" fn stop(_: libc::c_int) {
        STOP.store(true, Ordering::SeqCst);
        pipe::ring();
    }
    unsafe {
        libc::signal(libc::SIGTERM, stop as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, stop as *const () as libc::sighandler_t);
    }
}

#[cfg(not(unix))]
fn on_signals() {}

fn main() {
    let mut font: u32 = std::env::var("BITWALL_FONT").ok().and_then(|v| v.parse().ok()).unwrap_or(FONT);
    let mut options = Options::default();
    let mut vcom: Option<u16> = None;
    let mut presence: Option<String> = None;
    let mut panel = if cfg!(all(target_os = "linux", feature = "it8951")) { "it8951".to_string() } else { "file:eink-panel".to_string() };
    let mut glass = PANEL;
    let mut name = "paper".to_string();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let mut val = |what: &str| args.next().unwrap_or_else(|| fail(&format!("{a} wants {what}")));
        match a.as_str() {
            "--font" => font = val("a size").parse().unwrap_or_else(|_| fail("--font wants a size")),
            "--rotate" => {
                options.rotate = val("degrees").parse().ok().filter(|r| [0, 90, 180, 270].contains(r)).unwrap_or_else(|| fail("--rotate wants 0, 90, 180 or 270"))
            }
            "--mirror" => options.mirror = true,
            "--paper" => options.paper = true,
            "--screen" => options.paper = false,
            "--presence" => presence = Some(val("a presence name")),
            "--vcom" => {
                let v: f64 = val("volts").parse().unwrap_or_else(|_| fail("--vcom wants volts, -1.17"));
                vcom = Some((v.abs() * 1000.0).round() as u16);
            }
            "--panel" => panel = val("it8951 or file:DIR"),
            "--glass" => glass = size(&val("WxH")).unwrap_or_else(|| fail("--glass wants WxH")),
            "--cell" => {
                let f: u32 = val("a font").parse().unwrap_or_else(|_| fail("--cell wants a font"));
                let face = bitwall_eink::face::Face::new();
                let m = face.metrics(f);
                println!("{}x{}", m.cw, m.ch);
                return;
            }
            "-h" | "--help" => {
                println!("usage: bitwall-eink [--font <n>] [--rotate 0|90|180|270] [--mirror] [--vcom <volts>] [--presence <name>] [--panel it8951|file:DIR] [--glass WxH] [--paper|--screen] [wall]");
                return;
            }
            _ if a.starts_with('-') => fail(&format!("{a}: unknown option")),
            _ => name = a,
        }
    }
    if !wall_name(&name) {
        fail(&format!("{name}: not a wall's name"));
    }
    let file_dir = panel.strip_prefix("file:").map(PathBuf::from);
    let (the_panel, render) = match &file_dir {
        Some(dir) => (AnyPanel::File(FilePanel::new(dir, glass.0, glass.1).unwrap_or_else(|e| fail(&format!("{}: {e}", dir.display())))), "file"),
        None if panel == "it8951" => open_it8951(vcom),
        None => fail(&format!("{panel}: not a panel, it8951 or file:DIR")),
    };
    let (pw, ph) = the_panel.size();
    let eink = Eink::new(the_panel, options);
    let glyphs: Vec<&str> = ASK.iter().filter(|(_, cp)| eink.draws(*cp)).map(|(s, _)| *s).collect();
    let run = std::env::var("BITWALL_RUN").ok().filter(|v| !v.is_empty()).unwrap_or_else(pipe::run_dir);
    let (dir, door) = pipe::claim(&run, &name).unwrap_or_else(|e| fail(&e));
    let receiver = format!(
        "receiver bitwall-eink version={} started={}{}",
        env!("BITWALL_VERSION"),
        sys::unix_now(),
        sys::booted().map(|b| format!(" booted={b}")).unwrap_or_default()
    );
    let mut paper = Paper {
        eink,
        options,
        render,
        glyphs: spelled(&glyphs),
        host: sys::hostname(),
        addr: sys::address(),
        kitty: Kitty::default(),
        decoded: HashMap::new(),
        decode_ms: HashMap::new(),
        push_ms: None,
        refresh: None,
        pushes: 0,
    };
    let mut wall = Wall::new(&name, dir, door, &paper, font, receiver);
    if let Some(p) = &presence {
        wall.follow_presence(p);
    }
    on_signals();
    let (gw, gh) = paper.eink.glass();
    log!(
        "wall {name} in {run}: panel {render} {pw}x{ph}, glass {gw}x{gh} rotate {} mirror {} {}, font {} is {}x{}",
        options.rotate,
        options.mirror,
        if options.paper { "paper" } else { "screen" },
        wall.font,
        wall.cols,
        wall.rows
    );

    // A file panel takes another size as a glass with no screen does.
    let (tx, rx) = mpsc::channel::<(u32, u32)>();
    if file_dir.is_some() {
        std::thread::spawn(move || {
            for line in io::stdin().lock().lines() {
                let Ok(line) = line else { break };
                if let Some(s) = line.strip_prefix("glass ").and_then(|v| size(v.trim())) {
                    if tx.send(s).is_err() {
                        break;
                    }
                    pipe::ring();
                }
            }
        });
    }

    let mut base: Option<String> = None;
    let mut font_was = wall.font;
    let mut last_show = Instant::now() - Duration::from_secs(1);
    // Pushes failed in a row: after FAILS the controller is started again.
    let mut failed = 0u32;
    const FAILS: u32 = 2;
    loop {
        let now = Instant::now();
        let mut wait = Duration::from_millis(50);
        if let Some(due) = paper.eink.next() {
            wait = wait.min(due.saturating_duration_since(now));
        }
        pipe::wait(wait);
        if STOP.load(Ordering::SeqCst) {
            break;
        }
        while let Ok((w, h)) = rx.try_recv() {
            if let Some(dir) = &file_dir {
                match FilePanel::new(dir, w, h) {
                    Ok(p) => {
                        paper.eink = Eink::new(AnyPanel::File(p), paper.options);
                        paper.decoded.clear();
                        wall.glass_changed(&mut paper);
                        log!("glass {w}x{h}: {}x{} at font {}", wall.cols, wall.rows, wall.font);
                    }
                    Err(e) => log!("glass {w}x{h}: {e}"),
                }
            }
        }
        let now = Instant::now();
        wall.serve(now, &mut paper);

        // The base's kitty strings, read here; a refusal is said to it.
        let mut said = Vec::new();
        for (k, x, y) in wall.kitty_out.drain(..) {
            paper.kitty.string(&k, x, y, &mut said);
        }
        for line in said {
            wall.tell_base(&line);
        }

        // A sender attached, came back or another took the wall: a full
        // repaint, pushed clean.
        let holder = wall.base().filter(|_| wall.connected).map(|b| wall.sessions[b].name.clone());
        if holder != base {
            match &holder {
                Some(n) => {
                    log!("base {n} attached");
                    paper.eink.repaint(now);
                }
                None => log!("base {} gone", base.as_deref().unwrap_or("-")),
            }
            base = holder;
        }
        if wall.font != font_was {
            log!("font {}: {}x{} cells", wall.font, wall.cols, wall.rows);
            font_was = wall.font;
        }
        paper.eink.watching(!wall.dark());

        let touched = wall.sessions.iter().any(|s| s.grid.touched);
        let due = wall.frame_closed
            || wall.changed
            || paper.kitty.changed
            || (touched && now.duration_since(last_show) >= Duration::from_millis(100));
        if due {
            wall.frame_closed = false;
            wall.changed = false;
            paper.kitty.changed = false;
            for s in wall.sessions.iter_mut() {
                s.grid.touched = false;
            }
            let t = Instant::now();
            let scene = paper.scene(&wall);
            paper.eink.show(&scene, now);
            let ms = t.elapsed().as_secs_f64() * 1000.0;
            wall.draw_ms = Some(wall.draw_ms.map(|v| v * 0.8 + ms * 0.2).unwrap_or(ms));
            last_show = now;
        }
        match paper.eink.tick(Instant::now()) {
            Ok(Some(p)) => {
                failed = 0;
                paper.pushed(&p)
            }
            Ok(None) => {}
            Err(e) => {
                failed += 1;
                log!("push failed: {e}");
                if failed >= FAILS {
                    failed = 0;
                    match paper.eink.recover(Instant::now()) {
                        Ok(()) => log!("panel started again; the next push is clean"),
                        Err(e) => log!("panel did not start again: {e}"),
                    }
                }
            }
        }
    }
    log!("stopped after {} pushes", paper.pushes);
    wall.stop();
}

#[cfg(all(target_os = "linux", feature = "it8951"))]
fn open_it8951(vcom: Option<u16>) -> (AnyPanel, &'static str) {
    use bitwall_eink::panel::it8951::{Depth, It8951};
    let mut p = It8951::open(vcom, Depth::Eight).unwrap_or_else(|e| fail(&format!("it8951: {e}")));
    let v = p.vcom().unwrap_or(0);
    log!("it8951 {}x{} firmware {:?} lut {:?} vcom -{:.2} V", p.w, p.h, p.firmware, p.lut, v as f64 / 1000.0);
    (AnyPanel::It(p), "it8951")
}

#[cfg(not(all(target_os = "linux", feature = "it8951")))]
fn open_it8951(_vcom: Option<u16>) -> (AnyPanel, &'static str) {
    fail("it8951: built without the panel's driver, which is on Linux alone")
}
