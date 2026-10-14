//! Big text drawn from cells (`bitwall/letters.py`).
//!
//! ```no_run
//! use bitwall::frame::Frame;
//! use bitwall::letters::{self, Font, Style};
//!
//! # let wall: Option<bitwall::letters::WallInfo> = None;
//! let mut f = Frame::new(80, 24);
//! letters::draw(&mut f, 2, 1, "12:30",
//!               &Style::new(Font::Segment).fg(0xffd75f).height(9).smooth(true), None);
//! let fonts = [Font::Dots, Font::Half, Font::Block];
//! if let Some(got) = letters::fit("Hello there", 80, 24, &fonts, wall.as_ref(), false,
//!                                 &Style::default()) {
//!     letters::draw(&mut f, 0, 0, &got.text, &got.style, None);
//! }
//! ```
//!
//! A wall's own type is one size, so anything meant to be read across a
//! room is drawn: each letter a picture made of cells. The fonts, and what
//! each needs the wall to draw:
//!
//! | font | is | needs |
//! |---|---|---|
//! | `Block` | a 3x5 bitmap, one pixel a cell | nothing but spaces |
//! | `Half` | the same 3x5, two pixels a cell | `▀ ▄` |
//! | `Dots` | 5x7 letters as strokes on the braille dot grid | U+2800-28FF |
//! | `Segment` | a 14-segment display, as shapes | `▀ ▄` |
//! | `SegmentCut` | `Segment` with `Edges::Cut` | `▀ ▄ ◢ ◣ ◤ ◥` |
//! | `Segment7` | a 7-segment display, the clock's | `▀ ▄` |
//! | `Segment7Cut` | `Segment7` with `Edges::Cut` | `▀ ▄ ◢ ◣ ◤ ◥` |
//!
//! `Tile` and the faces `WhiteRabbit`, `Bytes256` and `Vt323` are kitty
//! pictures that Python draws with Pillow; here no wall is able to draw
//! them (`usable` says no), `fit` passes them over for the next font, and
//! `draw` draws them as Python does where it cannot: in the first of their
//! cell fonts that fits.
//!
//! The options are a `Style`, the same for every function and font, as
//! Python's keywords are: `scale` or `height`, `spacing`, `turn`, `align`,
//! `smooth`, `depth`, `cell`, `ghost`, `edges`, `px`, and the colours.
//! Given a wall, as a `WallInfo` (`wall_info(&w)`), the choices are the
//! wall's: a font it cannot draw is drawn as the nearest it can, cut edges
//! square where it has no diagonal halves, its colour depth and its cells'
//! size in pixels; whatever the style says outright stands over the wall.
//!
//! For the same text and options every function here gives what Python's
//! does, cell for cell: `tests/letters.rs` holds it to
//! `tests/golden/letters.json`, which `tests/golden/letters.py` writes.

use std::collections::HashSet;

use crate::frame::{Depth, FAINT, Frame};
use crate::protocol::{covers, number};
use crate::wall::Wall;

// ------------------------------------------------------------------ fonts

/// A font: the cell fonts, and the pictured ones this library cannot draw.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Font {
    Block,
    Half,
    Dots,
    Segment,
    SegmentCut,
    Segment7,
    Segment7Cut,
    Tile,
    WhiteRabbit,
    Bytes256,
    Vt323,
}

impl Font {
    pub const ALL: [Font; 11] = [
        Font::Block, Font::Half, Font::Dots, Font::Segment, Font::SegmentCut,
        Font::Segment7, Font::Segment7Cut, Font::Tile, Font::WhiteRabbit,
        Font::Bytes256, Font::Vt323,
    ];

    /// The name Python gives it (`letters.SEGMENT_CUT` is `segment_cut`).
    pub fn name(self) -> &'static str {
        match self {
            Font::Block => "block",
            Font::Half => "half",
            Font::Dots => "dots",
            Font::Segment => "segment",
            Font::SegmentCut => "segment_cut",
            Font::Segment7 => "segment7",
            Font::Segment7Cut => "segment7_cut",
            Font::Tile => "tile",
            Font::WhiteRabbit => "white_rabbit",
            Font::Bytes256 => "256_bytes",
            Font::Vt323 => "vt323",
        }
    }

    pub fn from_name(name: &str) -> Option<Font> {
        Font::ALL.into_iter().find(|f| f.name() == name)
    }

    /// Whether it is drawn as a kitty picture over its cells, which this
    /// library does not do.
    pub fn pictured(self) -> bool {
        matches!(self, Font::Tile | Font::WhiteRabbit | Font::Bytes256 | Font::Vt323)
    }

    fn face(self) -> bool {
        matches!(self, Font::WhiteRabbit | Font::Bytes256 | Font::Vt323)
    }

    fn segmented(self) -> bool {
        matches!(self, Font::Segment | Font::SegmentCut | Font::Segment7 | Font::Segment7Cut)
    }

    fn seven(self) -> bool {
        matches!(self, Font::Segment7 | Font::Segment7Cut)
    }

    /// What the wall must draw for it (`NEEDS`).
    fn needs(self) -> &'static str {
        match self {
            Font::Half | Font::Segment | Font::Segment7 => "▀▄",
            Font::Dots => "⠁⣿",
            Font::SegmentCut | Font::Segment7Cut => "▀▄◢◣◤◥",
            _ => "",
        }
    }

    /// What it is drawn as on a wall that cannot draw it, nearest first
    /// (`FONT_INSTEAD`).
    fn instead(self) -> &'static [Font] {
        match self {
            Font::Dots => &[Font::Half, Font::Block],
            Font::Half | Font::Segment | Font::Segment7 => &[Font::Block],
            Font::SegmentCut => &[Font::Segment, Font::Block],
            Font::Segment7Cut => &[Font::Segment7, Font::Block],
            _ => &[],
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default)]
pub enum Spacing {
    /// An i narrow, a w wide; digits keep one width.
    #[default]
    Proportional,
    Mono,
}

/// A quarter turn: `Up` anticlockwise, reading from the bottom up.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Turn {
    Up,
    Down,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Align {
    Left,
    Centre,
    Right,
}

/// How a crisp letter's slopes are drawn: hard cell edges, or the
/// diagonal halves ◢◣◤◥ where a slope passes through a cell.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Edges {
    Square,
    Cut,
}

/// How text is drawn: the font, the colours and Python's options. A
/// field left `None` is the default, or the wall's where one is given.
///
/// Colours are `0xRRGGBB`, or `frame::DEFAULT` for the terminal's own;
/// `bg` `None` is transparent, the grid showing through.
#[derive(Clone, Debug, PartialEq)]
pub struct Style {
    pub font: Font,
    pub fg: i32,
    pub bg: Option<i32>,
    /// Whole steps of the font's smallest size.
    pub scale: u32,
    /// Rows to fill instead: a shape font fills them exactly, a bitmap
    /// takes the biggest whole step that fits.
    pub height: Option<usize>,
    pub spacing: Spacing,
    pub turn: Option<Turn>,
    /// For text of several lines; left, or centred by `centred`.
    pub align: Option<Align>,
    /// Shade a segment font's edges by how much of each pixel is covered,
    /// where the depth is true or 256 and the colours are RGB.
    pub smooth: bool,
    pub depth: Option<Depth>,
    /// How much taller than wide a cell is: from `px`, else 2.
    pub cell: Option<f64>,
    /// The unlit segments' colour, or the unlit dots' behind `Dots`.
    pub ghost: Option<i32>,
    /// Square by default, cut for the `_CUT` fonts.
    pub edges: Option<Edges>,
    /// A cell's size in screen pixels, (wide, tall).
    pub px: Option<(u32, u32)>,
}

impl Default for Style {
    fn default() -> Style {
        Style {
            font: Font::Block,
            fg: 0xffffff,
            bg: None,
            scale: 1,
            height: None,
            spacing: Spacing::Proportional,
            turn: None,
            align: None,
            smooth: false,
            depth: None,
            cell: None,
            ghost: None,
            edges: None,
            px: None,
        }
    }
}

impl Style {
    pub fn new(font: Font) -> Style {
        Style { font, ..Style::default() }
    }
    pub fn font(mut self, font: Font) -> Style {
        self.font = font;
        self
    }
    pub fn fg(mut self, fg: i32) -> Style {
        self.fg = fg;
        self
    }
    pub fn bg(mut self, bg: i32) -> Style {
        self.bg = Some(bg);
        self
    }
    pub fn scale(mut self, scale: u32) -> Style {
        self.scale = scale;
        self
    }
    pub fn height(mut self, rows: usize) -> Style {
        self.height = Some(rows);
        self
    }
    pub fn spacing(mut self, spacing: Spacing) -> Style {
        self.spacing = spacing;
        self
    }
    pub fn mono(self) -> Style {
        self.spacing(Spacing::Mono)
    }
    pub fn turn(mut self, turn: Turn) -> Style {
        self.turn = Some(turn);
        self
    }
    pub fn align(mut self, align: Align) -> Style {
        self.align = Some(align);
        self
    }
    pub fn smooth(mut self, smooth: bool) -> Style {
        self.smooth = smooth;
        self
    }
    pub fn depth(mut self, depth: Depth) -> Style {
        self.depth = Some(depth);
        self
    }
    pub fn cell(mut self, cell: f64) -> Style {
        self.cell = Some(cell);
        self
    }
    pub fn ghost(mut self, ghost: i32) -> Style {
        self.ghost = Some(ghost);
        self
    }
    pub fn edges(mut self, edges: Edges) -> Style {
        self.edges = Some(edges);
        self
    }
    pub fn px(mut self, wide: u32, tall: u32) -> Style {
        self.px = Some((wide, tall));
        self
    }
}

/// What letters asks of a wall: what it draws, its colour depth, the size
/// of its cells. `wall_info(&w)` takes it from a live `Wall`; a test or a
/// sender drawing for a wall it knows can make one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WallInfo {
    /// Code points the screen draws, as (low, high); empty where it has
    /// not said, which is a yes to everything.
    pub glyphs: Vec<(u32, u32)>,
    pub depth: Depth,
    /// A cell's size in pixels, as the screen says it (`cell=WxH`).
    pub cell: Option<(u32, u32)>,
    /// The font size the area is drawn at, which gives a cell's size where
    /// the screen does not.
    pub font_size: Option<u32>,
    /// Whether its `caps` offer kitty graphics.
    pub kitty: bool,
}

impl Default for WallInfo {
    fn default() -> WallInfo {
        WallInfo { glyphs: Vec::new(), depth: Depth::True, cell: None, font_size: None, kitty: false }
    }
}

impl WallInfo {
    /// Whether the wall draws every character of `text`.
    pub fn can(&self, text: &str) -> bool {
        covers(&self.glyphs, text)
    }
}

/// What a live wall says of itself, for letters.
pub fn wall_info(w: &Wall) -> WallInfo {
    let glyphs = w.glyphs();
    let cell = w.said("screen").and_then(|said| {
        let (a, b) = said.get("cell")?.split_once('x')?;
        let ok = |s: &str| number(s).filter(|v| (1..=4096).contains(v)).map(|v| v as u32);
        Some((ok(a)?, ok(b)?))
    });
    WallInfo { glyphs, depth: w.depth(), cell, font_size: w.font_size(), kitty: w.offers("kitty") }
}

/// A cell drawn: its character, its colours, `None` for a colour left as
/// the grid has it, and whether a braille cell shows its unlit dots.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Piece {
    pub ch: char,
    pub fg: Option<i32>,
    pub bg: Option<i32>,
    pub faint: bool,
}

/// Text drawn: rows of cells, `None` where nothing is.
pub type Cells = Vec<Vec<Option<Piece>>>;

fn piece(ch: char, fg: Option<i32>, bg: Option<i32>) -> Option<Piece> {
    Some(Piece { ch, fg, bg, faint: false })
}

// ------------------------------------------------------------------ glyphs

/// Capitals, digits and marks, 3 pixels by 5, for `Block` and `Half`.
const SMALL: &[(char, &str)] = &[
    ('0', "### #.# #.# #.# ###"),
    ('1', "..# ..# ..# ..# ..#"),
    ('2', "### ..# ### #.. ###"),
    ('3', "### ..# ### ..# ###"),
    ('4', "#.# #.# ### ..# ..#"),
    ('5', "### #.. ### ..# ###"),
    ('6', "### #.. ### #.# ###"),
    ('7', "### ..# ..# ..# ..#"),
    ('8', "### #.# ### #.# ###"),
    ('9', "### #.# ### ..# ###"),
    ('A', "### #.# ### #.# #.#"),
    ('B', "##. #.# ##. #.# ##."),
    ('C', "### #.. #.. #.. ###"),
    ('D', "##. #.# #.# #.# ##."),
    ('E', "### #.. ### #.. ###"),
    ('F', "### #.. ### #.. #.."),
    ('G', "### #.. #.# #.# ###"),
    ('H', "#.# #.# ### #.# #.#"),
    ('I', "### .#. .#. .#. ###"),
    ('J', "### ..# ..# #.# ###"),
    ('K', "#.# #.# ##. #.# #.#"),
    ('L', "#.. #.. #.. #.. ###"),
    ('M', "#.# ### ### #.# #.#"),
    ('N', "##. #.# #.# #.# #.#"),
    ('O', "### #.# #.# #.# ###"),
    ('P', "### #.# ### #.. #.."),
    ('Q', "### #.# #.# ### ..#"),
    ('R', "### #.# ##. #.# #.#"),
    ('S', "### #.. ### ..# ###"),
    ('T', "### .#. .#. .#. .#."),
    ('U', "#.# #.# #.# #.# ###"),
    ('V', "#.# #.# #.# #.# .#."),
    ('W', "#.# #.# ### ### #.#"),
    ('X', "#.# #.# .#. #.# #.#"),
    ('Y', "#.# #.# .#. .#. .#."),
    ('Z', "### ..# .#. #.. ###"),
    (':', "... .#. ... .#. ..."),
    ('.', "... ... ... ... .#."),
    ('-', "... ... ### ... ..."),
    ('/', "..# ..# .#. #.. #.."),
    ('!', ".#. .#. .#. ... .#."),
    ('?', "### ..# .## ... .#."),
    ('%', "#.# ..# .#. #.. #.#"),
    ('+', "... .#. ### .#. ..."),
    (' ', "... ... ... ... ..."),
    ('\'', ".#. .#. ... ... ..."),
    (',', "... ... ... .#. #.."),
    ('"', "#.# #.# ... ... ..."),
    ('(', "..# .#. .#. .#. ..#"),
    (')', "#.. .#. .#. .#. #.."),
    ('&', "##. ##. ### #.# ###"),
    (';', "... .#. ... .#. #.."),
    ('#', "#.# ### #.# ### #.#"),
    ('=', "... ### ... ### ..."),
    ('*', "#.# .#. #.# ... ..."),
    ('_', "... ... ... ... ###"),
    ('←', ".#. #.. ### #.. .#."),
    ('→', ".#. ..# ### ..# .#."),
    ('↑', ".#. ### #.# .#. .#."),
    ('↓', ".#. .#. #.# ### .#."),
];

/// Upper and lower case, digits and marks, 5 by 7 with an eighth row for
/// descenders, for `Dots`.
const FIVE: &[(char, &str)] = &[
    ('A', ".###. #...# #...# ##### #...# #...# #...#"),
    ('B', "####. #...# #...# ####. #...# #...# ####."),
    ('C', ".###. #...# #.... #.... #.... #...# .###."),
    ('D', "####. #...# #...# #...# #...# #...# ####."),
    ('E', "##### #.... #.... ####. #.... #.... #####"),
    ('F', "##### #.... #.... ####. #.... #.... #...."),
    ('G', ".###. #...# #.... #.### #...# #...# .####"),
    ('H', "#...# #...# #...# ##### #...# #...# #...#"),
    ('I', ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###."),
    ('J', "..### ...#. ...#. ...#. ...#. #..#. .##.."),
    ('K', "#...# #..#. #.#.. ##... #.#.. #..#. #...#"),
    ('L', "#.... #.... #.... #.... #.... #.... #####"),
    ('M', "#...# ##.## #.#.# #.#.# #...# #...# #...#"),
    ('N', "#...# #...# ##..# #.#.# #..## #...# #...#"),
    ('O', ".###. #...# #...# #...# #...# #...# .###."),
    ('P', "####. #...# #...# ####. #.... #.... #...."),
    ('Q', ".###. #...# #...# #...# #.#.# #..#. .##.#"),
    ('R', "####. #...# #...# ####. #.#.. #..#. #...#"),
    ('S', ".#### #.... #.... .###. ....# ....# ####."),
    ('T', "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#.."),
    ('U', "#...# #...# #...# #...# #...# #...# .###."),
    ('V', "#...# #...# #...# #...# #...# .#.#. ..#.."),
    ('W', "#...# #...# #...# #.#.# #.#.# #.#.# .#.#."),
    ('X', "#...# #...# .#.#. ..#.. .#.#. #...# #...#"),
    ('Y', "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#.."),
    ('Z', "##### ....# ...#. ..#.. .#... #.... #####"),
    ('a', "..... ..... .###. ....# .#### #...# .####"),
    ('b', "#.... #.... #.##. ##..# #...# #...# ####."),
    ('c', "..... ..... .###. #.... #.... #...# .###."),
    ('d', "....# ....# .##.# #..## #...# #...# .####"),
    ('e', "..... ..... .###. #...# ##### #.... .###."),
    ('f', "..##. .#..# .#... ###.. .#... .#... .#..."),
    ('g', "..... ..... .#### #...# #...# .#### ....# .###."),
    ('h', "#.... #.... #.##. ##..# #...# #...# #...#"),
    ('i', "..#.. ..... .##.. ..#.. ..#.. ..#.. .###."),
    ('j', "...#. ..... ..##. ...#. ...#. ...#. #..#. .##.."),
    ('k', "#.... #.... #..#. #.#.. ##... #.#.. #..#."),
    ('l', ".##.. ..#.. ..#.. ..#.. ..#.. ..#.. .###."),
    ('m', "..... ..... ##.#. #.#.# #.#.# #...# #...#"),
    ('n', "..... ..... #.##. ##..# #...# #...# #...#"),
    ('o', "..... ..... .###. #...# #...# #...# .###."),
    ('p', "..... ..... ####. #...# #...# ####. #.... #...."),
    ('q', "..... ..... .#### #...# #...# .#### ....# ....#"),
    ('r', "..... ..... #.##. ##..# #.... #.... #...."),
    ('s', "..... ..... .###. #.... .###. ....# ####."),
    ('t', ".#... .#... ###.. .#... .#... .#..# ..##."),
    ('u', "..... ..... #...# #...# #...# #..## .##.#"),
    ('v', "..... ..... #...# #...# #...# .#.#. ..#.."),
    ('w', "..... ..... #...# #...# #.#.# #.#.# .#.#."),
    ('x', "..... ..... #...# .#.#. ..#.. .#.#. #...#"),
    ('y', "..... ..... #...# #...# #...# .#### ....# .###."),
    ('z', "..... ..... ##### ...#. ..#.. .#... #####"),
    ('0', ".###. #...# #..## #.#.# ##..# #...# .###."),
    ('1', "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###."),
    ('2', ".###. #...# ....# ...#. ..#.. .#... #####"),
    ('3', "##### ...#. ..#.. ...#. ....# #...# .###."),
    ('4', "...#. ..##. .#.#. #..#. ##### ...#. ...#."),
    ('5', "##### #.... ####. ....# ....# #...# .###."),
    ('6', "..##. .#... #.... ####. #...# #...# .###."),
    ('7', "##### ....# ...#. ..#.. .#... .#... .#..."),
    ('8', ".###. #...# #...# .###. #...# #...# .###."),
    ('9', ".###. #...# #...# .#### ....# ...#. .##.."),
    (' ', ""),
    ('!', "..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#.."),
    ('"', ".#.#. .#.#."),
    ('#', ".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#."),
    ('$', "..#.. .#### #.#.. .###. ..#.# ####. ..#.."),
    ('%', "##... ##..# ...#. ..#.. .#... #..## ...##"),
    ('&', ".##.. #..#. #.#.. .#... #.#.# #..#. .##.#"),
    ('\'', "..#.. ..#.."),
    ('(', "...#. ..#.. .#... .#... .#... ..#.. ...#."),
    (')', ".#... ..#.. ...#. ...#. ...#. ..#.. .#..."),
    ('*', "..... ..#.. #.#.# .###. #.#.# ..#.. ....."),
    ('+', "..... ..#.. ..#.. ##### ..#.. ..#.. ....."),
    (',', "..... ..... ..... ..... ..... .##.. ..#.. .#..."),
    ('-', "..... ..... ..... ####. ..... ..... ....."),
    ('.', "..... ..... ..... ..... ..... .##.. .##.."),
    ('/', "....# ....# ...#. ..#.. .#... #.... #...."),
    (':', "..... .##.. .##.. ..... .##.. .##.. ....."),
    (';', "..... .##.. .##.. ..... .##.. .##.. ..#.. .#..."),
    ('<', "...#. ..#.. .#... #.... .#... ..#.. ...#."),
    ('=', "..... ..... ##### ..... ##### ..... ....."),
    ('>', ".#... ..#.. ...#. ....# ...#. ..#.. .#..."),
    ('?', ".###. #...# ....# ...#. ..#.. ..... ..#.."),
    ('@', ".###. #...# #.### #.#.# #.### #.... .####"),
    ('[', ".###. .#... .#... .#... .#... .#... .###."),
    ('\\', "#.... #.... .#... ..#.. ...#. ....# ....#"),
    (']', ".###. ...#. ...#. ...#. ...#. ...#. .###."),
    ('^', "..#.. .#.#. #...#"),
    ('_', "..... ..... ..... ..... ..... ..... ..... #####"),
    ('`', ".#... ..#.."),
    ('{', "...#. ..#.. ..#.. .#... ..#.. ..#.. ...#."),
    ('|', "..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#.."),
    ('}', ".#... ..#.. ..#.. ...#. ..#.. ..#.. .#..."),
    ('~', "..... ..... .#... #.#.# ...#. ..... ....."),
    ('←', "..... ..#.. .#... ##### .#... ..#.. ....."),
    ('→', "..... ..#.. ...#. ##### ...#. ..#.. ....."),
    ('↑', "..#.. .###. #.#.# ..#.. ..#.. ..#.. ..#.."),
    ('↓', "..#.. ..#.. ..#.. ..#.. #.#.# .###. ..#.."),
];

/// Which of the fourteen segments each character lights: a top, b upper
/// right, c lower right, d bottom, e lower left, f upper left, g and h the
/// middle bar's halves, i and j the centre bars, k l m n the diagonals.
const SEGMENTS: &[(char, &str)] = &[
    ('0', "abcdef"),
    ('1', "bc"),
    ('2', "abdegh"),
    ('3', "abcdgh"),
    ('4', "bcfgh"),
    ('5', "acdfgh"),
    ('6', "acdefgh"),
    ('7', "abc"),
    ('8', "abcdefgh"),
    ('9', "abcdfgh"),
    ('A', "abcefgh"),
    ('B', "abcdhij"),
    ('C', "adef"),
    ('D', "abcdij"),
    ('E', "adefgh"),
    ('F', "aefgh"),
    ('G', "acdefh"),
    ('H', "bcefgh"),
    ('I', "adij"),
    ('J', "bcde"),
    ('K', "efgln"),
    ('L', "def"),
    ('M', "bcefkl"),
    ('N', "bcefkn"),
    ('O', "abcdef"),
    ('P', "abefgh"),
    ('Q', "abcdefn"),
    ('R', "abefghn"),
    ('S', "acdfgh"),
    ('T', "aij"),
    ('U', "bcdef"),
    ('V', "eflm"),
    ('W', "bcefmn"),
    ('X', "klmn"),
    ('Y', "klj"),
    ('Z', "adlm"),
    ('-', "gh"),
    ('_', "d"),
    ('+', "ghij"),
    ('*', "ghijklmn"),
    ('/', "lm"),
    ('\\', "kn"),
    ('=', "dgh"),
    ('<', "ln"),
    ('>', "km"),
    ('(', "ln"),
    (')', "km"),
    ('[', "adef"),
    (']', "abcd"),
    ('?', "abhj"),
    ('$', "acdfghij"),
    ('%', "cflm"),
    ('"', "fi"),
    ('#', "bcdghij"),
    ('&', "adeghkn"),
    ('@', "abdefhi"),
    (' ', ""),
    (':', ""),
    ('.', ""),
    (',', ""),
    ('\'', ""),
    ('!', ""),
];

/// Which of the seven bars each character lights on `Segment7`.
const SEVENS: &[(char, &str)] = &[
    ('0', "abcdef"),
    ('1', "bc"),
    ('2', "abdeg"),
    ('3', "abcdg"),
    ('4', "bcfg"),
    ('5', "acdfg"),
    ('6', "acdefg"),
    ('7', "abc"),
    ('8', "abcdefg"),
    ('9', "abcdfg"),
    ('A', "abcefg"),
    ('b', "cdefg"),
    ('C', "adef"),
    ('c', "deg"),
    ('d', "bcdeg"),
    ('E', "adefg"),
    ('F', "aefg"),
    ('G', "acdef"),
    ('H', "bcefg"),
    ('h', "cefg"),
    ('I', "ef"),
    ('J', "bcde"),
    ('L', "def"),
    ('n', "ceg"),
    ('o', "cdeg"),
    ('O', "abcdef"),
    ('P', "abefg"),
    ('q', "abcfg"),
    ('r', "eg"),
    ('S', "acdfg"),
    ('t', "defg"),
    ('U', "bcdef"),
    ('u', "cde"),
    ('y', "bcdfg"),
    ('-', "g"),
    ('_', "d"),
    ('=', "dg"),
    ('"', "bf"),
    ('?', "abeg"),
    ('[', "adef"),
    (']', "abcd"),
    (' ', ""),
    (':', ""),
    ('.', ""),
    (',', ""),
    ('\'', ""),
    ('!', ""),
];

/// SEGMENT's smallest size, in rows, and the step `scale` takes it in.
pub const SEGMENT_BASE: usize = 9;
pub const SEGMENT_MIN: usize = 4;
/// The smallest SEGMENT `fit` offers while another font would do: below
/// it a 14-segment letter's diagonals have too little room to read.
pub const SEGMENT_READABLE: usize = 7;
/// A cell of MesloLGS, the painter's font, as a share of its size.
pub const MESLO_CELL: (f64, f64) = (0.6, 1.26);
/// Smoothing helps where a stroke is this many pixels wide, or a cell this
/// few screen pixels across.
pub const SMOOTH_STROKE: usize = 3;
pub const SMOOTH_FINE_CELL: u32 = 6;
/// A cell at least this many pixels wide and tall keeps braille dots apart.
pub const DOTS_DISTINCT: (u32, u32) = (5, 10);
/// A ghost's luma: above this, and under this share of the ink's.
pub const GHOST_FLOOR: f64 = 6.0;
pub const GHOST_UNDER_INK: f64 = 0.5;
/// Half a `Dots` stroke's width, in the bitmap's pixels.
pub const DOTS_STROKE: f64 = 0.5;

const SAMPLES: usize = 4;
const EDGE: f64 = 0.7072;
const STEEP: f64 = 2.0;
const NARROW: &str = ":.,'!";

/// Punctuation that has a plain form a font can draw.
const INSTEAD: &[(char, &str)] = &[
    ('’', "'"), ('‘', "'"), ('“', "\""), ('”', "\""), ('—', "-"), ('–', "-"),
    ('…', "..."), ('×', "X"), ('·', "."), ('•', "."),
];

/// The arrows as the lines they are, in `Dots` pixel centres.
const ARROW_STROKES: &[(char, [(f64, f64, f64, f64); 3])] = &[
    ('↑', [(2.5, 0.5, 2.5, 6.5), (2.5, 0.5, 0.5, 2.5), (2.5, 0.5, 4.5, 2.5)]),
    ('↓', [(2.5, 0.5, 2.5, 6.5), (2.5, 6.5, 0.5, 4.5), (2.5, 6.5, 4.5, 4.5)]),
    ('→', [(0.5, 3.5, 4.5, 3.5), (4.5, 3.5, 2.5, 1.5), (4.5, 3.5, 2.5, 5.5)]),
    ('←', [(0.5, 3.5, 4.5, 3.5), (0.5, 3.5, 2.5, 1.5), (0.5, 3.5, 2.5, 5.5)]),
];

// ------------------------------------------------------------------ text

fn lookup(table: &'static [(char, &'static str)], c: char) -> Option<&'static str> {
    table.iter().find(|(k, _)| *k == c).map(|(_, v)| *v)
}

fn known(font: Font, c: char) -> bool {
    let table = if font.seven() {
        SEVENS
    } else if font.segmented() {
        SEGMENTS
    } else if font == Font::Dots {
        FIVE
    } else {
        SMALL
    };
    lookup(table, c).is_some()
}

fn lower(font: Font) -> bool {
    font.seven() || font == Font::Dots
}

/// A character's letters with their accents taken off, as Python's NFKD
/// then ASCII: the characters it decomposes to that are ASCII, perhaps none.
fn fold(c: char) -> String {
    if c.is_ascii() {
        return c.to_string();
    }
    match FOLD.binary_search_by_key(&(c as u32), |&(k, _)| k) {
        Ok(i) => FOLD[i].1.to_string(),
        Err(_) => String::new(),
    }
}

/// A character in the other case, where that is one character.
fn swapcase(c: char) -> Option<char> {
    let mut it: Box<dyn Iterator<Item = char>> = if c.is_lowercase() {
        Box::new(c.to_uppercase())
    } else if c.is_uppercase() {
        Box::new(c.to_lowercase())
    } else {
        return Some(c);
    };
    let first = it.next()?;
    if it.next().is_some() { None } else { Some(first) }
}

/// `text` as characters `font` can draw (Python's `plain`).
///
/// Accents are folded rather than dropped, curly quotes and dashes become
/// straight ones, a font without lower case gets capitals, a letter a font
/// has in only one case is drawn in that one, and anything still unknown
/// becomes a space. Line breaks are kept.
pub fn plain(text: &str, font: Font) -> String {
    if font.face() {
        return text.chars().map(|c| lookup(INSTEAD, c).map(str::to_string).unwrap_or(c.to_string())).collect();
    }
    let font = if font == Font::Tile { Font::Block } else { font };
    let lower = lower(font);
    let mut out = String::new();
    for c in text.chars() {
        if c == '\n' {
            out.push(c);
            continue;
        }
        let mut s = lookup(INSTEAD, c).map(str::to_string).unwrap_or(c.to_string());
        if !lower {
            s = s.to_uppercase();
        }
        let mut cs = s.chars();
        if let (Some(one), None) = (cs.next(), cs.next()) {
            if !known(font, one) {
                s = fold(one);
                if !lower {
                    s = s.to_uppercase();
                }
            }
        }
        let start = out.len();
        for k in s.chars() {
            if known(font, k) {
                out.push(k);
            } else {
                match swapcase(k) {
                    Some(sw) if known(font, sw) => out.push(sw),
                    _ => out.push(' '),
                }
            }
        }
        if out.len() == start {
            out.push(' ');
        }
    }
    out
}

/// Whether `wall` can draw `font`. No wall is a yes, the same as a wall
/// that has not said; a pictured font is a no on every wall here.
pub fn usable(font: Font, wall: Option<&WallInfo>) -> bool {
    if font.pictured() {
        return false;
    }
    let need = font.needs();
    need.is_empty() || wall.is_none_or(|w| w.can(need))
}

/// How many screen pixels a cell of `wall` is, (wide, tall): the screen's
/// own word, else an estimate from its font size for MesloLGS, else None.
pub fn resolution(wall: &WallInfo) -> Option<(u32, u32)> {
    if let Some(cell) = wall.cell {
        return Some(cell);
    }
    let size = wall.font_size? as f64;
    Some((
        (size * MESLO_CELL.0).round_ties_even().max(1.0) as u32,
        (size * MESLO_CELL.1).round_ties_even().max(1.0) as u32,
    ))
}

// ---------------------------------------------------------------- options

/// The options as `_setup` settles them.
#[derive(Clone, Debug)]
struct O {
    scale: usize,
    height: Option<usize>,
    spacing: Spacing,
    turn: Option<Turn>,
    align: Align,
    smooth: bool,
    depth: Depth,
    cell: f64,
    ghost: Option<i32>,
    edges: Edges,
    px: Option<(u32, u32)>,
    seven: bool,
}

/// The font the glyphs come from (Block, Half, Dots or Segment) and the
/// options, settled: a font the wall cannot draw as the nearest it can,
/// cut edges square where it has no diagonal halves, its depth and its
/// cells' size where the style does not say.
fn setup(style: &Style, wall: Option<&WallInfo>, align: Align) -> (Font, O) {
    let mut font = style.font;
    if let Some(w) = wall {
        if !usable(font, Some(w)) {
            font = font.instead().iter().copied().find(|f| usable(*f, Some(w))).unwrap_or(font);
        }
    }
    let mut edges = style.edges.unwrap_or(
        if matches!(font, Font::SegmentCut | Font::Segment7Cut) { Edges::Cut } else { Edges::Square });
    if edges == Edges::Cut && wall.is_some_and(|w| !w.can("◢◣◤◥")) {
        edges = Edges::Square;
    }
    let mut px = style.px;
    if px.is_none() {
        px = wall.and_then(resolution);
    }
    let mut cell = style.cell.unwrap_or(2.0);
    if let Some((a, b)) = px {
        px = Some((a.max(1), b.max(1)));
        if style.cell.is_none() {
            cell = b.max(1) as f64 / a.max(1) as f64;
        }
    }
    let depth = style.depth.or(wall.map(|w| w.depth)).unwrap_or(Depth::True);
    let o = O {
        scale: style.scale.max(1) as usize,
        height: style.height,
        spacing: style.spacing,
        turn: style.turn,
        align: style.align.unwrap_or(align),
        smooth: style.smooth,
        depth,
        cell,
        ghost: style.ghost,
        edges,
        px,
        seven: font.seven(),
    };
    let glyphs = if font.segmented() { Font::Segment } else { font };
    (glyphs, o)
}

fn plain_o(text: &str, font: Font, o: &O) -> String {
    plain(text, if o.seven { Font::Segment7 } else { font })
}

fn is_hex(c: i32) -> bool {
    (0..=0xffffff).contains(&c)
}

fn rgb(c: i32) -> (i32, i32, i32) {
    ((c >> 16) & 255, (c >> 8) & 255, c & 255)
}

fn luma((r, g, b): (i32, i32, i32)) -> f64 {
    0.2126 * r as f64 + 0.7152 * g as f64 + 0.0722 * b as f64
}

fn smooth_helps(n: usize, px: Option<(u32, u32)>) -> bool {
    match px {
        None => true,
        Some((w, _)) => seg_box(n).2 >= SMOOTH_STROKE || w <= SMOOTH_FINE_CELL,
    }
}

/// The ghost colour to send at `depth`, or None for none: at 256 colours
/// the palette's nearest to the tint that is no brighter than it, shows
/// above black and stays under half the ink; on a grey ramp the step no
/// brighter than it; with 16 or one, none.
pub fn ghost_for(ghost: Option<i32>, fg: i32, depth: Depth) -> Option<i32> {
    let g = match ghost {
        Some(g) if depth != Depth::True && is_hex(g) => g,
        _ => return ghost,
    };
    if matches!(depth, Depth::Ansi16 | Depth::Mono) {
        return None;
    }
    let want = rgb(g);
    let mut most = luma(want);
    if is_hex(fg) {
        most = most.min(luma(rgb(fg)) * GHOST_UNDER_INK);
    }
    if depth == Depth::Grey {
        let best = (0..24).map(|k| 8 + 10 * k).filter(|&v| GHOST_FLOOR <= v as f64 && v as f64 <= most).max()?;
        return Some((best << 16) | (best << 8) | best);
    }
    let mut best: Option<((i32, i32, i32), i32)> = None;
    for i in 16..256 {
        let c = palette_256(i);
        let l = luma(c);
        if !(GHOST_FLOOR <= l && l <= most) {
            continue;
        }
        let d = (c.0 - want.0).pow(2) + (c.1 - want.1).pow(2) + (c.2 - want.2).pow(2);
        if best.is_none_or(|(_, bd)| d < bd) {
            best = Some((c, d));
        }
    }
    best.map(|((r, g, b), _)| (r << 16) | (g << 8) | b)
}

fn palette_256(index: i32) -> (i32, i32, i32) {
    const CUBE: [i32; 6] = [0, 95, 135, 175, 215, 255];
    if index >= 232 {
        let v = 8 + 10 * (index - 232);
        return (v, v, v);
    }
    let i = (index - 16) as usize;
    (CUBE[i / 36], CUBE[i / 6 % 6], CUBE[i % 6])
}

fn dots_distinct(px: Option<(u32, u32)>) -> bool {
    px.is_none_or(|(w, h)| w >= DOTS_DISTINCT.0 && h >= DOTS_DISTINCT.1)
}

/// How many cells wide one of a bitmap's pixels is drawn, so a letter is
/// letter-shaped on a cell `cell` times taller than wide.
fn widen(font: Font, cell: f64) -> usize {
    let v = match font {
        Font::Block => (cell * 10.0 / 9.0).round_ties_even(),
        Font::Half => (cell * 5.0 / 9.0).round_ties_even(),
        _ => return 1,
    };
    v.max(1.0) as usize
}

// ----------------------------------------------------------- bitmap fonts

type Bits = Vec<Vec<u8>>;

/// A table's glyph as rows of 0 and 1, padded to the font's box.
fn glyph_table(spec: &str, width: usize, height: usize) -> Bits {
    let mut rows: Bits = spec
        .split_whitespace()
        .map(|r| {
            let mut row: Vec<u8> = r.chars().map(|c| (c == '#') as u8).collect();
            while row.len() < width {
                row.push(0);
            }
            row
        })
        .collect();
    while rows.len() < height {
        rows.push(vec![0; width]);
    }
    rows
}

/// A bitmap glyph, trimmed when proportional; a digit keeps its box.
fn bits(font: Font, ch: char, spacing: Spacing) -> Bits {
    let (table, width, height, space) = if font == Font::Dots { (FIVE, 5, 8, 3) } else { (SMALL, 3, 5, 2) };
    let spec = lookup(table, ch).unwrap_or_else(|| lookup(table, ' ').unwrap());
    let g = glyph_table(spec, width, height);
    if spacing == Spacing::Proportional && !ch.is_ascii_digit() {
        let used: Vec<usize> = (0..width).filter(|&x| g.iter().any(|r| r[x] != 0)).collect();
        if let (Some(&a), Some(&b)) = (used.first(), used.last()) {
            return g.iter().map(|r| r[a..=b].to_vec()).collect();
        }
        return g.iter().map(|_| vec![0; space]).collect();
    }
    g
}

fn bits_width(font: Font, ch: char, spacing: Spacing) -> usize {
    bits(font, ch, spacing)[0].len()
}

// --------------------------------------------------------------- dot fonts

type Seg = (f64, f64, f64, f64);

/// A `Dots` glyph as strokes between lit pixel centres, and its size.
fn strokes(ch: char, spacing: Spacing) -> (Vec<Seg>, usize, usize) {
    let g = bits(Font::Dots, ch, spacing);
    let (high, wide) = (g.len(), g[0].len());
    if let Some((_, lines)) = ARROW_STROKES.iter().find(|(c, _)| *c == ch) {
        let dx = (wide as f64 - 5.0) / 2.0;
        return (lines.iter().map(|&(ax, ay, bx, by)| (ax + dx, ay, bx + dx, by)).collect(), wide, high);
    }
    let on = |x: i64, y: i64| x >= 0 && y >= 0 && (x as usize) < wide && (y as usize) < high && g[y as usize][x as usize] != 0;
    let mut out = Vec::new();
    for y in 0..high as i64 {
        for x in 0..wide as i64 {
            if !on(x, y) {
                continue;
            }
            let (cx, cy) = (x as f64 + 0.5, y as f64 + 0.5);
            out.push((cx, cy, cx, cy));
            if on(x + 1, y) {
                out.push((cx, cy, cx + 1.0, cy));
            }
            if on(x, y + 1) {
                out.push((cx, cy, cx, cy + 1.0));
            }
            if on(x + 1, y + 1) && !on(x + 1, y) && !on(x, y + 1) {
                out.push((cx, cy, cx + 1.0, cy + 1.0));
            }
            if on(x - 1, y + 1) && !on(x - 1, y) && !on(x, y + 1) {
                out.push((cx, cy, cx - 1.0, cy + 1.0));
            }
        }
    }
    (out, wide, high)
}

fn near(px: f64, py: f64, (ax, ay, bx, by): Seg) -> f64 {
    let (dx, dy) = (bx - ax, by - ay);
    let length = dx * dx + dy * dy;
    let t = if length == 0.0 { 0.0 } else { (((px - ax) * dx + (py - ay) * dy) / length).min(1.0).max(0.0) };
    let (ex, ey) = (px - ax - t * dx, py - ay - t * dy);
    ex * ex + ey * ey
}

/// The dots a glyph lights at `size` dots a pixel, from the dot its top
/// left falls in, (fx, fy) into it.
fn dots_glyph(ch: char, spacing: Spacing, size: f64, fx: f64, fy: f64) -> Vec<(usize, usize)> {
    let (st, wide, high) = strokes(ch, spacing);
    let r2 = DOTS_STROKE * DOTS_STROKE + 1e-9;
    let mut out = Vec::new();
    let rows = (high as f64 * size + fy).ceil() as usize + 1;
    let cols = (wide as f64 * size + fx).ceil() as usize + 1;
    for j in 0..rows {
        let y = (j as f64 + 0.5 - fy) / size;
        let close: Vec<Seg> = st
            .iter()
            .copied()
            .filter(|s| s.1.min(s.3) - DOTS_STROKE <= y && y <= s.1.max(s.3) + DOTS_STROKE)
            .collect();
        if close.is_empty() {
            continue;
        }
        for i in 0..cols {
            let x = (i as f64 + 0.5 - fx) / size;
            if close.iter().any(|s| near(x, y, *s) <= r2) {
                out.push((i, j));
            }
        }
    }
    out
}

fn dot_size(n: usize) -> f64 {
    n as f64 / 2.0
}

type Lines = Vec<Vec<char>>;

fn offset(width: i64, wide: i64, align: Align) -> i64 {
    match align {
        Align::Right => wide - width,
        Align::Centre => (wide - width).div_euclid(2),
        Align::Left => 0,
    }
}

/// Where each `Dots` glyph goes, in dots, the block's size in dots, and
/// the boxes of unlit dots a ghost shows.
#[allow(clippy::type_complexity)]
fn dots_place(n: usize, lines: &Lines, o: &O) -> (Vec<(char, f64, f64)>, usize, usize, Vec<(f64, f64, f64, f64)>) {
    let size = dot_size(n);
    let spacing = o.spacing;
    let ghost = o.ghost.is_some();
    let boxes = ghost && spacing == Spacing::Mono && o.turn.is_none();
    let tall = 8.0 * size;
    let pitch = if ghost { tall + 4.0 } else { 9.0 * size };
    let nat: Vec<i64> = lines
        .iter()
        .map(|l| l.iter().map(|&c| bits_width(Font::Dots, c, spacing) as i64).sum::<i64>() + (l.len() as i64 - 1).max(0))
        .collect();
    let bx = 2.0 * (5.0 * size / 2.0).ceil();
    let widths: Vec<f64> = if boxes {
        lines.iter().map(|l| if l.is_empty() { 0.0 } else { l.len() as f64 * (bx + 2.0) - 2.0 }).collect()
    } else {
        nat.iter().map(|&w| w as f64 * size).collect()
    };
    let wide = widths.iter().copied().fold(0.0, f64::max);
    let max_nat = nat.iter().copied().max().unwrap_or(0);
    let mut places = Vec::new();
    let mut field = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let top = i as f64 * pitch;
        let mut left = if boxes {
            offset(widths[i] as i64, wide as i64, o.align) as f64
        } else {
            offset(nat[i], max_nat, o.align) as f64 * size
        };
        if ghost {
            left = 2.0 * (left / 2.0).floor();
        }
        if boxes {
            for &ch in line {
                places.push((ch, left + ((bx - 5.0 * size) / 2.0).floor(), top));
                field.push((left, top, left + bx, top + tall));
                left += bx + 2.0;
            }
            continue;
        }
        let mut x = 0usize;
        for &ch in line {
            places.push((ch, left + x as f64 * size, top));
            x += bits_width(Font::Dots, ch, spacing) + 1;
        }
        if ghost && !line.is_empty() {
            field.push((left, top, left + widths[i], top + tall));
        }
    }
    let down = if lines.is_empty() { 0.0 } else { (lines.len() - 1) as f64 * pitch + tall };
    (places, wide.ceil() as usize, down.ceil() as usize, field)
}

// ---------------------------------------------------------- segment shapes

/// A segment font letter n rows tall: its width, height and stroke, in
/// pixels.
fn seg_box(n: usize) -> (usize, usize, usize) {
    let high = 2 * n;
    let wide = ((high as f64 * 0.66).round_ties_even() as usize).max(3);
    let t = if n < 12 { 1 } else { ((wide as f64 * 0.14).round_ties_even() as usize).max(1) };
    (wide, high, t)
}

fn seg_width(ch: char, n: usize, spacing: Spacing) -> usize {
    let (wide, _, t) = seg_box(n);
    if spacing == Spacing::Mono || ch.is_ascii_digit() {
        return wide;
    }
    if ch == ' ' {
        return ((wide as f64 * 0.6).round_ties_even() as usize).max(2);
    }
    if NARROW.contains(ch) {
        return ((t as f64 * 2.5).round_ties_even() as usize).max((wide as f64 * 0.35).round_ties_even() as usize).max(2);
    }
    wide
}

fn seg_gap(n: usize) -> usize {
    ((seg_box(n).0 as f64 * 0.28).round_ties_even() as usize).max(1)
}

/// A signed distance: below 0 inside.
#[derive(Clone, Debug)]
enum Sd {
    Poly(Vec<(f64, f64, f64)>),
    Across { cx: f64, cy: f64, half: f64, reach: f64, norm: f64 },
    Down { cx: f64, cy: f64, half: f64, reach: f64, norm: f64 },
    Stroke { x0: f64, y0: f64, nx: f64, ny: f64, half: f64, ax: f64, ay: f64, bx: f64, by: f64 },
    Dot { cx: f64, cy: f64, r: f64 },
}

/// A shape: its distance, moved `dx` across, and the box it lies in.
#[derive(Clone, Debug)]
struct Shape {
    sd: Sd,
    dx: f64,
    bx: (f64, f64, f64, f64),
}

fn fmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

impl Shape {
    fn at(&self, x: f64, y: f64) -> f64 {
        let x = x - self.dx;
        match &self.sd {
            Sd::Poly(planes) => {
                let mut best = f64::NEG_INFINITY;
                for (k, &(nx, ny, c)) in planes.iter().enumerate() {
                    let v = nx * x + ny * y - c;
                    if k == 0 { best = v } else { best = fmax(best, v) }
                }
                best
            }
            &Sd::Across { cx, cy, half, reach, norm } => {
                let (dx, dy) = ((x - cx).abs(), (y - cy).abs());
                fmax(dy - half, (dx + dy / STEEP - reach) / norm)
            }
            &Sd::Down { cx, cy, half, reach, norm } => {
                let (dx, dy) = ((x - cx).abs(), (y - cy).abs());
                fmax(dx - half, (dy + STEEP * dx - reach) / norm)
            }
            &Sd::Stroke { x0, y0, nx, ny, half, ax, ay, bx, by } => {
                let mut v = ((x - x0) * nx + (y - y0) * ny).abs() - half;
                for w in [ax - x, x - bx, ay - y, y - by] {
                    v = fmax(v, w);
                }
                v
            }
            &Sd::Dot { cx, cy, r } => hypot(x - cx, y - cy) - r,
        }
    }

    fn moved(&self, dx: f64) -> Shape {
        let (x0, y0, x1, y1) = self.bx;
        Shape { sd: self.sd.clone(), dx: self.dx + dx, bx: (x0 + dx, y0, x1 + dx, y1) }
    }
}

/// `math.hypot` of two, as Python works it out: scaled by a power of two
/// to the larger, the square root of the sum of squares made exact by
/// Dekker's products, and corrected to the nearer of its neighbours.
fn hypot(x: f64, y: f64) -> f64 {
    let (x, y) = (x.abs(), y.abs());
    let max = x.max(y);
    if max == 0.0 || !max.is_finite() {
        return if x.is_infinite() || y.is_infinite() { f64::INFINITY } else { max };
    }
    let (_, max_e) = frexp(max);
    let scale = ldexp(1.0, -max_e);
    let mut csum = 1.0;
    let mut frac1 = 0.0;
    let mut frac2 = 0.0;
    for v in [x, y] {
        let v = v * scale;
        let pr_hi = v * v;
        let pr_lo = v.mul_add(v, -pr_hi);
        let sm_hi = csum + pr_hi;
        let sm_lo = (csum - sm_hi) + pr_hi;
        csum = sm_hi;
        frac1 += pr_lo;
        frac2 += sm_lo;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let pr_hi = -h * h;
    let pr_lo = (-h).mul_add(h, -pr_hi);
    let sm_hi = csum + pr_hi;
    let sm_lo = (csum - sm_hi) + pr_hi;
    csum = sm_hi;
    frac1 += pr_lo;
    frac2 += sm_lo;
    let x2 = csum - 1.0 + (frac1 + frac2);
    h += x2 / (2.0 * h);
    h / scale
}

fn frexp(v: f64) -> (f64, i32) {
    if v == 0.0 || !v.is_finite() {
        return (v, 0);
    }
    let bits = v.to_bits();
    let exp = ((bits >> 52) & 0x7ff) as i32;
    if exp == 0 {
        let (m, e) = frexp(v * (2.0f64).powi(64));
        return (m, e - 64);
    }
    let e = exp - 1022;
    let m = f64::from_bits((bits & !(0x7ffu64 << 52)) | (1022u64 << 52));
    (m, e)
}

fn ldexp(m: f64, e: i32) -> f64 {
    let mut v = m;
    let mut e = e;
    while e > 1000 {
        v *= (2.0f64).powi(1000);
        e -= 1000;
    }
    while e < -1000 {
        v *= (2.0f64).powi(-1000);
        e += 1000;
    }
    v * (2.0f64).powi(e)
}

/// A bar from tip to tip, its ends pointed along the cell's diagonal.
fn bar(x0: f64, y0: f64, x1: f64, y1: f64, thick: f64) -> Shape {
    let half = thick / 2.0;
    let (cx, cy) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0);
    if y0 == y1 {
        let reach = (x1 - x0) / 2.0;
        let norm = hypot(1.0, 1.0 / STEEP);
        return Shape { sd: Sd::Across { cx, cy, half, reach, norm }, dx: 0.0, bx: (x0, cy - half, x1, cy + half) };
    }
    let reach = (y1 - y0) / 2.0;
    let norm = hypot(1.0, STEEP);
    Shape { sd: Sd::Down { cx, cy, half, reach, norm }, dx: 0.0, bx: (cx - half, y0, cx + half, y1) }
}

/// A diagonal stroke from a to b, cut square to the box they span.
fn stroke(a: (f64, f64), b: (f64, f64), thick: f64) -> Shape {
    let ((x0, y0), (x1, y1)) = (a, b);
    let (lx, ly) = (x1 - x0, y1 - y0);
    let mut norm = hypot(lx, ly);
    if norm == 0.0 {
        norm = 1.0;
    }
    let (nx, ny) = (-ly / norm, lx / norm);
    let (ax, bx) = (x0.min(x1), x0.max(x1));
    let (ay, by) = (y0.min(y1), y0.max(y1));
    Shape { sd: Sd::Stroke { x0, y0, nx, ny, half: thick / 2.0, ax, ay, bx, by }, dx: 0.0, bx: (ax, ay, bx, by) }
}

/// The pixels a diagonal must light: a line one pixel wide from a to b.
fn path(a: (f64, f64), b: (f64, f64)) -> Vec<(i64, i64)> {
    let ((x0, y0), (x1, y1)) = (a, b);
    let sx = if x1 > x0 { 1.0 } else { -1.0 };
    let sy = if y1 > y0 { 1.0 } else { -1.0 };
    let (px, py) = ((x0 + sx * 0.5).floor() as i64, (y0 + sy * 0.5).floor() as i64);
    let qx = (x1 - if sx > 0.0 { 0.5 } else { 0.0 }).floor() as i64;
    let qy = (y1 - if sy > 0.0 { 0.5 } else { 0.0 }).floor() as i64;
    let steps = (qx - px).abs().max((qy - py).abs());
    (0..=steps)
        .map(|k| {
            let f = if steps != 0 { k as f64 / steps as f64 } else { 0.0 };
            (
                (px as f64 + (qx - px) as f64 * f).round_ties_even() as i64,
                (py as f64 + (qy - py) as f64 * f).round_ties_even() as i64,
            )
        })
        .collect()
}

fn dot(cx: f64, cy: f64, r: f64) -> Shape {
    Shape { sd: Sd::Dot { cx, cy, r }, dx: 0.0, bx: (cx - r, cy - r, cx + r, cy + r) }
}

/// How much of each pixel a shape covers, as ((x, y), amount).
fn coverage(shape: &Shape, wide: usize, high: usize) -> Vec<((usize, usize), f64)> {
    let (x0, y0, x1, y1) = shape.bx;
    let step = 1.0 / SAMPLES as f64;
    let offs: Vec<f64> = (0..SAMPLES).map(|i| (i as f64 + 0.5) * step).collect();
    let mut out = Vec::new();
    let ys = ((y0.trunc() as i64) - 1).max(0)..((y1.trunc() as i64) + 2).min(high as i64);
    for py in ys {
        let xs = ((x0.trunc() as i64) - 1).max(0)..((x1.trunc() as i64) + 2).min(wide as i64);
        for px in xs {
            let (fx, fy) = (px as f64, py as f64);
            let d = shape.at(fx + 0.5, fy + 0.5);
            if d <= -EDGE {
                out.push(((px as usize, py as usize), 1.0));
            } else if d < EDGE {
                let mut hit = 0;
                for &i in &offs {
                    for &j in &offs {
                        if shape.at(fx + i, fy + j) <= 0.0 {
                            hit += 1;
                        }
                    }
                }
                if hit > 0 {
                    out.push(((px as usize, py as usize), hit as f64 / (SAMPLES * SAMPLES) as f64));
                }
            }
        }
    }
    out
}

fn seg_dark(n: usize, t: usize) -> f64 {
    if n >= 8 {
        return (t as f64 * 0.35).max(1.0);
    }
    if n >= 6 { 0.5 } else { 0.25 }
}

fn poly(planes: Vec<(f64, f64, f64)>, bx: (f64, f64, f64, f64)) -> Shape {
    Shape { sd: Sd::Poly(planes), dx: 0.0, bx }
}

/// The bars of a letter w by h, stroke t, middle at cx, cy, mitred where
/// they meet and a gap apart, by name: a-f, G the whole middle bar, g h
/// its halves, i j the centre bars.
fn mitred(w: f64, h: f64, t: f64, cx: f64, cy: f64, gap: f64) -> Vec<(char, Shape)> {
    let r = 0.5f64.sqrt();
    let (g, s) = (gap, gap / 2.0);
    let mid = (cy - t / 2.0, cy + t / 2.0);
    let across = |y0: f64, y1: f64| vec![(0.0, -1.0, -y0), (0.0, 1.0, y1)];
    let down = |x0: f64, x1: f64| vec![(-1.0, 0.0, -x0), (1.0, 0.0, x1)];
    let point_left = |px: f64, k: f64| vec![(-r, r, (cy - px) * r - k), (-r, -r, (-px - cy) * r - k)];
    let point_right = |px: f64, k: f64| vec![(r, r, (px + cy) * r - k), (r, -r, (px - cy) * r - k)];
    let cat = |a: Vec<(f64, f64, f64)>, b: Vec<(f64, f64, f64)>| [a, b].concat();
    vec![
        ('a', poly(cat(across(0.0, t), vec![(-r, r, -g), (r, r, w * r - g)]), (0.0, 0.0, w, t))),
        ('d', poly(cat(across(h - t, h), vec![(-r, -r, -h * r - g), (r, -r, (w - h) * r - g)]), (0.0, h - t, w, h))),
        ('G', poly([across(mid.0, mid.1), point_left(0.0, s), point_right(w, s)].concat(), (0.0, mid.0, w, mid.1))),
        ('g', poly([across(mid.0, mid.1), point_left(0.0, s), point_right(cx, s)].concat(), (0.0, mid.0, cx, mid.1))),
        ('h', poly([across(mid.0, mid.1), point_left(cx, s), point_right(w, s)].concat(), (cx, mid.0, w, mid.1))),
        ('f', poly(cat(down(0.0, t), vec![(r, -r, 0.0), (r, r, cy * r - s)]), (0.0, 0.0, t, cy))),
        ('e', poly(cat(down(0.0, t), vec![(r, -r, -cy * r - s), (r, r, h * r)]), (0.0, cy, t, h))),
        ('b', poly(cat(down(w - t, w), vec![(-r, -r, -w * r), (-r, r, (cy - w) * r - s)]), (w - t, 0.0, w, cy))),
        ('c', poly(cat(down(w - t, w), vec![(-r, -r, -(cy + w) * r - s), (-r, r, (h - w) * r)]), (w - t, cy, w, h))),
        ('i', poly(
            cat(down(cx - t / 2.0, cx + t / 2.0), vec![
                (r, -r, (cx - t - g) * r), (-r, -r, (-cx - t - g) * r),
                (r, r, (cx + cy) * r - s), (-r, r, (cy - cx) * r - s),
            ]),
            (cx - t / 2.0, t + g, cx + t / 2.0, cy),
        )),
        ('j', poly(
            cat(down(cx - t / 2.0, cx + t / 2.0), vec![
                (r, -r, (cx - cy) * r - s), (-r, -r, -(cx + cy) * r - s),
                (r, r, (cx + h - t - g) * r), (-r, r, (h - t - g - cx) * r),
            ]),
            (cx - t / 2.0, cy, cx + t / 2.0, h - t - g),
        )),
    ]
}

/// Every segment of a letter n rows tall, by name, and the diagonals'
/// lines of pixels.
#[allow(clippy::type_complexity)]
fn seg_shapes(n: usize) -> (Vec<(char, Shape)>, Vec<(char, Vec<(i64, i64)>)>) {
    let (w, h, t) = seg_box(n);
    let (wf, hf, tf) = (w as f64, h as f64, t as f64);
    let gap = seg_dark(n, t);
    let cx = ((w - t) as f64 / 2.0).floor() + tf / 2.0;
    let cy = ((h - t) as f64 / 2.0).floor() + tf / 2.0;
    let short = |corner: (f64, f64)| {
        let (dx, dy) = (corner.0 - cx, corner.1 - cy);
        let length = hypot(dx, dy);
        let d = (tf + gap).min(length - tf);
        (corner, (cx + dx / length * d, cy + dy / length * d))
    };
    let ends = [
        ('k', short((tf + gap, tf + gap))),
        ('l', short(((w - t) as f64 - gap, tf + gap))),
        ('m', short((tf + gap, (h - t) as f64 - gap))),
        ('n', short(((w - t) as f64 - gap, (h - t) as f64 - gap))),
    ];
    let mut shapes = mitred(wf, hf, tf, cx, cy, gap);
    let mut paths = Vec::new();
    for (name, (a, b)) in ends {
        shapes.push((name, stroke(a, b, tf)));
        paths.push((name, path(a, b)));
    }
    (shapes, paths)
}

/// A segment character's lit and unlit shapes and lines, and its width.
struct GlyphShapes {
    lit: Vec<Shape>,
    dim: Vec<Shape>,
    width: usize,
    lit_paths: Vec<Vec<(i64, i64)>>,
    dim_paths: Vec<Vec<(i64, i64)>>,
}

fn glyph_shapes(ch: char, n: usize, spacing: Spacing, seven: bool) -> GlyphShapes {
    let (w, h, t) = seg_box(n);
    let (hf, tf) = (h as f64, t as f64);
    let width = seg_width(ch, n, spacing);
    let mut out = GlyphShapes { lit: vec![], dim: vec![], width, lit_paths: vec![], dim_paths: vec![] };
    if NARROW.contains(ch) {
        let r = (tf * 0.75).max(0.8);
        let cx = width as f64 / 2.0;
        out.lit = match ch {
            ':' => vec![dot(cx, hf * 0.3, r), dot(cx, hf * 0.7, r)],
            '.' | ',' => vec![dot(cx, hf - r, r)],
            '\'' => vec![bar(cx, 0.0, cx, hf * 0.3, tf)],
            _ => vec![bar(cx, hf * 0.03, cx, hf * 0.73, tf), dot(cx, hf - r, r)],
        };
    } else if ch != ' ' {
        let (on, off): (Vec<char>, Vec<char>) = if seven {
            let on: HashSet<char> = lookup(SEVENS, ch).unwrap_or("").chars().map(|k| if k == 'g' { 'G' } else { k }).collect();
            let off = "abcdefG".chars().filter(|k| !on.contains(k)).collect();
            (on.into_iter().collect(), off)
        } else {
            let on: HashSet<char> = lookup(SEGMENTS, ch).unwrap_or("").chars().collect();
            let mut off: Vec<char> = "abcdefghijklmn".chars().filter(|k| !on.contains(k)).collect();
            if on.contains(&'G') {
                off.retain(|k| *k != 'g' && *k != 'h');
            }
            (on.into_iter().collect(), off)
        };
        let (mut on, mut off) = (on, off);
        on.sort();
        off.sort();
        let shift = (width as i64 - w as i64).div_euclid(2);
        let (shapes, paths) = seg_shapes(n);
        let shape = |k: char| shapes.iter().find(|(name, _)| *name == k).unwrap().1.moved(shift as f64);
        let line = |k: char| {
            paths.iter().find(|(name, _)| *name == k).map(|(_, p)| p.iter().map(|&(x, y)| (x + shift, y)).collect::<Vec<_>>())
        };
        out.lit = on.iter().map(|&k| shape(k)).collect();
        out.dim = off.iter().map(|&k| shape(k)).collect();
        out.lit_paths = on.iter().filter_map(|&k| line(k)).collect();
        out.dim_paths = off.iter().filter_map(|&k| line(k)).collect();
    }
    out
}

/// A segment character as lit and unlit coverage, width x 2n row-major,
/// and its width.
fn seg_glyph(ch: char, n: usize, spacing: Spacing, seven: bool) -> (Vec<f64>, Vec<f64>, usize) {
    let gs = glyph_shapes(ch, n, spacing, seven);
    let (width, h) = (gs.width, 2 * n);
    let mut lit = vec![0.0; width * h];
    let mut dim = vec![0.0; width * h];
    for (into, shapes, paths) in [(&mut lit, &gs.lit, &gs.lit_paths), (&mut dim, &gs.dim, &gs.dim_paths)] {
        for shape in shapes {
            for ((x, y), a) in coverage(shape, width, h) {
                if n >= 8 || a >= 0.35 {
                    let p = &mut into[y * width + x];
                    *p = (*p + a).min(1.0);
                }
            }
        }
        for p in paths {
            for &(x, y) in p {
                if x >= 0 && (x as usize) < width && y >= 0 && (y as usize) < h {
                    into[y as usize * width + x as usize] = 1.0;
                }
            }
        }
    }
    (lit, dim, width)
}

/// The ways a cell can be split between two colours, the character that
/// draws the first part.
const SPLITS: [char; 6] = ['▀', '▄', '◢', '◣', '◤', '◥'];

fn split_has(split: char, u: f64, v: f64) -> bool {
    match split {
        '▀' => v < 0.5,
        '▄' => v >= 0.5,
        '◢' => u + v >= 1.0,
        '◣' => v >= u,
        '◤' => u + v < 1.0,
        _ => v < u,
    }
}

fn points() -> Vec<(f64, f64)> {
    let mut out = Vec::new();
    for j in 0..8 {
        for i in 0..4 {
            out.push(((i as f64 + 0.5) / 4.0, (j as f64 + 0.5) / 8.0));
        }
    }
    out
}

fn other(split: char) -> char {
    match split {
        '▀' => '▄',
        '▄' => '▀',
        '◢' => '◤',
        '◤' => '◢',
        '◣' => '◥',
        _ => '◣',
    }
}

fn argmax(v: &[usize; 3]) -> usize {
    let mut best = 0;
    for k in 1..3 {
        if v[k] > v[best] {
            best = k;
        }
    }
    best
}

/// A cut cell: its split ('\0' for whole), and which of nothing (0), lit
/// (1) and unlit (2) its two parts show.
type Cut = (char, usize, usize);

/// A segment character with its edges cut: each cell the split of it
/// that matches its points best.
fn seg_cut(ch: char, n: usize, spacing: Spacing, ghosted: bool, seven: bool) -> Vec<Vec<Cut>> {
    let mut gs = glyph_shapes(ch, n, spacing, seven);
    if !ghosted {
        gs.dim.clear();
        gs.dim_paths.clear();
    }
    let width = gs.width;
    let pts = points();
    let split_in: Vec<(char, Vec<usize>)> = SPLITS
        .iter()
        .map(|&s| (s, pts.iter().enumerate().filter(|(_, (u, v))| split_has(s, *u, *v)).map(|(k, _)| k).collect()))
        .collect();
    let mut out: Vec<Vec<Cut>> = Vec::new();
    for r in 0..n {
        let mut line = Vec::new();
        for c in 0..width {
            let (cf, rf) = (c as f64, r as f64);
            let close = |shapes: &Vec<Shape>| -> Vec<Shape> {
                shapes
                    .iter()
                    .filter(|s| {
                        let (x0, y0, x1, y1) = s.bx;
                        x0 - 1.0 <= cf + 1.0 && cf <= x1 + 1.0 && y0 - 1.0 <= 2.0 * rf + 2.0 && 2.0 * rf <= y1 + 1.0
                    })
                    .cloned()
                    .collect()
            };
            let near_on = close(&gs.lit);
            let near_off = close(&gs.dim);
            if near_on.is_empty() && near_off.is_empty() {
                line.push(('\0', 0, 0));
                continue;
            }
            let seen: Vec<usize> = pts
                .iter()
                .map(|&(u, v)| {
                    let (x, y) = (cf + u, 2.0 * rf + 2.0 * v);
                    if near_on.iter().any(|s| s.at(x, y) <= 0.0) {
                        1
                    } else if near_off.iter().any(|s| s.at(x, y) <= 0.0) {
                        2
                    } else {
                        0
                    }
                })
                .collect();
            let mut counts = [0usize; 3];
            for &k in &seen {
                counts[k] += 1;
            }
            let most = argmax(&counts);
            let mut best: Cut = ('\0', most, most);
            let mut wrong = 32 - counts[most];
            for (split, inside) in &split_in {
                if wrong == 0 {
                    break;
                }
                let mut part = [0usize; 3];
                for &k in inside {
                    part[seen[k]] += 1;
                }
                let rest = [counts[0] - part[0], counts[1] - part[1], counts[2] - part[2]];
                let a = argmax(&part);
                let b = argmax(&rest);
                let miss = (inside.len() - part[a]) + (32 - inside.len() - rest[b]);
                if a != b && miss < wrong {
                    best = (*split, a, b);
                    wrong = miss;
                }
            }
            line.push(best);
        }
        out.push(line);
    }
    for (paths, want) in [(&gs.dim_paths, 2usize), (&gs.lit_paths, 1usize)] {
        for p in paths {
            for &(x, y) in p {
                if !(x >= 0 && (x as usize) < width && y >= 0 && (y as usize) < 2 * n) {
                    continue;
                }
                let (x, y) = (x as usize, y as usize);
                let (r, top) = (y / 2, y % 2 == 0);
                let (split, a, b) = out[r][x];
                let v = if top { 0.25 } else { 0.75 };
                let here = if split == '\0' || split_has(split, 0.5, v) { a } else { b };
                if here == want || (want == 2 && here == 1) {
                    continue;
                }
                out[r][x] = if split == '\0' { (if top { '▀' } else { '▄' }, want, a) } else { ('\0', want, want) };
            }
        }
    }
    out
}

fn cut_cells(parts: &[Vec<Cut>], fg: i32, bg: Option<i32>, ghost: Option<i32>) -> Cells {
    let colour = [bg, Some(fg), ghost];
    parts
        .iter()
        .map(|line| {
            line.iter()
                .map(|&(split, a, b)| {
                    let (mut inside, mut outside) = (colour[a], colour[b]);
                    if split == '\0' {
                        return inside.and_then(|c| piece(' ', None, Some(c)));
                    }
                    let mut split = split;
                    if inside.is_none() {
                        split = other(split);
                        inside = outside;
                        outside = None;
                    }
                    piece(split, inside, outside)
                })
                .collect()
        })
        .collect()
}

// ---------------------------------------------------------------- layout

fn split_lines(text: &str) -> Lines {
    text.split('\n').map(|l| l.chars().collect()).collect()
}

/// The block's size in the font's own pixels, before turning or scaling:
/// line widths, width, height, a line's height and the gap between lines.
fn native(font: Font, n: usize, lines: &Lines, o: &O) -> (Vec<i64>, i64, i64, i64, i64) {
    let (widths, tall, gap): (Vec<i64>, i64, i64) = if font == Font::Segment {
        let widths = lines
            .iter()
            .map(|l| {
                l.iter().map(|&c| seg_width(c, n, o.spacing) as i64).sum::<i64>()
                    + seg_gap(n) as i64 * (l.len() as i64 - 1).max(0)
            })
            .collect();
        (widths, 2 * n as i64, 2 * ((n as f64 / 3.0).round_ties_even() as i64).max(1))
    } else {
        let widths = lines
            .iter()
            .map(|l| l.iter().map(|&c| bits_width(font, c, o.spacing) as i64).sum::<i64>() + (l.len() as i64 - 1).max(0))
            .collect();
        (widths, if font == Font::Dots { 8 } else { 5 }, 1)
    };
    let wide = widths.iter().copied().max().unwrap_or(0);
    let k = lines.len() as i64;
    let high = k * tall + (k - 1) * gap;
    (widths, wide, high, tall, gap)
}

fn per_cell(font: Font) -> (i64, i64) {
    match font {
        Font::Block => (1, 1),
        Font::Dots => (2, 4),
        _ => (1, 2),
    }
}

fn dims(font: Font, n: usize, lines: &Lines, o: &O) -> (usize, usize) {
    let (mut wide, mut high) = if font == Font::Dots {
        let (_, w, h, _) = dots_place(n, lines, o);
        (w as i64, h as i64)
    } else {
        let (_, w, h, _, _) = native(font, n, lines, o);
        (w, h)
    };
    if o.turn.is_some() {
        std::mem::swap(&mut wide, &mut high);
    }
    if matches!(font, Font::Block | Font::Half) {
        wide *= (n * widen(font, o.cell)) as i64;
        high *= n as i64;
    }
    let (cx, cy) = per_cell(font);
    ((-(-wide).div_euclid(cx)).max(0) as usize, (-(-high).div_euclid(cy)).max(0) as usize)
}

fn smallest(font: Font) -> usize {
    match font {
        Font::Segment => SEGMENT_MIN,
        Font::Dots => 2,
        _ => 1,
    }
}

/// The size step the options ask for, or None where nothing fits.
fn step(font: Font, lines: &Lines, o: &O) -> Option<usize> {
    let Some(want) = o.height else {
        let s = o.scale.max(1);
        return Some(match font {
            Font::Segment => SEGMENT_BASE * s,
            Font::Dots => 2 * s,
            _ => s,
        });
    };
    let lo0 = smallest(font);
    if dims(font, lo0, lines, o).1 > want {
        return None;
    }
    let (mut lo, mut hi) = (lo0, lo0);
    // Text with no characters, turned, is no taller at any step: Python
    // doubles until it overflows, and here the doubling stops.
    while hi < 1 << 16 && dims(font, hi * 2, lines, o).1 <= want {
        hi *= 2;
    }
    hi *= 2;
    while hi - lo > 1 {
        let mid = (lo + hi) / 2;
        if dims(font, mid, lines, o).1 <= want {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    Some(lo)
}

fn any_text(lines: &Lines) -> bool {
    lines.iter().any(|l| !l.is_empty())
}

/// (cols, rows) the text will take, without drawing it.
///
/// A pictured font is not drawn here, and takes (0, 0): `draw` draws it
/// in a cell font instead.
pub fn size(text: &str, style: &Style, wall: Option<&WallInfo>) -> (usize, usize) {
    if style.font.pictured() {
        return (0, 0);
    }
    let (font, o) = setup(style, wall, Align::Left);
    let lines = split_lines(&plain_o(text, font, &o));
    match step(font, &lines, &o) {
        Some(n) if any_text(&lines) => dims(font, n, &lines, &o),
        _ => (0, 0),
    }
}

// --------------------------------------------------------------- drawing

fn turned<T: Clone>(grid: &[Vec<T>], turn: Turn) -> Vec<Vec<T>> {
    if grid.is_empty() {
        return Vec::new();
    }
    let (high, wide) = (grid.len(), grid[0].len());
    match turn {
        Turn::Up => (0..wide).map(|x| (0..high).map(|y| grid[y][wide - 1 - x].clone()).collect()).collect(),
        Turn::Down => (0..wide).map(|x| (0..high).map(|y| grid[high - 1 - y][x].clone()).collect()).collect(),
    }
}

/// `Dots` text as a grid of lit dots and a grid of where a ghost's unlit
/// dots show.
fn dotted(n: usize, lines: &Lines, o: &O) -> (Vec<Vec<bool>>, Vec<Vec<bool>>) {
    let (places, across, down, boxes) = dots_place(n, lines, o);
    let size = dot_size(n);
    let mut lit = vec![vec![false; across]; down];
    let mut field = vec![vec![false; across]; down];
    for (ch, left, top) in places {
        let (col0, row0) = (left.floor(), top.floor());
        for (gx, gy) in dots_glyph(ch, o.spacing, size, left - col0, top - row0) {
            let (y, x) = (row0 as i64 + gy as i64, col0 as i64 + gx as i64);
            if y >= 0 && (y as usize) < down && x >= 0 && (x as usize) < across {
                lit[y as usize][x as usize] = true;
            }
        }
    }
    for (x0, y0, x1, y1) in boxes {
        for y in (y0.trunc() as usize)..down.min(y1.ceil() as usize) {
            for x in (x0.trunc() as usize)..across.min(x1.ceil() as usize) {
                field[y][x] = true;
            }
        }
    }
    if let Some(t) = o.turn {
        lit = turned(&lit, t);
        field = turned(&field, t);
    }
    (lit, field)
}

type Grid = Vec<Vec<f64>>;

/// The block as lit and unlit coverage, in the pixels the font packs
/// into cells.
fn pixels(font: Font, n: usize, lines: &Lines, o: &O) -> (Grid, Grid) {
    let (widths, wide, high, tall, gap) = native(font, n, lines, o);
    let (wide, high) = (wide as usize, high as usize);
    let mut lit = vec![vec![0.0; wide]; high];
    let mut dim = vec![vec![0.0; wide]; high];
    for (i, line) in lines.iter().enumerate() {
        let top = i * (tall + gap) as usize;
        let mut x = offset(widths[i], wide as i64, o.align);
        for &ch in line {
            if font == Font::Segment {
                let (on, off, width) = seg_glyph(ch, n, o.spacing, o.seven);
                for gy in 0..2 * n {
                    for gx in 0..width {
                        let px = x + gx as i64;
                        if px < 0 || px as usize >= wide {
                            continue;
                        }
                        let (a, d) = (on[gy * width + gx], off[gy * width + gx]);
                        if a != 0.0 {
                            lit[top + gy][px as usize] = a;
                        }
                        if d != 0.0 {
                            dim[top + gy][px as usize] = d;
                        }
                    }
                }
                x += (width + seg_gap(n)) as i64;
            } else {
                let g = bits(font, ch, o.spacing);
                for (gy, row) in g.iter().enumerate() {
                    for (gx, &v) in row.iter().enumerate() {
                        if v != 0 {
                            lit[top + gy][(x + gx as i64) as usize] = 1.0;
                        }
                    }
                }
                x += g[0].len() as i64 + 1;
            }
        }
    }
    if let Some(t) = o.turn {
        lit = turned(&lit, t);
        dim = turned(&dim, t);
    }
    if font != Font::Segment {
        let across = n * widen(font, o.cell);
        lit = lit
            .iter()
            .flat_map(|row| {
                let wide: Vec<f64> = row.iter().flat_map(|&v| std::iter::repeat_n(v, across)).collect();
                std::iter::repeat_n(wide, n)
            })
            .collect();
        dim = if lit.is_empty() { Vec::new() } else { vec![vec![0.0; lit[0].len()]; lit.len()] };
    }
    (lit, dim)
}

/// `top` laid over `under` at `amount`.
fn mix(top: (i32, i32, i32), under: (i32, i32, i32), amount: f64) -> i32 {
    let m = |c: i32, u: i32| (u as f64 + (c - u) as f64 * amount).round_ties_even() as i32;
    (m(top.0, under.0) << 16) | (m(top.1, under.1) << 8) | m(top.2, under.2)
}

type Pix = Vec<Vec<Option<i32>>>;

/// Each pixel's colour, or the background.
#[allow(clippy::too_many_arguments)]
fn colours(lit: &Grid, dim: &Grid, fg: i32, bg: Option<i32>, ghost: Option<i32>, smooth: bool, base: i32) -> Pix {
    let (f, b) = (rgb(fg), rgb(base));
    let gh = ghost.map(rgb);
    let paint = |a: f64, g: f64| -> Option<i32> {
        if smooth {
            if a <= 0.0 && (g <= 0.0 || gh.is_none()) {
                return bg;
            }
            let under = match gh {
                Some(gh) if g > 0.0 => rgb(mix(gh, b, g)),
                _ => b,
            };
            return Some(if a > 0.0 { mix(f, under, a) } else { mix(under, under, 1.0) });
        }
        if a >= 0.5 {
            return Some(fg);
        }
        if ghost.is_some() && g >= 0.5 {
            return ghost;
        }
        bg
    };
    lit.iter().zip(dim).map(|(ra, rg)| ra.iter().zip(rg).map(|(&a, &g)| paint(a, g)).collect()).collect()
}

/// Pixels into cells, in the font's way of making a cell.
fn pack(font: Font, pix: &Pix, cols: usize, rows: usize, bg: Option<i32>) -> Cells {
    let high = pix.len();
    let wide = if high > 0 { pix[0].len() } else { 0 };
    let at = |x: usize, y: usize| if y < high && x < wide { pix[y][x] } else { bg };
    (0..rows)
        .map(|r| {
            (0..cols)
                .map(|c| {
                    if font == Font::Block {
                        return at(c, r).and_then(|v| piece(' ', None, Some(v)));
                    }
                    let (up, down) = (at(c, 2 * r), at(c, 2 * r + 1));
                    if up == down {
                        up.and_then(|v| piece(' ', None, Some(v)))
                    } else if up.is_none() {
                        piece('▄', down, None)
                    } else if down.is_none() {
                        piece('▀', up, None)
                    } else {
                        piece('▀', up, down)
                    }
                })
                .collect()
        })
        .collect()
}

const BRAILLE: [[u32; 2]; 4] = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]];

/// Dots into braille cells; a cell in a ghost's field shows its unlit
/// dots in the ghost's colour.
fn pack_dots(lit: &[Vec<bool>], field: &[Vec<bool>], cols: usize, rows: usize, fg: i32, bg: Option<i32>, ghost: Option<i32>) -> Cells {
    let high = lit.len();
    let wide = if high > 0 { lit[0].len() } else { 0 };
    let blank = bg.and_then(|b| piece(' ', None, Some(b)));
    (0..rows)
        .map(|r| {
            (0..cols)
                .map(|c| {
                    let (mut bits, mut faint) = (0u32, false);
                    for dy in 0..4 {
                        let y = 4 * r + dy;
                        if y >= high {
                            break;
                        }
                        for dx in 0..2 {
                            let x = 2 * c + dx;
                            if x < wide {
                                if lit[y][x] {
                                    bits |= BRAILLE[dy][dx];
                                }
                                if field[y][x] {
                                    faint = true;
                                }
                            }
                        }
                    }
                    let ch = char::from_u32(0x2800 + bits).unwrap();
                    if faint && ghost.is_some() {
                        Some(Piece { ch, fg: Some(fg), bg: ghost, faint: true })
                    } else if bits != 0 {
                        piece(ch, Some(fg), bg)
                    } else {
                        blank
                    }
                })
                .collect()
        })
        .collect()
}

/// The corners of bitmap pixel x, y to cut or fill, and the half drawn.
fn corners(at: &dyn Fn(i64, i64) -> bool, x: i64, y: i64) -> Vec<(&'static str, char)> {
    let mut out = Vec::new();
    let here = at(x, y);
    for (corner, dy, dx) in [("tl", -1, -1), ("tr", -1, 1), ("bl", 1, -1), ("br", 1, 1)] {
        let (side, vert, diag) = (at(x + dx, y), at(x, y + dy), at(x + dx, y + dy));
        if here {
            if !side && !vert && !diag && at(x - dx, y) && at(x, y - dy) {
                out.push((corner, keep(corner)));
            }
        } else if side && vert && !diag {
            out.push((corner, fill(corner)));
        }
    }
    out
}

fn keep(corner: &str) -> char {
    match corner {
        "tl" => '◢',
        "tr" => '◣',
        "bl" => '◥',
        _ => '◤',
    }
}

fn fill(corner: &str) -> char {
    match corner {
        "tl" => '◤',
        "tr" => '◥',
        "bl" => '◣',
        _ => '◢',
    }
}

/// Block or Half cells with their corners cut and their diagonal steps
/// joined. `bits` is the bitmap as drawn before scaling, each of its
/// pixels `across` cells wide and n of the font's pixels tall.
#[allow(clippy::too_many_arguments)]
fn pack_cut(font: Font, bits: &Grid, n: usize, across: usize, square: Cells, fg: i32, bg: Option<i32>) -> Cells {
    let high = bits.len() as i64;
    let wide = if high > 0 { bits[0].len() as i64 } else { 0 };
    let per = if font == Font::Block { 1 } else { 2 };
    let mut out = square;
    let rows = out.len() as i64;
    let cols = if rows > 0 { out[0].len() as i64 } else { 0 };
    let full = piece(' ', None, Some(fg));
    let blank = bg.and_then(|b| piece(' ', None, Some(b)));
    let at = |x: i64, y: i64| y >= 0 && y < high && x >= 0 && x < wide && bits[y as usize][x as usize] != 0.0;

    let mut outside: HashSet<(i64, i64)> = HashSet::new();
    let mut todo: Vec<(i64, i64)> = Vec::new();
    for x in -1..=wide {
        for y in [-1, high] {
            todo.push((x, y));
        }
    }
    for y in 0..high {
        for x in [-1, wide] {
            todo.push((x, y));
        }
    }
    while let Some((x, y)) = todo.pop() {
        if outside.contains(&(x, y)) || !(-1 <= x && x <= wide && -1 <= y && y <= high) {
            continue;
        }
        if at(x, y) {
            continue;
        }
        outside.insert((x, y));
        todo.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]);
    }

    let (n, per_i) = (n as i64, per as i64);
    for py in 0..high {
        let (top, bottom) = (py * n, py * n + n);
        let first = -(-top).div_euclid(per_i);
        let last = bottom.div_euclid(per_i) - 1;
        if last < first {
            continue;
        }
        for px in 0..wide {
            let here = at(px, py);
            if !here && !outside.contains(&(px, py)) {
                continue;
            }
            for (corner, half) in corners(&at, px, py) {
                let t = corner.as_bytes()[0] == b't';
                let l = corner.as_bytes()[1] == b'l';
                if (if t { top } else { bottom }).rem_euclid(per_i) != 0 {
                    continue;
                }
                let c = px * across as i64 + if l { 0 } else { across as i64 - 1 };
                let r = if t { first } else { last };
                if !(0 <= r && r < rows && 0 <= c && c < cols) {
                    continue;
                }
                let (r, c) = (r as usize, c as usize);
                if out[r][c] != if here { full } else { blank } {
                    continue;
                }
                out[r][c] = piece(half, Some(fg), bg);
            }
        }
    }
    out
}

/// The text's cells: `font` the glyph font and `o` the settled options.
fn render(text: &str, font: Font, fg: i32, bg: Option<i32>, base: Option<i32>, o: &O) -> Cells {
    let lines = split_lines(&plain_o(text, font, o));
    let n = step(font, &lines, o);
    let base_c = base.unwrap_or(0);
    let smooth = font == Font::Segment
        && o.smooth
        && n.is_some_and(|n| smooth_helps(n, o.px))
        && matches!(o.depth, Depth::True | Depth::Xterm256)
        && is_hex(fg)
        && is_hex(base_c)
        && o.ghost.is_none_or(is_hex);
    let ghost = if matches!(font, Font::Segment | Font::Dots) { ghost_for(o.ghost, fg, o.depth) } else { None };
    let cut = o.edges == Edges::Cut
        && !matches!(font, Font::Dots | Font::Half)
        && !(font == Font::Segment && (smooth || o.turn.is_some()));
    let Some(n) = n else { return Vec::new() };
    if !any_text(&lines) {
        return Vec::new();
    }
    if font == Font::Segment && o.turn.is_none() {
        return seg_rows(&lines, n, o, fg, bg, ghost, smooth, base_c, cut);
    }
    let (cols, rows) = dims(font, n, &lines, o);
    if font == Font::Dots {
        let (lit, field) = dotted(n, &lines, o);
        return pack_dots(&lit, &field, cols, rows, fg, bg, ghost);
    }
    let (lit, dim) = pixels(font, n, &lines, o);
    let pix = colours(&lit, &dim, fg, bg, ghost, smooth, base_c);
    let got = pack(font, &pix, cols, rows, bg);
    if cut {
        let one = O { cell: 0.0, ..o.clone() };
        let (bitmap, _) = pixels(font, 1, &lines, &one);
        return pack_cut(font, &bitmap, n, n * widen(font, o.cell), got, fg, bg);
    }
    got
}

/// One segment character as cells, a pixel a cell wide.
#[allow(clippy::too_many_arguments)]
fn seg_cells(ch: char, n: usize, spacing: Spacing, fg: i32, bg: Option<i32>, ghost: Option<i32>, smooth: bool, base: i32, cut: bool, seven: bool) -> Cells {
    if cut {
        let parts = seg_cut(ch, n, spacing, ghost.is_some(), seven);
        return cut_cells(&parts, fg, bg, ghost);
    }
    let (on, off, width) = seg_glyph(ch, n, spacing, seven);
    let lit: Grid = (0..2 * n).map(|y| on[y * width..(y + 1) * width].to_vec()).collect();
    let dim: Grid = (0..2 * n).map(|y| off[y * width..(y + 1) * width].to_vec()).collect();
    let pix = colours(&lit, &dim, fg, bg, ghost, smooth, base);
    pack(Font::Segment, &pix, width, n, bg)
}

/// Upright segment text, assembled from its characters' cells.
#[allow(clippy::too_many_arguments)]
fn seg_rows(lines: &Lines, n: usize, o: &O, fg: i32, bg: Option<i32>, ghost: Option<i32>, smooth: bool, base: i32, cut: bool) -> Cells {
    let (widths, _, _, _, gap) = native(Font::Segment, n, lines, o);
    let (cols, _) = dims(Font::Segment, n, lines, o);
    let blank = bg.and_then(|b| piece(' ', None, Some(b)));
    let space = seg_gap(n);
    let mut out: Cells = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if i > 0 {
            for _ in 0..gap / 2 {
                out.push(vec![blank; cols]);
            }
        }
        let left = offset(widths[i], cols as i64, o.align).max(0) as usize;
        let mut rows: Cells = (0..n).map(|_| vec![blank; left]).collect();
        for (j, &ch) in line.iter().enumerate() {
            if j > 0 {
                for r in rows.iter_mut() {
                    r.extend(std::iter::repeat_n(blank, space));
                }
            }
            let cells = seg_cells(ch, n, o.spacing, fg, bg, ghost, smooth, base, cut, o.seven);
            for (r, part) in rows.iter_mut().zip(cells) {
                r.extend(part);
            }
        }
        for mut r in rows {
            if r.len() < cols {
                r.resize(cols, blank);
            }
            out.push(r);
        }
    }
    out
}

/// The text as rows of cells, `None` where nothing is drawn (Python's
/// `rows`). A smooth edge is mixed into `bg`, or black where there is
/// none; `draw` mixes it into the frame's own colour instead.
///
/// A pictured font is not drawn here, and is no rows.
pub fn rows(text: &str, style: &Style, wall: Option<&WallInfo>) -> Cells {
    if style.font.pictured() {
        return Vec::new();
    }
    let (font, o) = setup(style, wall, Align::Left);
    render(text, font, style.fg, style.bg, style.bg, &o)
}

fn put(frame: &mut Frame, gx: i64, gy: i64, p: Piece) {
    let i = gy as usize * frame.cols + gx as usize;
    frame.ch[i] = p.ch as u32;
    if let Some(fg) = p.fg {
        frame.fg[i] = fg;
    }
    if let Some(bg) = p.bg {
        frame.bg[i] = bg;
    }
    frame.attrs[i] = if p.faint { FAINT } else { 0 };
}

fn inside(frame: &Frame, x: i64, y: i64) -> bool {
    x >= 0 && y >= 0 && (x as usize) < frame.cols && (y as usize) < frame.rows
}

/// Paint the text into a frame, its top left at x, y, clipped to the
/// frame; returns the (cols, rows) it takes.
///
/// Given the wall, a font it cannot draw is drawn as the nearest it can,
/// as `size` measures it. A pictured font is drawn as Python draws one it
/// cannot picture: `Block` with its corners cut, `Half` or `Dots` for
/// `Tile`, `Dots`, `Half` or `Block` for a face, the first that fits, or
/// the words themselves on one row.
pub fn draw(frame: &mut Frame, x: i64, y: i64, text: &str, style: &Style, wall: Option<&WallInfo>) -> (usize, usize) {
    if style.font.pictured() {
        return draw_instead(frame, x, y, text, style, wall);
    }
    let (font, o) = setup(style, wall, Align::Left);
    let mut base = style.bg;
    if base.is_none() && inside(frame, x, y) {
        let under = frame.bg[y as usize * frame.cols + x as usize];
        base = if is_hex(under) { Some(under) } else { None };
    }
    let out = render(text, font, style.fg, style.bg, base, &o);
    for (dy, line) in out.iter().enumerate() {
        for (dx, c) in line.iter().enumerate() {
            let (gx, gy) = (x + dx as i64, y + dy as i64);
            if let Some(p) = c {
                if inside(frame, gx, gy) {
                    put(frame, gx, gy, *p);
                }
            }
        }
    }
    (out.first().map_or(0, Vec::len), out.len())
}

/// A pictured font drawn in the first of its cell fonts the wall can draw
/// and the size asked for fits (`_tile_fallback`).
fn draw_instead(frame: &mut Frame, x: i64, y: i64, text: &str, style: &Style, wall: Option<&WallInfo>) -> (usize, usize) {
    let base = Style { ghost: None, edges: None, ..style.clone() };
    let order: [(Font, Option<Edges>); 3] = if style.font == Font::Tile {
        [(Font::Block, Some(Edges::Cut)), (Font::Half, None), (Font::Dots, None)]
    } else {
        [(Font::Dots, None), (Font::Half, None), (Font::Block, Some(Edges::Cut))]
    };
    for (font, edges) in order {
        if !usable(font, wall) {
            continue;
        }
        let mut these = Style { font, edges, ..base.clone() };
        if these.edges == Some(Edges::Cut) && wall.is_some_and(|w| !w.can("◢◣◤◥")) {
            these.edges = Some(Edges::Square);
        }
        if size(text, &these, wall) != (0, 0) {
            return draw(frame, x, y, text, &these, wall);
        }
    }
    let line: Vec<char> = plain(text, Font::Block).replace('\n', " ").trim().chars().collect();
    if line.is_empty() || !(y >= 0 && (y as usize) < frame.rows) {
        return (0, 0);
    }
    for (i, &ch) in line.iter().enumerate() {
        let gx = x + i as i64;
        if gx >= 0 && (gx as usize) < frame.cols {
            let bg = style.bg;
            put(frame, gx, y, Piece { ch, fg: Some(style.fg), bg, faint: false });
        }
    }
    (line.len().min((frame.cols as i64 - x).max(0) as usize), 1)
}

// ------------------------------------------------------------------- fit

/// What `fit` chose: the text as it is to be drawn, broken onto lines
/// where it was wrapped, and the style to draw it with, its font the one
/// that fitted and its height the rows it takes. Draw it as it is:
/// `draw(&mut f, x, y, &got.text, &got.style, None)`.
#[derive(Clone, Debug, PartialEq)]
pub struct Fitted {
    pub text: String,
    pub style: Style,
}

/// Words broken into lines no wider than cols at step n, or None where
/// one word alone is wider.
fn wrapped(font: Font, n: usize, words: &[&str], cols: i64, o: &O) -> Option<Vec<String>> {
    let mut lines = Vec::new();
    let mut line = String::new();
    let width = |s: &str| dims(font, n, &vec![s.chars().collect()], o).0 as i64;
    for &word in words {
        if width(word) > cols {
            return None;
        }
        let trial = if line.is_empty() { word.to_string() } else { format!("{line} {word}") };
        if line.is_empty() || width(&trial) <= cols {
            line = trial;
        } else {
            lines.push(std::mem::replace(&mut line, word.to_string()));
        }
    }
    if !line.is_empty() {
        lines.push(line);
    }
    Some(lines)
}

/// The biggest the text can be drawn in cols x rows, or None.
///
/// Fonts are tried in the order given, passing over any the wall cannot
/// draw, and the first that fits at all is the answer, at its biggest.
/// With `wrap`, upright text may be broken between words onto more lines
/// where that lets it be bigger. `style` gives the options; its font is
/// not used.
///
/// With a wall (or `px`), the answer suits its resolution: `Segment` is
/// passed over below `SEGMENT_READABLE` rows and `Dots` where its dots
/// would blur, while another font fits; either is used only where nothing
/// else does. The cells' size and the wall's depth go into the answer, so
/// drawing it makes the same choices.
pub fn fit(text: &str, cols: usize, rows: usize, fonts: &[Font], wall: Option<&WallInfo>, wrap: bool, style: &Style) -> Option<Fitted> {
    fit_aligned(text, cols as i64, rows as i64, fonts, wall, wrap, style, Align::Left)
}

#[allow(clippy::too_many_arguments)]
fn fit_aligned(text: &str, cols: i64, rows: i64, fonts: &[Font], wall: Option<&WallInfo>, wrap: bool, style: &Style, align: Align) -> Option<Fitted> {
    [true, false].into_iter().find_map(|strict| fit_once(text, cols, rows, fonts, wall, wrap, style, align, strict))
}

#[allow(clippy::too_many_arguments)]
fn fit_once(text: &str, cols: i64, rows: i64, fonts: &[Font], wall: Option<&WallInfo>, wrap: bool, style: &Style, align: Align, strict: bool) -> Option<Fitted> {
    for &asked in fonts {
        if !usable(asked, wall) {
            continue;
        }
        let (font, o) = setup(&Style { font: asked, ..style.clone() }, wall, align);
        if strict && font == Font::Dots && !dots_distinct(o.px) {
            continue;
        }
        let low = if strict && font == Font::Segment && !o.seven { SEGMENT_READABLE } else { smallest(font) };
        let t = plain_o(text, font, &o);
        let paras = split_lines(&t);
        let mut best: Option<(String, usize)> = None;
        if o.turn.is_some() {
            for h in (1..=rows.max(0) as usize).rev() {
                let trial = O { height: Some(h), ..o.clone() };
                if let Some(n) = step(font, &paras, &trial) {
                    let (c, r) = dims(font, n, &paras, &trial);
                    if n >= low && c as i64 <= cols {
                        best = Some((t.clone(), r));
                        break;
                    }
                }
            }
        } else {
            let probe = O { height: None, scale: 1, ..o.clone() };
            let top = if font == Font::Segment { rows } else { rows.max(1) };
            let mut n = top;
            while n >= low as i64 {
                let nn = n as usize;
                n -= 1;
                let lines: Lines = if wrap {
                    let mut split: Option<Vec<String>> = Some(Vec::new());
                    for para in t.split('\n') {
                        let words: Vec<&str> = para.split_whitespace().collect();
                        match wrapped(font, nn, &words, cols, &probe) {
                            None => {
                                split = None;
                                break;
                            }
                            Some(got) => {
                                let s = split.as_mut().unwrap();
                                if got.is_empty() {
                                    s.push(String::new());
                                } else {
                                    s.extend(got);
                                }
                            }
                        }
                    }
                    match split {
                        None => continue,
                        Some(s) => s.iter().map(|l| l.chars().collect()).collect(),
                    }
                } else {
                    paras.clone()
                };
                let (c, r) = dims(font, nn, &lines, &probe);
                if c as i64 <= cols && r as i64 <= rows {
                    let joined: Vec<String> = lines.iter().map(|l| l.iter().collect()).collect();
                    best = Some((joined.join("\n"), r));
                    break;
                }
            }
        }
        if let Some((text, height)) = best {
            if text.trim().is_empty() {
                continue;
            }
            let mut got = Style { font: asked, scale: 1, height: Some(height), ..style.clone() };
            if o.px.is_some() {
                got.px = o.px;
            }
            if wall.is_some() && style.depth.is_none() {
                got.depth = Some(o.depth);
            }
            got.align = style.align.or(if align == Align::Left { None } else { Some(align) });
            return Some(Fitted { text, style: got });
        }
    }
    None
}

/// Fit the text to the frame and draw it in the middle (Python's
/// `centred`): centred lines unless the style says otherwise, wrapped
/// where `wrap`. Where no font fits, the text is written as it is across
/// the middle row, cut to the width. Returns what `fit` chose.
pub fn centred(frame: &mut Frame, text: &str, fonts: &[Font], wall: Option<&WallInfo>, margin: usize, wrap: bool, style: &Style) -> Option<Fitted> {
    let (wide, high) = (frame.cols as i64, frame.rows as i64);
    let got = fit_aligned(text, wide - 2 * margin as i64, high - 2 * margin as i64, fonts, wall, wrap, style, Align::Centre);
    let Some(got) = got else {
        let line: Vec<char> = text.replace('\n', " ").chars().take(wide.max(0) as usize).collect();
        if high > 0 && !line.is_empty() {
            let x0 = (wide - line.len() as i64).div_euclid(2);
            for (i, &ch) in line.iter().enumerate() {
                put(frame, x0 + i as i64, high / 2, Piece { ch, fg: Some(style.fg), bg: style.bg, faint: false });
            }
        }
        return None;
    };
    let measure = Style { smooth: false, depth: None, ghost: None, ..got.style.clone() };
    let (c, r) = size(&got.text, &measure, None);
    draw(frame, (wide - c as i64).div_euclid(2), (high - r as i64).div_euclid(2), &got.text, &got.style, None);
    Some(got)
}

// ------------------------------------------------------------------ tables

/// Each character that folds to ASCII, and what it folds to: Python's
/// `unicodedata.normalize("NFKD", c).encode("ascii", "ignore")`, where
/// that is not empty, for every character past ASCII.
#[rustfmt::skip]
const FOLD: &[(u32, &str)] = &[
    (0xa0, " "), (0xa8, " "), (0xaa, "a"), (0xaf, " "), (0xb2, "2"), (0xb3, "3"),
    (0xb4, " "), (0xb8, " "), (0xb9, "1"), (0xba, "o"), (0xbc, "14"), (0xbd, "12"),
    (0xbe, "34"), (0xc0, "A"), (0xc1, "A"), (0xc2, "A"), (0xc3, "A"), (0xc4, "A"),
    (0xc5, "A"), (0xc7, "C"), (0xc8, "E"), (0xc9, "E"), (0xca, "E"), (0xcb, "E"),
    (0xcc, "I"), (0xcd, "I"), (0xce, "I"), (0xcf, "I"), (0xd1, "N"), (0xd2, "O"),
    (0xd3, "O"), (0xd4, "O"), (0xd5, "O"), (0xd6, "O"), (0xd9, "U"), (0xda, "U"),
    (0xdb, "U"), (0xdc, "U"), (0xdd, "Y"), (0xe0, "a"), (0xe1, "a"), (0xe2, "a"),
    (0xe3, "a"), (0xe4, "a"), (0xe5, "a"), (0xe7, "c"), (0xe8, "e"), (0xe9, "e"),
    (0xea, "e"), (0xeb, "e"), (0xec, "i"), (0xed, "i"), (0xee, "i"), (0xef, "i"),
    (0xf1, "n"), (0xf2, "o"), (0xf3, "o"), (0xf4, "o"), (0xf5, "o"), (0xf6, "o"),
    (0xf9, "u"), (0xfa, "u"), (0xfb, "u"), (0xfc, "u"), (0xfd, "y"), (0xff, "y"),
    (0x100, "A"), (0x101, "a"), (0x102, "A"), (0x103, "a"), (0x104, "A"), (0x105, "a"),
    (0x106, "C"), (0x107, "c"), (0x108, "C"), (0x109, "c"), (0x10a, "C"), (0x10b, "c"),
    (0x10c, "C"), (0x10d, "c"), (0x10e, "D"), (0x10f, "d"), (0x112, "E"), (0x113, "e"),
    (0x114, "E"), (0x115, "e"), (0x116, "E"), (0x117, "e"), (0x118, "E"), (0x119, "e"),
    (0x11a, "E"), (0x11b, "e"), (0x11c, "G"), (0x11d, "g"), (0x11e, "G"), (0x11f, "g"),
    (0x120, "G"), (0x121, "g"), (0x122, "G"), (0x123, "g"), (0x124, "H"), (0x125, "h"),
    (0x128, "I"), (0x129, "i"), (0x12a, "I"), (0x12b, "i"), (0x12c, "I"), (0x12d, "i"),
    (0x12e, "I"), (0x12f, "i"), (0x130, "I"), (0x132, "IJ"), (0x133, "ij"), (0x134, "J"),
    (0x135, "j"), (0x136, "K"), (0x137, "k"), (0x139, "L"), (0x13a, "l"), (0x13b, "L"),
    (0x13c, "l"), (0x13d, "L"), (0x13e, "l"), (0x13f, "L"), (0x140, "l"), (0x143, "N"),
    (0x144, "n"), (0x145, "N"), (0x146, "n"), (0x147, "N"), (0x148, "n"), (0x149, "n"),
    (0x14c, "O"), (0x14d, "o"), (0x14e, "O"), (0x14f, "o"), (0x150, "O"), (0x151, "o"),
    (0x154, "R"), (0x155, "r"), (0x156, "R"), (0x157, "r"), (0x158, "R"), (0x159, "r"),
    (0x15a, "S"), (0x15b, "s"), (0x15c, "S"), (0x15d, "s"), (0x15e, "S"), (0x15f, "s"),
    (0x160, "S"), (0x161, "s"), (0x162, "T"), (0x163, "t"), (0x164, "T"), (0x165, "t"),
    (0x168, "U"), (0x169, "u"), (0x16a, "U"), (0x16b, "u"), (0x16c, "U"), (0x16d, "u"),
    (0x16e, "U"), (0x16f, "u"), (0x170, "U"), (0x171, "u"), (0x172, "U"), (0x173, "u"),
    (0x174, "W"), (0x175, "w"), (0x176, "Y"), (0x177, "y"), (0x178, "Y"), (0x179, "Z"),
    (0x17a, "z"), (0x17b, "Z"), (0x17c, "z"), (0x17d, "Z"), (0x17e, "z"), (0x17f, "s"),
    (0x1a0, "O"), (0x1a1, "o"), (0x1af, "U"), (0x1b0, "u"), (0x1c4, "DZ"), (0x1c5, "Dz"),
    (0x1c6, "dz"), (0x1c7, "LJ"), (0x1c8, "Lj"), (0x1c9, "lj"), (0x1ca, "NJ"), (0x1cb, "Nj"),
    (0x1cc, "nj"), (0x1cd, "A"), (0x1ce, "a"), (0x1cf, "I"), (0x1d0, "i"), (0x1d1, "O"),
    (0x1d2, "o"), (0x1d3, "U"), (0x1d4, "u"), (0x1d5, "U"), (0x1d6, "u"), (0x1d7, "U"),
    (0x1d8, "u"), (0x1d9, "U"), (0x1da, "u"), (0x1db, "U"), (0x1dc, "u"), (0x1de, "A"),
    (0x1df, "a"), (0x1e0, "A"), (0x1e1, "a"), (0x1e6, "G"), (0x1e7, "g"), (0x1e8, "K"),
    (0x1e9, "k"), (0x1ea, "O"), (0x1eb, "o"), (0x1ec, "O"), (0x1ed, "o"), (0x1f0, "j"),
    (0x1f1, "DZ"), (0x1f2, "Dz"), (0x1f3, "dz"), (0x1f4, "G"), (0x1f5, "g"), (0x1f8, "N"),
    (0x1f9, "n"), (0x1fa, "A"), (0x1fb, "a"), (0x200, "A"), (0x201, "a"), (0x202, "A"),
    (0x203, "a"), (0x204, "E"), (0x205, "e"), (0x206, "E"), (0x207, "e"), (0x208, "I"),
    (0x209, "i"), (0x20a, "I"), (0x20b, "i"), (0x20c, "O"), (0x20d, "o"), (0x20e, "O"),
    (0x20f, "o"), (0x210, "R"), (0x211, "r"), (0x212, "R"), (0x213, "r"), (0x214, "U"),
    (0x215, "u"), (0x216, "U"), (0x217, "u"), (0x218, "S"), (0x219, "s"), (0x21a, "T"),
    (0x21b, "t"), (0x21e, "H"), (0x21f, "h"), (0x226, "A"), (0x227, "a"), (0x228, "E"),
    (0x229, "e"), (0x22a, "O"), (0x22b, "o"), (0x22c, "O"), (0x22d, "o"), (0x22e, "O"),
    (0x22f, "o"), (0x230, "O"), (0x231, "o"), (0x232, "Y"), (0x233, "y"), (0x2b0, "h"),
    (0x2b2, "j"), (0x2b3, "r"), (0x2b7, "w"), (0x2b8, "y"), (0x2d8, " "), (0x2d9, " "),
    (0x2da, " "), (0x2db, " "), (0x2dc, " "), (0x2dd, " "), (0x2e1, "l"), (0x2e2, "s"),
    (0x2e3, "x"), (0x37a, " "), (0x37e, ";"), (0x384, " "), (0x385, " "), (0x1d2c, "A"),
    (0x1d2e, "B"), (0x1d30, "D"), (0x1d31, "E"), (0x1d33, "G"), (0x1d34, "H"), (0x1d35, "I"),
    (0x1d36, "J"), (0x1d37, "K"), (0x1d38, "L"), (0x1d39, "M"), (0x1d3a, "N"), (0x1d3c, "O"),
    (0x1d3e, "P"), (0x1d3f, "R"), (0x1d40, "T"), (0x1d41, "U"), (0x1d42, "W"), (0x1d43, "a"),
    (0x1d47, "b"), (0x1d48, "d"), (0x1d49, "e"), (0x1d4d, "g"), (0x1d4f, "k"), (0x1d50, "m"),
    (0x1d52, "o"), (0x1d56, "p"), (0x1d57, "t"), (0x1d58, "u"), (0x1d5b, "v"), (0x1d62, "i"),
    (0x1d63, "r"), (0x1d64, "u"), (0x1d65, "v"), (0x1d9c, "c"), (0x1da0, "f"), (0x1dbb, "z"),
    (0x1e00, "A"), (0x1e01, "a"), (0x1e02, "B"), (0x1e03, "b"), (0x1e04, "B"), (0x1e05, "b"),
    (0x1e06, "B"), (0x1e07, "b"), (0x1e08, "C"), (0x1e09, "c"), (0x1e0a, "D"), (0x1e0b, "d"),
    (0x1e0c, "D"), (0x1e0d, "d"), (0x1e0e, "D"), (0x1e0f, "d"), (0x1e10, "D"), (0x1e11, "d"),
    (0x1e12, "D"), (0x1e13, "d"), (0x1e14, "E"), (0x1e15, "e"), (0x1e16, "E"), (0x1e17, "e"),
    (0x1e18, "E"), (0x1e19, "e"), (0x1e1a, "E"), (0x1e1b, "e"), (0x1e1c, "E"), (0x1e1d, "e"),
    (0x1e1e, "F"), (0x1e1f, "f"), (0x1e20, "G"), (0x1e21, "g"), (0x1e22, "H"), (0x1e23, "h"),
    (0x1e24, "H"), (0x1e25, "h"), (0x1e26, "H"), (0x1e27, "h"), (0x1e28, "H"), (0x1e29, "h"),
    (0x1e2a, "H"), (0x1e2b, "h"), (0x1e2c, "I"), (0x1e2d, "i"), (0x1e2e, "I"), (0x1e2f, "i"),
    (0x1e30, "K"), (0x1e31, "k"), (0x1e32, "K"), (0x1e33, "k"), (0x1e34, "K"), (0x1e35, "k"),
    (0x1e36, "L"), (0x1e37, "l"), (0x1e38, "L"), (0x1e39, "l"), (0x1e3a, "L"), (0x1e3b, "l"),
    (0x1e3c, "L"), (0x1e3d, "l"), (0x1e3e, "M"), (0x1e3f, "m"), (0x1e40, "M"), (0x1e41, "m"),
    (0x1e42, "M"), (0x1e43, "m"), (0x1e44, "N"), (0x1e45, "n"), (0x1e46, "N"), (0x1e47, "n"),
    (0x1e48, "N"), (0x1e49, "n"), (0x1e4a, "N"), (0x1e4b, "n"), (0x1e4c, "O"), (0x1e4d, "o"),
    (0x1e4e, "O"), (0x1e4f, "o"), (0x1e50, "O"), (0x1e51, "o"), (0x1e52, "O"), (0x1e53, "o"),
    (0x1e54, "P"), (0x1e55, "p"), (0x1e56, "P"), (0x1e57, "p"), (0x1e58, "R"), (0x1e59, "r"),
    (0x1e5a, "R"), (0x1e5b, "r"), (0x1e5c, "R"), (0x1e5d, "r"), (0x1e5e, "R"), (0x1e5f, "r"),
    (0x1e60, "S"), (0x1e61, "s"), (0x1e62, "S"), (0x1e63, "s"), (0x1e64, "S"), (0x1e65, "s"),
    (0x1e66, "S"), (0x1e67, "s"), (0x1e68, "S"), (0x1e69, "s"), (0x1e6a, "T"), (0x1e6b, "t"),
    (0x1e6c, "T"), (0x1e6d, "t"), (0x1e6e, "T"), (0x1e6f, "t"), (0x1e70, "T"), (0x1e71, "t"),
    (0x1e72, "U"), (0x1e73, "u"), (0x1e74, "U"), (0x1e75, "u"), (0x1e76, "U"), (0x1e77, "u"),
    (0x1e78, "U"), (0x1e79, "u"), (0x1e7a, "U"), (0x1e7b, "u"), (0x1e7c, "V"), (0x1e7d, "v"),
    (0x1e7e, "V"), (0x1e7f, "v"), (0x1e80, "W"), (0x1e81, "w"), (0x1e82, "W"), (0x1e83, "w"),
    (0x1e84, "W"), (0x1e85, "w"), (0x1e86, "W"), (0x1e87, "w"), (0x1e88, "W"), (0x1e89, "w"),
    (0x1e8a, "X"), (0x1e8b, "x"), (0x1e8c, "X"), (0x1e8d, "x"), (0x1e8e, "Y"), (0x1e8f, "y"),
    (0x1e90, "Z"), (0x1e91, "z"), (0x1e92, "Z"), (0x1e93, "z"), (0x1e94, "Z"), (0x1e95, "z"),
    (0x1e96, "h"), (0x1e97, "t"), (0x1e98, "w"), (0x1e99, "y"), (0x1e9a, "a"), (0x1e9b, "s"),
    (0x1ea0, "A"), (0x1ea1, "a"), (0x1ea2, "A"), (0x1ea3, "a"), (0x1ea4, "A"), (0x1ea5, "a"),
    (0x1ea6, "A"), (0x1ea7, "a"), (0x1ea8, "A"), (0x1ea9, "a"), (0x1eaa, "A"), (0x1eab, "a"),
    (0x1eac, "A"), (0x1ead, "a"), (0x1eae, "A"), (0x1eaf, "a"), (0x1eb0, "A"), (0x1eb1, "a"),
    (0x1eb2, "A"), (0x1eb3, "a"), (0x1eb4, "A"), (0x1eb5, "a"), (0x1eb6, "A"), (0x1eb7, "a"),
    (0x1eb8, "E"), (0x1eb9, "e"), (0x1eba, "E"), (0x1ebb, "e"), (0x1ebc, "E"), (0x1ebd, "e"),
    (0x1ebe, "E"), (0x1ebf, "e"), (0x1ec0, "E"), (0x1ec1, "e"), (0x1ec2, "E"), (0x1ec3, "e"),
    (0x1ec4, "E"), (0x1ec5, "e"), (0x1ec6, "E"), (0x1ec7, "e"), (0x1ec8, "I"), (0x1ec9, "i"),
    (0x1eca, "I"), (0x1ecb, "i"), (0x1ecc, "O"), (0x1ecd, "o"), (0x1ece, "O"), (0x1ecf, "o"),
    (0x1ed0, "O"), (0x1ed1, "o"), (0x1ed2, "O"), (0x1ed3, "o"), (0x1ed4, "O"), (0x1ed5, "o"),
    (0x1ed6, "O"), (0x1ed7, "o"), (0x1ed8, "O"), (0x1ed9, "o"), (0x1eda, "O"), (0x1edb, "o"),
    (0x1edc, "O"), (0x1edd, "o"), (0x1ede, "O"), (0x1edf, "o"), (0x1ee0, "O"), (0x1ee1, "o"),
    (0x1ee2, "O"), (0x1ee3, "o"), (0x1ee4, "U"), (0x1ee5, "u"), (0x1ee6, "U"), (0x1ee7, "u"),
    (0x1ee8, "U"), (0x1ee9, "u"), (0x1eea, "U"), (0x1eeb, "u"), (0x1eec, "U"), (0x1eed, "u"),
    (0x1eee, "U"), (0x1eef, "u"), (0x1ef0, "U"), (0x1ef1, "u"), (0x1ef2, "Y"), (0x1ef3, "y"),
    (0x1ef4, "Y"), (0x1ef5, "y"), (0x1ef6, "Y"), (0x1ef7, "y"), (0x1ef8, "Y"), (0x1ef9, "y"),
    (0x1fbd, " "), (0x1fbf, " "), (0x1fc0, " "), (0x1fc1, " "), (0x1fcd, " "), (0x1fce, " "),
    (0x1fcf, " "), (0x1fdd, " "), (0x1fde, " "), (0x1fdf, " "), (0x1fed, " "), (0x1fee, " "),
    (0x1fef, "`"), (0x1ffd, " "), (0x1ffe, " "), (0x2000, " "), (0x2001, " "), (0x2002, " "),
    (0x2003, " "), (0x2004, " "), (0x2005, " "), (0x2006, " "), (0x2007, " "), (0x2008, " "),
    (0x2009, " "), (0x200a, " "), (0x2017, " "), (0x2024, "."), (0x2025, ".."), (0x2026, "..."),
    (0x202f, " "), (0x203c, "!!"), (0x203e, " "), (0x2047, "??"), (0x2048, "?!"), (0x2049, "!?"),
    (0x205f, " "), (0x2070, "0"), (0x2071, "i"), (0x2074, "4"), (0x2075, "5"), (0x2076, "6"),
    (0x2077, "7"), (0x2078, "8"), (0x2079, "9"), (0x207a, "+"), (0x207c, "="), (0x207d, "("),
    (0x207e, ")"), (0x207f, "n"), (0x2080, "0"), (0x2081, "1"), (0x2082, "2"), (0x2083, "3"),
    (0x2084, "4"), (0x2085, "5"), (0x2086, "6"), (0x2087, "7"), (0x2088, "8"), (0x2089, "9"),
    (0x208a, "+"), (0x208c, "="), (0x208d, "("), (0x208e, ")"), (0x2090, "a"), (0x2091, "e"),
    (0x2092, "o"), (0x2093, "x"), (0x2095, "h"), (0x2096, "k"), (0x2097, "l"), (0x2098, "m"),
    (0x2099, "n"), (0x209a, "p"), (0x209b, "s"), (0x209c, "t"), (0x20a8, "Rs"), (0x2100, "a/c"),
    (0x2101, "a/s"), (0x2102, "C"), (0x2103, "C"), (0x2105, "c/o"), (0x2106, "c/u"), (0x2109, "F"),
    (0x210a, "g"), (0x210b, "H"), (0x210c, "H"), (0x210d, "H"), (0x210e, "h"), (0x2110, "I"),
    (0x2111, "I"), (0x2112, "L"), (0x2113, "l"), (0x2115, "N"), (0x2116, "No"), (0x2119, "P"),
    (0x211a, "Q"), (0x211b, "R"), (0x211c, "R"), (0x211d, "R"), (0x2120, "SM"), (0x2121, "TEL"),
    (0x2122, "TM"), (0x2124, "Z"), (0x2128, "Z"), (0x212a, "K"), (0x212b, "A"), (0x212c, "B"),
    (0x212d, "C"), (0x212f, "e"), (0x2130, "E"), (0x2131, "F"), (0x2133, "M"), (0x2134, "o"),
    (0x2139, "i"), (0x213b, "FAX"), (0x2145, "D"), (0x2146, "d"), (0x2147, "e"), (0x2148, "i"),
    (0x2149, "j"), (0x2150, "17"), (0x2151, "19"), (0x2152, "110"), (0x2153, "13"), (0x2154, "23"),
    (0x2155, "15"), (0x2156, "25"), (0x2157, "35"), (0x2158, "45"), (0x2159, "16"), (0x215a, "56"),
    (0x215b, "18"), (0x215c, "38"), (0x215d, "58"), (0x215e, "78"), (0x215f, "1"), (0x2160, "I"),
    (0x2161, "II"), (0x2162, "III"), (0x2163, "IV"), (0x2164, "V"), (0x2165, "VI"), (0x2166, "VII"),
    (0x2167, "VIII"), (0x2168, "IX"), (0x2169, "X"), (0x216a, "XI"), (0x216b, "XII"), (0x216c, "L"),
    (0x216d, "C"), (0x216e, "D"), (0x216f, "M"), (0x2170, "i"), (0x2171, "ii"), (0x2172, "iii"),
    (0x2173, "iv"), (0x2174, "v"), (0x2175, "vi"), (0x2176, "vii"), (0x2177, "viii"), (0x2178, "ix"),
    (0x2179, "x"), (0x217a, "xi"), (0x217b, "xii"), (0x217c, "l"), (0x217d, "c"), (0x217e, "d"),
    (0x217f, "m"), (0x2189, "03"), (0x2260, "="), (0x226e, "<"), (0x226f, ">"), (0x2460, "1"),
    (0x2461, "2"), (0x2462, "3"), (0x2463, "4"), (0x2464, "5"), (0x2465, "6"), (0x2466, "7"),
    (0x2467, "8"), (0x2468, "9"), (0x2469, "10"), (0x246a, "11"), (0x246b, "12"), (0x246c, "13"),
    (0x246d, "14"), (0x246e, "15"), (0x246f, "16"), (0x2470, "17"), (0x2471, "18"), (0x2472, "19"),
    (0x2473, "20"), (0x2474, "(1)"), (0x2475, "(2)"), (0x2476, "(3)"), (0x2477, "(4)"), (0x2478, "(5)"),
    (0x2479, "(6)"), (0x247a, "(7)"), (0x247b, "(8)"), (0x247c, "(9)"), (0x247d, "(10)"), (0x247e, "(11)"),
    (0x247f, "(12)"), (0x2480, "(13)"), (0x2481, "(14)"), (0x2482, "(15)"), (0x2483, "(16)"), (0x2484, "(17)"),
    (0x2485, "(18)"), (0x2486, "(19)"), (0x2487, "(20)"), (0x2488, "1."), (0x2489, "2."), (0x248a, "3."),
    (0x248b, "4."), (0x248c, "5."), (0x248d, "6."), (0x248e, "7."), (0x248f, "8."), (0x2490, "9."),
    (0x2491, "10."), (0x2492, "11."), (0x2493, "12."), (0x2494, "13."), (0x2495, "14."), (0x2496, "15."),
    (0x2497, "16."), (0x2498, "17."), (0x2499, "18."), (0x249a, "19."), (0x249b, "20."), (0x249c, "(a)"),
    (0x249d, "(b)"), (0x249e, "(c)"), (0x249f, "(d)"), (0x24a0, "(e)"), (0x24a1, "(f)"), (0x24a2, "(g)"),
    (0x24a3, "(h)"), (0x24a4, "(i)"), (0x24a5, "(j)"), (0x24a6, "(k)"), (0x24a7, "(l)"), (0x24a8, "(m)"),
    (0x24a9, "(n)"), (0x24aa, "(o)"), (0x24ab, "(p)"), (0x24ac, "(q)"), (0x24ad, "(r)"), (0x24ae, "(s)"),
    (0x24af, "(t)"), (0x24b0, "(u)"), (0x24b1, "(v)"), (0x24b2, "(w)"), (0x24b3, "(x)"), (0x24b4, "(y)"),
    (0x24b5, "(z)"), (0x24b6, "A"), (0x24b7, "B"), (0x24b8, "C"), (0x24b9, "D"), (0x24ba, "E"),
    (0x24bb, "F"), (0x24bc, "G"), (0x24bd, "H"), (0x24be, "I"), (0x24bf, "J"), (0x24c0, "K"),
    (0x24c1, "L"), (0x24c2, "M"), (0x24c3, "N"), (0x24c4, "O"), (0x24c5, "P"), (0x24c6, "Q"),
    (0x24c7, "R"), (0x24c8, "S"), (0x24c9, "T"), (0x24ca, "U"), (0x24cb, "V"), (0x24cc, "W"),
    (0x24cd, "X"), (0x24ce, "Y"), (0x24cf, "Z"), (0x24d0, "a"), (0x24d1, "b"), (0x24d2, "c"),
    (0x24d3, "d"), (0x24d4, "e"), (0x24d5, "f"), (0x24d6, "g"), (0x24d7, "h"), (0x24d8, "i"),
    (0x24d9, "j"), (0x24da, "k"), (0x24db, "l"), (0x24dc, "m"), (0x24dd, "n"), (0x24de, "o"),
    (0x24df, "p"), (0x24e0, "q"), (0x24e1, "r"), (0x24e2, "s"), (0x24e3, "t"), (0x24e4, "u"),
    (0x24e5, "v"), (0x24e6, "w"), (0x24e7, "x"), (0x24e8, "y"), (0x24e9, "z"), (0x24ea, "0"),
    (0x2a74, "::="), (0x2a75, "=="), (0x2a76, "==="), (0x2c7c, "j"), (0x2c7d, "V"), (0x3000, " "),
    (0x309b, " "), (0x309c, " "), (0x3200, "()"), (0x3201, "()"), (0x3202, "()"), (0x3203, "()"),
    (0x3204, "()"), (0x3205, "()"), (0x3206, "()"), (0x3207, "()"), (0x3208, "()"), (0x3209, "()"),
    (0x320a, "()"), (0x320b, "()"), (0x320c, "()"), (0x320d, "()"), (0x320e, "()"), (0x320f, "()"),
    (0x3210, "()"), (0x3211, "()"), (0x3212, "()"), (0x3213, "()"), (0x3214, "()"), (0x3215, "()"),
    (0x3216, "()"), (0x3217, "()"), (0x3218, "()"), (0x3219, "()"), (0x321a, "()"), (0x321b, "()"),
    (0x321c, "()"), (0x321d, "()"), (0x321e, "()"), (0x3220, "()"), (0x3221, "()"), (0x3222, "()"),
    (0x3223, "()"), (0x3224, "()"), (0x3225, "()"), (0x3226, "()"), (0x3227, "()"), (0x3228, "()"),
    (0x3229, "()"), (0x322a, "()"), (0x322b, "()"), (0x322c, "()"), (0x322d, "()"), (0x322e, "()"),
    (0x322f, "()"), (0x3230, "()"), (0x3231, "()"), (0x3232, "()"), (0x3233, "()"), (0x3234, "()"),
    (0x3235, "()"), (0x3236, "()"), (0x3237, "()"), (0x3238, "()"), (0x3239, "()"), (0x323a, "()"),
    (0x323b, "()"), (0x323c, "()"), (0x323d, "()"), (0x323e, "()"), (0x323f, "()"), (0x3240, "()"),
    (0x3241, "()"), (0x3242, "()"), (0x3243, "()"), (0x3250, "PTE"), (0x3251, "21"), (0x3252, "22"),
    (0x3253, "23"), (0x3254, "24"), (0x3255, "25"), (0x3256, "26"), (0x3257, "27"), (0x3258, "28"),
    (0x3259, "29"), (0x325a, "30"), (0x325b, "31"), (0x325c, "32"), (0x325d, "33"), (0x325e, "34"),
    (0x325f, "35"), (0x32b1, "36"), (0x32b2, "37"), (0x32b3, "38"), (0x32b4, "39"), (0x32b5, "40"),
    (0x32b6, "41"), (0x32b7, "42"), (0x32b8, "43"), (0x32b9, "44"), (0x32ba, "45"), (0x32bb, "46"),
    (0x32bc, "47"), (0x32bd, "48"), (0x32be, "49"), (0x32bf, "50"), (0x32c0, "1"), (0x32c1, "2"),
    (0x32c2, "3"), (0x32c3, "4"), (0x32c4, "5"), (0x32c5, "6"), (0x32c6, "7"), (0x32c7, "8"),
    (0x32c8, "9"), (0x32c9, "10"), (0x32ca, "11"), (0x32cb, "12"), (0x32cc, "Hg"), (0x32cd, "erg"),
    (0x32ce, "eV"), (0x32cf, "LTD"), (0x3358, "0"), (0x3359, "1"), (0x335a, "2"), (0x335b, "3"),
    (0x335c, "4"), (0x335d, "5"), (0x335e, "6"), (0x335f, "7"), (0x3360, "8"), (0x3361, "9"),
    (0x3362, "10"), (0x3363, "11"), (0x3364, "12"), (0x3365, "13"), (0x3366, "14"), (0x3367, "15"),
    (0x3368, "16"), (0x3369, "17"), (0x336a, "18"), (0x336b, "19"), (0x336c, "20"), (0x336d, "21"),
    (0x336e, "22"), (0x336f, "23"), (0x3370, "24"), (0x3371, "hPa"), (0x3372, "da"), (0x3373, "AU"),
    (0x3374, "bar"), (0x3375, "oV"), (0x3376, "pc"), (0x3377, "dm"), (0x3378, "dm2"), (0x3379, "dm3"),
    (0x337a, "IU"), (0x3380, "pA"), (0x3381, "nA"), (0x3382, "A"), (0x3383, "mA"), (0x3384, "kA"),
    (0x3385, "KB"), (0x3386, "MB"), (0x3387, "GB"), (0x3388, "cal"), (0x3389, "kcal"), (0x338a, "pF"),
    (0x338b, "nF"), (0x338c, "F"), (0x338d, "g"), (0x338e, "mg"), (0x338f, "kg"), (0x3390, "Hz"),
    (0x3391, "kHz"), (0x3392, "MHz"), (0x3393, "GHz"), (0x3394, "THz"), (0x3395, "l"), (0x3396, "ml"),
    (0x3397, "dl"), (0x3398, "kl"), (0x3399, "fm"), (0x339a, "nm"), (0x339b, "m"), (0x339c, "mm"),
    (0x339d, "cm"), (0x339e, "km"), (0x339f, "mm2"), (0x33a0, "cm2"), (0x33a1, "m2"), (0x33a2, "km2"),
    (0x33a3, "mm3"), (0x33a4, "cm3"), (0x33a5, "m3"), (0x33a6, "km3"), (0x33a7, "ms"), (0x33a8, "ms2"),
    (0x33a9, "Pa"), (0x33aa, "kPa"), (0x33ab, "MPa"), (0x33ac, "GPa"), (0x33ad, "rad"), (0x33ae, "rads"),
    (0x33af, "rads2"), (0x33b0, "ps"), (0x33b1, "ns"), (0x33b2, "s"), (0x33b3, "ms"), (0x33b4, "pV"),
    (0x33b5, "nV"), (0x33b6, "V"), (0x33b7, "mV"), (0x33b8, "kV"), (0x33b9, "MV"), (0x33ba, "pW"),
    (0x33bb, "nW"), (0x33bc, "W"), (0x33bd, "mW"), (0x33be, "kW"), (0x33bf, "MW"), (0x33c0, "k"),
    (0x33c1, "M"), (0x33c2, "a.m."), (0x33c3, "Bq"), (0x33c4, "cc"), (0x33c5, "cd"), (0x33c6, "Ckg"),
    (0x33c7, "Co."), (0x33c8, "dB"), (0x33c9, "Gy"), (0x33ca, "ha"), (0x33cb, "HP"), (0x33cc, "in"),
    (0x33cd, "KK"), (0x33ce, "KM"), (0x33cf, "kt"), (0x33d0, "lm"), (0x33d1, "ln"), (0x33d2, "log"),
    (0x33d3, "lx"), (0x33d4, "mb"), (0x33d5, "mil"), (0x33d6, "mol"), (0x33d7, "PH"), (0x33d8, "p.m."),
    (0x33d9, "PPM"), (0x33da, "PR"), (0x33db, "sr"), (0x33dc, "Sv"), (0x33dd, "Wb"), (0x33de, "Vm"),
    (0x33df, "Am"), (0x33e0, "1"), (0x33e1, "2"), (0x33e2, "3"), (0x33e3, "4"), (0x33e4, "5"),
    (0x33e5, "6"), (0x33e6, "7"), (0x33e7, "8"), (0x33e8, "9"), (0x33e9, "10"), (0x33ea, "11"),
    (0x33eb, "12"), (0x33ec, "13"), (0x33ed, "14"), (0x33ee, "15"), (0x33ef, "16"), (0x33f0, "17"),
    (0x33f1, "18"), (0x33f2, "19"), (0x33f3, "20"), (0x33f4, "21"), (0x33f5, "22"), (0x33f6, "23"),
    (0x33f7, "24"), (0x33f8, "25"), (0x33f9, "26"), (0x33fa, "27"), (0x33fb, "28"), (0x33fc, "29"),
    (0x33fd, "30"), (0x33fe, "31"), (0x33ff, "gal"), (0xa7f2, "C"), (0xa7f3, "F"), (0xa7f4, "Q"),
    (0xfb00, "ff"), (0xfb01, "fi"), (0xfb02, "fl"), (0xfb03, "ffi"), (0xfb04, "ffl"), (0xfb05, "st"),
    (0xfb06, "st"), (0xfb29, "+"), (0xfc5e, " "), (0xfc5f, " "), (0xfc60, " "), (0xfc61, " "),
    (0xfc62, " "), (0xfc63, " "), (0xfdfa, "   "), (0xfdfb, " "), (0xfe10, ","), (0xfe13, ":"),
    (0xfe14, ";"), (0xfe15, "!"), (0xfe16, "?"), (0xfe19, "..."), (0xfe30, ".."), (0xfe33, "_"),
    (0xfe34, "_"), (0xfe35, "("), (0xfe36, ")"), (0xfe37, "{"), (0xfe38, "}"), (0xfe47, "["),
    (0xfe48, "]"), (0xfe49, " "), (0xfe4a, " "), (0xfe4b, " "), (0xfe4c, " "), (0xfe4d, "_"),
    (0xfe4e, "_"), (0xfe4f, "_"), (0xfe50, ","), (0xfe52, "."), (0xfe54, ";"), (0xfe55, ":"),
    (0xfe56, "?"), (0xfe57, "!"), (0xfe59, "("), (0xfe5a, ")"), (0xfe5b, "{"), (0xfe5c, "}"),
    (0xfe5f, "#"), (0xfe60, "&"), (0xfe61, "*"), (0xfe62, "+"), (0xfe63, "-"), (0xfe64, "<"),
    (0xfe65, ">"), (0xfe66, "="), (0xfe68, "\\"), (0xfe69, "$"), (0xfe6a, "%"), (0xfe6b, "@"),
    (0xfe70, " "), (0xfe72, " "), (0xfe74, " "), (0xfe76, " "), (0xfe78, " "), (0xfe7a, " "),
    (0xfe7c, " "), (0xfe7e, " "), (0xff01, "!"), (0xff02, "\""), (0xff03, "#"), (0xff04, "$"),
    (0xff05, "%"), (0xff06, "&"), (0xff07, "'"), (0xff08, "("), (0xff09, ")"), (0xff0a, "*"),
    (0xff0b, "+"), (0xff0c, ","), (0xff0d, "-"), (0xff0e, "."), (0xff0f, "/"), (0xff10, "0"),
    (0xff11, "1"), (0xff12, "2"), (0xff13, "3"), (0xff14, "4"), (0xff15, "5"), (0xff16, "6"),
    (0xff17, "7"), (0xff18, "8"), (0xff19, "9"), (0xff1a, ":"), (0xff1b, ";"), (0xff1c, "<"),
    (0xff1d, "="), (0xff1e, ">"), (0xff1f, "?"), (0xff20, "@"), (0xff21, "A"), (0xff22, "B"),
    (0xff23, "C"), (0xff24, "D"), (0xff25, "E"), (0xff26, "F"), (0xff27, "G"), (0xff28, "H"),
    (0xff29, "I"), (0xff2a, "J"), (0xff2b, "K"), (0xff2c, "L"), (0xff2d, "M"), (0xff2e, "N"),
    (0xff2f, "O"), (0xff30, "P"), (0xff31, "Q"), (0xff32, "R"), (0xff33, "S"), (0xff34, "T"),
    (0xff35, "U"), (0xff36, "V"), (0xff37, "W"), (0xff38, "X"), (0xff39, "Y"), (0xff3a, "Z"),
    (0xff3b, "["), (0xff3c, "\\"), (0xff3d, "]"), (0xff3e, "^"), (0xff3f, "_"), (0xff40, "`"),
    (0xff41, "a"), (0xff42, "b"), (0xff43, "c"), (0xff44, "d"), (0xff45, "e"), (0xff46, "f"),
    (0xff47, "g"), (0xff48, "h"), (0xff49, "i"), (0xff4a, "j"), (0xff4b, "k"), (0xff4c, "l"),
    (0xff4d, "m"), (0xff4e, "n"), (0xff4f, "o"), (0xff50, "p"), (0xff51, "q"), (0xff52, "r"),
    (0xff53, "s"), (0xff54, "t"), (0xff55, "u"), (0xff56, "v"), (0xff57, "w"), (0xff58, "x"),
    (0xff59, "y"), (0xff5a, "z"), (0xff5b, "{"), (0xff5c, "|"), (0xff5d, "}"), (0xff5e, "~"),
    (0xffe3, " "), (0x107a5, "q"), (0x1ccd6, "A"), (0x1ccd7, "B"), (0x1ccd8, "C"), (0x1ccd9, "D"),
    (0x1ccda, "E"), (0x1ccdb, "F"), (0x1ccdc, "G"), (0x1ccdd, "H"), (0x1ccde, "I"), (0x1ccdf, "J"),
    (0x1cce0, "K"), (0x1cce1, "L"), (0x1cce2, "M"), (0x1cce3, "N"), (0x1cce4, "O"), (0x1cce5, "P"),
    (0x1cce6, "Q"), (0x1cce7, "R"), (0x1cce8, "S"), (0x1cce9, "T"), (0x1ccea, "U"), (0x1cceb, "V"),
    (0x1ccec, "W"), (0x1cced, "X"), (0x1ccee, "Y"), (0x1ccef, "Z"), (0x1ccf0, "0"), (0x1ccf1, "1"),
    (0x1ccf2, "2"), (0x1ccf3, "3"), (0x1ccf4, "4"), (0x1ccf5, "5"), (0x1ccf6, "6"), (0x1ccf7, "7"),
    (0x1ccf8, "8"), (0x1ccf9, "9"), (0x1d400, "A"), (0x1d401, "B"), (0x1d402, "C"), (0x1d403, "D"),
    (0x1d404, "E"), (0x1d405, "F"), (0x1d406, "G"), (0x1d407, "H"), (0x1d408, "I"), (0x1d409, "J"),
    (0x1d40a, "K"), (0x1d40b, "L"), (0x1d40c, "M"), (0x1d40d, "N"), (0x1d40e, "O"), (0x1d40f, "P"),
    (0x1d410, "Q"), (0x1d411, "R"), (0x1d412, "S"), (0x1d413, "T"), (0x1d414, "U"), (0x1d415, "V"),
    (0x1d416, "W"), (0x1d417, "X"), (0x1d418, "Y"), (0x1d419, "Z"), (0x1d41a, "a"), (0x1d41b, "b"),
    (0x1d41c, "c"), (0x1d41d, "d"), (0x1d41e, "e"), (0x1d41f, "f"), (0x1d420, "g"), (0x1d421, "h"),
    (0x1d422, "i"), (0x1d423, "j"), (0x1d424, "k"), (0x1d425, "l"), (0x1d426, "m"), (0x1d427, "n"),
    (0x1d428, "o"), (0x1d429, "p"), (0x1d42a, "q"), (0x1d42b, "r"), (0x1d42c, "s"), (0x1d42d, "t"),
    (0x1d42e, "u"), (0x1d42f, "v"), (0x1d430, "w"), (0x1d431, "x"), (0x1d432, "y"), (0x1d433, "z"),
    (0x1d434, "A"), (0x1d435, "B"), (0x1d436, "C"), (0x1d437, "D"), (0x1d438, "E"), (0x1d439, "F"),
    (0x1d43a, "G"), (0x1d43b, "H"), (0x1d43c, "I"), (0x1d43d, "J"), (0x1d43e, "K"), (0x1d43f, "L"),
    (0x1d440, "M"), (0x1d441, "N"), (0x1d442, "O"), (0x1d443, "P"), (0x1d444, "Q"), (0x1d445, "R"),
    (0x1d446, "S"), (0x1d447, "T"), (0x1d448, "U"), (0x1d449, "V"), (0x1d44a, "W"), (0x1d44b, "X"),
    (0x1d44c, "Y"), (0x1d44d, "Z"), (0x1d44e, "a"), (0x1d44f, "b"), (0x1d450, "c"), (0x1d451, "d"),
    (0x1d452, "e"), (0x1d453, "f"), (0x1d454, "g"), (0x1d456, "i"), (0x1d457, "j"), (0x1d458, "k"),
    (0x1d459, "l"), (0x1d45a, "m"), (0x1d45b, "n"), (0x1d45c, "o"), (0x1d45d, "p"), (0x1d45e, "q"),
    (0x1d45f, "r"), (0x1d460, "s"), (0x1d461, "t"), (0x1d462, "u"), (0x1d463, "v"), (0x1d464, "w"),
    (0x1d465, "x"), (0x1d466, "y"), (0x1d467, "z"), (0x1d468, "A"), (0x1d469, "B"), (0x1d46a, "C"),
    (0x1d46b, "D"), (0x1d46c, "E"), (0x1d46d, "F"), (0x1d46e, "G"), (0x1d46f, "H"), (0x1d470, "I"),
    (0x1d471, "J"), (0x1d472, "K"), (0x1d473, "L"), (0x1d474, "M"), (0x1d475, "N"), (0x1d476, "O"),
    (0x1d477, "P"), (0x1d478, "Q"), (0x1d479, "R"), (0x1d47a, "S"), (0x1d47b, "T"), (0x1d47c, "U"),
    (0x1d47d, "V"), (0x1d47e, "W"), (0x1d47f, "X"), (0x1d480, "Y"), (0x1d481, "Z"), (0x1d482, "a"),
    (0x1d483, "b"), (0x1d484, "c"), (0x1d485, "d"), (0x1d486, "e"), (0x1d487, "f"), (0x1d488, "g"),
    (0x1d489, "h"), (0x1d48a, "i"), (0x1d48b, "j"), (0x1d48c, "k"), (0x1d48d, "l"), (0x1d48e, "m"),
    (0x1d48f, "n"), (0x1d490, "o"), (0x1d491, "p"), (0x1d492, "q"), (0x1d493, "r"), (0x1d494, "s"),
    (0x1d495, "t"), (0x1d496, "u"), (0x1d497, "v"), (0x1d498, "w"), (0x1d499, "x"), (0x1d49a, "y"),
    (0x1d49b, "z"), (0x1d49c, "A"), (0x1d49e, "C"), (0x1d49f, "D"), (0x1d4a2, "G"), (0x1d4a5, "J"),
    (0x1d4a6, "K"), (0x1d4a9, "N"), (0x1d4aa, "O"), (0x1d4ab, "P"), (0x1d4ac, "Q"), (0x1d4ae, "S"),
    (0x1d4af, "T"), (0x1d4b0, "U"), (0x1d4b1, "V"), (0x1d4b2, "W"), (0x1d4b3, "X"), (0x1d4b4, "Y"),
    (0x1d4b5, "Z"), (0x1d4b6, "a"), (0x1d4b7, "b"), (0x1d4b8, "c"), (0x1d4b9, "d"), (0x1d4bb, "f"),
    (0x1d4bd, "h"), (0x1d4be, "i"), (0x1d4bf, "j"), (0x1d4c0, "k"), (0x1d4c1, "l"), (0x1d4c2, "m"),
    (0x1d4c3, "n"), (0x1d4c5, "p"), (0x1d4c6, "q"), (0x1d4c7, "r"), (0x1d4c8, "s"), (0x1d4c9, "t"),
    (0x1d4ca, "u"), (0x1d4cb, "v"), (0x1d4cc, "w"), (0x1d4cd, "x"), (0x1d4ce, "y"), (0x1d4cf, "z"),
    (0x1d4d0, "A"), (0x1d4d1, "B"), (0x1d4d2, "C"), (0x1d4d3, "D"), (0x1d4d4, "E"), (0x1d4d5, "F"),
    (0x1d4d6, "G"), (0x1d4d7, "H"), (0x1d4d8, "I"), (0x1d4d9, "J"), (0x1d4da, "K"), (0x1d4db, "L"),
    (0x1d4dc, "M"), (0x1d4dd, "N"), (0x1d4de, "O"), (0x1d4df, "P"), (0x1d4e0, "Q"), (0x1d4e1, "R"),
    (0x1d4e2, "S"), (0x1d4e3, "T"), (0x1d4e4, "U"), (0x1d4e5, "V"), (0x1d4e6, "W"), (0x1d4e7, "X"),
    (0x1d4e8, "Y"), (0x1d4e9, "Z"), (0x1d4ea, "a"), (0x1d4eb, "b"), (0x1d4ec, "c"), (0x1d4ed, "d"),
    (0x1d4ee, "e"), (0x1d4ef, "f"), (0x1d4f0, "g"), (0x1d4f1, "h"), (0x1d4f2, "i"), (0x1d4f3, "j"),
    (0x1d4f4, "k"), (0x1d4f5, "l"), (0x1d4f6, "m"), (0x1d4f7, "n"), (0x1d4f8, "o"), (0x1d4f9, "p"),
    (0x1d4fa, "q"), (0x1d4fb, "r"), (0x1d4fc, "s"), (0x1d4fd, "t"), (0x1d4fe, "u"), (0x1d4ff, "v"),
    (0x1d500, "w"), (0x1d501, "x"), (0x1d502, "y"), (0x1d503, "z"), (0x1d504, "A"), (0x1d505, "B"),
    (0x1d507, "D"), (0x1d508, "E"), (0x1d509, "F"), (0x1d50a, "G"), (0x1d50d, "J"), (0x1d50e, "K"),
    (0x1d50f, "L"), (0x1d510, "M"), (0x1d511, "N"), (0x1d512, "O"), (0x1d513, "P"), (0x1d514, "Q"),
    (0x1d516, "S"), (0x1d517, "T"), (0x1d518, "U"), (0x1d519, "V"), (0x1d51a, "W"), (0x1d51b, "X"),
    (0x1d51c, "Y"), (0x1d51e, "a"), (0x1d51f, "b"), (0x1d520, "c"), (0x1d521, "d"), (0x1d522, "e"),
    (0x1d523, "f"), (0x1d524, "g"), (0x1d525, "h"), (0x1d526, "i"), (0x1d527, "j"), (0x1d528, "k"),
    (0x1d529, "l"), (0x1d52a, "m"), (0x1d52b, "n"), (0x1d52c, "o"), (0x1d52d, "p"), (0x1d52e, "q"),
    (0x1d52f, "r"), (0x1d530, "s"), (0x1d531, "t"), (0x1d532, "u"), (0x1d533, "v"), (0x1d534, "w"),
    (0x1d535, "x"), (0x1d536, "y"), (0x1d537, "z"), (0x1d538, "A"), (0x1d539, "B"), (0x1d53b, "D"),
    (0x1d53c, "E"), (0x1d53d, "F"), (0x1d53e, "G"), (0x1d540, "I"), (0x1d541, "J"), (0x1d542, "K"),
    (0x1d543, "L"), (0x1d544, "M"), (0x1d546, "O"), (0x1d54a, "S"), (0x1d54b, "T"), (0x1d54c, "U"),
    (0x1d54d, "V"), (0x1d54e, "W"), (0x1d54f, "X"), (0x1d550, "Y"), (0x1d552, "a"), (0x1d553, "b"),
    (0x1d554, "c"), (0x1d555, "d"), (0x1d556, "e"), (0x1d557, "f"), (0x1d558, "g"), (0x1d559, "h"),
    (0x1d55a, "i"), (0x1d55b, "j"), (0x1d55c, "k"), (0x1d55d, "l"), (0x1d55e, "m"), (0x1d55f, "n"),
    (0x1d560, "o"), (0x1d561, "p"), (0x1d562, "q"), (0x1d563, "r"), (0x1d564, "s"), (0x1d565, "t"),
    (0x1d566, "u"), (0x1d567, "v"), (0x1d568, "w"), (0x1d569, "x"), (0x1d56a, "y"), (0x1d56b, "z"),
    (0x1d56c, "A"), (0x1d56d, "B"), (0x1d56e, "C"), (0x1d56f, "D"), (0x1d570, "E"), (0x1d571, "F"),
    (0x1d572, "G"), (0x1d573, "H"), (0x1d574, "I"), (0x1d575, "J"), (0x1d576, "K"), (0x1d577, "L"),
    (0x1d578, "M"), (0x1d579, "N"), (0x1d57a, "O"), (0x1d57b, "P"), (0x1d57c, "Q"), (0x1d57d, "R"),
    (0x1d57e, "S"), (0x1d57f, "T"), (0x1d580, "U"), (0x1d581, "V"), (0x1d582, "W"), (0x1d583, "X"),
    (0x1d584, "Y"), (0x1d585, "Z"), (0x1d586, "a"), (0x1d587, "b"), (0x1d588, "c"), (0x1d589, "d"),
    (0x1d58a, "e"), (0x1d58b, "f"), (0x1d58c, "g"), (0x1d58d, "h"), (0x1d58e, "i"), (0x1d58f, "j"),
    (0x1d590, "k"), (0x1d591, "l"), (0x1d592, "m"), (0x1d593, "n"), (0x1d594, "o"), (0x1d595, "p"),
    (0x1d596, "q"), (0x1d597, "r"), (0x1d598, "s"), (0x1d599, "t"), (0x1d59a, "u"), (0x1d59b, "v"),
    (0x1d59c, "w"), (0x1d59d, "x"), (0x1d59e, "y"), (0x1d59f, "z"), (0x1d5a0, "A"), (0x1d5a1, "B"),
    (0x1d5a2, "C"), (0x1d5a3, "D"), (0x1d5a4, "E"), (0x1d5a5, "F"), (0x1d5a6, "G"), (0x1d5a7, "H"),
    (0x1d5a8, "I"), (0x1d5a9, "J"), (0x1d5aa, "K"), (0x1d5ab, "L"), (0x1d5ac, "M"), (0x1d5ad, "N"),
    (0x1d5ae, "O"), (0x1d5af, "P"), (0x1d5b0, "Q"), (0x1d5b1, "R"), (0x1d5b2, "S"), (0x1d5b3, "T"),
    (0x1d5b4, "U"), (0x1d5b5, "V"), (0x1d5b6, "W"), (0x1d5b7, "X"), (0x1d5b8, "Y"), (0x1d5b9, "Z"),
    (0x1d5ba, "a"), (0x1d5bb, "b"), (0x1d5bc, "c"), (0x1d5bd, "d"), (0x1d5be, "e"), (0x1d5bf, "f"),
    (0x1d5c0, "g"), (0x1d5c1, "h"), (0x1d5c2, "i"), (0x1d5c3, "j"), (0x1d5c4, "k"), (0x1d5c5, "l"),
    (0x1d5c6, "m"), (0x1d5c7, "n"), (0x1d5c8, "o"), (0x1d5c9, "p"), (0x1d5ca, "q"), (0x1d5cb, "r"),
    (0x1d5cc, "s"), (0x1d5cd, "t"), (0x1d5ce, "u"), (0x1d5cf, "v"), (0x1d5d0, "w"), (0x1d5d1, "x"),
    (0x1d5d2, "y"), (0x1d5d3, "z"), (0x1d5d4, "A"), (0x1d5d5, "B"), (0x1d5d6, "C"), (0x1d5d7, "D"),
    (0x1d5d8, "E"), (0x1d5d9, "F"), (0x1d5da, "G"), (0x1d5db, "H"), (0x1d5dc, "I"), (0x1d5dd, "J"),
    (0x1d5de, "K"), (0x1d5df, "L"), (0x1d5e0, "M"), (0x1d5e1, "N"), (0x1d5e2, "O"), (0x1d5e3, "P"),
    (0x1d5e4, "Q"), (0x1d5e5, "R"), (0x1d5e6, "S"), (0x1d5e7, "T"), (0x1d5e8, "U"), (0x1d5e9, "V"),
    (0x1d5ea, "W"), (0x1d5eb, "X"), (0x1d5ec, "Y"), (0x1d5ed, "Z"), (0x1d5ee, "a"), (0x1d5ef, "b"),
    (0x1d5f0, "c"), (0x1d5f1, "d"), (0x1d5f2, "e"), (0x1d5f3, "f"), (0x1d5f4, "g"), (0x1d5f5, "h"),
    (0x1d5f6, "i"), (0x1d5f7, "j"), (0x1d5f8, "k"), (0x1d5f9, "l"), (0x1d5fa, "m"), (0x1d5fb, "n"),
    (0x1d5fc, "o"), (0x1d5fd, "p"), (0x1d5fe, "q"), (0x1d5ff, "r"), (0x1d600, "s"), (0x1d601, "t"),
    (0x1d602, "u"), (0x1d603, "v"), (0x1d604, "w"), (0x1d605, "x"), (0x1d606, "y"), (0x1d607, "z"),
    (0x1d608, "A"), (0x1d609, "B"), (0x1d60a, "C"), (0x1d60b, "D"), (0x1d60c, "E"), (0x1d60d, "F"),
    (0x1d60e, "G"), (0x1d60f, "H"), (0x1d610, "I"), (0x1d611, "J"), (0x1d612, "K"), (0x1d613, "L"),
    (0x1d614, "M"), (0x1d615, "N"), (0x1d616, "O"), (0x1d617, "P"), (0x1d618, "Q"), (0x1d619, "R"),
    (0x1d61a, "S"), (0x1d61b, "T"), (0x1d61c, "U"), (0x1d61d, "V"), (0x1d61e, "W"), (0x1d61f, "X"),
    (0x1d620, "Y"), (0x1d621, "Z"), (0x1d622, "a"), (0x1d623, "b"), (0x1d624, "c"), (0x1d625, "d"),
    (0x1d626, "e"), (0x1d627, "f"), (0x1d628, "g"), (0x1d629, "h"), (0x1d62a, "i"), (0x1d62b, "j"),
    (0x1d62c, "k"), (0x1d62d, "l"), (0x1d62e, "m"), (0x1d62f, "n"), (0x1d630, "o"), (0x1d631, "p"),
    (0x1d632, "q"), (0x1d633, "r"), (0x1d634, "s"), (0x1d635, "t"), (0x1d636, "u"), (0x1d637, "v"),
    (0x1d638, "w"), (0x1d639, "x"), (0x1d63a, "y"), (0x1d63b, "z"), (0x1d63c, "A"), (0x1d63d, "B"),
    (0x1d63e, "C"), (0x1d63f, "D"), (0x1d640, "E"), (0x1d641, "F"), (0x1d642, "G"), (0x1d643, "H"),
    (0x1d644, "I"), (0x1d645, "J"), (0x1d646, "K"), (0x1d647, "L"), (0x1d648, "M"), (0x1d649, "N"),
    (0x1d64a, "O"), (0x1d64b, "P"), (0x1d64c, "Q"), (0x1d64d, "R"), (0x1d64e, "S"), (0x1d64f, "T"),
    (0x1d650, "U"), (0x1d651, "V"), (0x1d652, "W"), (0x1d653, "X"), (0x1d654, "Y"), (0x1d655, "Z"),
    (0x1d656, "a"), (0x1d657, "b"), (0x1d658, "c"), (0x1d659, "d"), (0x1d65a, "e"), (0x1d65b, "f"),
    (0x1d65c, "g"), (0x1d65d, "h"), (0x1d65e, "i"), (0x1d65f, "j"), (0x1d660, "k"), (0x1d661, "l"),
    (0x1d662, "m"), (0x1d663, "n"), (0x1d664, "o"), (0x1d665, "p"), (0x1d666, "q"), (0x1d667, "r"),
    (0x1d668, "s"), (0x1d669, "t"), (0x1d66a, "u"), (0x1d66b, "v"), (0x1d66c, "w"), (0x1d66d, "x"),
    (0x1d66e, "y"), (0x1d66f, "z"), (0x1d670, "A"), (0x1d671, "B"), (0x1d672, "C"), (0x1d673, "D"),
    (0x1d674, "E"), (0x1d675, "F"), (0x1d676, "G"), (0x1d677, "H"), (0x1d678, "I"), (0x1d679, "J"),
    (0x1d67a, "K"), (0x1d67b, "L"), (0x1d67c, "M"), (0x1d67d, "N"), (0x1d67e, "O"), (0x1d67f, "P"),
    (0x1d680, "Q"), (0x1d681, "R"), (0x1d682, "S"), (0x1d683, "T"), (0x1d684, "U"), (0x1d685, "V"),
    (0x1d686, "W"), (0x1d687, "X"), (0x1d688, "Y"), (0x1d689, "Z"), (0x1d68a, "a"), (0x1d68b, "b"),
    (0x1d68c, "c"), (0x1d68d, "d"), (0x1d68e, "e"), (0x1d68f, "f"), (0x1d690, "g"), (0x1d691, "h"),
    (0x1d692, "i"), (0x1d693, "j"), (0x1d694, "k"), (0x1d695, "l"), (0x1d696, "m"), (0x1d697, "n"),
    (0x1d698, "o"), (0x1d699, "p"), (0x1d69a, "q"), (0x1d69b, "r"), (0x1d69c, "s"), (0x1d69d, "t"),
    (0x1d69e, "u"), (0x1d69f, "v"), (0x1d6a0, "w"), (0x1d6a1, "x"), (0x1d6a2, "y"), (0x1d6a3, "z"),
    (0x1d7ce, "0"), (0x1d7cf, "1"), (0x1d7d0, "2"), (0x1d7d1, "3"), (0x1d7d2, "4"), (0x1d7d3, "5"),
    (0x1d7d4, "6"), (0x1d7d5, "7"), (0x1d7d6, "8"), (0x1d7d7, "9"), (0x1d7d8, "0"), (0x1d7d9, "1"),
    (0x1d7da, "2"), (0x1d7db, "3"), (0x1d7dc, "4"), (0x1d7dd, "5"), (0x1d7de, "6"), (0x1d7df, "7"),
    (0x1d7e0, "8"), (0x1d7e1, "9"), (0x1d7e2, "0"), (0x1d7e3, "1"), (0x1d7e4, "2"), (0x1d7e5, "3"),
    (0x1d7e6, "4"), (0x1d7e7, "5"), (0x1d7e8, "6"), (0x1d7e9, "7"), (0x1d7ea, "8"), (0x1d7eb, "9"),
    (0x1d7ec, "0"), (0x1d7ed, "1"), (0x1d7ee, "2"), (0x1d7ef, "3"), (0x1d7f0, "4"), (0x1d7f1, "5"),
    (0x1d7f2, "6"), (0x1d7f3, "7"), (0x1d7f4, "8"), (0x1d7f5, "9"), (0x1d7f6, "0"), (0x1d7f7, "1"),
    (0x1d7f8, "2"), (0x1d7f9, "3"), (0x1d7fa, "4"), (0x1d7fb, "5"), (0x1d7fc, "6"), (0x1d7fd, "7"),
    (0x1d7fe, "8"), (0x1d7ff, "9"), (0x1f100, "0."), (0x1f101, "0,"), (0x1f102, "1,"), (0x1f103, "2,"),
    (0x1f104, "3,"), (0x1f105, "4,"), (0x1f106, "5,"), (0x1f107, "6,"), (0x1f108, "7,"), (0x1f109, "8,"),
    (0x1f10a, "9,"), (0x1f110, "(A)"), (0x1f111, "(B)"), (0x1f112, "(C)"), (0x1f113, "(D)"), (0x1f114, "(E)"),
    (0x1f115, "(F)"), (0x1f116, "(G)"), (0x1f117, "(H)"), (0x1f118, "(I)"), (0x1f119, "(J)"), (0x1f11a, "(K)"),
    (0x1f11b, "(L)"), (0x1f11c, "(M)"), (0x1f11d, "(N)"), (0x1f11e, "(O)"), (0x1f11f, "(P)"), (0x1f120, "(Q)"),
    (0x1f121, "(R)"), (0x1f122, "(S)"), (0x1f123, "(T)"), (0x1f124, "(U)"), (0x1f125, "(V)"), (0x1f126, "(W)"),
    (0x1f127, "(X)"), (0x1f128, "(Y)"), (0x1f129, "(Z)"), (0x1f12a, "S"), (0x1f12b, "C"), (0x1f12c, "R"),
    (0x1f12d, "CD"), (0x1f12e, "WZ"), (0x1f130, "A"), (0x1f131, "B"), (0x1f132, "C"), (0x1f133, "D"),
    (0x1f134, "E"), (0x1f135, "F"), (0x1f136, "G"), (0x1f137, "H"), (0x1f138, "I"), (0x1f139, "J"),
    (0x1f13a, "K"), (0x1f13b, "L"), (0x1f13c, "M"), (0x1f13d, "N"), (0x1f13e, "O"), (0x1f13f, "P"),
    (0x1f140, "Q"), (0x1f141, "R"), (0x1f142, "S"), (0x1f143, "T"), (0x1f144, "U"), (0x1f145, "V"),
    (0x1f146, "W"), (0x1f147, "X"), (0x1f148, "Y"), (0x1f149, "Z"), (0x1f14a, "HV"), (0x1f14b, "MV"),
    (0x1f14c, "SD"), (0x1f14d, "SS"), (0x1f14e, "PPV"), (0x1f14f, "WC"), (0x1f16a, "MC"), (0x1f16b, "MD"),
    (0x1f16c, "MR"), (0x1f190, "DJ"), (0x1fbf0, "0"), (0x1fbf1, "1"), (0x1fbf2, "2"), (0x1fbf3, "3"),
    (0x1fbf4, "4"), (0x1fbf5, "5"), (0x1fbf6, "6"), (0x1fbf7, "7"), (0x1fbf8, "8"), (0x1fbf9, "9"),
];
