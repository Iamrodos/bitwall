//! dots, with `--term`: on a wall, or in this terminal with no wall.
//!
//!     cargo run --release --example term_dots -- pi.local
//!     cargo run --release --example term_dots -- --term

use bitwall::frame::{Frame, DEFAULT};
use bitwall::sender::{self, Options};
use bitwall::{log, term};

fn main() {
    let args = term::option(Options::new("term_dots", "a dot that wanders over a field of dots"))
        .clamped("fps", 12.0, 1.0, 60.0, "frames a second at most")
        .parse_env();
    let period = sender::period(args.number("fps"));
    let mut tick = 0usize;
    let source = move |cols: usize, rows: usize| {
        let mut f = Frame::new(cols, rows);
        for y in 0..rows {
            f.text(0, y, &"·".repeat(cols), 0x444444, DEFAULT, 0);
        }
        if cols * rows > 0 {
            let at = tick % (cols * rows);
            f.ch[at] = '●' as u32;
            f.fg[at] = 0xffaa00;
        }
        tick += 1;
        Some(f)
    };
    if term::wanted(&args) {
        term::main(source, period);
    }
    let target = args.target_or_exit();
    sender::run(&target, period, |w, _stop| {
        w.set_source(source);
        log!("term_dots on {target}");
    });
}
