//! Colour to the panel's greys. A colour is its luminance; a cell's grey is
//! the nearest of the panel's levels, and a picture's is dithered to them.
//! On paper, the default, a cell is turned over in brightness, so the
//! black ground senders compose on is the white of the paper and their
//! light text is ink; a picture is left as it is, since a photograph
//! turned over is a negative.

use crate::scene::DEFAULT;
use crate::tune::{CURVE_CEILING, CURVE_FLOOR, CURVE_GAMMA, LEVELS};

/// The painter's own ink and ground, which `default` means on a screen.
pub const INK: u8 = 204;
pub const GROUND: u8 = 0;
/// What they mean on paper: black ink on white, so default text is the
/// darkest the panel draws rather than a dark grey.
pub const PAPER_INK: u8 = 0;
pub const PAPER_GROUND: u8 = 255;

/// The step between two levels: 17 for sixteen.
pub const STEP: u32 = 255 / (LEVELS - 1);

/// Luminance of `0xrrggbb`, as Rec. 601 weighs it: what a greyscale
/// conversion of a photograph gives, so cells and pictures agree.
pub fn lum(rgb: i64) -> u8 {
    let r = (rgb >> 16) & 255;
    let g = (rgb >> 8) & 255;
    let b = rgb & 255;
    ((r * 299 + g * 587 + b * 114 + 500) / 1000) as u8
}

/// The nearest level to `v`, as the grey that shows it.
pub fn nearest(v: u8) -> u8 {
    let s = STEP;
    (((v as u32 + s / 2) / s) * s) as u8
}

/// A grey stretched between the curve's floor and ceiling, nothing more:
/// near black to black, near white to white, the rest in proportion,
/// for a kitty picture turned over with the cells before it is dithered.
pub fn paper_levels(v: u8) -> u8 {
    let x = ((v as f64 / 255.0 - CURVE_FLOOR) / (CURVE_CEILING - CURVE_FLOOR)).clamp(0.0, 1.0);
    (x * 255.0).round() as u8
}

/// A cell's grey, curved and brought to the nearest level (see `tune`).
pub fn curve(v: u8) -> u8 {
    static LUT: std::sync::OnceLock<[u8; 256]> = std::sync::OnceLock::new();
    LUT.get_or_init(|| {
        let mut t = [0u8; 256];
        for (i, o) in t.iter_mut().enumerate() {
            let x = ((i as f64 / 255.0 - CURVE_FLOOR) / (CURVE_CEILING - CURVE_FLOOR)).clamp(0.0, 1.0).powf(CURVE_GAMMA);
            *o = ((x * (LEVELS - 1) as f64).round() as u32 * STEP) as u8;
        }
        t
    })[v as usize]
}

/// Whether a grey is black or white, which the fast modes draw.
pub fn bw(v: u8) -> bool {
    v == 0 || v == 255
}

/// How cells are turned to greys.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Tones {
    pub paper: bool,
}

impl Tones {
    pub fn ink(&self) -> u8 {
        if self.paper { PAPER_INK } else { INK }
    }

    pub fn ground(&self) -> u8 {
        if self.paper { PAPER_GROUND } else { GROUND }
    }

    /// A cell colour as a grey and an alpha: DEFAULT is `default`, a
    /// colour with an alpha carries it, any other colour is opaque. On
    /// paper the grey is turned over.
    pub fn grey(&self, c: i64, default: u8) -> (u8, u8) {
        if c == DEFAULT {
            return (default, 255);
        }
        let a = if c >> 24 == 0 { 255 } else { ((c >> 24) - 1).min(255) as u8 };
        let g = lum(c & 0xffffff);
        (if self.paper { 255 - g } else { g }, a)
    }
}

/// `top` at alpha `a` over `under`.
#[inline]
pub fn over(top: u8, a: u8, under: u8) -> u8 {
    match a {
        255 => top,
        0 => under,
        _ => ((top as u32 * a as u32 + under as u32 * (255 - a as u32) + 127) / 255) as u8,
    }
}

/// How a picture is brought to the panel's levels.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Dither {
    /// Floyd-Steinberg, serpentine: the error of each pixel carried to the
    /// ones after it, each row walked the other way from the last so no
    /// worms grow along soft gradients; it leaves no pattern of its own.
    Diffuse,
    /// An 8x8 Bayer matrix: each pixel judged on its own, a fine regular
    /// texture.
    Ordered,
}

const BAYER: [[u8; 8]; 8] = [
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
];

/// Greys `w` by `h` brought to the panel's levels in place. A pixel whose
/// alpha is 0 is skipped, and its error goes nowhere.
pub fn dither(grey: &mut [u8], alpha: &[u8], w: usize, h: usize, how: Dither) {
    match how {
        Dither::Ordered => {
            let s = STEP as i32;
            for y in 0..h {
                for x in 0..w {
                    let i = y * w + x;
                    if alpha[i] == 0 {
                        continue;
                    }
                    // A threshold from -s/2 to s/2 added before rounding.
                    let t = (BAYER[y & 7][x & 7] as i32 * 2 + 1) * s / 128 - s / 2;
                    let v = (grey[i] as i32 + t).clamp(0, 255) as u8;
                    grey[i] = nearest(v);
                }
            }
        }
        Dither::Diffuse => {
            // Errors in sixteenths of a grey, two rows of them, a pixel's
            // own at x + 1 with room either side.
            let mut cur = vec![0i32; w + 2];
            let mut next = vec![0i32; w + 2];
            for y in 0..h {
                let back = y % 2 == 1;
                for k in 0..w {
                    let x = if back { w - 1 - k } else { k };
                    let i = y * w + x;
                    if alpha[i] == 0 {
                        continue;
                    }
                    let want = (grey[i] as i32 * 16 + cur[x + 1]).clamp(0, 255 * 16);
                    let got = nearest(((want + 8) / 16) as u8);
                    grey[i] = got;
                    let e = want - got as i32 * 16;
                    let (ahead, behind) = if back { (x, x + 2) } else { (x + 2, x) };
                    cur[ahead] += e * 7 / 16;
                    next[behind] += e * 3 / 16;
                    next[x + 1] += e * 5 / 16;
                    next[ahead] += e / 16;
                }
                std::mem::swap(&mut cur, &mut next);
                next.iter_mut().for_each(|v| *v = 0);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn levels() {
        assert_eq!(STEP, 17);
        assert_eq!(nearest(0), 0);
        assert_eq!(nearest(8), 0);
        assert_eq!(nearest(9), 17);
        assert_eq!(nearest(255), 255);
        assert_eq!(nearest(246), 238);
        for v in 0..=255u8 {
            assert_eq!(nearest(v) as u32 % STEP, 0);
        }
    }

    #[test]
    fn the_curve_darkens_and_lands_on_levels() {
        assert_eq!(curve(0), 0);
        assert_eq!(curve(20), 0, "below the floor is ink");
        assert_eq!(curve(255), 255);
        for v in 0..=255u8 {
            assert_eq!(curve(v) as u32 % STEP, 0);
            if v < 128 {
                assert!(curve(v) <= nearest(v), "{v}, ink and dim text, is never lighter");
            }
        }
        assert!(curve(128) < nearest(128));
        assert_eq!(curve(210), 255, "a ground near white is paper");
    }

    #[test]
    fn paper_turns_cells_over() {
        let p = Tones { paper: true };
        assert_eq!(p.grey(0xffffff, 0), (0, 255));
        assert_eq!(p.grey(0x000000, 0), (255, 255));
        assert_eq!(p.grey(DEFAULT, p.ink()), (0, 255));
        assert_eq!(p.grey(DEFAULT, p.ground()), (255, 255));
        let s = Tones { paper: false };
        assert_eq!(s.grey(0xffffff, 0), (255, 255));
        assert_eq!(s.grey(DEFAULT, s.ink()), (204, 255));
        // An alpha of 127, as (a + 1) << 24 carries it.
        assert_eq!(p.grey(128 << 24 | 0xffffff, 0), (0, 127));
    }

    #[test]
    fn dither_keeps_the_mean_and_lands_on_levels() {
        for how in [Dither::Diffuse, Dither::Ordered] {
            let (w, h) = (64, 64);
            let mut g = vec![100u8; w * h];
            let a = vec![255u8; w * h];
            dither(&mut g, &a, w, h, how);
            assert!(g.iter().all(|&v| v as u32 % STEP == 0));
            let mean = g.iter().map(|&v| v as f64).sum::<f64>() / (w * h) as f64;
            assert!((mean - 100.0).abs() < 1.5, "{how:?} mean {mean}");
            assert!(g.contains(&85) && g.contains(&102), "{how:?} mixes the two levels around 100");
        }
    }

    #[test]
    fn dither_leaves_levels_alone() {
        for how in [Dither::Diffuse, Dither::Ordered] {
            let mut g: Vec<u8> = (0..256).map(|i| nearest(i as u8)).collect();
            let want = g.clone();
            dither(&mut g, &[255; 256], 16, 16, how);
            assert_eq!(g, want, "{how:?}");
        }
    }
}
