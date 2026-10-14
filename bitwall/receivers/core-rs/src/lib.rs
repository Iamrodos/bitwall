//! A bitwall wall for a receiver to give a glass: the door, the sessions
//! and every command, ported from bitwall/receivers/core.py, which is the
//! reference wherever PROTOCOL.md leaves a choice; the base's and each
//! cells layer's stream read into a grid; each video layer's latest frame
//! kept as it came. The receiver supplies the glass (`Glass`), asks the
//! wall for its layers (`Wall::stack`) and draws them.

pub mod glass;
pub mod grid;
pub mod pipe;
pub mod proto;
pub mod sys;
pub mod video;
pub mod wall;

#[cfg(feature = "decode")]
pub mod decode;

pub use glass::{Ask, Glass};
pub use video::{Codec, Frame};
pub use wall::{Layer, Surface, Wall};
