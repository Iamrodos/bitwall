//! A whole sender in Rust: colour flowing across a wall, as senders/wave.py.
//!
//!     cargo run --release --example wave -- pi.local

use bitwall::frame::Frame;
use bitwall::wall::{attach_with, stop_on_signals, wait_for};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn colour(x: f64, y: f64, t: f64) -> i32 {
    let hue = (x / 90.0 + (y / 10.0 + t).sin() / 5.0 + t / 12.0).rem_euclid(1.0);
    let (s, v, h) = (0.65, 1.0, hue * 6.0);
    let f = h - h.floor();
    let (p, q, u) = (v * (1.0 - s), v * (1.0 - s * f), v * (1.0 - s * (1.0 - f)));
    let (r, g, b) = [(v, u, p), (q, v, p), (p, v, u), (p, q, v), (u, p, v), (v, p, q)][h as usize % 6];
    ((r * 255.0) as i32) << 16 | ((g * 255.0) as i32) << 8 | (b * 255.0) as i32
}

fn render(cols: usize, rows: usize) -> Option<Frame> {   // every frame, any size
    let t = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64();
    let mut f = Frame::new(cols, rows);
    for i in 0..cols * rows {
        let (x, y) = ((i % cols) as f64, (i / cols * 2) as f64);
        (f.ch[i], f.fg[i], f.bg[i]) = ('▀' as u32, colour(x, y, t), colour(x, y + 1.0, t));
    }
    Some(f)
}

fn main() {
    let target = std::env::args().nth(1).expect("the wall, host:wall");
    let stop = stop_on_signals();
    let w = attach_with(&target, Duration::from_secs_f64(1.0 / 30.0)).expect("attach");
    w.describe("Wave", "Colour flowing across the wall").ok();
    w.transition(|w| {
        w.set_font_size(12).ok();
        w.set_source(render);
    });
    let (cols, rows) = w.area();
    println!("wave on {target} {cols}x{rows}");
    wait_for(&stop);
    w.detach();
}
