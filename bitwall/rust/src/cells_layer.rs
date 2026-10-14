//! A cells layer above the base (`bitwall/__init__.py`, `CellsLayer`).
//!
//! The wall's grid again, at a place of its own among the layers, over
//! whatever the base and the layers beneath it show, every cell it has not
//! drawn transparent. It is a `Wall` in every way but the session it asks
//! for -- the same render loop, writer and link -- so it is written to with
//! `set_source`, told its area and `repaint` as the base is, and moved with
//! the rest of the sender. Until it asks otherwise its grid is the base's,
//! cell for cell; `set_font_size` or `grid` gives it a grid of its own,
//! kept through a change of glass, and `follow_base` goes back. Over a film
//! there is no base grid, and a layer that has not asked has the wall's
//! font as it stands. `z` is the place the wall gave, once it has opened
//! the layer. A wall whose `caps` do not say `cells` in `above=` refuses
//! it, and the layer stays out (`stopped`).

use std::ops::Deref;
use std::thread;
use std::time::{Duration, Instant};

use crate::session::Target;
use crate::wall::{take_instance, Kind, Place, Wall};

/// How long `attach` waits for the wall to say the layer's area.
const AREA_WAIT: Duration = Duration::from_secs(8);

/// A cells layer: a `Wall` (it derefs to one) whose session is a layer at
/// a place above the base. Cheap to clone; every clone is the same layer.
#[derive(Clone)]
pub struct CellsLayer {
    wall: Wall,
}

impl Deref for CellsLayer {
    type Target = Wall;

    fn deref(&self) -> &Wall {
        &self.wall
    }
}

/// Open a cells layer on `target` at `place`, a frame at most every 50 ms,
/// and wait up to eight seconds for the wall to say its area.
pub fn attach(target: &str, place: Place) -> Result<CellsLayer, String> {
    attach_with(target, place, Duration::from_millis(50))
}

/// `attach`, a frame at most every `min_frame`.
pub fn attach_with(target: &str, place: Place, min_frame: Duration) -> Result<CellsLayer, String> {
    let target = Target::parse(target)?;
    Ok(open(target, take_instance(), min_frame.as_secs_f64(), place))
}

fn open(target: Target, instance: Option<String>, min_frame: f64, place: Place) -> CellsLayer {
    let wall = Wall::new(target, instance, min_frame, Kind::Cells(place));
    wall.start();
    // A base learns its area from the font it asks for, and a layer asks
    // for none: it waits for the wall to say it.
    let deadline = Instant::now() + AREA_WAIT;
    while Instant::now() < deadline && wall.said("area").is_none() && wall.stopped().is_none() {
        thread::sleep(Duration::from_millis(100));
    }
    CellsLayer { wall }
}

impl Wall {
    /// A cells layer over this wall's base, on the same wall and saying
    /// the same instance, so a controller knows the two as one sender's
    /// and moves them together. Its frame period is this Wall's.
    pub fn cells_layer(&self, place: Place) -> CellsLayer {
        let min_frame = self.asked_frame();
        open(self.target(), self.instance().map(String::from), min_frame, place)
    }
}

impl CellsLayer {
    /// The place the wall gave, once it has opened the layer.
    pub fn z(&self) -> Option<i64> {
        self.wall.z_given()
    }

    /// Give up a font or grid of the layer's own and follow the base's
    /// grid again, as a layer that has asked neither does.
    pub fn follow_base(&self) {
        self.wall.follow_base();
    }

    /// The layer as the Wall it is.
    pub fn wall(&self) -> &Wall {
        &self.wall
    }
}
