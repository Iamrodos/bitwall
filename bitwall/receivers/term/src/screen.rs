//! The glass: the terminal the receiver runs in. The wall's layers are
//! composed into one grid, the base at the bottom, each cells layer over it
//! blended by its alpha, the overlay on top, and only the cells that differ
//! from what the terminal already shows are written to it.

use bitwall_receiver::grid::{palette, Cell, BOLD, DEFAULT, FAINT, INK, ITALIC, NONE, PAPER, REVERSE, STRIKE, TAIL, UNDER};
use bitwall_receiver::{Ask, Glass, Wall};
use std::io::Write;
use std::time::Instant;

#[derive(Clone, Copy, PartialEq)]
struct Look {
    ch: u32,
    fg: i64,
    bg: i64,
    at: u8,
}

impl Look {
    const BLANK: Look = Look { ch: ' ' as u32, fg: DEFAULT, bg: DEFAULT, at: 0 };
    /// Never shown, so everything is drawn again.
    const UNKNOWN: Look = Look { ch: NONE, fg: -2, bg: -2, at: 0 };
}

pub struct Screen {
    cols: usize,
    rows: usize,
    shown: Vec<Look>,
    pub host: String,
    pub addr: String,
    send_host: String,
    mouse: bool,
    /// The help box is up.
    pub helping: bool,
    /// The font the area is said to be drawn at, since a terminal cannot
    /// say, and whether it draws kitty graphics.
    font: u32,
    kitty: bool,
}

/// The terminal as the wall's glass: cells it cannot resize, truecolour,
/// no pixels the wall can see, and the keys and clicks of whoever is at it.
impl Glass for Screen {
    fn area(&self, _ask: Ask) -> (usize, usize, u32) {
        (self.cols, self.rows, self.font)
    }

    fn colour(&self) -> &str {
        "true"
    }

    fn rate(&self) -> u32 {
        60
    }

    fn render(&self) -> &str {
        "terminal"
    }

    fn kitty(&self) -> bool {
        self.kitty
    }

    fn accepts(&self) -> &str {
        "touch keyboard"
    }
}

/// `v`, which may carry an alpha as grid.rs keeps it, over `under`.
fn blend(v: i64, under: i64) -> i64 {
    let a = if v >> 24 == 0 { 255 } else { (v >> 24) - 1 };
    if a >= 255 {
        return v & 0xffffff;
    }
    let mix = |s: i64| {
        let (t, u) = ((v >> s) & 255, (under >> s) & 255);
        ((t * a + u * (255 - a)) / 255) << s
    };
    mix(16) | mix(8) | mix(0)
}

fn rgb(v: i64, default: i64) -> i64 {
    if v == DEFAULT {
        default
    } else {
        v & 0xffffff
    }
}

/// A cells layer's cell over what is beneath it: a cell it has not drawn,
/// or a space in the default colours, shows what is beneath; a ground left
/// unset is none; a colour with an alpha is blended.
fn over(c: &Cell, under: Look) -> Look {
    if c.ch == NONE {
        return under;
    }
    let (mut ink, mut ground) = (c.fg, if c.bg == DEFAULT { None } else { Some(c.bg) });
    if c.at & REVERSE != 0 {
        let g = ground.unwrap_or(DEFAULT);
        ground = Some(if ink == DEFAULT { INK } else { ink });
        ink = g;
    }
    let beneath = rgb(under.bg, PAPER);
    let bg = match ground {
        None => under.bg,
        Some(g) => blend(g, beneath),
    };
    if c.ch == ' ' as u32 || c.ch == TAIL && ground.is_none() {
        if ground.is_none() {
            return under;
        }
        // A ground with nothing on it: what is beneath shows through it as
        // far as its alpha lets it.
        let fg = if under.ch == ' ' as u32 { under.fg } else { blend(ground.unwrap(), rgb(under.fg, INK)) };
        let opaque = ground.unwrap() >> 24 == 0;
        return Look { ch: if opaque { ' ' as u32 } else { under.ch }, fg, bg, at: if opaque { 0 } else { under.at } };
    }
    let fg = if ink == DEFAULT { DEFAULT } else { blend(ink, rgb(bg, PAPER)) };
    Look { ch: c.ch, fg, bg, at: c.at & !(REVERSE | FAINT) }
}

impl Screen {
    pub fn new(cols: usize, rows: usize, font: u32, kitty: bool, host: String, addr: String) -> Screen {
        let send_host = host.to_lowercase();
        Screen { cols, rows, shown: vec![Look::UNKNOWN; cols * rows], host, addr, send_host, mouse: false, helping: false, font, kitty }
    }

    /// The alternate screen, the cursor hidden, keys as they are pressed.
    pub fn enter(&mut self) {
        let _ = crossterm::terminal::enable_raw_mode();
        let mut o = std::io::stdout();
        let _ = o.write_all(b"\x1b[?1049h\x1b[?25l\x1b[0m\x1b[2J");
        let _ = o.flush();
    }

    /// Everything given back as it was.
    pub fn leave(&mut self) {
        self.set_mouse(false);
        let mut o = std::io::stdout();
        let _ = o.write_all(b"\x1b[0m\x1b[?25h\x1b[?1049l");
        let _ = o.flush();
        let _ = crossterm::terminal::disable_raw_mode();
    }

    /// Clicks reported only while the base wants taps, so the terminal's
    /// own selection works otherwise.
    pub fn set_mouse(&mut self, on: bool) {
        if on == self.mouse {
            return;
        }
        self.mouse = on;
        let mut o = std::io::stdout();
        let _ = if on {
            crossterm::execute!(o, crossterm::event::EnableMouseCapture)
        } else {
            crossterm::execute!(o, crossterm::event::DisableMouseCapture)
        };
    }

    pub fn resize(&mut self, cols: usize, rows: usize) {
        self.cols = cols;
        self.rows = rows;
        self.shown = vec![Look::UNKNOWN; cols * rows];
        let mut o = std::io::stdout();
        let _ = o.write_all(b"\x1b[0m\x1b[2J");
    }

    /// The wall as it should show, every layer composed.
    fn compose(&self, wall: &Wall) -> Vec<Look> {
        let (cols, rows) = (self.cols, self.rows);
        if wall.dark() {
            return vec![Look { ch: ' ' as u32, fg: DEFAULT, bg: PAPER, at: 0 }; cols * rows];
        }
        let mut out = vec![Look::BLANK; cols * rows];
        match wall.base().filter(|_| wall.connected) {
            Some(b) => {
                let g = &wall.sessions[b].grid;
                for y in 0..rows.min(g.rows) {
                    for x in 0..cols.min(g.cols) {
                        let c = g.at(x, y);
                        out[y * cols + x] = Look { ch: c.ch, fg: c.fg, bg: c.bg, at: c.at & !FAINT };
                    }
                }
            }
            None => self.status(wall, &mut out),
        }
        for j in wall.layers() {
            let g = &wall.sessions[j].grid;
            for y in 0..rows.min(g.rows) {
                for x in 0..cols.min(g.cols) {
                    let i = y * cols + x;
                    out[i] = over(g.at(x, y), out[i]);
                }
            }
        }
        if wall.stats.on && wall.connected && !wall.overlay_lines.is_empty() {
            self.overlay(wall, &mut out);
        }
        if self.helping {
            self.help(&mut out);
        }
        // A wide character's tail is drawn by the character before it; one
        // whose head was covered is a space.
        for y in 0..rows {
            for x in 0..cols {
                let i = y * cols + x;
                if out[i].ch == TAIL && (x == 0 || !bitwall_receiver::grid::wide(out[i - 1].ch)) {
                    out[i].ch = ' ' as u32;
                }
                if bitwall_receiver::grid::wide(out[i].ch) && (x + 1 >= cols || out[i + 1].ch != TAIL) {
                    out[i].ch = ' ' as u32;
                }
            }
        }
        out
    }

    fn text(&self, out: &mut [Look], x: usize, y: usize, s: &str, fg: i64, at: u8) -> usize {
        let mut x = x;
        if y >= self.rows {
            return x;
        }
        for c in s.chars() {
            if x >= self.cols {
                break;
            }
            out[y * self.cols + x] = Look { ch: c as u32, fg, bg: DEFAULT, at };
            x += 1;
        }
        x
    }

    /// The status page, while no sender is on the wall, as the painter's
    /// and bitwall-shell's: what this display is, where to send to, its
    /// area, and when a sender was last seen.
    fn status(&self, wall: &Wall, out: &mut [Look]) {
        let x0 = self.cols.saturating_sub(60) / 2;
        let mut y = self.rows.saturating_sub(11) / 2;
        let label = palette(244);
        let value = palette(250);
        let dim = palette(240);
        let pair = |s: &Screen, out: &mut [Look], y: usize, k: &str, v: &str, fg: i64, at: u8| {
            let x = s.text(out, x0 + 3, y, &format!("{k:<14}"), label, 0);
            s.text(out, x + 1, y, v, fg, at);
        };
        y += 1;
        let x = self.text(out, x0 + 5, y, "bitwall", palette(5), BOLD);
        self.text(out, x + 3, y, "disconnected", dim, 0);
        y += 2;
        pair(self, out, y, "this display", &format!("{}  {}", self.host, self.addr), value, 0);
        pair(self, out, y + 1, "wall", &wall.name, value, 0);
        pair(self, out, y + 2, "send to", &format!("{}:{}", self.send_host, wall.name), value, 0);
        pair(self, out, y + 3, "font size", &wall.font.to_string(), value, 0);
        pair(self, out, y + 4, "area", &format!("{}x{} cells", wall.cols, wall.rows), value, 0);
        pair(self, out, y + 5, "stream", "no sender connected", palette(1), BOLD);
        let seen = wall.last_seen.clone().unwrap_or_else(|| "never this run".into());
        pair(self, out, y + 6, "last seen", &seen, value, 0);
        self.text(out, x0 + 3, y + 8, &bitwall_receiver::sys::clock(false), dim, 0);
    }

    /// The receiver's numbers over the grid, in a box at the point asked,
    /// a cell in from the edge.
    fn overlay(&self, wall: &Wall, out: &mut [Look]) {
        self.panel(&wall.overlay_lines, wall.stats.at.as_str(), out);
    }

    /// The viewer's keys, in the middle, while `?` holds it up.
    fn help(&self, out: &mut [Look]) {
        let lines: Vec<String> = [
            "bitwall -- keys",
            "",
            "?       this",
            "s       the wall's stats, for everyone watching",
            "esc     put this away",
            "ctrl+c  end the wall",
            "",
            "? s esc are the viewer's while no sender",
            "is listening to the keyboard",
        ]
        .iter()
        .map(|l| l.to_string())
        .collect();
        self.panel(&lines, "c", out);
    }

    /// Lines in a box of dark glass at a point of the grid, a cell in from
    /// the edge.
    fn panel(&self, lines: &[String], at: &str, out: &mut [Look]) {
        let w = (lines.iter().map(|l| l.chars().count()).max().unwrap_or(0) + 2).min(self.cols);
        let h = (lines.len() + 2).min(self.rows);
        let x = if at.contains('w') {
            1
        } else if at.contains('e') {
            self.cols.saturating_sub(w + 1)
        } else {
            self.cols.saturating_sub(w) / 2
        };
        let y = if at.starts_with('n') {
            1
        } else if at.starts_with('s') {
            self.rows.saturating_sub(h + 1)
        } else {
            self.rows.saturating_sub(h) / 2
        };
        // A dark glass three quarters opaque, as grid.rs keeps an alpha: the
        // picture shows through it dimmed, and the numbers stand on it.
        let glass = (192 << 24) | 0x101418;
        for dy in 0..h {
            for dx in 0..w {
                if y + dy < self.rows && x + dx < self.cols {
                    let i = (y + dy) * self.cols + x + dx;
                    let under = out[i];
                    let bg = blend(glass, rgb(under.bg, PAPER));
                    out[i] = if under.ch == ' ' as u32 || under.ch == TAIL {
                        Look { ch: under.ch, fg: DEFAULT, bg, at: 0 }
                    } else {
                        Look { ch: under.ch, fg: blend(glass, rgb(under.fg, INK)), bg, at: under.at & !BOLD }
                    };
                }
            }
        }
        for (k, l) in lines.iter().enumerate().take(h.saturating_sub(2)) {
            for (dx, c) in l.chars().take(w - 2).enumerate() {
                let i = (y + 1 + k) * self.cols + x + 1 + dx;
                let bg = out[i].bg;
                out[i] = Look { ch: c as u32, fg: 0xd8dee9, bg, at: 0 };
            }
        }
    }

    /// Write what changed. The time it took, in ms.
    pub fn draw(&mut self, wall: &mut Wall) -> f64 {
        let started = Instant::now();
        let now = self.compose(wall);
        let mut s = String::new();
        let mut cur: Option<(i64, i64, u8)> = None;
        let mut at = usize::MAX;
        for (i, l) in now.iter().enumerate() {
            if *l == self.shown[i] || l.ch == TAIL {
                continue;
            }
            let (x, y) = (i % self.cols, i / self.cols);
            if i != at {
                s.push_str(&format!("\x1b[{};{}H", y + 1, x + 1));
            }
            if cur != Some((l.fg, l.bg, l.at)) {
                s.push_str("\x1b[0");
                for (bit, code) in [(BOLD, ";1"), (ITALIC, ";3"), (UNDER, ";4"), (STRIKE, ";9"), (REVERSE, ";7")] {
                    if l.at & bit != 0 {
                        s.push_str(code);
                    }
                }
                for (v, base) in [(l.fg, 38), (l.bg, 48)] {
                    if v == DEFAULT {
                        s.push_str(&format!(";{}", base + 1));
                    } else {
                        s.push_str(&format!(";{base};2;{};{};{}", (v >> 16) & 255, (v >> 8) & 255, v & 255));
                    }
                }
                s.push('m');
                cur = Some((l.fg, l.bg, l.at));
            }
            s.push(char::from_u32(l.ch).unwrap_or(' '));
            let width = if bitwall_receiver::grid::wide(l.ch) { 2 } else { 1 };
            at = i + width;
            if width == 2 && i + 1 < now.len() {
                self.shown[i + 1] = now[i + 1];
            }
            self.shown[i] = *l;
        }
        let mut bytes = s.into_bytes();
        // The base's kitty strings, each at the cell it was sent at, for the
        // terminal to draw.
        for (k, x, y) in wall.kitty_out.drain(..) {
            bytes.extend_from_slice(format!("\x1b[0m\x1b[{};{}H", y + 1, x + 1).as_bytes());
            bytes.extend_from_slice(&k);
        }
        if !bytes.is_empty() {
            let mut o = std::io::stdout().lock();
            let _ = o.write_all(&bytes);
            let _ = o.flush();
        }
        started.elapsed().as_secs_f64() * 1000.0
    }
}

/// Whether the terminal draws kitty graphics: kitty's own question, a
/// one-pixel image asked about and not drawn, then the device attributes
/// every terminal answers, so the answer is known without waiting long.
/// The graphics reply before the attributes is a yes.
pub fn ask_kitty() -> bool {
    let said = ask(b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c", 500);
    said.contains("_Gi=31;OK")
}

#[cfg(windows)]
fn ask(query: &[u8], ms: u64) -> String {
    use windows_sys::Win32::System::Console::*;
    use windows_sys::Win32::System::Threading::WaitForSingleObject;
    unsafe {
        let input = GetStdHandle(STD_INPUT_HANDLE);
        let mut mode = 0;
        if GetConsoleMode(input, &mut mode) == 0 {
            return String::new();
        }
        SetConsoleMode(input, ENABLE_VIRTUAL_TERMINAL_INPUT);
        let mut o = std::io::stdout();
        let _ = o.write_all(query);
        let _ = o.flush();
        let mut said = String::new();
        let until = Instant::now() + std::time::Duration::from_millis(ms);
        let mut recs: [INPUT_RECORD; 64] = std::mem::zeroed();
        while Instant::now() < until {
            WaitForSingleObject(input, 20);
            let mut n = 0;
            if GetNumberOfConsoleInputEvents(input, &mut n) == 0 || n == 0 {
                continue;
            }
            let mut got = 0;
            if ReadConsoleInputW(input, recs.as_mut_ptr(), recs.len() as u32, &mut got) == 0 {
                break;
            }
            for r in &recs[..got as usize] {
                if r.EventType as u32 == KEY_EVENT as u32 {
                    let k = r.Event.KeyEvent;
                    let c = k.uChar.UnicodeChar;
                    if k.bKeyDown != 0 && c != 0 {
                        if let Some(c) = char::from_u32(c as u32) {
                            said.push(c);
                        }
                    }
                }
            }
            if let Some(da) = said.find("\x1b[?") {
                if said[da..].contains('c') {
                    break;
                }
            }
        }
        SetConsoleMode(input, mode);
        said
    }
}

#[cfg(unix)]
fn ask(query: &[u8], ms: u64) -> String {
    let _ = crossterm::terminal::enable_raw_mode();
    let mut o = std::io::stdout();
    let _ = o.write_all(query);
    let _ = o.flush();
    let mut said = Vec::new();
    let until = Instant::now() + std::time::Duration::from_millis(ms);
    while Instant::now() < until {
        let mut p = libc::pollfd { fd: 0, events: libc::POLLIN, revents: 0 };
        if unsafe { libc::poll(&mut p, 1, 20) } <= 0 {
            continue;
        }
        let mut b = [0u8; 512];
        let n = unsafe { libc::read(0, b.as_mut_ptr() as _, b.len()) };
        if n <= 0 {
            break;
        }
        said.extend_from_slice(&b[..n as usize]);
        let s = String::from_utf8_lossy(&said);
        if let Some(da) = s.find("\x1b[?") {
            if s[da..].contains('c') {
                break;
            }
        }
    }
    let _ = crossterm::terminal::disable_raw_mode();
    String::from_utf8_lossy(&said).into_owned()
}
