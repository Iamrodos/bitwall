//! The smallest sender that draws, in Rust: README.md's dots.
//!
//!     cargo run --release --example dots -- pi.local

use bitwall::frame::Frame;
use bitwall::wall::{attach, stop_on_signals, wait_for};

fn main() {
    let target = std::env::args().nth(1).unwrap_or_else(|| "localhost".into());
    let stop = stop_on_signals();
    let w = attach(&target).expect("attach");
    w.describe("Dots", "the smallest thing that draws").ok();
    w.transition(|w| {
        w.set_font_size(18).ok();
        w.set_source(|cols: usize, rows: usize| {
            let mut f = Frame::new(cols, rows);
            for y in 0..rows {
                f.text(0, y, &"·".repeat(cols), 0x444444, bitwall::frame::DEFAULT, 0);
            }
            Some(f)
        });
    });
    let (cols, rows) = w.area();
    println!("dots -> {target} {cols}x{rows}");
    wait_for(&stop);
    w.detach();
}
