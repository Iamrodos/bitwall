//! The grid a stream makes, read as bitwall/grid.py and the web page's
//! parser read it: a reset, a cursor move, colour runs, and APC strings,
//! which are commands, kitty graphics, or dropped. Not a terminal: that is
//! what keeps it affordable.

pub const BOLD: u8 = 1;
pub const ITALIC: u8 = 2;
pub const UNDER: u8 = 4;
pub const STRIKE: u8 = 8;
pub const REVERSE: u8 = 16;
pub const FAINT: u8 = 32;

/// A colour the sender left as the terminal's own.
pub const DEFAULT: i64 = -1;
/// What a picture paints a default in, as the page does.
pub const INK: i64 = 0xd0d0d0;
pub const PAPER: i64 = 0x000000;
/// The trailing half of a wide character; a cell a layer has not drawn.
pub const TAIL: u32 = 0;
pub const NONE: u32 = u32::MAX;

/// The xterm palette, as grid.py has it.
pub fn palette(i: u8) -> i64 {
    const BASE: [i64; 16] = [
        0x000000, 0xcd0000, 0x00cd00, 0xcdcd00, 0x0000ee, 0xcd00cd, 0x00cdcd, 0xe5e5e5, 0x7f7f7f, 0xff0000,
        0x00ff00, 0xffff00, 0x5c5cff, 0xff00ff, 0x00ffff, 0xffffff,
    ];
    let i = i as usize;
    if i < 16 {
        return BASE[i];
    }
    if i < 232 {
        let steps = [0i64, 95, 135, 175, 215, 255];
        let n = i - 16;
        return (steps[n / 36] << 16) | (steps[(n / 6) % 6] << 8) | steps[n % 6];
    }
    let v = 8 + (i as i64 - 232) * 10;
    (v << 16) | (v << 8) | v
}

/// Double width: the sender writes one cell and skips the next.
pub fn wide(cp: u32) -> bool {
    (0x1100..=0x115f).contains(&cp)
        || ((0x2e80..=0xa4cf).contains(&cp) && cp != 0x303f)
        || (0xac00..=0xd7a3).contains(&cp)
        || (0xf900..=0xfaff).contains(&cp)
        || (0xfe30..=0xfe6f).contains(&cp)
        || (0xff00..=0xff60).contains(&cp)
        || (0xffe0..=0xffe6).contains(&cp)
        || (0x1f300..=0x1f64f).contains(&cp)
        || (0x1f900..=0x1f9ff).contains(&cp)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Cell {
    pub ch: u32,
    pub fg: i64,
    pub bg: i64,
    pub at: u8,
}

impl Cell {
    pub const BLANK: Cell = Cell { ch: ' ' as u32, fg: DEFAULT, bg: DEFAULT, at: 0 };
    pub const UNDRAWN: Cell = Cell { ch: NONE, fg: DEFAULT, bg: DEFAULT, at: 0 };
}

/// Something in a stream that is not paint.
pub enum Said {
    /// The body of `ESC _ bitwall ... ESC \`, after `bitwall `.
    Command(String),
    /// A kitty graphics string, whole, and the cell it was sent at.
    Kitty(Vec<u8>, usize, usize),
}

#[derive(Clone, Copy, PartialEq)]
enum State {
    Ground,
    Esc,
    Csi,
    Apc,
    ApcEsc,
}

const COMMAND_MAX: usize = 4096;
const KITTY_MAX: usize = 512 << 10;

/// Cells as rows, and the parser that writes them. `layer` is a cells
/// layer above the base: a cell it has not drawn is NONE, and `ESC[2J`
/// takes everything it drew away.
pub struct Grid {
    pub cols: usize,
    pub rows: usize,
    pub cells: Vec<Cell>,
    pub layer: bool,
    x: usize,
    y: usize,
    fg: i64,
    bg: i64,
    at: u8,
    state: State,
    params: Vec<u8>,
    apc: Vec<u8>,
    apc_over: bool,
    utf8: Vec<u8>,
    /// Frames closed since asked: each bare `ESC[0m`.
    pub resets: usize,
    /// Whether a cell changed since asked.
    pub touched: bool,
}

impl Grid {
    pub fn new(cols: usize, rows: usize, layer: bool) -> Grid {
        let mut g = Grid {
            cols: 0,
            rows: 0,
            cells: Vec::new(),
            layer,
            x: 0,
            y: 0,
            fg: DEFAULT,
            bg: DEFAULT,
            at: 0,
            state: State::Ground,
            params: Vec::new(),
            apc: Vec::new(),
            apc_over: false,
            utf8: Vec::new(),
            resets: 0,
            touched: true,
        };
        g.resize(cols, rows);
        g
    }

    pub fn resize(&mut self, cols: usize, rows: usize) {
        self.cols = cols;
        self.rows = rows;
        let fill = if self.layer { Cell::UNDRAWN } else { Cell::BLANK };
        self.cells = vec![fill; cols * rows];
        self.touched = true;
    }

    /// `ESC[2J`: the base keeps its grid, as the page does; a layer's is
    /// taken back to nothing drawn.
    pub fn wipe(&mut self) {
        if self.layer {
            self.cells.iter_mut().for_each(|c| *c = Cell::UNDRAWN);
            self.touched = true;
        }
    }

    pub fn at(&self, x: usize, y: usize) -> &Cell {
        &self.cells[y * self.cols + x]
    }

    /// Read bytes into the grid, whatever piece of a stream they are; what
    /// is not paint comes back in `said`. `kitty` keeps kitty strings,
    /// which are otherwise dropped as any other APC that is not ours.
    pub fn feed(&mut self, data: &[u8], kitty: bool, said: &mut Vec<Said>) {
        for &b in data {
            match self.state {
                State::Ground => {
                    if b == 0x1b {
                        self.flush_utf8();
                        self.state = State::Esc;
                    } else if b < 0x20 || b == 0x7f {
                        self.flush_utf8();
                    } else {
                        self.text_byte(b);
                    }
                }
                State::Esc => match b {
                    b'[' => {
                        self.params.clear();
                        self.state = State::Csi;
                    }
                    b'_' => {
                        self.apc.clear();
                        self.apc_over = false;
                        self.state = State::Apc;
                    }
                    0x1b => {}
                    _ => self.state = State::Ground,
                },
                State::Csi => match b {
                    0x30..=0x3f => {
                        if self.params.len() < 256 {
                            self.params.push(b)
                        }
                    }
                    0x20..=0x2f => {}
                    0x40..=0x7e => {
                        self.state = State::Ground;
                        self.csi(b);
                    }
                    0x1b => self.state = State::Esc,
                    _ => self.state = State::Ground,
                },
                State::Apc => {
                    if b == 0x1b {
                        self.state = State::ApcEsc;
                    } else {
                        self.apc_byte(b);
                    }
                }
                State::ApcEsc => {
                    if b == b'\\' {
                        self.state = State::Ground;
                        self.apc_done(kitty, said);
                    } else {
                        self.apc_byte(0x1b);
                        if b == 0x1b {
                            continue;
                        }
                        self.apc_byte(b);
                        self.state = State::Apc;
                    }
                }
            }
        }
    }

    fn apc_byte(&mut self, b: u8) {
        if self.apc_over {
            return;
        }
        let limit = if self.apc.first() == Some(&b'G') { KITTY_MAX } else { COMMAND_MAX + 8 };
        if self.apc.len() >= limit {
            self.apc_over = true;
            self.apc.clear();
        } else {
            self.apc.push(b);
        }
    }

    fn apc_done(&mut self, kitty: bool, said: &mut Vec<Said>) {
        if self.apc_over {
            return;
        }
        if let Some(body) = self.apc.strip_prefix(b"bitwall ") {
            if body.len() <= COMMAND_MAX {
                said.push(Said::Command(String::from_utf8_lossy(body).into_owned()));
            }
        } else if kitty && self.apc.first() == Some(&b'G') {
            let mut whole = Vec::with_capacity(self.apc.len() + 4);
            whole.extend_from_slice(b"\x1b_");
            whole.extend_from_slice(&self.apc);
            whole.extend_from_slice(b"\x1b\\");
            said.push(Said::Kitty(whole, self.x, self.y));
        }
        self.apc.clear();
    }

    fn text_byte(&mut self, b: u8) {
        if b < 0x80 {
            self.flush_utf8();
            self.put(b as u32);
            return;
        }
        if b & 0xc0 == 0x80 && !self.utf8.is_empty() {
            self.utf8.push(b);
            let need = match self.utf8[0] {
                0xc0..=0xdf => 2,
                0xe0..=0xef => 3,
                _ => 4,
            };
            if self.utf8.len() == need {
                let c = std::str::from_utf8(&self.utf8).ok().and_then(|s| s.chars().next());
                self.utf8.clear();
                self.put(c.map(|c| c as u32).unwrap_or(0xfffd));
            }
            return;
        }
        self.flush_utf8();
        if (0xc2..=0xf4).contains(&b) {
            self.utf8.push(b);
        } else {
            self.put(0xfffd);
        }
    }

    fn flush_utf8(&mut self) {
        if !self.utf8.is_empty() {
            self.utf8.clear();
            self.put(0xfffd);
        }
    }

    fn put(&mut self, cp: u32) {
        let (x, y) = (self.x, self.y);
        if y >= self.rows || x >= self.cols {
            return;
        }
        let i = y * self.cols + x;
        self.cells[i] = Cell { ch: cp, fg: self.fg, bg: self.bg, at: self.at };
        if wide(cp) {
            if x + 1 < self.cols {
                self.cells[i + 1] = Cell { ch: TAIL, fg: self.fg, bg: self.bg, at: self.at };
            }
            self.x = x + 2;
        } else {
            self.x = x + 1;
        }
        self.touched = true;
    }

    fn csi(&mut self, fin: u8) {
        let params = std::mem::take(&mut self.params);
        let p = String::from_utf8_lossy(&params);
        match fin {
            b'm' => {
                if p == "0" {
                    self.resets += 1;
                }
                self.sgr(&p);
            }
            b'J' => self.wipe(),
            b'H' => {
                let mut a = p.split(';');
                let n = |s: Option<&str>| s.and_then(|s| s.parse::<usize>().ok()).filter(|&v| v > 0).unwrap_or(1);
                self.y = n(a.next()) - 1;
                self.x = n(a.next()) - 1;
            }
            _ => {}
        }
        self.params = params;
        self.params.clear();
    }

    fn sgr(&mut self, params: &str) {
        let ps: Vec<i64> = if params.is_empty() {
            vec![0]
        } else {
            params.split(';').map(|s| if s.bytes().all(|b| b.is_ascii_digit()) && !s.is_empty() { s.parse().unwrap_or(0) } else { 0 }).collect()
        };
        let raw: Vec<&str> = if params.is_empty() { vec!["0"] } else { params.split(';').collect() };
        let (mut fg, mut bg, mut at) = (self.fg, self.bg, self.at);
        let n = ps.len();
        let mut i = 0;
        while i < n {
            let v = ps[i];
            if v == 38 || v == 48 {
                let mode = raw.get(i + 1).copied().unwrap_or("");
                let mut col = DEFAULT;
                if mode == "2" && i + 4 < n {
                    col = ((ps[i + 2] & 255) << 16) | ((ps[i + 3] & 255) << 8) | (ps[i + 4] & 255);
                    i += 4;
                } else if mode == "5" && i + 2 < n {
                    col = palette((ps[i + 2] & 255) as u8);
                    i += 2;
                } else if mode == "6" && i + 5 < n {
                    // A colour with an alpha, as a cells layer above the
                    // base may send it: the alpha held above the colour's
                    // bits, opaque as the colour alone.
                    col = ((ps[i + 2] & 255) << 16) | ((ps[i + 3] & 255) << 8) | (ps[i + 4] & 255);
                    let a = ps[i + 5] & 255;
                    if a != 255 {
                        col |= (a + 1) << 24;
                    }
                    i += 5;
                } else {
                    i += 1;
                }
                if v == 38 {
                    fg = col
                } else {
                    bg = col
                }
            } else {
                match v {
                    0 => {
                        fg = DEFAULT;
                        bg = DEFAULT;
                        at = 0;
                    }
                    30..=37 => fg = palette((v - 30) as u8),
                    40..=47 => bg = palette((v - 40) as u8),
                    90..=97 => fg = palette((v - 82) as u8),
                    100..=107 => bg = palette((v - 92) as u8),
                    39 => fg = DEFAULT,
                    49 => bg = DEFAULT,
                    1 => at |= BOLD,
                    2 => at |= FAINT,
                    3 => at |= ITALIC,
                    4 => at |= UNDER,
                    7 => at |= REVERSE,
                    9 => at |= STRIKE,
                    22 => at &= !(BOLD | FAINT),
                    23 => at &= !ITALIC,
                    24 => at &= !UNDER,
                    27 => at &= !REVERSE,
                    29 => at &= !STRIKE,
                    _ => {}
                }
            }
            i += 1;
        }
        self.fg = fg;
        self.bg = bg;
        self.at = at;
    }

    /// A cell as the glass shows it: its character, ink and ground with
    /// reverse and the defaults resolved, and whether it is faint braille.
    fn look(c: &Cell) -> (u32, i64, i64, bool) {
        let mut ink = if c.fg == DEFAULT { INK } else { c.fg & 0xffffff };
        let mut ground = if c.bg == DEFAULT { PAPER } else { c.bg & 0xffffff };
        if c.at & REVERSE != 0 {
            std::mem::swap(&mut ink, &mut ground);
        }
        (c.ch, ink, ground, c.at & FAINT != 0 && c.ch != TAIL && (0x2800..=0x28ff).contains(&c.ch))
    }

    /// Each row as looks, for a mirror to diff against.
    pub fn looks(&self) -> Vec<Vec<(u32, i64, i64, bool)>> {
        (0..self.rows).map(|y| (0..self.cols).map(|x| Grid::look(self.at(x, y))).collect()).collect()
    }

    /// The base's grid as a sender would write it, as grid.py's picture()
    /// does: each row placed with a cursor move, colour set as truecolour
    /// only where it changes, ending with the bare reset. With `was`, only
    /// the cells that differ. Empty when nothing changed.
    pub fn picture(&self, was: Option<&Vec<Vec<(u32, i64, i64, bool)>>>) -> (Vec<u8>, Vec<Vec<(u32, i64, i64, bool)>>) {
        let now = self.looks();
        let mut out = String::new();
        let (mut ink, mut ground, mut dim) = (-2i64, -2i64, false);
        for (y, row) in now.iter().enumerate() {
            let before = was.and_then(|w| w.get(y));
            if before == Some(row) {
                continue;
            }
            let mut at = usize::MAX;
            for (x, &(c, fi, gr, fa)) in row.iter().enumerate() {
                if c == TAIL || before.map(|b| b.get(x) == Some(&(c, fi, gr, fa))).unwrap_or(false) {
                    continue;
                }
                if x != at {
                    out.push_str(&format!("\x1b[{};{}H", y + 1, x + 1));
                }
                if fa != dim {
                    out.push_str(if fa { "\x1b[2m" } else { "\x1b[22m" });
                    dim = fa;
                }
                if fi != ink {
                    out.push_str(&format!("\x1b[38;2;{};{};{}m", fi >> 16, (fi >> 8) & 255, fi & 255));
                    ink = fi;
                }
                if gr != ground {
                    out.push_str(&format!("\x1b[48;2;{};{};{}m", gr >> 16, (gr >> 8) & 255, gr & 255));
                    ground = gr;
                }
                out.push(char::from_u32(c).unwrap_or(' '));
                at = x + if x + 1 < self.cols && row[x + 1].0 == TAIL { 2 } else { 1 };
            }
        }
        if out.is_empty() {
            return (Vec::new(), now);
        }
        out.push_str("\x1b[0m");
        (out.into_bytes(), now)
    }

    /// A cells layer's grid as the painter's lgrid_picture() says it: each
    /// drawn cell placed and coloured, alpha as `38;6`/`48;6`, no ground as
    /// `49`, every cell not drawn left out.
    pub fn layer_picture(&self) -> Vec<u8> {
        fn colour(v: i64, base: i64) -> String {
            let rgb = v & 0xffffff;
            let (r, g, b) = (rgb >> 16, (rgb >> 8) & 255, rgb & 255);
            if v >> 24 != 0 {
                format!("\x1b[{};6;{r};{g};{b};{}m", base + 8, (v >> 24) - 1)
            } else {
                format!("\x1b[{};2;{r};{g};{b}m", base + 8)
            }
        }
        let mut out = String::new();
        let (mut ink, mut ground, mut dim) = (String::new(), String::new(), false);
        for y in 0..self.rows {
            let mut at = usize::MAX;
            for x in 0..self.cols {
                let c = self.at(x, y);
                if c.ch == NONE || c.ch == TAIL {
                    continue;
                }
                let mut ink_v = if c.fg == DEFAULT { INK } else { c.fg };
                let mut ground_v = if c.bg == DEFAULT { None } else { Some(c.bg) };
                if c.at & REVERSE != 0 {
                    let g = ground_v.unwrap_or(1 << 24);
                    ground_v = Some(ink_v);
                    ink_v = g;
                }
                let fi = colour(ink_v, 30);
                let gr = ground_v.map(|g| colour(g, 40)).unwrap_or_else(|| "\x1b[49m".into());
                if x != at {
                    out.push_str(&format!("\x1b[{};{}H", y + 1, x + 1));
                }
                if fi != ink {
                    out.push_str(&fi);
                    ink = fi;
                }
                if gr != ground {
                    out.push_str(&gr);
                    ground = gr;
                }
                let fa = c.at & FAINT != 0 && (0x2800..=0x28ff).contains(&c.ch);
                if fa != dim {
                    out.push_str(if fa { "\x1b[2m" } else { "\x1b[22m" });
                    dim = fa;
                }
                out.push(char::from_u32(c.ch).unwrap_or(' '));
                at = x + if x + 1 < self.cols && self.at(x + 1, y).ch == TAIL { 2 } else { 1 };
            }
        }
        out.push_str("\x1b[0m");
        out.into_bytes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paints_and_splits() {
        let mut g = Grid::new(10, 3, false);
        let mut said = Vec::new();
        g.feed(b"\x1b[0m\x1b[2;3H\x1b[38;2;255;0;0mhi\x1b_bitwall meta set name X\x1b\\\x1b[0m", false, &mut said);
        assert_eq!(g.at(2, 1).ch, 'h' as u32);
        assert_eq!(g.at(3, 1).fg, 0xff0000);
        assert_eq!(g.resets, 2);
        assert!(matches!(&said[0], Said::Command(c) if c == "meta set name X"));
    }

    #[test]
    fn a_command_cut_in_two() {
        let mut g = Grid::new(4, 1, false);
        let mut said = Vec::new();
        g.feed(b"ab\x1b_bitwall pi", false, &mut said);
        g.feed(b"ng\x1b\\c", false, &mut said);
        assert!(matches!(&said[0], Said::Command(c) if c == "ping"));
        assert_eq!(g.at(2, 0).ch, 'c' as u32);
    }

    #[test]
    fn utf8_cut_in_two() {
        let mut g = Grid::new(4, 1, false);
        let mut said = Vec::new();
        let b = "\u{2588}".as_bytes();
        g.feed(&b[..1], false, &mut said);
        g.feed(&b[1..], false, &mut said);
        assert_eq!(g.at(0, 0).ch, 0x2588);
    }
}
