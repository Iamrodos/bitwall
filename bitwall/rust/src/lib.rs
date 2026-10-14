//! bitwall for a sender written in Rust.
//!
//! The same contract as the Python library in `bitwall/`, and for the same
//! frames the same bytes on the wire: a wall cannot tell which wrote to it.
//! Each module is the Python module of the same name, and its tests hold it
//! to golden files that Python wrote (`tests/golden/`).
//!
//! - `protocol`: the words: a line parsed and rendered, a command wrapped
//!   as APC, the bounds on what a wall may say (`bitwall/protocol.py`).
//! - `frame`: a frame as arrays, diffed and serialised
//!   (`bitwall/arrays.py`, `bitwall/stream.py`).
//! - `session`: a wall's door and sessions, here or over ssh
//!   (`bitwall/session.py`).
//! - `wall`: the base, its render loop and what it hears
//!   (`bitwall/__init__.py`).
//! - `cells_layer`: a cells layer above the base, the same engine as the
//!   base at a place of its own (`CellsLayer`).
//! - `moving`: a running sender moved by its controller, SIGUSR1 and the
//!   record (`bitwall/moving.py`).
//! - `layer`: a video layer (`bitwall/video.py`), and `qoi` the pictures
//!   it sends.
//! - `sender`: what every sender's `main` does: the log (`log!`, `warn!`),
//!   options that are never fatal, and a run that detaches last.
//! - `pixels`: a picture in pixels as cells, half blocks to octants
//!   (`senders/games/cellfilm/player.py`'s painting).
//! - `letters`: big text drawn from cells (`bitwall/letters.py`).
//! - `term`: a sender's picture in this terminal with no wall, `--term`
//!   (a game's `pane-<name>`).
//!
//! Threads, not async, as the Python library has them.

pub mod cells_layer;
pub mod frame;
pub mod layer;
pub mod letters;
pub mod moving;
pub mod pixels;
pub mod protocol;
pub mod qoi;
pub mod sender;
pub mod session;
pub mod term;
pub mod wall;
