//! What the wall holds, as the receiver core hands it over: the base's
//! cells, and above them the layers in the order they are drawn, cells
//! layers and pictures among each other. The e-ink wall composes this into
//! its glass and works out for itself what changed, so the core hands the
//! whole of it over whenever a frame closes and need keep no diff.
//!
//! A cell is spelled as the core's grid keeps it (`grid.rs`), so handing
//! one over is a copy.

use std::sync::Arc;

pub const BOLD: u8 = 1;
pub const ITALIC: u8 = 2;
pub const UNDER: u8 = 4;
pub const STRIKE: u8 = 8;
pub const REVERSE: u8 = 16;
pub const FAINT: u8 = 32;

/// A colour the sender left as the wall's own.
pub const DEFAULT: i64 = -1;
/// The second half of a wide character.
pub const TAIL: u32 = 0;
/// A cell a layer has not drawn.
pub const NONE: u32 = u32::MAX;

/// One cell: a code point, and its colours as `0xrrggbb`, DEFAULT, or on a
/// cells layer above the base `(a + 1) << 24 | 0xrrggbb` for an alpha `a`
/// from 0 to 254 (255 is opaque and carries none). `at` is the SGR
/// attributes, BOLD and the rest.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub struct Cell {
    pub ch: u32,
    pub fg: i64,
    pub bg: i64,
    pub at: u8,
}

impl Cell {
    pub const BLANK: Cell = Cell { ch: ' ' as u32, fg: DEFAULT, bg: DEFAULT, at: 0 };
    pub const UNDRAWN: Cell = Cell { ch: NONE, fg: DEFAULT, bg: DEFAULT, at: 0 };

    pub fn new(ch: char, fg: i64, bg: i64, at: u8) -> Cell {
        Cell { ch: ch as u32, fg, bg, at }
    }
}

/// A grid of cells at a font, centred on the glass by its own cell as the
/// painter centres it: the base's, or a cells layer's. `cells` is
/// `cols * rows` long, row by row. `pictures` are the kitty graphics
/// placed in it, in the order they are drawn over its cells, each with its
/// box in glass pixels.
#[derive(Clone, Debug, PartialEq)]
pub struct Cells {
    pub font: u32,
    pub cols: usize,
    pub rows: usize,
    pub cells: Vec<Cell>,
    pub pictures: Vec<Picture>,
}

impl Cells {
    pub fn new(font: u32, cols: usize, rows: usize, fill: Cell) -> Cells {
        Cells { font, cols, rows, cells: vec![fill; cols * rows], pictures: Vec::new() }
    }

    pub fn at(&self, x: usize, y: usize) -> &Cell {
        &self.cells[y * self.cols + x]
    }

    pub fn set(&mut self, x: usize, y: usize, c: Cell) {
        if x < self.cols && y < self.rows {
            self.cells[y * self.cols + x] = c;
        }
    }

    /// Text written from (x, y) along the row, one cell a character, a
    /// wide one taking two.
    pub fn text(&mut self, x: usize, y: usize, s: &str, fg: i64, bg: i64, at: u8) {
        let mut x = x;
        for ch in s.chars() {
            self.set(x, y, Cell::new(ch, fg, bg, at));
            x += 1;
            if crate::face::wide(ch as u32) {
                self.set(x, y, Cell { ch: TAIL, fg, bg, at });
                x += 1;
            }
        }
    }
}

/// A picture over the cells: RGBA, `w` by `h`, scaled to the box at
/// `at` and `size` in glass pixels, as a video layer's frame is. A new
/// frame is a new `Arc`, which is how the wall knows it changed without
/// reading it.
#[derive(Clone, Debug)]
pub struct Picture {
    pub rgba: Arc<Vec<u8>>,
    pub w: u32,
    pub h: u32,
    pub at: (i32, i32),
    pub size: (u32, u32),
}

impl PartialEq for Picture {
    fn eq(&self, o: &Picture) -> bool {
        Arc::ptr_eq(&self.rgba, &o.rgba) && self.w == o.w && self.h == o.h && self.at == o.at && self.size == o.size
    }
}

/// What one layer above the base shows.
#[derive(Clone, Debug, PartialEq)]
pub enum Show {
    Cells(Cells),
    Picture(Picture),
}

/// A layer above the base. `id` is the layer's own for as long as it is
/// up, whatever slot it moves to: the session's, say.
#[derive(Clone, Debug, PartialEq)]
pub struct Layer {
    pub id: u64,
    pub show: Show,
}

/// Everything the wall shows: the base, or nothing where no sender holds
/// it, and the layers above it bottom to top.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Scene {
    pub base: Option<Cells>,
    pub layers: Vec<Layer>,
}
