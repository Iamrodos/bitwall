//! A frame as arrays, diffed and serialised (`bitwall/arrays.py`,
//! `bitwall/stream.py` `serialise`).
//!
//! A frame is four parallel arrays rather than a grid of cells, so a diff
//! is one pass comparing integers and the bytes are written straight into
//! one buffer: no cell is ever an object, and no escape is formatted
//! through a formatter. What leaves is byte for byte what Python writes
//! for the same arrays, which `tests/frame.rs` holds it to.

pub const BOLD: u8 = 1;
pub const ITALICS: u8 = 2;
pub const UNDERSCORE: u8 = 4;
pub const STRIKE: u8 = 8;
pub const REVERSE: u8 = 16;
pub const FAINT: u8 = 32;

/// The terminal's own colour.
pub const DEFAULT: i32 = -1;

/// How many unchanged cells a run carries rather than breaking: moving
/// the cursor costs about eight bytes, so a few cells resent are cheaper
/// (`stream.GAP`).
pub const GAP: usize = 6;

/// The colours a wall can show, from its `screen colour=`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Depth {
    True,
    Xterm256,
    Ansi16,
    /// The 24-step grey ramp of the 256 palette.
    Grey,
    /// One ink: black or white.
    Mono,
}

impl Depth {
    /// The depth a wall's `colour=` names. A name it does not know is
    /// truecolour, as Python takes one.
    pub fn from_name(name: &str) -> Depth {
        match name {
            "256" => Depth::Xterm256,
            "16" => Depth::Ansi16,
            "grey" => Depth::Grey,
            "mono" => Depth::Mono,
            _ => Depth::True,
        }
    }

    /// The name a wall says it by, `colour=`.
    pub fn name(self) -> &'static str {
        match self {
            Depth::True => "true",
            Depth::Xterm256 => "256",
            Depth::Ansi16 => "16",
            Depth::Grey => "grey",
            Depth::Mono => "mono",
        }
    }
}

/// One frame, row-major, `cols * rows` of each: a code point (0 for the
/// second half of a wide glyph), fg and bg as 0xRRGGBB or `DEFAULT`, and
/// the attribute bits.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub cols: usize,
    pub rows: usize,
    pub ch: Vec<u32>,
    pub fg: Vec<i32>,
    pub bg: Vec<i32>,
    pub attrs: Vec<u8>,
}

impl Frame {
    /// A blank frame: spaces in the default colours.
    pub fn new(cols: usize, rows: usize) -> Frame {
        let n = cols * rows;
        Frame {
            cols,
            rows,
            ch: vec![' ' as u32; n],
            fg: vec![DEFAULT; n],
            bg: vec![DEFAULT; n],
            attrs: vec![0; n],
        }
    }

    /// Text written from (x, y), clipped at the right edge.
    ///
    /// One code point a cell, and a double-width one two, its second cell
    /// 0 in the same colours. Width is the receivers' (`wide`), so the
    /// cursor here steps as theirs does.
    pub fn text(&mut self, x: usize, y: usize, s: &str, fg: i32, bg: i32, attrs: u8) {
        if y >= self.rows {
            return;
        }
        let row = y * self.cols;
        let mut x = x;
        for c in s.chars() {
            if x >= self.cols {
                break;
            }
            let cp = c as u32;
            let i = row + x;
            self.ch[i] = cp;
            self.fg[i] = fg;
            self.bg[i] = bg;
            self.attrs[i] = attrs;
            if wide(cp) {
                if x + 1 < self.cols {
                    self.ch[i + 1] = 0;
                    self.fg[i + 1] = fg;
                    self.bg[i + 1] = bg;
                    self.attrs[i + 1] = attrs;
                }
                x += 2;
            } else {
                x += 1;
            }
        }
    }

    fn same(&self, other: &Frame, i: usize) -> bool {
        self.ch[i] == other.ch[i]
            && self.fg[i] == other.fg[i]
            && self.bg[i] == other.bg[i]
            && self.attrs[i] == other.attrs[i]
    }
}

/// Whether a code point is drawn two cells wide: the ranges the web page
/// and `bitwall/grid.py` step the cursor by.
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

/// The bytes that take a wall showing `prev` (or nothing, or something
/// not known, where `full`) to showing `cur`: the same bytes
/// `bitwall.stream.serialise` makes, bare `ESC[0m` frame delimiters and
/// all. Empty where nothing changed and not `full`.
///
/// A `prev` of another shape is no help, and every cell is sent, though
/// without the opening reset that only `full` writes.
pub fn serialise(cur: &Frame, prev: Option<&Frame>, full: bool, depth: Depth) -> Vec<u8> {
    let mut out = Vec::new();
    serialise_into(&mut out, cur, prev, full, depth);
    out
}

/// `serialise`, appended to `out`, so a caller sending frame after frame
/// keeps one buffer and its capacity.
pub fn serialise_into(out: &mut Vec<u8>, cur: &Frame, prev: Option<&Frame>, full: bool, depth: Depth) {
    const RESET: &[u8] = b"\x1b[0m";
    let cols = cur.cols;
    let n = cols * cur.rows;
    let prev = match prev {
        Some(p) if !full && p.cols == cur.cols && p.rows == cur.rows => Some(p),
        _ => None,
    };
    if full {
        out.extend_from_slice(RESET);
    }
    if prev.is_none() {
        // A picture whose look changes every cell is about 38 bytes a
        // cell in truecolour, a page of text a few.
        out.reserve(n * 16 + 8);
    }
    let mut w = Writer::new(out, depth);
    let mut drew = false;
    for y in 0..cur.rows {
        let row = y * cols;
        let mut x = 0;
        while x < cols {
            if let Some(p) = prev {
                x = first_change(cur, p, row + x, row + cols) - row;
                if x == cols {
                    break;
                }
            }
            let start = x;
            let mut end = x + 1;
            if let Some(p) = prev {
                let mut gap = 0;
                let mut probe = x + 1;
                while probe < cols {
                    if cur.same(p, row + probe) {
                        gap += 1;
                        if gap > GAP {
                            break;
                        }
                    } else {
                        end = probe + 1;
                        gap = 0;
                    }
                    probe += 1;
                }
            } else {
                end = cols;
            }
            drew = true;
            w.room((end - start) * MAX_CELL + MAX_CURSOR);
            w.cursor(y + 1, start + 1);
            for i in row + start..row + end {
                let c = cur.ch[i];
                if c == 0 {
                    continue; // second half of a wide glyph
                }
                let look = (cur.attrs[i], cur.fg[i], cur.bg[i]);
                if w.look != Some(look) {
                    w.sgr(look);
                    w.look = Some(look);
                }
                w.char(c);
            }
            x = end;
        }
    }
    w.done();
    if drew || full {
        out.extend_from_slice(RESET);
    }
}

/// The first cell from `i` to `end` that differs between the frames, or
/// `end`. Sixteen cells are compared at a time without a branch, which
/// the compiler makes vector code, since most of a frame is unchanged.
#[inline]
fn first_change(cur: &Frame, p: &Frame, mut i: usize, end: usize) -> usize {
    const K: usize = 16;
    while i + K <= end {
        let (a, b) = (&cur.ch[i..i + K], &p.ch[i..i + K]);
        let (c, d) = (&cur.fg[i..i + K], &p.fg[i..i + K]);
        let (e, f) = (&cur.bg[i..i + K], &p.bg[i..i + K]);
        let (g, h) = (&cur.attrs[i..i + K], &p.attrs[i..i + K]);
        let mut m = 0u32;
        for k in 0..K {
            m |= (a[k] ^ b[k]) | (c[k] ^ d[k]) as u32 | (e[k] ^ f[k]) as u32 | (g[k] ^ h[k]) as u32;
        }
        if m != 0 {
            break;
        }
        i += K;
    }
    while i < end && cur.same(p, i) {
        i += 1;
    }
    i
}

/// A frame's bytes, written through a cursor into room made a run at a
/// time, so a piece is a store and an add rather than a vector's checks.
struct Writer<'a> {
    out: &'a mut Vec<u8>,
    ptr: *mut u8,
    pos: usize,
    cap: usize,
    /// The look last written, which carries on until another is.
    look: Option<(u8, i32, i32)>,
    reducer: Reducer,
}

/// Colours reduced to a depth, kept in a direct-mapped cache: a picture
/// repeats its colours far more than it has them.
struct Reducer {
    depth: Depth,
    slots: Vec<(i32, u32)>,
}

/// The attribute parameters by bits, in the order `stream._style` writes
/// them: `;1;2;3;4;9;7` as each is set.
const ATTR_PARAMS: [(u8, u8); 6] = [
    (BOLD, b'1'),
    (FAINT, b'2'),
    (ITALICS, b'3'),
    (UNDERSCORE, b'4'),
    (STRIKE, b'9'),
    (REVERSE, b'7'),
];

/// The 16 terminal colours as xterm has them (`stream._BASIC`).
const BASIC: [(i32, i32, i32); 16] = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
];

/// The xterm cube's six levels a channel.
const CUBE: [i32; 6] = [0, 95, 135, 175, 215, 255];

/// The most one drawn cell can take: the longest look, `ESC[0`, six
/// attributes and two truecolours, and four bytes of UTF-8, with room
/// for a four-byte write past the end.
const MAX_CELL: usize = 64;
/// The most a cursor move can take.
const MAX_CURSOR: usize = 48;

/// Each byte 0 to 255 in decimal, padded to four bytes, and its length.
const DEC: [([u8; 4], usize); 256] = {
    let mut t = [([0u8; 4], 0usize); 256];
    let mut v = 0;
    while v < 256 {
        t[v] = if v < 10 {
            ([b'0' + v as u8, 0, 0, 0], 1)
        } else if v < 100 {
            ([b'0' + (v / 10) as u8, b'0' + (v % 10) as u8, 0, 0], 2)
        } else {
            ([b'0' + (v / 100) as u8, b'0' + (v / 10 % 10) as u8, b'0' + (v % 10) as u8, 0], 3)
        };
        v += 1;
    }
    t
};

impl<'a> Writer<'a> {
    fn new(out: &'a mut Vec<u8>, depth: Depth) -> Writer<'a> {
        let (pos, cap, ptr) = (out.len(), out.capacity(), out.as_mut_ptr());
        Writer { out, ptr, pos, cap, look: None, reducer: Reducer { depth, slots: Vec::new() } }
    }

    /// Room for `n` bytes more, made before a run's writes, which then
    /// need no check each.
    fn room(&mut self, n: usize) {
        if self.cap - self.pos < n {
            self.done();
            self.out.reserve(n);
            self.cap = self.out.capacity();
            self.ptr = self.out.as_mut_ptr();
        }
    }

    /// What has been written, given to the vector.
    fn done(&mut self) {
        // SAFETY: every byte below `pos` has been written, and `pos` is
        // within the capacity `room` made.
        unsafe { self.out.set_len(self.pos) }
    }

    /// Bytes, into room `room` made.
    #[inline(always)]
    fn put(&mut self, s: &[u8]) {
        debug_assert!(self.pos + s.len() <= self.cap);
        // SAFETY: `room` reserved a run's worst case, `MAX_CELL` a cell
        // and `MAX_CURSOR` its move, before any of its writes.
        unsafe { std::ptr::copy_nonoverlapping(s.as_ptr(), self.ptr.add(self.pos), s.len()) }
        self.pos += s.len();
    }

    /// Four bytes written and the first `n` kept: a short piece without a
    /// copy of variable length.
    #[inline(always)]
    fn put4(&mut self, b: [u8; 4], n: usize) {
        debug_assert!(self.pos + 4 <= self.cap);
        // SAFETY: as `put`; the worst cases leave room for the four.
        unsafe { std::ptr::copy_nonoverlapping(b.as_ptr(), self.ptr.add(self.pos), 4) }
        self.pos += n;
    }

    #[inline(always)]
    fn byte(&mut self, b: u8) {
        self.put4([b, 0, 0, 0], 1);
    }

    #[inline(always)]
    fn num(&mut self, v: usize) {
        if v < 256 {
            let (d, n) = DEC[v];
            self.put4(d, n);
        } else {
            let (buf, i) = decimal(v);
            self.put(&buf[i..]);
        }
    }

    #[inline(always)]
    fn cursor(&mut self, row: usize, col: usize) {
        self.put4(*b"\x1b[\0\0", 2);
        self.num(row);
        self.byte(b';');
        self.num(col);
        self.byte(b'H');
    }

    #[inline(always)]
    fn char(&mut self, c: u32) {
        if c < 0x80 {
            self.byte(c as u8);
        } else {
            // A lone surrogate is no character, and Python would refuse
            // to encode it: it goes as the replacement.
            let ch = char::from_u32(c).unwrap_or('\u{fffd}');
            let mut buf = [0u8; 4];
            let n = ch.encode_utf8(&mut buf).len();
            self.put4(buf, n);
        }
    }

    /// `r;g;b` of a truecolour.
    #[inline(always)]
    fn rgb(&mut self, v: i32) {
        let (r, n) = DEC[((v >> 16) & 255) as usize];
        self.put4(r, n);
        self.byte(b';');
        let (g, n) = DEC[((v >> 8) & 255) as usize];
        self.put4(g, n);
        self.byte(b';');
        let (b, n) = DEC[(v & 255) as usize];
        self.put4(b, n);
    }

    /// One look's escape: `ESC[0`, its attributes, its colours.
    #[inline(always)]
    fn sgr(&mut self, (at, fg, bg): (u8, i32, i32)) {
        self.put4(*b"\x1b[0\0", 3);
        if at != 0 {
            for (bit, p) in ATTR_PARAMS {
                if at & bit != 0 {
                    self.put4([b';', p, 0, 0], 2);
                }
            }
        }
        self.byte(b';');
        self.colour(fg, 30);
        self.byte(b';');
        self.colour(bg, 40);
        self.byte(b'm');
    }

    /// One colour's parameters at this depth: `stream._colour_text` of
    /// `stream._reduce`, for a truecolour or the terminal's own.
    #[inline(always)]
    fn colour(&mut self, v: i32, base: usize) {
        if v < 0 {
            self.num(base + 9);
            return;
        }
        if self.reducer.depth == Depth::True {
            self.num(base + 8);
            self.put4(*b";2;\0", 3);
            self.rgb(v);
            return;
        }
        let got = self.reducer.reduce(v) as usize;
        match self.reducer.depth {
            Depth::Ansi16 if got >= 8 => {
                // The bright eight go as the truecolour they are, as
                // Python spells them; the low eight as palette indices.
                let (br, bg, bb) = BASIC[got];
                self.num(base + 8);
                self.put4(*b";2;\0", 3);
                self.rgb((br << 16) | (bg << 8) | bb);
            }
            Depth::Mono => self.num(base + got),
            _ => self.indexed(base, got),
        }
    }

    #[inline(always)]
    fn indexed(&mut self, base: usize, index: usize) {
        self.num(base + 8);
        self.put4(*b";5;\0", 3);
        self.num(index);
    }
}

/// A number of any size in decimal, at the end of the buffer from the
/// index returned.
#[inline(never)]
fn decimal(mut v: usize) -> ([u8; 20], usize) {
    let mut buf = [0u8; 20];
    let mut i = buf.len();
    while v > 0 {
        i -= 1;
        buf[i] = b'0' + (v % 10) as u8;
        v /= 10;
    }
    (buf, i)
}

impl Reducer {
    /// A colour as this depth has it: a 256 index, one of the 16, or
    /// for one ink 0 or 7 (`stream._reduce`).
    #[inline(never)]
    fn reduce(&mut self, v: i32) -> u32 {
        const SLOTS: usize = 1 << 12;
        if self.slots.is_empty() {
            self.slots = vec![(-1, 0); SLOTS];
        }
        let slot = ((v as u32).wrapping_mul(0x9e37_79b1) >> 20) as usize;
        let (key, got) = self.slots[slot];
        if key == v {
            return got;
        }
        let (r, g, b) = ((v >> 16) & 255, (v >> 8) & 255, v & 255);
        let got = match self.depth {
            Depth::Xterm256 => nearest_256(r, g, b) as u32,
            Depth::Grey => {
                let g = ((luma(r, g, b) - 8.0) / 10.0).round_ties_even();
                232 + g.clamp(0.0, 23.0) as u32
            }
            Depth::Ansi16 => {
                let mut best = 0;
                let mut far = i32::MAX;
                for (i, &(br, bg, bb)) in BASIC.iter().enumerate() {
                    let d = (br - r) * (br - r) + (bg - g) * (bg - g) + (bb - b) * (bb - b);
                    if d < far {
                        far = d;
                        best = i as u32;
                    }
                }
                best
            }
            Depth::Mono => {
                if luma(r, g, b) >= 128.0 { 7 } else { 0 }
            }
            Depth::True => unreachable!(),
        };
        self.slots[slot] = (v, got);
        got
    }
}

/// sRGB luminance, summed as Python's `sum` sums floats, with
/// Neumaier's compensation, so a value near a rounding boundary rounds to
/// the same grey.
fn luma(r: i32, g: i32, b: i32) -> f64 {
    let (mut f, mut c) = (0.0f64, 0.0f64);
    for x in [0.2126 * r as f64, 0.7152 * g as f64, 0.0722 * b as f64] {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    if c != 0.0 && c.is_finite() { f + c } else { f }
}

/// Each channel value's nearest cube level, the lower on a tie.
const LEVEL: [u8; 256] = {
    let mut t = [0u8; 256];
    let mut v = 0;
    while v < 256 {
        let mut best = 0;
        let mut i = 1;
        while i < 6 {
            if (CUBE[i] - v as i32).abs() < (CUBE[best] - v as i32).abs() {
                best = i;
            }
            i += 1;
        }
        t[v] = best as u8;
        v += 1;
    }
    t
};

/// The grey ramp's step for each sum of a colour's channels, worked out
/// as Python works it out, in floats.
fn grey_by_sum() -> &'static [u8; 766] {
    static T: std::sync::OnceLock<[u8; 766]> = std::sync::OnceLock::new();
    T.get_or_init(|| {
        let mut t = [0u8; 766];
        for (s, g) in t.iter_mut().enumerate() {
            *g = (((s as f64 / 3.0) - 8.0) / 10.0).round_ties_even().clamp(0.0, 23.0) as u8;
        }
        t
    })
}

/// The 256-colour index nearest a colour: the cube or the grey ramp,
/// whichever is strictly nearer, the cube on a tie (`stream._nearest_256`).
fn nearest_256(r: i32, g: i32, b: i32) -> usize {
    let (ri, gi, bi) = (LEVEL[r as usize] as usize, LEVEL[g as usize] as usize, LEVEL[b as usize] as usize);
    let (cr, cg, cb) = (CUBE[ri], CUBE[gi], CUBE[bi]);
    let grey = grey_by_sum()[(r + g + b) as usize] as i32;
    let level = 8 + 10 * grey;
    let far = |x: i32, y: i32, z: i32| (x - r) * (x - r) + (y - g) * (y - g) + (z - b) * (z - b);
    if far(level, level, level) < far(cr, cg, cb) {
        232 + grey as usize
    } else {
        16 + 36 * ri + 6 * gi + bi
    }
}
