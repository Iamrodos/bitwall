# bitwall-term: a wall on a terminal

A receiver whose glass is the terminal it runs in, on Windows, a Mac or
Linux, as one program with nothing else to install. `bitwall-shell` and
`bitwall-ps` are the least a receiver can be, and stay that way; the
painter is a screen, pixels on a GPU. This is a terminal done properly.

    bitwall-term [--font <n>] [wall]        wall 0 unless named

    cargo build --release                   target/release/bitwall-term

`BITWALL_RUN` puts the walls elsewhere. `--font` (or `BITWALL_FONT`) is the
size the area is said to be drawn at, 14 unless given, since a terminal
cannot say. Ctrl+C ends it, and the wall goes with it.

## What it is

The shell receivers copy the base's stream to the terminal and never look
at it. This one reads the stream into a grid, as `bitwall/grid.py` and the
web page's parser read it: a reset, a cursor move, colour runs, and APC
strings, which are commands, kitty graphics, or dropped. It is not a
terminal emulator, which is what keeps it cheap. From the grid it holds:

- **cells layers** above the base, stacked in the two stacks of PROTOCOL.md
  (*Layers and types*), each blended over what is beneath by its alpha, a
  cell it has not drawn and a space in the default colours showing through;
- **the stats overlay**, drawn over the grid in a box at the point asked;
- **`picture`** of the base or of a cells layer, as `grid`, and **`mirror`**
  of the base, a whole grid and then diffs;
- **`measures`**, with the parse time of a frame (`frame`), the time to
  write the changed cells to the terminal (`draw`), and the process's CPU;
- **input**: the terminal's keys and clicks, said to the base as `input key`
  and `input tap` where it asked for them with `want`, named as the web
  page names them (`Return`, `BackSpace`, `space`, `Up`, `F5`, `a`). Clicks
  are reported by the terminal only while the base wants taps, so its own
  selection works the rest of the time. While the base has not asked for
  the keyboard the bare keys are the viewer's, as on the web receiver's
  page: `s` the wall's stats, for everyone watching, `?` the keys, `esc`
  to put them away.

It draws only the cells that differ from what the terminal already shows,
once a frame has closed (each bare `ESC[0m`), or a tenth of a second on
for a sender that stops mid-frame. A drag of the terminal's size is one
change, taken once it has held for 300 ms, as a glass is; every cells
session is then told its `area`, the `screen` line and `repaint`.

## What it says

    caps area font grid meta blank want input presence repaint watching ping detach close screen picture mirror stats sessions follow measures clear kitty layers=8 types=cells above=cells controllers=8
    measures wall grid fps in out frame draw sender pong display cpu render
    pictures grid
    screen colour=true rate=60
    accepts touch keyboard

`kitty` is there where the terminal answers kitty's question yes, asked
once as it starts, as bitwall-shell asks it. The base's kitty strings are
passed to the terminal at the cell they were sent at, and deleted when the
base goes.

## Limits

- **No glass in pixels.** The terminal draws every pixel and the wall never
  sees them, so `picture glass` and `mirror glass` are `not offered`, and
  `screen` says no `pixels` or `cell`. For the same reason there is no video
  layer and no `data`: `type=video` is `no-such-type`.
- **No font of its own.** A terminal cannot change its cell, so `font` and
  `grid` are answered with the area there is, which PROTOCOL.md allows
  (*Requests are advisory*); a `grid` larger than the terminal is
  `too many cells for this screen`. A cells layer has the base's grid.
- **Faint braille** is drawn as plain braille: a terminal has no way to
  show the unlit dots.
- **No mirror of a cells layer**, as the receiver core offers none.

## How it is made

The wall is `receivers/core-rs`, shared with every receiver in Rust: the
door, sessions and every command, the streams read into grids, and the
pipes (its README.md). This program is the terminal as its glass.

| file | what |
|---|---|
| `src/screen.rs` | the terminal as the wall's `Glass`: the layers composed, drawn to the terminal, the status page and the overlay; kitty's question |
| `src/main.rs` | the loop: keys and clicks, the terminal's size once it has held, and a draw between frames |

Each pipe the wall reads has a thread that hands over one piece at a time
and reads the next only once the wall has taken it, so a wall that stops
reading fills the pipe and its writer waits. On Windows the pipes are
those of WINDOWS.md -- the door's four instances, the first the claim,
remote clients refused, `out` written without waiting -- each made for its
user, the administrators and the system, so a wall made in an elevated
console is reached from one that is not, and from ssh.

Its version is the SHA-256 of its sources and core-rs's, worked out by
`build.rs` from the same list as `SOURCES` in `receivers/__init__.py`.

## Measured

On Windows 11, in a terminal of 155x33:

- `bitwall-ctl`: `report`, `sessions`, `stats`, `blank`, `presence`,
  `font`, `grid` and an unknown command each answered as the receiver core
  answers them.
- snake on the base at 20 fps, its cap: 0.06 ms to read a frame.
- fluid on the base, the whole grid every frame: 20 fps, its cap, 1.5 MB/s
  in, 0.5 ms to read a frame, 5 ms to draw it, 12-16% of a core.
- the ticker as a cells layer at 1 over snake, then over touch.py when it
  took the base from snake with `take=1`.
- touch.py heard `input key key=o`, `k`, `Return` and `Up` typed into the
  terminal.
- two resizes 100 ms apart were one `area 155x20 14` and one `repaint` to
  each sender.
- Ctrl+C: the terminal given back and every pipe gone; started again, the
  base and the layer came back by themselves, each at its place.

On an M-series Mac, macOS 15, in a terminal of 144x80:

- a game on the base and the ticker as a cells layer at 1, both started on
  `localhost:term`; the control page pictures them as cells, and keeps them
  live at its rate.
- Killed and started again with Ctrl+C: both came back by themselves, each
  at its place.
- `tests/bitwall-contracts "the term receiver"`: 40 passed, none failed,
  45 known and 10 skipped, the known ones and the skips being what is
  above under *Limits*: video, a font of its own, and the glass.

A Mac's `poll()` never says a fifo's writer has gone, so core-rs's `pipe/unix.rs`
reads a fifo on each quiet tenth of a second once a writer has written to
it, and an empty read there is end of file; without it a sender that
closed `in` was never `closed eof`.
