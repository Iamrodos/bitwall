//! The glass: what a receiver puts behind the wall. The wall keeps the
//! door, the sessions and every layer's content; the glass says what it
//! can show and draws it. A terminal is a glass of cells with no pixels;
//! a panel or a window is one of pixels, whose cell comes from the font.

use crate::wall::Wall;

/// What the area was asked to be: a font, or a number of cells.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Ask {
    Font(u32),
    Grid(usize, usize),
}

/// A receiver's glass, as the wall asks it. Every answer is read when it
/// is said, so a glass that changes -- a terminal dragged, a panel turned
/// -- says so with `Wall::glass_changed` and the wall says it on.
pub trait Glass {
    /// The cells there are for what was asked, and the font they are drawn
    /// at. A grid is the largest font whose cells cover it; a glass that
    /// cannot change its cell answers with the area it has, whatever was
    /// asked. An area that does not cover a grid asked makes the request
    /// `too many cells for this screen`.
    fn area(&self, ask: Ask) -> (usize, usize, u32);

    /// The glass in pixels, or None where the receiver never sees them, as
    /// a terminal does not. A glass of pixels takes video layers, and is
    /// pictured and mirrored by `look`.
    fn pixels(&self) -> Option<(u32, u32)> {
        None
    }

    /// A cell's size in pixels at a font, said in `screen` beside `pixels`.
    fn cell(&self, _font: u32) -> Option<(u32, u32)> {
        None
    }

    /// `screen`'s `colour=`: `true` for truecolour, `grey` for a panel of
    /// greys, and so on.
    fn colour(&self) -> &str;

    /// `screen`'s `rate=`: how many times a second it can change.
    fn rate(&self) -> u32;

    /// `screen`'s `glyphs=`: the code points it draws, as CSS
    /// `unicode-range` spells them, or None where it does not say.
    fn glyphs(&self) -> Option<String> {
        None
    }

    /// What draws it, `render=` in `measure` and the overlay.
    fn render(&self) -> &str;

    /// Whether the base's kitty graphics reach it, passed on by the wall as
    /// `Wall::kitty_out`; `kitty` in `caps` where they do.
    fn kitty(&self) -> bool {
        false
    }

    /// What a viewer may give it: `accepts`' words, `touch keyboard`, or
    /// none.
    fn accepts(&self) -> &str {
        ""
    }

    /// Measures of its own, each name and its value now, `-` until it has
    /// one: shown in the overlay as `show` asks, said on the wall's
    /// `measure` line, and named in `measures` after the wall's own.
    fn measures(&self) -> Vec<(&'static str, String)> {
        Vec::new()
    }

    /// How long the layer at `z` took to decode its last frame, in ms,
    /// said on that layer's `measure` line.
    fn decode_ms(&self, _z: usize) -> Option<f64> {
        None
    }

    /// The glass as it shows, every layer composed and the overlay left
    /// out, as though lit, scaled to `w` by `h`: RGBA, four bytes a pixel.
    /// None where it cannot be had, which is `not offered`.
    fn look(&mut self, _wall: &Wall, _w: u32, _h: u32) -> Option<Vec<u8>> {
        None
    }
}
