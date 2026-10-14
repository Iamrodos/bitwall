//! The characters drawn as geometry rather than type, exactly as the
//! painter (`bitwall-paint.c`) draws them, so a wall looks the same on
//! paper as on a Pi's glass: blocks, eighths, quadrants, sextants,
//! octants, the quarter bars and fractions as rectangles; box lines light,
//! heavy and double from the middle of the cell to its edges; rounded
//! corners as quarter ellipses; the diagonal halves; shades as a mix of
//! ink and ground; braille as dots on an even grid; and the LED emitter.
//! Each is a coverage mask of one cell, as a glyph is, so the cell is
//! drawn the same way whichever made it.

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::face::{Mask, Metrics};

pub const BRAILLE: u32 = 0x2800;
const TRIANGLE: u32 = 0x25E2;
const ARC: u32 = 0x256D;
const LEDS: u32 = 0x25AA;
const LEGACY: u32 = 0x1FB00;
const OCTANT: u32 = 0x1CD00;
const QUARTER: u32 = 0x1CEA0;

/// A pixel rectangle in a cell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct R {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

const UP: usize = 0;
const RIGHT: usize = 1;
const DOWN: usize = 2;
const LEFT: usize = 3;

/// Box drawing U+2500-257F as arm weights [up, right, down, left]: 0 none,
/// 1 light, 2 heavy, 3 double. All zero is the font's.
fn arms(cp: u32) -> [u8; 4] {
    const L: u8 = 1;
    const H: u8 = 2;
    const D: u8 = 3;
    match cp.wrapping_sub(0x2500) {
        0x00 => [0, L, 0, L], 0x01 => [0, H, 0, H],
        0x02 => [L, 0, L, 0], 0x03 => [H, 0, H, 0],
        0x0c => [0, L, L, 0], 0x0d => [0, H, L, 0],
        0x0e => [0, L, H, 0], 0x0f => [0, H, H, 0],
        0x10 => [0, 0, L, L], 0x11 => [0, 0, L, H],
        0x12 => [0, 0, H, L], 0x13 => [0, 0, H, H],
        0x14 => [L, L, 0, 0], 0x15 => [L, H, 0, 0],
        0x16 => [H, L, 0, 0], 0x17 => [H, H, 0, 0],
        0x18 => [L, 0, 0, L], 0x19 => [L, 0, 0, H],
        0x1a => [H, 0, 0, L], 0x1b => [H, 0, 0, H],
        0x1c => [L, L, L, 0], 0x1d => [L, H, L, 0],
        0x1e => [H, L, L, 0], 0x1f => [L, L, H, 0],
        0x20 => [H, L, H, 0], 0x21 => [H, H, L, 0],
        0x22 => [L, H, H, 0], 0x23 => [H, H, H, 0],
        0x24 => [L, 0, L, L], 0x25 => [L, 0, L, H],
        0x26 => [H, 0, L, L], 0x27 => [L, 0, H, L],
        0x28 => [H, 0, H, L], 0x29 => [H, 0, L, H],
        0x2a => [L, 0, H, H], 0x2b => [H, 0, H, H],
        0x2c => [0, L, L, L], 0x2d => [0, L, L, H],
        0x2e => [0, H, L, L], 0x2f => [0, H, L, H],
        0x30 => [0, L, H, L], 0x31 => [0, L, H, H],
        0x32 => [0, H, H, L], 0x33 => [0, H, H, H],
        0x34 => [L, L, 0, L], 0x35 => [L, L, 0, H],
        0x36 => [L, H, 0, L], 0x37 => [L, H, 0, H],
        0x38 => [H, L, 0, L], 0x39 => [H, L, 0, H],
        0x3a => [H, H, 0, L], 0x3b => [H, H, 0, H],
        0x3c => [L, L, L, L], 0x3d => [L, L, L, H],
        0x3e => [L, H, L, L], 0x3f => [L, H, L, H],
        0x40 => [H, L, L, L], 0x41 => [L, L, H, L],
        0x42 => [H, L, H, L], 0x43 => [H, L, L, H],
        0x44 => [H, H, L, L], 0x45 => [L, L, H, H],
        0x46 => [L, H, H, L], 0x47 => [H, H, L, H],
        0x48 => [L, H, H, H], 0x49 => [H, L, H, H],
        0x4a => [H, H, H, L], 0x4b => [H, H, H, H],
        0x50 => [0, D, 0, D], 0x51 => [D, 0, D, 0],
        0x52 => [0, D, L, 0], 0x53 => [0, L, D, 0],
        0x54 => [0, D, D, 0], 0x55 => [0, 0, L, D],
        0x56 => [0, 0, D, L], 0x57 => [0, 0, D, D],
        0x58 => [L, D, 0, 0], 0x59 => [D, L, 0, 0],
        0x5a => [D, D, 0, 0], 0x5b => [L, 0, 0, D],
        0x5c => [D, 0, 0, L], 0x5d => [D, 0, 0, D],
        0x5e => [L, D, L, 0], 0x5f => [D, L, D, 0],
        0x60 => [D, D, D, 0], 0x61 => [L, 0, L, D],
        0x62 => [D, 0, D, L], 0x63 => [D, 0, D, D],
        0x64 => [0, D, L, D], 0x65 => [0, L, D, L],
        0x66 => [0, D, D, D], 0x67 => [L, D, 0, D],
        0x68 => [D, L, 0, L], 0x69 => [D, D, 0, D],
        0x6a => [L, D, L, D], 0x6b => [D, L, D, L],
        0x6c => [D, D, D, D],
        0x74 => [0, 0, 0, L], 0x75 => [L, 0, 0, 0],
        0x76 => [0, L, 0, 0], 0x77 => [0, 0, L, 0],
        0x78 => [0, 0, 0, H], 0x79 => [H, 0, 0, 0],
        0x7a => [0, H, 0, 0], 0x7b => [0, 0, H, 0],
        0x7c => [0, H, 0, L], 0x7d => [L, 0, H, 0],
        0x7e => [0, L, 0, H], 0x7f => [H, 0, L, 0],
        _ => [0; 4],
    }
}

type Frac = (f64, f64, f64, f64);

/// The blocks as fractions of a cell, built once: shapes_init and
/// legacy_init in the painter.
fn fracs() -> &'static HashMap<u32, Vec<Frac>> {
    static T: OnceLock<HashMap<u32, Vec<Frac>>> = OnceLock::new();
    T.get_or_init(|| {
        let mut t: HashMap<u32, Vec<Frac>> = HashMap::new();
        let mut add = |cp: u32, x: f64, y: f64, w: f64, h: f64| t.entry(cp).or_default().push((x, y, w, h));
        add(0x2588, 0.0, 0.0, 1.0, 1.0);
        add(0x2580, 0.0, 0.0, 1.0, 0.5);
        add(0x2584, 0.0, 0.5, 1.0, 0.5);
        add(0x258c, 0.0, 0.0, 0.5, 1.0);
        add(0x2590, 0.5, 0.0, 0.5, 1.0);
        add(0x2596, 0.0, 0.5, 0.5, 0.5);
        add(0x2597, 0.5, 0.5, 0.5, 0.5);
        add(0x2598, 0.0, 0.0, 0.5, 0.5);
        add(0x2599, 0.0, 0.0, 0.5, 1.0);
        add(0x2599, 0.5, 0.5, 0.5, 0.5);
        add(0x259a, 0.0, 0.0, 0.5, 0.5);
        add(0x259a, 0.5, 0.5, 0.5, 0.5);
        add(0x259b, 0.0, 0.0, 1.0, 0.5);
        add(0x259b, 0.0, 0.5, 0.5, 0.5);
        add(0x259c, 0.0, 0.0, 1.0, 0.5);
        add(0x259c, 0.5, 0.5, 0.5, 0.5);
        add(0x259d, 0.5, 0.0, 0.5, 0.5);
        add(0x259e, 0.5, 0.0, 0.5, 0.5);
        add(0x259e, 0.0, 0.5, 0.5, 0.5);
        add(0x259f, 0.5, 0.0, 0.5, 1.0);
        add(0x259f, 0.0, 0.5, 0.5, 0.5);
        add(LEDS, 0.1, 0.3, 0.8, 0.4);
        for i in 1..8u32 {
            if i == 4 {
                continue;
            }
            let f = i as f64 / 8.0;
            add(0x2580 + i, 0.0, 1.0 - f, 1.0, f);
            add(0x2588 + i, 0.0, 0.0, 1.0 - f, 1.0);
        }
        add(0x2594, 0.0, 0.0, 1.0, 1.0 / 8.0);
        add(0x2595, 7.0 / 8.0, 0.0, 1.0 / 8.0, 1.0);

        // A pattern of a cell cut two across and `rows` down; rows alike
        // and next to each other are one rectangle.
        let grid = |cp: u32, bits: u32, rows: u32, add: &mut dyn FnMut(u32, f64, f64, f64, f64)| {
            let mut r = 0;
            while r < rows {
                let row = bits >> (2 * r) & 3;
                let mut end = r + 1;
                while end < rows && (bits >> (2 * end) & 3) == row {
                    end += 1;
                }
                if row != 0 {
                    add(
                        cp,
                        if row == 2 { 0.5 } else { 0.0 },
                        r as f64 / rows as f64,
                        if row == 3 { 1.0 } else { 0.5 },
                        (end - r) as f64 / rows as f64,
                    );
                }
                r = end;
            }
        };
        const DRAWN: [u32; 26] = [
            0x00, 0x01, 0x02, 0x03, 0x05, 0x0a, 0x0f, 0x14, 0x28, 0x3f, 0x40, 0x50, 0x55, 0x5a, 0x5f, 0x80, 0xa0,
            0xa5, 0xaa, 0xaf, 0xc0, 0xf0, 0xf5, 0xfa, 0xfc, 0xff,
        ];
        let mut cp = LEGACY;
        for b in 1..63 {
            if b != 21 && b != 42 {
                grid(cp, b, 3, &mut add);
                cp += 1;
            }
        }
        let mut cp = OCTANT;
        for b in 0..256 {
            if !DRAWN.contains(&b) {
                grid(cp, b, 4, &mut add);
                cp += 1;
            }
        }
        const BARS: [[u8; 4]; 16] = [
            [2, 3, 2, 1], [1, 3, 3, 1], [0, 3, 3, 1], [0, 3, 2, 1],
            [0, 2, 1, 2], [0, 1, 1, 3], [0, 0, 1, 3], [0, 0, 1, 2],
            [0, 0, 2, 1], [0, 0, 3, 1], [1, 0, 3, 1], [2, 0, 2, 1],
            [3, 0, 1, 2], [3, 0, 1, 3], [3, 1, 1, 3], [3, 2, 1, 2],
        ];
        for (i, b) in BARS.iter().enumerate() {
            add(QUARTER + i as u32, b[0] as f64 / 4.0, b[1] as f64 / 4.0, b[2] as f64 / 4.0, b[3] as f64 / 4.0);
        }
        let e = 1.0 / 8.0;
        for i in 0..6u32 {
            add(0x1FB70 + i, (i + 1) as f64 / 8.0, 0.0, e, 1.0);
            add(0x1FB76 + i, 0.0, (i + 1) as f64 / 8.0, 1.0, e);
        }
        add(0x1FB7C, 0.0, 0.0, e, 7.0 * e);
        add(0x1FB7C, 0.0, 7.0 * e, 1.0, e);
        add(0x1FB7D, 0.0, 0.0, 1.0, e);
        add(0x1FB7D, 0.0, e, e, 7.0 * e);
        add(0x1FB7E, 0.0, 0.0, 1.0, e);
        add(0x1FB7E, 7.0 * e, e, e, 7.0 * e);
        add(0x1FB7F, 7.0 * e, 0.0, e, 7.0 * e);
        add(0x1FB7F, 0.0, 7.0 * e, 1.0, e);
        add(0x1FBE4, 0.25, 0.0, 0.5, 0.5);
        add(0x1FBE5, 0.25, 0.5, 0.5, 0.5);
        add(0x1FBE6, 0.0, 0.25, 0.5, 0.5);
        add(0x1FBE7, 0.5, 0.25, 0.5, 0.5);
        add(0x1FB80, 0.0, 0.0, 1.0, e);
        add(0x1FB80, 0.0, 7.0 * e, 1.0, e);
        for i in 0..4 {
            add(0x1FB81, 0.0, (if i == 3 { 7 } else { 2 * i }) as f64 / 8.0, 1.0, e);
        }
        const PARTS: [f64; 5] = [2.0, 3.0, 5.0, 6.0, 7.0];
        for (i, p) in PARTS.iter().enumerate() {
            add(0x1FB82 + i as u32, 0.0, 0.0, 1.0, p / 8.0);
            add(0x1FB87 + i as u32, 1.0 - p / 8.0, 0.0, p / 8.0, 1.0);
        }
        t
    })
}

/// A fraction of a cell in pixels, each edge rounded on its own so two
/// halves meet on the same pixel and nothing between them is unpainted.
fn frac_px(f: &Frac, cw: u32, ch: u32) -> R {
    let (cw, ch) = (cw as f64, ch as f64);
    let x0 = (f.0 * cw + 0.5) as i32;
    let x1 = ((f.0 + f.2) * cw + 0.5) as i32;
    let y0 = (f.1 * ch + 0.5) as i32;
    let y1 = ((f.1 + f.3) * ch + 0.5) as i32;
    R { x: x0, y: y0, w: (x1 - x0).max(1), h: (y1 - y0).max(1) }
}

/// A braille pattern's dots: every dot the same whole number of pixels,
/// 0.6 of its slot, centred in it.
pub fn dots(bits: u32, cw: u32, ch: u32) -> Vec<R> {
    const COL: [i32; 8] = [0, 0, 0, 1, 1, 1, 0, 1];
    const ROW: [i32; 8] = [0, 1, 2, 0, 1, 2, 3, 3];
    let dw = ((cw as f64 * 0.3 + 0.5) as i32).max(1);
    let dh = ((ch as f64 * 0.15 + 0.5) as i32).max(1);
    (0..8)
        .filter(|b| bits & (1 << b) != 0)
        .map(|b| R {
            x: ((COL[b] as f64 * 0.5 + 0.25) * cw as f64 + 0.5) as i32 - dw / 2,
            y: ((ROW[b] as f64 * 0.25 + 0.125) * ch as f64 + 0.5) as i32 - dh / 2,
            w: dw,
            h: dh,
        })
        .collect()
}

/// Whether pixel (x, y) is in triangle `which` (◢ ◣ ◤ ◥), judged by its
/// centre, so a triangle and the one completing its cell share every pixel.
fn tri_in(which: u32, x: u32, y: u32, cw: u32, ch: u32) -> bool {
    let u = (x as f64 + 0.5) / cw as f64;
    let v = (y as f64 + 0.5) / ch as f64;
    match which {
        0 => u + v >= 1.0,
        1 => v >= u,
        2 => u + v < 1.0,
        _ => v < u,
    }
}

fn weights(size: u32) -> (i32, i32) {
    let light = ((size as f64 / 12.0 + 0.5) as i32).max(1);
    let heavy = ((size as f64 / 5.0 + 0.5) as i32).max(2);
    (light, heavy)
}

#[derive(Clone, Copy, Default)]
struct Span {
    lo: i32,
    hi: i32,
}

/// Where an arm's lines lie across it, in a cell `len` pixels that way.
fn strokes(weight: u8, len: i32, light: i32, heavy: i32, out: &mut [Span; 2]) -> usize {
    let t = if weight == 2 { heavy } else { light };
    let n = if weight == 3 { 2 } else { 1 };
    let gap = light;
    let width = if n == 2 { 2 * t + gap } else { t };
    let lo = ((len - width) / 2).max(0);
    out[0] = Span { lo, hi: lo + t };
    if n == 2 {
        out[1] = Span { lo: lo + t + gap, hi: lo + 2 * t + gap };
    }
    for s in out.iter_mut().take(n) {
        s.hi = s.hi.min(len);
        s.lo = s.lo.min(s.hi);
    }
    n
}

/// Whether pixel (x, y) is on arc `which` of ╭ ╮ ╯ ╰: a quarter ellipse
/// round the cell's corner through the centre lines of the light strokes
/// meeting it, as thick as they are.
fn arc_in(which: u32, x: u32, y: u32, cw: u32, ch: u32, size: u32) -> bool {
    let (light, heavy) = weights(size);
    let mut v = [Span::default(); 2];
    let mut h = [Span::default(); 2];
    strokes(1, cw as i32, light, heavy, &mut v);
    strokes(1, ch as i32, light, heavy, &mut h);
    let xc = (v[0].lo + v[0].hi) as f64 / 2.0;
    let yc = (h[0].lo + h[0].hi) as f64 / 2.0;
    let (cw, ch) = (cw as f64, ch as f64);
    let cx = if which == 0 || which == 3 { cw } else { 0.0 };
    let cy = if which < 2 { ch } else { 0.0 };
    let rx = if cx > 0.0 { cw - xc } else { xc };
    let ry = if cy > 0.0 { ch - yc } else { yc };
    if rx <= 0.0 || ry <= 0.0 {
        return false;
    }
    let dx = x as f64 + 0.5 - cx;
    let dy = y as f64 + 0.5 - cy;
    let f = dx * dx / (rx * rx) + dy * dy / (ry * ry) - 1.0;
    let gx = 2.0 * dx / (rx * rx);
    let gy = 2.0 * dy / (ry * ry);
    let half = light as f64 / 2.0;
    f * f <= half * half * (gx * gx + gy * gy)
}

/// How far a line of an arm reaches into the middle: closed across what
/// crosses it, a double line turning into its neighbour on its side.
#[allow(clippy::too_many_arguments)]
fn reach(a: &[u8; 4], arm: usize, side: usize, other: usize, n: usize, len: i32, light: i32, heavy: i32, far: bool) -> i32 {
    let mut sp = [Span::default(); 2];
    let ahead = (arm + 2) % 4;
    let mid = len / 2;
    if n == 1 {
        let mut best = if far { -1 } else { i32::MAX };
        for s in [side, other] {
            if a[s] == 0 {
                continue;
            }
            let m = strokes(a[s], len, light, heavy, &mut sp);
            for k in sp.iter().take(m) {
                best = if far { best.max(k.hi) } else { best.min(k.lo) };
            }
        }
        if best != -1 && best != i32::MAX {
            return best;
        }
        if a[ahead] != 0 {
            return mid;
        }
        strokes(a[arm], len, light, heavy, &mut sp);
        return if far { sp[0].hi } else { sp[0].lo };
    }
    if a[side] != 0 {
        let m = strokes(a[side], len, light, heavy, &mut sp);
        return if far { sp[0].hi } else { sp[m - 1].lo };
    }
    if a[ahead] != 0 {
        return mid;
    }
    if a[other] != 0 {
        let m = strokes(a[other], len, light, heavy, &mut sp);
        return if far { sp[m - 1].hi } else { sp[0].lo };
    }
    mid
}

/// A box drawing character as rectangles in a cell, lines as thick as a
/// font of `size` draws them.
fn box_px(a: &[u8; 4], cw: u32, ch: u32, size: u32) -> Vec<R> {
    let (light, heavy) = weights(size);
    let mut out = Vec::new();
    let mut sp = [Span::default(); 2];
    for arm in 0..4 {
        if a[arm] == 0 {
            continue;
        }
        let vertical = arm == UP || arm == DOWN;
        let (first, second) = if vertical { (LEFT, RIGHT) } else { (UP, DOWN) };
        let len = if vertical { ch } else { cw } as i32;
        let towards = arm == UP || arm == LEFT;
        let n = strokes(a[arm], if vertical { cw } else { ch } as i32, light, heavy, &mut sp);
        for (j, s) in sp.iter().enumerate().take(n) {
            let (side, other) = if j == 0 { (first, second) } else { (second, first) };
            let at = reach(a, arm, side, other, n, len, light, heavy, towards);
            let (lo, hi) = if towards { (0, at) } else { (at, len) };
            if hi <= lo || s.hi <= s.lo {
                continue;
            }
            out.push(if vertical {
                R { x: s.lo, w: s.hi - s.lo, y: lo, h: hi - lo }
            } else {
                R { y: s.lo, h: s.hi - s.lo, x: lo, w: hi - lo }
            });
        }
    }
    out
}

/// Shading as the ink's strength over the ground, or 0 for no shade.
fn shade_of(cp: u32) -> f64 {
    match cp {
        0x2591 => 0.25,
        0x2592 => 0.5,
        0x2593 => 0.75,
        _ => 0.0,
    }
}

/// Whether `cp` is drawn here rather than by the font.
pub fn drawn(cp: u32) -> bool {
    (TRIANGLE..TRIANGLE + 4).contains(&cp)
        || (ARC..ARC + 4).contains(&cp)
        || (BRAILLE + 1..BRAILLE + 0x100).contains(&cp)
        || shade_of(cp) > 0.0
        || ((0x2500..0x2580).contains(&cp) && arms(cp) != [0; 4])
        || fracs().contains_key(&cp)
}

/// `cp`'s coverage of one cell at `m`, or None where the font draws it.
pub fn mask(cp: u32, m: &Metrics) -> Option<Mask> {
    let (cw, ch) = (m.cw, m.ch);
    let mut cov = vec![0u8; (cw * ch) as usize];
    let fill = |cov: &mut Vec<u8>, r: &R| {
        for y in r.y.max(0)..(r.y + r.h).min(ch as i32) {
            for x in r.x.max(0)..(r.x + r.w).min(cw as i32) {
                cov[(y as u32 * cw + x as u32) as usize] = 255;
            }
        }
    };
    if (TRIANGLE..TRIANGLE + 4).contains(&cp) || (ARC..ARC + 4).contains(&cp) {
        let tri = cp < TRIANGLE + 4 && cp >= TRIANGLE;
        for y in 0..ch {
            for x in 0..cw {
                let on = if tri { tri_in(cp - TRIANGLE, x, y, cw, ch) } else { arc_in(cp - ARC, x, y, cw, ch, m.size) };
                if on {
                    cov[(y * cw + x) as usize] = 255;
                }
            }
        }
    } else if (BRAILLE..BRAILLE + 0x100).contains(&cp) {
        if cp == BRAILLE {
            return None;
        }
        for r in dots(cp - BRAILLE, cw, ch) {
            fill(&mut cov, &r);
        }
    } else if shade_of(cp) > 0.0 {
        cov.fill((shade_of(cp) * 255.0 + 0.5) as u8);
    } else if (0x2500..0x2580).contains(&cp) && arms(cp) != [0; 4] {
        for r in box_px(&arms(cp), cw, ch, m.size) {
            fill(&mut cov, &r);
        }
    } else if let Some(fs) = fracs().get(&cp) {
        for f in fs {
            fill(&mut cov, &frac_px(f, cw, ch));
        }
    } else {
        return None;
    }
    Some(Mask { w: cw, h: ch, cov })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::face::Face;

    fn m(size: u32) -> Metrics {
        Face::new().metrics(size)
    }

    fn row(k: &Mask, y: u32) -> Vec<u8> {
        k.cov[(y * k.w) as usize..((y + 1) * k.w) as usize].to_vec()
    }

    #[test]
    fn counts_match_the_painter() {
        // Sextants: 60 of 64 patterns; octants: 230 of 256.
        assert_eq!((0x1FB00..0x1FB3C).filter(|&c| fracs().contains_key(&c)).count(), 60);
        assert_eq!((OCTANT..OCTANT + 0xE6).filter(|&c| fracs().contains_key(&c)).count(), 0xE6);
        assert!(!fracs().contains_key(&(OCTANT + 0xE6)));
        assert!(drawn(0x2500) && drawn(0x256D) && drawn(0x25E2) && drawn(0x2588) && drawn(0x28FF));
        assert!(!drawn(0x2571) && !drawn(0x2504) && !drawn('A' as u32) && !drawn(0x1FB3C));
    }

    #[test]
    fn blocks_fill_their_part_and_halves_meet() {
        let m = m(17);
        let full = mask(0x2588, &m).unwrap();
        assert!(full.cov.iter().all(|&v| v == 255));
        let up = mask(0x2580, &m).unwrap();
        let down = mask(0x2584, &m).unwrap();
        for i in 0..up.cov.len() {
            assert_eq!(up.cov[i] as u32 + down.cov[i] as u32, 255, "halves share every pixel once");
        }
        // Every octant pattern and its complement tile the cell.
        let left = mask(0x258c, &m).unwrap();
        assert!(row(&left, 0)[..(m.cw / 2) as usize].iter().all(|&v| v == 255));
    }

    #[test]
    fn box_lines_run_edge_to_edge_and_join() {
        let m = m(17);
        let h = mask(0x2500, &m).unwrap();
        // Some row is ink from edge to edge, and it is the light weight.
        let full: Vec<u32> = (0..m.ch).filter(|&y| row(&h, y).iter().all(|&v| v == 255)).collect();
        assert_eq!(full.len(), 1, "17/12 rounds to a light line of 1");
        let v = mask(0x2502, &m).unwrap();
        let cols: Vec<u32> = (0..m.cw).filter(|&x| (0..m.ch).all(|y| v.cov[(y * m.cw + x) as usize] == 255)).collect();
        assert_eq!(cols.len(), 1);
        // A cross covers both, and a heavy line is thicker.
        let cross = mask(0x253C, &m).unwrap();
        assert!(row(&cross, full[0]).iter().all(|&v| v == 255));
        let heavy = mask(0x2501, &m).unwrap();
        assert_eq!((0..m.ch).filter(|&y| row(&heavy, y).iter().all(|&v| v == 255)).count(), 3);
        // Double lines are two, a light line apart.
        let d = mask(0x2550, &m).unwrap();
        assert_eq!((0..m.ch).filter(|&y| row(&d, y).iter().all(|&v| v == 255)).count(), 2);
    }

    #[test]
    fn a_rounded_corner_meets_the_lines() {
        let m = m(17);
        let h = mask(0x2500, &m).unwrap();
        let y = (0..m.ch).find(|&y| row(&h, y)[0] == 255).unwrap();
        // ╭ reaches the right edge on the horizontal line's row.
        let a = mask(0x256D, &m).unwrap();
        assert_eq!(a.cov[(y * m.cw + m.cw - 1) as usize], 255);
        assert!(a.cov.iter().any(|&v| v == 0));
    }

    #[test]
    fn triangles_complete_each_other() {
        let m = m(17);
        let a = mask(0x25E2, &m).unwrap();
        let b = mask(0x25E4, &m).unwrap();
        for i in 0..a.cov.len() {
            assert_eq!(a.cov[i] as u32 + b.cov[i] as u32, 255);
        }
    }

    #[test]
    fn braille_dots_are_even() {
        let m = m(17);
        let d = dots(0xff, m.cw, m.ch);
        assert_eq!(d.len(), 8);
        assert!(d.iter().all(|r| r.w == d[0].w && r.h == d[0].h));
        let k = mask(0x28FF, &m).unwrap();
        assert_eq!(k.cov.iter().filter(|&&v| v == 255).count() as i32, 8 * d[0].w * d[0].h);
    }
}
