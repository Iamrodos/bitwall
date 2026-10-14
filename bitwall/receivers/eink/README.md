# bitwall-eink: a wall on electronic paper

A receiver whose glass is a panel of e-paper behind a Raspberry Pi: cells,
cells layers and pictures composed in sixteen greys and pushed to the
panel when the wall decides, since a panel that takes half a second to
refresh, and wears with every refresh, cannot simply paint what it is
sent. Built and measured on a Waveshare 10.3" e-Paper HAT (B), an IT8951
controller driving an ED103TC2 panel of 1872x1404 in 16 greys, on a
Raspberry Pi 3.

    bitwall-eink [--font <n>] [--rotate 0|90|180|270] [--mirror]
                 [--vcom <volts>] [--presence <name>] [--panel it8951|file:DIR] [--glass WxH]
                 [--paper|--screen] [wall]          wall `paper` unless named
    bitwall-eink --cell <font>                       a cell's pixels at a font

    cargo build --release                            target/release/bitwall-eink

| option | what |
|---|---|
| `--font N` | the font the wall starts at, 22 unless given (or `BITWALL_FONT`): 144x50 cells of 13x28 on the 10.3" panel |
| `--rotate N` | degrees clockwise the glass is turned on the panel, for one hung that way; at 90 and 270 the wall is taller than wide |
| `--mirror` | the glass mirrored left to right after turning, for a panel that shows it backwards. Not for the HAT above: its driver already loads each row right to left, as the ED103TC2 scans |
| `--vcom V` | the panel's VCOM, from the label on its ribbon (`-1.17`); set every start, since the board forgets it |
| `--presence NAME` | the presence the wall follows, said on its `presence` line so the controller feeding that name finds it; nobody there, and nothing is pushed while the picture stays |
| `--panel` | `it8951`, the HAT on the Pi's SPI, on Linux; `file:DIR` writes every push to DIR as a PNG and a line of log, the panel on a machine with none, and the default there |
| `--glass WxH` | a file panel's size, 1872x1404 unless given; a line `glass WxH` on stdin changes it |
| `--paper`, `--screen` | cells turned over in brightness, black grounds white paper (the default), or drawn as a screen draws them |

`BITWALL_RUN` puts the walls elsewhere. SIGTERM or SIGINT ends it, the
picture left on the panel. Its log is its stdout: each clean push, each
sender that attaches or goes, each change of font.

## Setting it up

The HAT drives its own chip select, so the kernel's is moved off GPIO8.
In `/boot/firmware/config.txt`:

    dtparam=spi=on
    dtoverlay=spi0-1cs,cs0_pin=7

RST is GPIO17 and HRDY GPIO24, as the HAT wires them. The user it runs as
needs to be in the `spi` and `gpio` groups.

It builds with cargo on the Pi itself, or on another machine for the Pi's
target (`armv7-unknown-linux-gnueabihf` on a 32-bit Raspberry Pi OS, as the Pi 3 it was measured on runs; `aarch64-unknown-linux-gnu` on a 64-bit one) with any
Rust cross-compiler: it links nothing but libc. The driver, `it8951`, is
a default feature and built on Linux alone; elsewhere the wall has the
file panel only.

`bitwall-eink.service` runs it from boot as a user service, as
`bitwall-screen.service` runs the painter, so its walls are in the user's
runtime directory, `/run/user/<uid>/bitwall`:

    mkdir -p ~/bitwall-eink ~/.config/systemd/user
    cp target/release/bitwall-eink ~/bitwall-eink/
    cp bitwall-eink.service ~/.config/systemd/user/
    sudo loginctl enable-linger $USER
    systemctl --user enable --now bitwall-eink

Its `ExecStart` carries the VCOM and the wall's name, `paper`, so a sender
draws on it as `pi.local:paper`. Its log is `~/bitwall-eink.log`.

## What it is

The wall is `receivers/core-rs`, as `receivers/term`'s is: the door, the
sessions and every command (its README.md). This program is the panel as
its glass. Whenever a frame closes or the layers change, everything the
wall holds is handed over as one scene -- the base's cells with its kitty
pictures, cells layers, video layers' frames decoded to pictures, the
stats overlay -- composed into greys, and pushed when the policy says.
With no sender on the wall it shows a status page, as the painter does,
with no clock, so it stands still.

**How it draws.** The face is the painter's, MesloLGS Nerd Font Mono,
built in, a size of n drawn at n pixels to the em as the painter draws
it; the characters the painter draws as geometry are drawn as
geometry here too (`shapes.rs`). Each glyph is hinted, by FreeType's
autohinter for a monochrome screen as skrifa ports it, so a stem is
whole pixels of ink and never a grey smear over two; bold is the face's
own bold, built in from `fonts/` beside its licence; and a Nerd Font icon
with a blank after it on the same ground takes both cells, at the
proportions Nerd Fonts gives it outside the Mono face, as Ghostty and
kitty draw it. Colour becomes grey by its luminance.

- **Paper**, the default: cells are turned over in brightness, so the
  black ground senders compose on is white paper and their light text is
  ink. A kitty picture in the base's grid is turned over with the cells
  around it, since wordmarks and tile letters are drawn for a dark wall
  (`Compositor::kitty_paper`); a picture on a video layer never is, since
  a photograph turned over is a negative.
- **Cells** go through a contrast curve onto the sixteen levels, which
  darkens ink and pulls dim grey text down, so anti-aliased text reads as
  ink on paper.
- **Pictures**, on a layer or in the grid, are dithered onto the levels by
  serpentine Floyd-Steinberg and go through no curve.

**When it pushes.** A change is held until the senders have been quiet
for half a second, or two seconds after the first, whichever is sooner,
so a sender that never stops still shows. Only what differs from what the
panel shows is pushed, in at most six boxes, those near each other
merged. A box all black and white is refreshed fast (DU); one with any
grey in it in sixteen greys (GC16); and an area of the panel that has had
twenty fast refreshes gets a GC16, which clears the ghosts they leave. A
full repaint -- the wall starting, a sender attaching, a new font -- is
pushed clean: the whole panel to white (INIT), black, white, then the
picture in GC16; so is a push an hour after the last clean one. While
nobody is watching (`presence`) nothing is pushed and the picture stays;
on their return what waits goes at once, in GC16. Every number is in
`src/tune.rs`, and nowhere else.

## What it says

    caps area font grid meta blank want input presence repaint watching ping detach close screen picture mirror stats sessions follow measures frame clear kitty layers=8 types=cells,video above=video,cells codecs=qoi images=qoi,png,jpeg controllers=8
    measures wall grid fps in out frame draw sender pong display cpu render push refresh temp
    pictures qoi grid
    screen colour=grey rate=1 pixels=1872x1404 cell=13x28 glyphs=...

`colour=grey` and `rate=1` tell a sender what it is drawing for: sixteen
greys, and no animation. Among the measures, `draw` is the time to
compose a scene, `push` how long the last push took, `refresh` what it
was (`clean`, or `2xGC16,1xDU`), and `temp` the Pi's temperature. A
`picture glass` is the glass as composed, which is what the panel shows
or is about to.

## Measured

On a Raspberry Pi 3 and the 10.3" HAT, as `examples/panel.rs` times them
(`cargo run --release --example panel`):

- the whole panel loaded at 8 bits a pixel: about 1.85 s
- a GC16 refresh of the whole panel: about 0.55 s
- a small box of text pushed: about 0.5 s; a black and white box in DU:
  about 0.3 s
- a clean push: a few seconds

`examples/sample.rs` puts a sample wall through the file panel on any
machine and says how long each part took.

## Limits

- **No films.** No `data`, no video base and no raw `stream` of the glass:
  a video layer is a picture at a time, `frame` with `hold`.
- **A sender that changes every second keeps the panel refreshing**, a
  push every two seconds and the ghosting that comes with it. A dashboard
  for this wall should hold still between the things it reports, with no
  ticking clock or seconds.
- **The CPU on a Pi 3 is high while pushes keep coming**: each is loaded
  over SPI by the Pi itself.
- **No input**: the panel has no touch, so `accepts` is empty.
