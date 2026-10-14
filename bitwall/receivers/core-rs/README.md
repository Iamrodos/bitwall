# bitwall-receiver: a wall for a receiver in Rust

The part of a receiver that is the same whatever it paints on: the
door, the sessions and every command of PROTOCOL.md, ported from
`receivers/core.py`, which is the reference wherever PROTOCOL.md leaves a
choice. A receiver built on it brings a glass and draws; it never parses
a request, keeps a session or answers a command. `receivers/term` is
one, with a terminal for its glass; `receivers/eink` another, with a
panel of electronic paper.

    [dependencies]
    bitwall-receiver = { path = "../core-rs" }                         # cells
    bitwall-receiver = { path = "../core-rs", features = ["decode"] }  # and pictures decoded

## What it keeps

- **the base and cells layers**, each stream read into a grid (`grid.rs`),
  stacked in the two stacks of PROTOCOL.md, *Layers and types*, a cells
  layer with the base's grid;
- **video layers**, where the glass has pixels: `open <name> layer
  z=above|top|<n> type=video`, placed, moved (`moved`) and refused (`full`)
  as cells layers are, and their `frame` commands with QOI, PNG or JPEG,
  `at=`, `size=`, `hold=<ms>|still`, and `clear`. Each frame's line and
  its payload's header and end are checked (`video.rs`), every refusal
  said as the painter says it, and the latest frame kept as it came,
  encoded, with where it goes and when it came. A hold that runs out,
  `clear`, the session's end or new pixels take it down, and every mirror
  of it is told `mirror <name> clear`;
- **pictures and mirrors**: of a cells layer as its grid, of a video layer
  as the frame it shows, and of the glass as a QOI of what the glass's
  `look` gives; `mirror` of the base, of a video layer frame by frame, and
  of the glass when it has changed;
- `report`, `sessions`, `measures`, `stats` and the overlay's lines,
  `follow`, `meta`, `blank`, `presence`, `want` and `input`, pings and
  backpressure.

What it does not keep: a cells layer's own font or grid (it is answered
with the base's area), a stream on `data` (no film, no video base, no
`align`), a raw glass `stream`, and a JPEG picture of the glass.

## The glass

The receiver implements `Glass` (`glass.rs`), and hands it to the wall
whenever the wall may need to ask it:

```rust
pub enum Ask { Font(u32), Grid(usize, usize) }

pub trait Glass {
    fn area(&self, ask: Ask) -> (usize, usize, u32);      // cols, rows, the font they are at
    fn pixels(&self) -> Option<(u32, u32)> { None }       // Some: video layers, pictures of the glass
    fn cell(&self, font: u32) -> Option<(u32, u32)> { None }
    fn colour(&self) -> &str;                             // screen colour=
    fn rate(&self) -> u32;                                // screen rate=
    fn glyphs(&self) -> Option<String> { None }           // screen glyphs=
    fn render(&self) -> &str;                             // measure render=
    fn kitty(&self) -> bool { false }                     // the base's kitty strings reach it
    fn accepts(&self) -> &str { "" }                      // accepts' words
    fn measures(&self) -> Vec<(&'static str, String)> { Vec::new() }  // its own, after the wall's
    fn decode_ms(&self, z: usize) -> Option<f64> { None }
    fn look(&mut self, wall: &Wall, w: u32, h: u32) -> Option<Vec<u8>> { None }  // RGBA, as though lit
}
```

The base's kitty strings reach a glass that says `kitty` whole, in
`Wall::kitty_out` with the cell each was sent at; a glass that draws them
itself reads them, and says what it refuses on the base's `out` with
`Wall::tell_base`.

`area` is the whole of font and grid: a glass that cannot change its
cell, as a terminal cannot, answers with the area it has; one of pixels
divides them by the cell at the font, and for a grid finds the largest
font whose cells cover it. A glass of pixels says `types=cells,video
above=video,cells codecs=qoi images=qoi,png,jpeg` and `pictures qoi
grid`; one without says `types=cells above=cells`, refuses `type=video`
with `no-such-type`, and says `picture glass` is `not offered`.

## A receiver's loop

```rust
let glass = MyGlass::new();
let (dir, door) = pipe::claim(&run, &name)?;
let mut wall = Wall::new(&name, dir, door, &glass, font, receiver_line);
loop {
    pipe::wait(Duration::from_millis(15));        // a pipe's bell, or the time
    if glass_resized { wall.glass_changed(&mut glass); }
    wall.serve(Instant::now(), &mut glass);
    if wall.changed || wall.frame_closed {
        wall.changed = false;
        wall.frame_closed = false;
        for layer in wall.stack() {               // bottom to top
            match layer.surface {
                Surface::Cells(grid) => { /* grid.at(x, y) */ }
                Surface::Video(Some(frame)) => { /* frame.placed(), decode::rgba(frame) */ }
                Surface::Video(None) => {}        // transparent
            }
        }
        // wall.dark(), wall.connected (the status page), wall.overlay_lines
    }
}
```

A `Frame` is `codec`, its own `w` by `h`, `x`, `y` and `size` (the box,
`placed()` gives all four), `hold`, `data` as it came, `came`, and `seq`,
which changes with every frame, so a glass decodes each one once.
`decode::rgba(frame)`, with the `decode` feature, is its pixels: `qoi`,
`png` and `zune-jpeg`, small decoders in Rust with nothing else beneath
them. A receiver of cells leaves the feature off and builds none of them.

## Held to the contracts

`examples/headless.rs` is the core on a glass of pixels and no screen,
composed in software when it is looked at. `tests/bitwall-contracts`
runs every clause against it once it is built:

    cargo build --release --features decode --example headless
    tests/bitwall-contracts "the core-rs headless receiver"

| file | what |
|---|---|
| `src/wall.rs` | the door, sessions and every command |
| `src/glass.rs` | the boundary a receiver implements |
| `src/grid.rs` | the stream read into cells, and said back as `grid` |
| `src/video.rs` | a frame's line and header checked, the frame kept; QOI written for a picture of the glass |
| `src/decode.rs` | a frame decoded to RGBA (`decode`) |
| `src/pipe/` | a wall's pipes behind one interface: fifos in `unix.rs`, named pipes in `windows.rs` |
| `src/proto.rs`, `src/sys.rs` | the protocol's words; what the machine says of itself |
