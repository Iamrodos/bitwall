//! Every number the e-ink wall decides by, in one place. They are starting
//! points measured on one panel, a Waveshare 10.3" ED103TC2 on an IT8951
//! behind a Raspberry Pi 3, and are meant to be changed here and nowhere
//! else.

use std::time::Duration;

/// A push waits until the senders have been quiet this long...
pub const QUIET: Duration = Duration::from_millis(500);
/// ...or until this long after the first change it holds, whichever is
/// sooner, so a sender that never stops still shows.
pub const MOST: Duration = Duration::from_secs(2);

/// Fast updates an area of the panel takes before its next update is a
/// full grey one, which clears the ghosts the fast mode leaves.
pub const FAST_LIMIT: u32 = 20;
/// The side of an area that counts fast updates, in panel pixels.
pub const AREA: u32 = 234;

/// What a clean refresh does before the picture goes up in GC16, step by
/// step over the whole panel. One INIT left faint ghosts of earlier
/// pictures in white areas after an hour of full pushes, so it drives the
/// whole panel black and white as well.
pub const CLEAN: &[crate::policy::Clean] = &[crate::policy::Clean::Init, crate::policy::Clean::Black, crate::policy::Clean::White];

/// A clean refresh (CLEAN, then GC16 of the whole panel) at most this often
/// on its own account, and only as part of a push, so never when nothing
/// has changed.
pub const CLEAN_EVERY: Duration = Duration::from_secs(3600);

/// The most boxes one push loads and refreshes. Changes scattered more
/// widely are merged until there are this many, the merge that adds the
/// least area first.
pub const MAX_BOXES: usize = 6;
/// Two changed boxes this close (in glass pixels) are one box: a refresh
/// has a fixed cost, so two small ones near each other cost more than one
/// a little bigger.
pub const NEAR: u32 = 24;

/// The side of the square a change is tracked in, in glass pixels: what
/// is composed again after a change is every such square it touched.
pub const TILE: u32 = 16;

/// A loaded area's left edge and width are a multiple of this many panel
/// pixels: 4 is what 4bpp packing needs and 8 holds for every packing the
/// IT8951 takes but 1bpp.
pub const ALIGN: u32 = 8;

/// Grey levels the panel shows.
pub const LEVELS: u32 = 16;

/// The curve a cell's grey goes through before it is brought to a level,
/// on the grey as it will show (0 black ink, after paper's turning over):
/// t = v/255, t = clamp((t - FLOOR) / (CEILING - FLOOR), 0, 1) ^ GAMMA. It
/// darkens ink and pulls dim grey text down, so anti-aliased text reads
/// as ink on paper; judged on the panel. CEILING makes paper of every
/// ground near white: a sender drawing on a dark colour of its own, not
/// the terminal's black, turns over to a light grey, and is white here.
/// Pictures do not go through the curve; kitty pictures turned over with
/// the cells go through `paper_levels`, its floor and ceiling alone.
pub const CURVE_FLOOR: f64 = 0.08;
pub const CURVE_CEILING: f64 = 0.80;
pub const CURVE_GAMMA: f64 = 1.8;

/// Whether a cell is rounded to two pixels across and four down, as the
/// Pis' painters round it with BITWALL_EVEN_CELLS=1. Paper keeps the
/// face's own cell, which is what font 22 was judged at on the panel.
pub const EVEN_CELLS: bool = false;

/// The font a wall starts at: 144x50 cells of 13x28 on the 10.3" panel,
/// chosen looking at it. And the sizes it takes.
pub const FONT: u32 = 22;
pub const FONT_MIN: u32 = 4;
pub const FONT_MAX: u32 = 400;

/// The IT8951's SPI clocks: commands slowly, pixels fast.
pub const SPI_SLOW: u32 = 2_000_000;
pub const SPI_FAST: u32 = 12_000_000;
/// The panel's VCOM, in millivolts below zero, from its ribbon label.
pub const VCOM_MV: u16 = 1170;
/// The most bytes one spidev transfer carries, its default `bufsiz`.
pub const SPI_CHUNK: usize = 4096;
/// The most bytes one USB load carries, its header with them.
pub const USB_CHUNK: usize = 60 * 1024;
