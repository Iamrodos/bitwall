//! A base, a cells layer and a video layer: dots on the base, a band
//! across the middle on a cells layer that a marker runs along, and an
//! orange square in pixels on a video layer above both. Moved by a
//! controller like any sender built on the library (`moving`).
//!
//!     cargo run --release --example band -- localhost:0

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bitwall::frame::{Frame, BOLD, DEFAULT};
use bitwall::layer::{Hold, Layer};
use bitwall::qoi;
use bitwall::wall::{attach, stop_on_signals, wait_for, Place};

fn now() -> String {
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    format!("{:02}:{:02}:{:02}", (t / 3600) % 24, (t / 60) % 60, t % 60)
}

fn main() {
    let target = std::env::args().nth(1).unwrap_or_else(|| "localhost".into());
    let stop = stop_on_signals();
    let w = attach(&target).expect("attach");
    w.describe("Band", "a base of dots and a cells layer over it").ok();
    w.transition(|w| {
        w.set_source(|cols: usize, rows: usize| {
            let mut f = Frame::new(cols, rows);
            for y in 0..rows {
                f.text(0, y, &"·".repeat(cols), 0x444444, DEFAULT, 0);
            }
            Some(f)
        });
    });
    let band = w.cells_layer(Place::from_env().unwrap_or(Place::Above));
    let tick = Arc::new(AtomicUsize::new(0));
    let t = tick.clone();
    band.set_source(move |cols: usize, rows: usize| {
        let mut f = Frame::new(cols, rows);
        let y = rows / 2;
        for x in 0..cols {
            f.bg[y * cols + x] = 0x203060;
        }
        let at = t.load(Ordering::Relaxed) % cols.max(1);
        f.text(at, y, "●", 0xffcc00, 0x203060, BOLD);
        Some(f)
    });
    let mut sprite = Layer::new(&target, Place::Above).expect("a layer");
    if let Some(i) = w.instance() {
        sprite = sprite.instance(i);
    }
    sprite.open(Duration::from_secs(5)).expect("open the layer");
    let square = qoi::encode(&[0xff, 0x80, 0x00, 0xff].repeat(48 * 48), 48, 48);
    let show = |l: &Layer| l.frame(&square, (16, 16), None, Hold::Still).unwrap_or(false);
    show(&sprite);
    println!("{} attached to {}: base {:?}, layer z={:?} {:?}", now(), target, w.area(), band.z(), band.area());
    let mut owed = false;
    while !stop.load(Ordering::SeqCst) {
        tick.fetch_add(1, Ordering::Relaxed);
        // Asked for while the layer is not yet open on a wall that is new,
        // the frame goes once it is.
        owed |= sprite.repaint_asked();
        if owed && show(&sprite) {
            owed = false;
            println!("{} the square sent again on {}", now(), w.target());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    wait_for(&stop);
    sprite.close();
    band.detach();
    w.detach();
}
