//! cells on a wall, in Rust: the same organisms as `../main.py`, drawn by
//! `life.rs` and sent through bitwall's Rust library.
//!
//!     cells pi.local
//!     cells --population 12 --speed 1.5 pi.local
//!     cells --no-split --no-merge --hue 200 --spread 30 pi.local
//!     cells --term                    # in this terminal, no wall; q to quit
//!
//! The options are main.py's, and as there never fatal
//! (`bitwall::sender`): font and fps are clamped as they are parsed, the
//! organisms' numbers by `Config::clamp`, and each change is said on
//! stderr.

mod life;

use bitwall::sender::{self, Options};
use bitwall::{log, term, warn};
use life::{Config, World, KINDS};

fn main() {
    let names = KINDS.iter().map(|k| k.name).collect::<Vec<_>>().join(",");
    let args = term::option(Options::new("cells", "Soft organisms that drift, split, merge and die"))
        // bodies big enough to read across a room
        .clamped("font", 18.0, 4.0, 400.0, "font size to ask for")
        .clamped("fps", 12.0, 1.0, 60.0, "frames a second at most")
        .number("population", 8.0, "organisms aimed for on a 150x45 grid; scales with the area, 1..60")
        .number("speed", 1.0, "multiplier on every kind's speed, 0.1..5")
        .number("size", 1.0, "multiplier on every kind's size, 0.3..3")
        .number("lifespan", 1.0, "multiplier on every kind's lifespan, 0.1..10")
        .maybe("hue", "base hue in degrees for every body, instead of each kind's own palette")
        .number("spread", 40.0, "hue spread either side of --hue, 0..180")
        .text("kinds", &names, "which kinds are born, comma separated")
        .maybe("seed", "random seed, for a run that repeats")
        .switch("split", true, "turn off splitting")
        .switch("merge", true, "turn off merging")
        .switch("birth", true, "turn off births")
        .switch("death", true, "turn off deaths")
        .switch("growth", true, "turn off growing")
        .switch("aura", true, "turn off the glow round each body")
        .parse_env();

    let mut cfg = Config {
        population: args.number("population"),
        speed: args.number("speed"),
        size: args.number("size"),
        lifespan: args.number("lifespan"),
        hue: args.maybe("hue"),
        spread: args.number("spread"),
        kinds: args.text("kinds").split(',').map(str::trim).filter(|k| !k.is_empty())
            .map(String::from).collect(),
        seed: args.maybe("seed").map(|v| v as u64),
        split: args.on("split"),
        merge: args.on("merge"),
        birth: args.on("birth"),
        death: args.on("death"),
        growth: args.on("growth"),
        aura: args.on("aura"),
    };
    for note in cfg.clamp() {
        warn!("cells: {note}");
    }
    let font = args.number("font").round() as u32;
    let population = cfg.population;
    let mut world = World::new(cfg);
    let period = sender::period(args.number("fps"));

    if term::wanted(&args) {
        term::main(move |cols: usize, rows: usize| Some(world.frame(cols, rows)), period);
    }
    let target = args.target_or_exit();
    sender::run(&target, period, |w, _stop| {
        w.transition(|w| {
            if let Err(e) = w.set_font_size(font) {
                warn!("cells: font {font}: {e}");
            }
            w.set_source(move |cols: usize, rows: usize| Some(world.frame(cols, rows)));
        });
        if let Err(e) = w.describe("cells", "Soft organisms that drift, split, merge and die") {
            warn!("cells: meta: {e}");
        }
        log!("cells: population {population} (rust)");
    });
}
