# bitwall in Rust

A sender written in Rust, on the same contract as the Python library in
`bitwall/`: for the same frames it writes the same bytes, so a wall cannot
tell which wrote to it. It is for a sender whose picture is expensive to
make. `senders/games/cells/rust` is the one there is: 30 fps at 1706x480
cells, against 5 for the Python cells, for the same CPU.

Rust is needed only where a Rust sender is built and run (the Mac today).
The wall's end is unchanged: a Pi or Windows painter, or a web wall, reads
what a Rust sender writes as it reads Python's.

## A sender

```rust
use bitwall::frame::{Frame, DEFAULT};
use bitwall::wall::{attach, stop_on_signals, wait_for};

fn main() {
    let stop = stop_on_signals();                   // SIGINT and SIGTERM
    let w = attach("pi.local").expect("attach");     // or attach_with(target, min_frame)
    w.describe("Dots", "the smallest thing that draws").ok();
    w.transition(|w| {                              // font and source change as one move
        w.set_font_size(18).ok();
        w.set_source(|cols: usize, rows: usize| {   // None: nothing changed
            let mut f = Frame::new(cols, rows);
            for y in 0..rows {
                f.text(0, y, &"·".repeat(cols), 0x444444, DEFAULT, 0);
            }
            Some(f)
        });
    });
    let (cols, rows) = w.area();                     // always ask; never assume
    wait_for(&stop);
    w.detach();                                      // tells the wall it is free
}
```

That is `examples/dots.rs`: `cargo run --release --example dots -- pi.local`.

A source is anything `FnMut(cols, rows) -> Option<Frame> + Send`, or a type
with `bitwall::wall::Source`. A `Frame` is arrays, a cell each, row by row:
`ch` a code point (0 for the second half of a wide glyph), `fg` and `bg`
`0xRRGGBB` or `DEFAULT`, and `attrs` the bits `BOLD`, `ITALICS`, `UNDERSCORE`,
`STRIKE`, `REVERSE` and `FAINT`. Fill the arrays directly for a picture, or
`text` for words. A frame of the wrong size is clipped or padded.

`Wall` is a handle every thread may share (`Clone`); its methods are the
Python Wall's: `area`, `set_source`, `set_font_size`, `grid`, `transition`
(or `begin`, which holds until the guard drops), `describe` and `meta`,
`watching`, `can` and `glyphs`, `offers` and `capabilities`, `want` and
`input`, `blank`, `passthrough` for kitty graphics, `stopped`, `detach`.
`cargo doc --open` has them all.

Everything in AGENTS.md *The sender API* holds: print the time at the start
of each log line and only events, stop on SIGTERM and detach last, change
font and source only inside `transition`, and let the wall decide when
nobody is watching.

## A cells layer

```rust
let band = w.cells_layer(Place::Above);           // or cells_layer::attach(target, place)
band.set_source(|cols, rows| ...);                // cells it leaves DEFAULT are transparent
band.z();                                         // the place the wall gave
band.follow_base();                               // after a set_font_size or grid of its own
```

A `CellsLayer` is a `Wall` (it derefs to one) whose session is a layer
above the base, on a wall whose `caps` say `cells` in `above=`: the same
render loop and writer, every method of a Wall. Made from a Wall it says
the Wall's instance, so a controller knows the two as one sender's.
`Place::from_env()` is where the catalogue entry put it.
`examples/band.rs` is a base and a layer over it.

## Moving

A sender a controller started (`BITWALL_INSTANCE`, `BITWALL_RECORD`) is
moved by `bitwall-senders move` with no code of its own, as in Python
(CONTROL.md, *Moving a sender*): on SIGUSR1 the library reads `target`
from the record and calls `retarget` on every Wall, cells layer and video
layer of the process that says the instance. The old wall is told
`detach` (a video layer's `close`); the new one is told meta, font or
grid, wants and blank again and painted whole. A video layer opens at the
place first asked for, ends a stream on `data` rather than carry it, and
sets `repaint_asked` for a frame that stands alone.
`when_lost` and `before_leaving` are called as in Python. A sender may
call `retarget` itself. Anything else that can move implements
`moving::Retarget` and is given to `moving::hold`.

## In a terminal, with no wall

`term` runs any source in the terminal it was started from, the Rust
`pane-<name>`: sized from the tty, diffed with `frame::serialise`, a
full repaint on a resize, the terminal given back on a stop, `q`, or a
panic. A sender takes `--term` by declaring it and choosing:

```rust
let args = term::option(Options::new("cells", "...")).parse_env();
if term::wanted(&args) {
    term::main(source, sender::period(fps));     // never returns
}
let target = args.target_or_exit();
```

`examples/term_dots.rs` is the smallest; `cells --term` the real one.

## Building

```sh
cd bitwall/rust && cargo build --release      # the library, and cargo test
cd senders/games/cells/rust && cargo build --release
```

A sender's crate depends on the library by path, `bitwall = { path =
"<repo>/bitwall/rust" }`. A controller starts the built binary: its
catalogue entry's `start` names it, and its options are passed as
`--<name> <value>` with the wall last, as for any sender. A Rust sender's
entry lives beside its crate, not in `senders/catalogue/`, since every
sender host reads that one and only a host that built the binary can
start it: `senders/games/cells/rust/catalogue/cells.json`, with `start`
`../target/release/cells`, linked in where it is built as
`~/.bitwall/senders/rust`, so the Mac lists it as `rust/cells`.

## How it is kept the same as Python

| module | is | proved by |
|---|---|---|
| `protocol` | the words: parse, render, APC commands, the bounds on what a wall may say | `tests/golden/protocol.json`, 1134 cases Python wrote, and a fuzz test |
| `frame` | a frame as arrays, diffed and serialised at every colour depth | `tests/golden/frame.bin`, 175 outputs of `stream.serialise`, byte for byte |
| `session` | the door and sessions, here or over ssh, with Python's own opener | `tests/session_painter.rs`, against a real painter |
| `wall` | the base: attach, the render loop, what the wall says, attach again, retarget | its unit tests, and senders run on a Mac and on a Pi |
| `cells_layer` | a cells layer, the base's engine at a place of its own | `examples/band.rs` on a Mac painter, moved between its walls |
| `moving` | SIGUSR1, the record, everything held moved | its unit tests, and `instance.py repoint` on `examples/band.rs` |
| `layer`, `qoi` | a video layer: pictures, a film on `data`, opened again when the wall comes back | `tests/golden/layer_qoi.bin`, 24 pictures byte for byte, and `tests/layer_painter.rs` against a real painter |
| `pixels` | a picture in pixels as cells, half blocks to octants and braille | `tests/golden/pixels.bin`, 224 cases of cellfilm's `player.py` |
| `letters` | big text in the cell fonts, chosen by what the wall can draw | `tests/golden/letters.json`, 4195 cases cell for cell |
| `sender` | `log!`, `warn!`, options never fatal, `run` | its unit tests, and cells on a painter |
| `term` | a source drawn in this terminal, `--term` | its unit tests, and cells in a pty |

`tests/golden/*.py` write the golden files from the Python library; run
them again when it changes (`uv run --no-project --with numpy python
tests/golden/frame.py`), and `cargo test` holds the Rust to them.

## Limits

Not in the Rust library, where Python has them:

- pictures and mirrors;
- an alpha in a `Frame` colour (`rrggbbaa`, `38;6;r;g;b;a`) for a
  translucent cells layer;
- a Windows host as the wall's end over ssh (the relay); a wall on the
  same Windows machine;
- TILE and the outline fonts in `letters`, which are kitty pictures;
- the picture helpers and panes.
