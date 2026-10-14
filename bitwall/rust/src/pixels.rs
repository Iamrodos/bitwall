//! A picture in pixels as a frame of cells: each cell a tile of pixels,
//! drawn as the one character whose shape splits the tile best, in two
//! colours.
//!
//! The painting is the video sender's, `senders/games/cellfilm/player.py`,
//! which `senders/picture` paints with too, and for the same pixels this
//! makes the same cells, which `tests/pixels.rs` holds it to:
//!
//! | mode | tile | split | characters |
//! |---|---|---|---|
//! | `Flat` | 1x1 | none | a space on the pixel's colour |
//! | `HalfBlock` | 1x2 | top and bottom | `▀` (player `half`) |
//! | `Quadrant` | 2x2 | at the mean brightness | `▘`..`▟` (player `quad`) |
//! | `Sextant` | 2x3 | the best split | sextants, `▌` `▐` |
//! | `Octant` | 2x4 | the best split | octants and the 26 others |
//! | `Braille` | 2x4 | at the mean brightness | braille (player `braille`) |
//! | `Fine` | 2x4 | the best split | a block where one fits, braille else (player `fine`) |
//!
//! **Two colours a cell, and where the split falls.** Once a tile's pixels
//! are parted into ink and ground, the mean of each part is the colour
//! that leaves the least squared error, so the only question is the
//! parting. The mean split puts every pixel brighter than the tile's mean
//! brightness in the ink: one pass, and right often enough. The best split
//! sorts the tile by brightness and scores each of the N-1 places a
//! threshold can fall by the error it leaves (the between-groups sum of
//! squares, from prefix sums), which is the best of all 2^N partings,
//! since one not ordered by brightness is improved by swapping a dark ink
//! pixel for a bright ground one. Brightness is Rec. 601 luma in integers,
//! `(77r + 151g + 28b) >> 8`, and a tie in it keeps the tile's order.
//!
//! **A cell with nothing to split is a space.** One whose brightness
//! spreads less than `FLAT`, or whose split took every pixel or none, is a
//! space on the tile's mean colour, as is a half block whose two pixels
//! match: the same on the glass and fewer bytes on the wire. Every colour
//! is the picture's; `DEFAULT` is only the unused ink of a space, so a
//! picture's black is black, not the wall's own ground.
//!
//! **Sextants and octants are a pattern each.** Every pattern of a cell
//! cut 2x3 is a sextant or one of `▌ ▐`, and every pattern of one cut 2x4
//! an octant or one of the 26 other characters that draw it (AGENTS.md,
//! *What the receivers draw as geometry*), so their split is never bent to
//! fit a character. Bit `2r + c` of a pattern is row r, column c.
//!
//! **A wall that cannot draw a mode gets the next one down**:
//! `Mode::drawable` asks, as `Wall::can` answers, and steps
//! `Octant` -> `Sextant` -> `Quadrant` -> `HalfBlock` -> `Flat`,
//! `Fine` -> `Quadrant`, `Braille` -> `HalfBlock`.
//!
//! A picture is best made at the mode's own size, `cols * tw` by
//! `rows * th` (`Mode::tile`); one of another size is sampled to it,
//! nearest pixel.

use crate::frame::{DEFAULT, Frame};
use rayon::prelude::*;

/// How a cell draws its tile of pixels.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Mode {
    /// 1x1: a space on the pixel's colour.
    Flat,
    /// 1x2: `▀`, ink on top.
    HalfBlock,
    /// 2x2: quadrants, split at the mean brightness.
    Quadrant,
    /// 2x3: sextants, the best split.
    Sextant,
    /// 2x4: octants, the best split.
    Octant,
    /// 2x4: braille dots, split at the mean brightness.
    Braille,
    /// 2x4: the best split, as a block where one draws it and braille
    /// where none does.
    Fine,
}

/// Every mode, in the order the golden file numbers them.
pub const MODES: [Mode; 7] =
    [Mode::Flat, Mode::HalfBlock, Mode::Quadrant, Mode::Sextant, Mode::Octant, Mode::Braille, Mode::Fine];

/// The spread of brightness below which a tile is one colour
/// (player `FLAT`).
pub const FLAT: i32 = 10;

impl Mode {
    /// Pixels a cell, (across, down).
    pub fn tile(self) -> (usize, usize) {
        match self {
            Mode::Flat => (1, 1),
            Mode::HalfBlock => (1, 2),
            Mode::Quadrant => (2, 2),
            Mode::Sextant => (2, 3),
            Mode::Octant | Mode::Braille | Mode::Fine => (2, 4),
        }
    }

    /// Every character this mode may draw but the space, for `Wall::can`.
    pub fn glyphs(self) -> String {
        let table: &[u32] = match self {
            Mode::Flat => &[],
            Mode::HalfBlock => &[0x2580],
            Mode::Quadrant => &QUAD,
            Mode::Sextant => &SEXTANT,
            Mode::Octant => &OCTANT,
            Mode::Braille => &BRAILLE,
            Mode::Fine => &FINE,
        };
        // The empty and the full pattern are always drawn as a space.
        let n = table.len();
        table
            .iter()
            .enumerate()
            .filter(|&(m, _)| m != 0 && m + 1 != n)
            .filter_map(|(_, &cp)| char::from_u32(cp))
            .chain(if self == Mode::HalfBlock { Some('▀') } else { None })
            .collect()
    }

    /// The mode to try where a wall cannot draw this one.
    pub fn fallback(self) -> Option<Mode> {
        match self {
            Mode::Octant => Some(Mode::Sextant),
            Mode::Sextant | Mode::Fine => Some(Mode::Quadrant),
            Mode::Quadrant | Mode::Braille => Some(Mode::HalfBlock),
            Mode::HalfBlock => Some(Mode::Flat),
            Mode::Flat => None,
        }
    }

    /// This mode, or the first down its fallbacks whose characters the
    /// wall draws: `mode.drawable(&|s| w.can(s))`. `Flat` draws anywhere.
    pub fn drawable(self, can: &dyn Fn(&str) -> bool) -> Mode {
        let mut m = self;
        loop {
            if m == Mode::Flat || can(&m.glyphs()) {
                return m;
            }
            m = m.fallback().unwrap_or(Mode::Flat);
        }
    }
}

/// A picture, `w * h` pixels of RGB row by row, as `cols * rows` cells.
pub fn to_frame(rgb: &[u8], w: usize, h: usize, mode: Mode, cols: usize, rows: usize) -> Frame {
    let mut f = Frame::new(0, 0);
    to_frame_into(&mut f, rgb, w, h, mode, cols, rows);
    f
}

/// `to_frame` into a frame already there, which keeps its arrays when
/// the size is the same: a sender painting frame after frame allocates
/// nothing but two small sampling tables a call.
pub fn to_frame_into(f: &mut Frame, rgb: &[u8], w: usize, h: usize, mode: Mode, cols: usize, rows: usize) {
    assert!(rgb.len() >= w * h * 3, "pixels: {} bytes is not {w}x{h} RGB", rgb.len());
    let n = cols * rows;
    f.cols = cols;
    f.rows = rows;
    f.ch.resize(n, ' ' as u32);
    f.fg.resize(n, DEFAULT);
    f.bg.resize(n, DEFAULT);
    f.attrs.clear();
    f.attrs.resize(n, 0);
    if n == 0 {
        return;
    }
    if w == 0 || h == 0 {
        f.ch.fill(' ' as u32);
        f.fg.fill(DEFAULT);
        f.bg.fill(DEFAULT);
        return;
    }
    let (tw, th) = mode.tile();
    let src = Src::new(rgb, w, h, cols * tw, rows * th);
    f.ch.par_chunks_mut(cols)
        .zip(f.fg.par_chunks_mut(cols))
        .zip(f.bg.par_chunks_mut(cols))
        .enumerate()
        .for_each(|(cy, ((ch, fg), bg))| paint_row(mode, &src, cy, ch, fg, bg));
}

/// The picture, and where each pixel of the tile grid comes from in it:
/// byte offsets, a row's and a column's, which add to the pixel's.
struct Src<'a> {
    rgb: &'a [u8],
    xs: Vec<usize>,
    ys: Vec<usize>,
}

impl<'a> Src<'a> {
    fn new(rgb: &'a [u8], w: usize, h: usize, gw: usize, gh: usize) -> Src<'a> {
        let xs = (0..gw).map(|x| x * w / gw * 3).collect();
        let ys = (0..gh).map(|y| y * h / gh * w * 3).collect();
        Src { rgb, xs, ys }
    }

    #[inline(always)]
    fn px(&self, x: usize, y: usize) -> [u8; 3] {
        let o = self.ys[y] + self.xs[x];
        [self.rgb[o], self.rgb[o + 1], self.rgb[o + 2]]
    }
}

#[inline(always)]
fn luma(p: [u8; 3]) -> i32 {
    (p[0] as i32 * 77 + p[1] as i32 * 151 + p[2] as i32 * 28) >> 8
}

#[inline(always)]
fn colour(p: [u8; 3]) -> i32 {
    ((p[0] as i32) << 16) | ((p[1] as i32) << 8) | p[2] as i32
}

fn paint_row(mode: Mode, src: &Src, cy: usize, ch: &mut [u32], fg: &mut [i32], bg: &mut [i32]) {
    match mode {
        Mode::Flat => {
            for cx in 0..ch.len() {
                ch[cx] = ' ' as u32;
                fg[cx] = DEFAULT;
                bg[cx] = colour(src.px(cx, cy));
            }
        }
        Mode::HalfBlock => {
            for cx in 0..ch.len() {
                let top = src.px(cx, 2 * cy);
                let bot = src.px(cx, 2 * cy + 1);
                if top == bot {
                    ch[cx] = ' ' as u32;
                    fg[cx] = DEFAULT;
                } else {
                    ch[cx] = 0x2580;
                    fg[cx] = colour(top);
                }
                bg[cx] = colour(bot);
            }
        }
        Mode::Quadrant => tiles::<2, 2, 4>(src, cy, false, &QUAD, ch, fg, bg),
        Mode::Sextant => tiles::<2, 3, 6>(src, cy, true, &SEXTANT, ch, fg, bg),
        Mode::Octant => tiles::<2, 4, 8>(src, cy, true, &OCTANT, ch, fg, bg),
        Mode::Braille => tiles::<2, 4, 8>(src, cy, false, &BRAILLE, ch, fg, bg),
        Mode::Fine => tiles::<2, 4, 8>(src, cy, true, &FINE, ch, fg, bg),
    }
}

/// One row of cells of a two-colour mode. Pixel `r * TW + c` of a tile is
/// bit `r * TW + c` of its pattern.
#[allow(clippy::too_many_arguments)]
fn tiles<const TW: usize, const TH: usize, const N: usize>(
    src: &Src,
    cy: usize,
    best: bool,
    table: &[u32],
    ch: &mut [u32],
    fg: &mut [i32],
    bg: &mut [i32],
) {
    let mut px = [[0u8; 3]; N];
    let mut lum = [0i32; N];
    for cx in 0..ch.len() {
        for r in 0..TH {
            for c in 0..TW {
                let p = src.px(cx * TW + c, cy * TH + r);
                px[r * TW + c] = p;
                lum[r * TW + c] = luma(p);
            }
        }
        let (lo, hi) = lum.iter().fold((i32::MAX, i32::MIN), |(lo, hi), &l| (lo.min(l), hi.max(l)));
        let (c, f, b) = if hi - lo < FLAT {
            // Flat whatever the split: no need to look for one.
            (' ' as u32, DEFAULT, mean_colour::<N>(&px))
        } else {
            let mask = if best { best_split::<N>(&px, &lum) } else { mean_split::<N>(&lum) };
            two_colours::<N>(&px, mask, table)
        };
        ch[cx] = c;
        fg[cx] = f;
        bg[cx] = b;
    }
}

/// Ink is every pixel brighter than the tile's mean brightness.
#[inline(always)]
fn mean_split<const N: usize>(lum: &[i32; N]) -> u32 {
    let mean = lum.iter().sum::<i32>() / N as i32;
    let mut mask = 0;
    for (i, &l) in lum.iter().enumerate() {
        if l > mean {
            mask |= 1 << i;
        }
    }
    mask
}

/// Ink is the pixels above the brightness threshold that leaves the least
/// squared error, the first such where several tie.
#[inline(always)]
fn best_split<const N: usize>(px: &[[u8; 3]; N], lum: &[i32; N]) -> u32 {
    // The tile's pixels darkest first, equal ones in tile order: each
    // pixel's rank counted rather than sorted, which has no branch to
    // mispredict.
    let mut order = [0u8; N];
    for i in 0..N {
        let mut rank = 0;
        for j in 0..N {
            rank += ((lum[j] < lum[i]) | ((lum[j] == lum[i]) & (j < i))) as usize;
        }
        order[rank] = i as u8;
    }
    let mut tot = [0i64; 3];
    for p in px {
        for k in 0..3 {
            tot[k] += p[k] as i64;
        }
    }
    let mut dark = [0i64; 3];
    let mut best = f64::NEG_INFINITY;
    let mut split = 1;
    for nd in 1..N {
        let p = px[order[nd - 1] as usize];
        for k in 0..3 {
            dark[k] += p[k] as i64;
        }
        let mut dd = 0i64;
        let mut ll = 0i64;
        for k in 0..3 {
            let l = tot[k] - dark[k];
            dd += dark[k] * dark[k];
            ll += l * l;
        }
        let score = dd as f64 / nd as f64 + ll as f64 / (N - nd) as f64;
        if score > best {
            best = score;
            split = nd;
        }
    }
    let mut mask = 0;
    for &i in &order[split..] {
        mask |= 1 << i;
    }
    mask
}

/// The tile's mean colour.
#[inline(always)]
fn mean_colour<const N: usize>(px: &[[u8; 3]; N]) -> i32 {
    let mut tot = [0u32; 3];
    for p in px {
        for k in 0..3 {
            tot[k] += p[k] as u32;
        }
    }
    let n = N as u32;
    colour([(tot[0] / n) as u8, (tot[1] / n) as u8, (tot[2] / n) as u8])
}

/// The character, ink and ground for a tile parted by `mask`: each part's
/// mean colour, or a space on the whole tile's where the parting took
/// every pixel or none.
#[inline(always)]
fn two_colours<const N: usize>(px: &[[u8; 3]; N], mask: u32, table: &[u32]) -> (u32, i32, i32) {
    let nf = mask.count_ones();
    let nb = N as u32 - nf;
    if nf == 0 || nb == 0 {
        return (' ' as u32, DEFAULT, mean_colour::<N>(px));
    }
    let mut tot = [0u32; 3];
    let mut ink = [0u32; 3];
    for i in 0..N {
        let p = px[i];
        let on = (mask >> i) & 1;
        for k in 0..3 {
            tot[k] += p[k] as u32;
            ink[k] += p[k] as u32 * on;
        }
    }
    let f = [(ink[0] / nf) as u8, (ink[1] / nf) as u8, (ink[2] / nf) as u8];
    let g = [((tot[0] - ink[0]) / nb) as u8, ((tot[1] - ink[1]) / nb) as u8, ((tot[2] - ink[2]) / nb) as u8];
    (table[mask as usize], colour(f), colour(g))
}

// ------------------------------------------------------------- tables

/// Quadrants by pattern: bit 0 top left, 1 top right, 2 bottom left,
/// 3 bottom right.
pub const QUAD: [u32; 16] = [
    0x20, 0x2598, 0x259d, 0x2580, 0x2596, 0x258c, 0x259e, 0x259b, 0x2597, 0x259a, 0x2590, 0x259c, 0x2584, 0x2599,
    0x259f, 0x2588,
];

/// The patterns of a 2x4 cell that characters other than the octants
/// draw, and those characters (`senders/sysinfo`'s, the receivers').
const NOT_OCTANTS: [(u8, u32); 26] = [
    (0, 0x20),
    (1, 0x1CEA8),
    (2, 0x1CEAB),
    (3, 0x1FB82),
    (5, 0x2598),
    (10, 0x259D),
    (15, 0x2580),
    (20, 0x1FBE6),
    (40, 0x1FBE7),
    (63, 0x1FB85),
    (64, 0x1CEA3),
    (80, 0x2596),
    (85, 0x258C),
    (90, 0x259E),
    (95, 0x259B),
    (128, 0x1CEA0),
    (160, 0x2597),
    (165, 0x259A),
    (170, 0x2590),
    (175, 0x259C),
    (192, 0x2582),
    (240, 0x2584),
    (245, 0x2599),
    (250, 0x259F),
    (252, 0x2586),
    (255, 0x2588),
];

/// The one character that draws each pattern of a 2x4 cell.
pub const OCTANT: [u32; 256] = {
    let mut t = [0u32; 256];
    let (mut b, mut k, mut cp) = (0, 0, 0x1CD00);
    while b < 256 {
        if k < NOT_OCTANTS.len() && NOT_OCTANTS[k].0 as usize == b {
            t[b] = NOT_OCTANTS[k].1;
            k += 1;
        } else {
            t[b] = cp;
            cp += 1;
        }
        b += 1;
    }
    t
};

/// The one character that draws each pattern of a 2x3 cell.
pub const SEXTANT: [u32; 64] = {
    let mut t = [0u32; 64];
    let (mut b, mut cp) = (0, 0x1FB00);
    while b < 64 {
        t[b] = match b {
            0 => 0x20,
            21 => 0x258C,
            42 => 0x2590,
            63 => 0x2588,
            _ => {
                cp += 1;
                cp - 1
            }
        };
        b += 1;
    }
    t
};

/// Braille by 2x4 pattern: tile bit b is the dot `DOT[b]`.
pub const BRAILLE: [u32; 256] = {
    const DOT: [u32; 8] = [0, 3, 1, 4, 2, 5, 6, 7];
    let mut t = [0u32; 256];
    let mut m = 0;
    while m < 256 {
        let mut out = 0;
        let mut b = 0;
        while b < 8 {
            if m & (1 << b) != 0 {
                out |= 1 << DOT[b];
            }
            b += 1;
        }
        t[m] = 0x2800 + out;
        m += 1;
    }
    t
};

/// A quadrant where one draws a 2x4 pattern exactly, braille otherwise
/// (player `FINE_IX`).
pub const FINE: [u32; 256] = {
    let mut t = BRAILLE;
    let mut q = 0;
    while q < 16 {
        // Quadrant bit (TL, TR, BL, BR) as two rows of the 2x4 tile each.
        let mut m = 0;
        if q & 1 != 0 {
            m |= 0b0000_0101;
        }
        if q & 2 != 0 {
            m |= 0b0000_1010;
        }
        if q & 4 != 0 {
            m |= 0b0101_0000;
        }
        if q & 8 != 0 {
            m |= 0b1010_0000;
        }
        t[m] = QUAD[q];
        q += 1;
    }
    t
};
