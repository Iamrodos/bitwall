//! The font, its cell, and its glyphs as coverage masks.
//!
//! The face is the painter's, MesloLGS Nerd Font Mono, embedded, and a
//! font size is what it is on the painter: a size of n draws the face at n
//! pixels to the em. The cell is worked out as the painter's SDL_ttf works
//! it out -- the advance of `M` across, the face's line height (ascender
//! less descender) rounded up down -- then rounded to two pixels across
//! and four down as the Pis' painters round it, the glyph in the middle.
//! So a font gives the same grid here as on a Pi's glass of the same size.
//!
//! Glyphs are rasterised by skrifa and zeno, the two halves of swash:
//! pure Rust, no system library to build for the Pi. skrifa hints the
//! outline with its port of FreeType's autohinter for a monochrome target,
//! so every stem, the baseline and the x-height fall on whole pixels,
//! across as well as down, and sixteen greys show a stem as ink rather
//! than as a grey smear over two columns; zeno fills the hinted outline
//! anti-aliased, so curves stay smooth. swash itself would only hint
//! heights, its hinting fixed for LCD screens. Bold is the face's own
//! bold, MesloLGS Nerd Font Mono Bold. Italic has no face of its own here
//! and is the glyph slanted, as the web receiver's browser makes it.
//!
//! An icon is squeezed into one cell in the Mono face. Given two cells --
//! where a blank follows it (`compose`) -- it is drawn from the Nerd Font
//! face that is not Mono, whose icons Nerd Fonts' patcher sized for two
//! cells keeping each set's own proportions, as Ghostty and kitty draw an
//! icon with a space after it: centred in the two cells less a gap at
//! their right, so the word after it does not touch it.

use std::collections::HashMap;
use std::sync::Arc;

/// The share of a cell left clear at the right of an icon drawn across
/// two, between it and the word after it.
const ICON_GAP: f32 = 0.4;

use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, Engine, HintingInstance, HintingOptions, OutlineGlyphCollection, OutlinePen, Target};
use skrifa::{FontRef, MetadataProvider};
use zeno::{Command, Format, Mask as Fill, Origin, Transform, Vector};

use crate::tune::{EVEN_CELLS, FONT_MAX, FONT_MIN};

static REGULAR: &[u8] = include_bytes!("../../pi/MesloLGSNerdFontMono-Regular.ttf");
static BOLD: &[u8] = include_bytes!("../fonts/MesloLGSNerdFontMono-Bold.ttf");
static ICONS: &[u8] = include_bytes!("../fonts/MesloLGSNerdFont-Regular.ttf");

/// The Nerd Font icon sets the wall says it draws (`glyphs=`), each of
/// which may take two cells. The Powerline glyphs are not among them:
/// they are drawn to fill exactly the one cell they are given.
pub fn icon(cp: u32) -> bool {
    (0xe300..=0xe3e3).contains(&cp)
        || (0xea60..=0xec1e).contains(&cp)
        || (0xf000..=0xf2e0).contains(&cp)
        || (0xf400..=0xf533).contains(&cp)
        || (0xf0001..=0xf1af0).contains(&cp)
}

/// A cell at one font size, and where a glyph sits in it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Metrics {
    pub size: u32,
    pub cw: u32,
    pub ch: u32,
    /// Where the face's own cell sits in the rounded one.
    pub tx: u32,
    pub ty: u32,
    /// The baseline, down from the top of the face's own cell.
    pub ascent: u32,
}

impl Metrics {
    /// The baseline, down from the top of the cell.
    pub fn baseline(&self) -> u32 {
        self.ty + self.ascent
    }
}

/// A glyph's coverage, `w` by `h`, 0 to 255, placed in its cell or cells.
#[derive(Debug)]
pub struct Mask {
    pub w: u32,
    pub h: u32,
    pub cov: Vec<u8>,
}

/// The hinting a glyph is drawn with: FreeType's autohinter for a
/// monochrome target, which puts every stem's edges on whole pixels across
/// and down. The face's own TrueType instructions move heights alone, so a
/// stem hinted by them still falls between two columns.
const HINTING: HintingOptions = HintingOptions { engine: Engine::Auto(None), target: Target::Mono };

/// One of the embedded fonts: its characters, advances and outlines, and
/// its hinting at each size asked for.
struct Font {
    font: FontRef<'static>,
    outlines: OutlineGlyphCollection<'static>,
    hinted: HashMap<u32, Option<HintingInstance>>,
}

impl Font {
    fn new(ttf: &'static [u8]) -> Font {
        let font = FontRef::new(ttf).expect("an embedded font reads");
        Font { font: font.clone(), outlines: font.outline_glyphs(), hinted: HashMap::new() }
    }

    fn gid(&self, cp: u32) -> Option<skrifa::GlyphId> {
        self.font.charmap().map(cp)
    }

    /// `cp`'s outline at `size` pixels to the em, hinted where the font
    /// hints it, as path commands with y down from the baseline.
    fn outline(&mut self, cp: u32, size: f32, hint: bool) -> Vec<Command> {
        let gid = self.gid(cp).unwrap_or_default();
        let mut pen = Pen(Vec::new());
        let Some(glyph) = self.outlines.get(gid) else { return pen.0 };
        let outlines = &self.outlines;
        let instance = if hint {
            self.hinted
                .entry(size.to_bits())
                .or_insert_with(|| HintingInstance::new(outlines, Size::new(size), LocationRef::default(), HINTING).ok())
                .as_ref()
        } else {
            None
        };
        let drawn = match instance {
            Some(i) => glyph.draw(DrawSettings::hinted(i, false), &mut pen),
            None => glyph.draw(DrawSettings::unhinted(Size::new(size), LocationRef::default()), &mut pen),
        };
        if drawn.is_err() {
            pen.0.clear();
        }
        pen.0
    }
}

/// A glyph's outline as zeno's path, turned the right way up for a
/// raster: y down from the baseline.
struct Pen(Vec<Command>);

impl OutlinePen for Pen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.0.push(Command::MoveTo(Vector::new(x, -y)));
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.0.push(Command::LineTo(Vector::new(x, -y)));
    }
    fn quad_to(&mut self, cx: f32, cy: f32, x: f32, y: f32) {
        self.0.push(Command::QuadTo(Vector::new(cx, -cy), Vector::new(x, -y)));
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.0.push(Command::CurveTo(Vector::new(cx0, -cy0), Vector::new(cx1, -cy1), Vector::new(x, -y)));
    }
    fn close(&mut self) {
        self.0.push(Command::Close);
    }
}

pub struct Face {
    regular: Font,
    bold: Font,
    icons: Font,
    upem: f32,
    asc: f32,
    desc: f32,
    m_advance: f32,
    glyphs: HashMap<(u32, u32, u8, u8), Arc<Mask>>,
}

/// Glyphs held before the cache starts again.
const CACHE: usize = 8192;

impl Face {
    pub fn new() -> Face {
        let regular = Font::new(REGULAR);
        let fm = regular.font.metrics(Size::unscaled(), LocationRef::default());
        let m = regular.gid('M' as u32).expect("the face has an M");
        let m_advance = regular.font.glyph_metrics(Size::unscaled(), LocationRef::default()).advance_width(m).unwrap_or(0.0);
        Face {
            regular,
            bold: Font::new(BOLD),
            icons: Font::new(ICONS),
            upem: fm.units_per_em as f32,
            asc: fm.ascent,
            desc: fm.descent,
            m_advance,
            glyphs: HashMap::new(),
        }
    }

    /// The cell at font `size`.
    pub fn metrics(&self, size: u32) -> Metrics {
        self.metrics_with(size, EVEN_CELLS)
    }

    /// The cell at font `size`, rounded to even cells or not.
    pub fn metrics_with(&self, size: u32, even: bool) -> Metrics {
        let size = size.clamp(FONT_MIN, FONT_MAX);
        let k = size as f32 / self.upem;
        let cw0 = ((self.m_advance * k).round() as u32).max(1);
        // Rounded up as FreeType's FT_CEIL rounds, with a hair of slack
        // for float error on an exact fit.
        let ceil = |v: f32| (v - 1e-4).ceil().max(1.0) as u32;
        let ch0 = ceil((self.asc - self.desc) * k);
        let ascent = ceil(self.asc * k);
        let (mut cw, mut ch) = (cw0, ch0);
        if even {
            cw = cw.div_ceil(2) * 2;
            ch = ch.div_ceil(4) * 4;
        }
        Metrics { size, cw, ch, tx: (cw - cw0) / 2, ty: (ch - ch0) / 2, ascent }
    }

    /// The grid a glass `w` by `h` holds at font `size`.
    pub fn area(&self, size: u32, w: u32, h: u32) -> (usize, usize) {
        let m = self.metrics(size);
        (((w / m.cw).max(1)) as usize, ((h / m.ch).max(1)) as usize)
    }

    /// The largest font whose grid on a glass `w` by `h` holds `cols` by
    /// `rows`, or None where none does.
    pub fn font_for(&self, cols: usize, rows: usize, w: u32, h: u32) -> Option<u32> {
        let (mut lo, mut hi, mut best) = (FONT_MIN, FONT_MAX, None);
        while lo <= hi {
            let mid = (lo + hi) / 2;
            let (c, r) = self.area(mid, w, h);
            if c >= cols && r >= rows {
                best = Some(mid);
                lo = mid + 1;
            } else {
                hi = mid - 1;
            }
        }
        best
    }

    /// Whether the face has a glyph of its own for `cp`.
    pub fn has(&self, cp: u32) -> bool {
        self.regular.gid(cp).is_some_and(|g| g.to_u32() != 0)
    }

    /// How many cells `cp` takes at `m`: two where the face advances past it
    /// by well over one cell, as the painter judges it.
    pub fn cells(&self, cp: u32, m: &Metrics) -> u32 {
        let Some(g) = self.regular.gid(cp) else { return 1 };
        let adv = self.regular.font.glyph_metrics(Size::new(m.size as f32), LocationRef::default()).advance_width(g).unwrap_or(0.0);
        if adv > m.cw as f32 * 1.5 { 2 } else { 1 }
    }

    /// `cp`'s coverage at `m`, `cells` cells wide, `style` BOLD and ITALIC
    /// as the cell has them; cached. An icon two cells wide is the icon
    /// at its own proportions across both.
    pub fn glyph(&mut self, cp: u32, m: &Metrics, cells: u32, style: u8) -> Arc<Mask> {
        let key = (cp, m.size, style, cells as u8);
        if let Some(g) = self.glyphs.get(&key) {
            return g.clone();
        }
        if self.glyphs.len() >= CACHE {
            self.glyphs.clear();
        }
        let g = Arc::new(self.make(cp, m, cells, style));
        self.glyphs.insert(key, g.clone());
        g
    }

    fn make(&mut self, cp: u32, m: &Metrics, cells: u32, style: u8) -> Mask {
        let (w, h) = (m.cw * cells, m.ch);
        let mut cov = vec![0u8; (w * h) as usize];
        let spread = cells == 2 && icon(cp);
        let size = m.size as f32;
        // Italic slants by a fifth of the height above the baseline, the
        // slant's middle at three tenths of the em.
        let slant = (style & crate::scene::ITALIC != 0).then(|| Transform { xx: 1.0, xy: 0.0, yx: -0.2, yy: 1.0, x: -0.2 * size * 0.3, y: 0.0 });
        let (data, p, left) = if spread {
            // The icons carry no hinting. One too big for its two cells,
            // which Nerd Fonts' sizing should never leave, is drawn smaller
            // until it fits.
            let fill = |path: &[Command]| Fill::new(path).format(Format::Alpha).origin(Origin::TopLeft).transform(slant).render();
            let room = w - (m.cw as f32 * ICON_GAP).round() as u32;
            let (mut data, mut p) = fill(&self.icons.outline(cp, size, false));
            let fit = (room as f32 / p.width.max(1) as f32).min(h as f32 / p.height.max(1) as f32);
            if fit < 1.0 {
                (data, p) = fill(&self.icons.outline(cp, (size * fit).floor(), false));
            }
            let left = (room as i32 - p.width as i32) / 2;
            (data, p, left)
        } else {
            let font = if style & crate::scene::BOLD != 0 { &mut self.bold } else { &mut self.regular };
            let path = font.outline(cp, size, true);
            let (data, p) = Fill::new(&path[..]).format(Format::Alpha).origin(Origin::TopLeft).transform(slant).render();
            (data, p, m.tx as i32 + p.left)
        };
        // `top` is the glyph's top row, down from the baseline.
        let top = m.baseline() as i32 + p.top;
        let (gw, gh) = (p.width as i32, p.height as i32);
        for gy in 0..gh {
            let y = top + gy;
            if y < 0 || y >= h as i32 {
                continue;
            }
            for gx in 0..gw {
                let x = left + gx;
                if x < 0 || x >= w as i32 {
                    continue;
                }
                cov[(y as u32 * w + x as u32) as usize] = data[(gy * gw + gx) as usize];
            }
        }
        Mask { w, h, cov }
    }
}

impl Default for Face {
    fn default() -> Face {
        Face::new()
    }
}

/// Double width as the sender's library has it: the sender writes one cell
/// and skips the next.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cells_match_the_pis_painters() {
        let f = Face::new();
        // Measured on two Pi 5 walls, both with BITWALL_EVEN_CELLS=1:
        // font 13 is 8x20 and font 20 is 12x28.
        let m = f.metrics_with(13, true);
        assert_eq!((m.cw, m.ch), (8, 20));
        let m = f.metrics_with(20, true);
        assert_eq!((m.cw, m.ch), (12, 28));
        assert_eq!((1920 / 8, 1080 / 20), (240, 54));
        // Without the rounding, the face's own cell.
        let m = f.metrics_with(13, false);
        assert_eq!((m.cw, m.ch, m.tx, m.ty), (8, 17, 0, 0));
    }

    #[test]
    fn the_panel_at_22_is_144_by_50() {
        let f = Face::new();
        assert_eq!(f.area(crate::tune::FONT, 1872, 1404), (144, 50));
        assert_eq!(f.metrics(22).cw, 13);
        assert_eq!(f.font_for(144, 50, 1872, 1404), Some(22));
    }

    #[test]
    fn a_glyph_has_ink_inside_its_cell() {
        let mut f = Face::new();
        let m = f.metrics(20);
        let g = f.glyph('M' as u32, &m, 1, 0);
        assert_eq!((g.w, g.h), (m.cw, m.ch));
        let ink: u32 = g.cov.iter().map(|&v| v as u32).sum();
        assert!(ink > 255 * 20);
        let b = f.glyph('M' as u32, &m, 1, crate::scene::BOLD);
        assert!(b.cov.iter().map(|&v| v as u32).sum::<u32>() > ink);
        assert!(f.has('M' as u32) && f.has(0xe0b0)); // a powerline glyph
    }

    #[test]
    fn bold_is_the_bold_face() {
        let mut f = Face::new();
        let m = f.metrics(22);
        let r = f.glyph('l' as u32, &m, 1, 0);
        let b = f.glyph('l' as u32, &m, 1, crate::scene::BOLD);
        // The bold stem is wider solid ink, and neither has grey beside it.
        let row = |g: &Mask| g.cov[(m.baseline() as usize - 6) * g.w as usize..][..g.w as usize].to_vec();
        let solid = |g: &Mask| row(g).iter().filter(|&&v| v == 255).count();
        assert!(solid(&b) > solid(&r) && solid(&r) > 0, "{:?} {:?}", row(&b), row(&r));
        assert!(row(&b).iter().chain(row(&r).iter()).all(|&v| v == 0 || v == 255));
    }

    #[test]
    fn stems_fall_on_whole_pixels() {
        let mut f = Face::new();
        for size in [22, 40] {
            let m = f.metrics(size);
            let g = f.glyph('|' as u32, &m, 1, 0);
            let row = &g.cov[(m.baseline() as usize - 4) * g.w as usize..][..g.w as usize];
            // Ink and paper, and no grey either side of the stem.
            assert!(row.iter().any(|&v| v == 255), "{size}: {row:?}");
            assert!(row.iter().all(|&v| v == 0 || v == 255), "{size}: {row:?}");
        }
    }

    #[test]
    fn an_icon_given_two_cells_is_centred_clear_of_the_word_after_it() {
        let mut f = Face::new();
        let m = f.metrics(22);
        for cp in [0xf05a9, 0xf0502, 0xf0335, 0xf0a72] {
            let one = f.glyph(cp, &m, 1, 0);
            let two = f.glyph(cp, &m, 2, 0);
            assert_eq!(two.w, 2 * m.cw);
            let cols: Vec<u32> = (0..two.w).filter(|&x| (0..two.h).any(|y| two.cov[(y * two.w + x) as usize] > 0)).collect();
            let (l, r) = (cols[0], *cols.last().unwrap());
            let room = two.w - (m.cw as f32 * ICON_GAP).round() as u32;
            assert!(r < room, "U+{cp:X} reaches {r}, into the gap after {room}");
            // Within two pixels: a glyph's box can be a column wider than its ink.
            assert!((l as i32 - (room - 1 - r) as i32).abs() <= 2, "U+{cp:X} from {l} to {r} of {room}");
            let ink = |g: &Mask| g.cov.iter().map(|&v| v as u32).sum::<u32>();
            assert!(ink(&two) > ink(&one) || cp == 0xf0335, "U+{cp:X} is no bigger across two cells");
        }
        assert!(!icon(0xe0b0) && icon(0xf0a72) && !icon('A' as u32));
    }
}
