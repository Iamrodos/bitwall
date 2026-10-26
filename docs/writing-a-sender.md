# Writing a sender

Everything you need to build something that puts content on a bitwall
wall, whoever you are, person or agent. PROTOCOL.md is the contract
underneath, and you should not need it to write a sender.

---

## 1. What the system is

A **sender** computes everything and streams it to a **receiver**, which
paints it and knows nothing else. All credentials and all data stay on the
sender. A receiver is a screen on a dumb machine.

The **receiver contract is three things**:

1. accept a font size request
2. publish the area it can offer, in cells
3. consume a stream and paint it

That is the entire interface. A Raspberry Pi's screen, a window on a Mac
or Windows, a browser tab, a terminal and a plain shell are all receivers,
and a sender cannot tell them apart except by what they say they can do.

**You are writing a sender.** A sender is a normal program. It opens no
socket, implements no protocol, and holds no state about the screen. It
attaches, asks for a font size, and hands over grids.

### The wire

A wall is a directory on its host with a door. `attach()` asks at the door
for a session and gets two fifos: frames and requests go down its `in`,
everything the wall has to say to you comes back up its `out`. On another
host the same happens through ssh.

You never touch any of that. It is described only so you know that the
area you read is a *fact published by the receiver*, not a value you set.

A target names a wall: `pi.local` is wall 0 there, `pi.local:1` its second
screen, `localhost:kitchen` a web wall named kitchen on this machine.

### The cell format

```python
(text, fg, bg, bold, italics, underscore, strikethrough, reverse[, faint])
```

Build them with `bitwall.cell(text, fg, bg, ...)`, which fills the
defaults. Colours are:

| Value | Meaning |
|---|---|
| `"default"` | the receiver's own foreground or background |
| `"ff8800"` | truecolour, as six hex digits, no `#` |
| `"ff880080"` | truecolour with an alpha, for a cells layer over something (section 8) |
| `0`-`255` | an xterm-256 palette index |
| `"red"`, `"blue"` | the eight named colours, `black` to `white` |

`faint=True` on a braille character asks the wall to show the unlit dots
too, in the cell's ground colour: a dot-matrix display.

**Truecolour works end to end.** Do not quantise to 256 colours and do not
dither; a screen that shows fewer colours says so and is sent fewer, for
you (section 4).

A grid is `rows` lists of `cols` cells. That is all a sender ever produces.

---

## 2. A complete sender

```python
#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# ///
"""A clock on the wall.

    senders/clock.py pi.local
"""
import os
import signal
import sys
import threading
import time

# The repo root, so `import bitwall` finds the library.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bitwall                                        # noqa: E402
from bitwall import cell                              # noqa: E402


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def render(cols, rows):
    grid = [[cell(" ") for _ in range(cols)] for _ in range(rows)]
    text = time.strftime("%H:%M:%S")[:cols]
    for i, ch in enumerate(text):
        grid[rows // 2][(cols - len(text)) // 2 + i] = cell(ch, "88ccff")
    return grid


def main():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    w = bitwall.attach(sys.argv[1], min_frame=1 / 10)
    w.meta(name="Clock", description="The time, in the middle")
    with w.transition():
        w.set_font_size(20)
        w.set_source(render)
    log(f"clock on {sys.argv[1]}, {w.area()[0]}x{w.area()[1]}")
    stop.wait()
    log("stopped")
    w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run it with a wall's target: `senders/clock.py localhost` for a wall on
this machine, `senders/clock.py pi.local:1` for a Pi's second screen. A
sender outside the repo puts the repo root on its path the same way, from
wherever it keeps it. `senders/hello.py` is a slightly larger one, drawn
with `rich`.

Everything in it is there for a reason:

- **`meta`** says what the sender is, so a controller or the control page
  shows "Clock" rather than a host and a number. It is said again whenever
  the sender attaches or a receiver asks. Nothing in it is private:
  whoever can read the wall can read it.
- **`transition()`** changes the font and the source as one move
  (section 5).
- **The log line** starts with the time and is flushed (section 10).
- **SIGTERM as well as SIGINT**, and **`detach()` last**: a controller
  stops a sender with SIGTERM, and `detach()` tells the wall it is free at
  once rather than leaving it to find out by a ping nobody answers.

---

## 3. Three shapes, and how to choose

| | You write | It is wrapped by | Frames driven by |
|---|---|---|---|
| **Pure Python** | `render(cols, rows)` returning a grid | `FunctionSource` | a tick, every `min_frame` |
| **A terminal program** | a program that draws with ANSI | `CommandSource` | the program's own output |
| **A TUI you did not write** | the command that starts it | the same | the program's own output |
| **A tmux session** | a session, or a tmux config that builds one | `TmuxSource` | the session's own output |

**Choose pure Python unless you have a reason not to.** It is the fastest
path, with no pty, no second process and no terminal emulator, and the
only one where you control every cell exactly. A dashboard should start
here.

**Choose a terminal program when you want one that already exists**:
`btop`, `lazygit`, a log tailer, something with its own layout and key
handling. `CommandSource("btop")` runs it in a pty of its own, sized to the
wall. The price is a pty and a terminal emulation, paid for the rows that
change: btop at 240x67 through tmux costs the sender about 2% of a core,
and cmatrix, which changes every row every frame, about 21%. Both terminal
sources need the emulator built once, `bitwall/vt/build`.

**Choose tmux when the scene is a session**: one someone else is attached
to as well, several panes laid out by a tmux config, or a program that must
outlive the sender.

**Never do both.** If you were going to write the program anyway, do not
write it as a terminal program and host it: you would be rendering
your grid to ANSI so that an emulator can parse it back into a grid.
Compute the cells directly.

**A picture already in numpy arrays should be handed over as arrays.**
`FunctionSource(arrays=render)`, with `render(cols, rows)` returning
`(ch, fg, bg)` as (rows, cols) arrays (code points, and `0xRRGGBB` or -1
for the terminal's own colour), is diffed and serialised in bulk, to the
same bytes the cells would have made. At 384x98 that is the difference
between a film at 16fps and at 20 with most of a core to spare. See
`bitwall/arrays.py`; the film and LED senders use it.

**A sender may be written in Rust** where its picture costs more than
Python can make in time: `bitwall/rust` is this API with the same bytes on
the wire (its README.md), and `senders/games/cells/rust` uses it.
Everything here holds for it too.

### Who owns the clock, and where slow work goes

**Pure Python: bitwall owns the clock.** `render(cols, rows)` is called at
the frame rate, twenty times a second at `min_frame=1/20`, whether
anything changed or not. It is a hot path. Anything slow has no business
being in it: reading files, a network call, a database, and especially a
cloud-backed path like OneDrive or Google Drive, which can be cold and
take seconds to answer where a local file takes microseconds.

Put the work on its own thread and its own timer, publish a plain
snapshot, and let `render()` do nothing but lay out the last one it was
given:

```python
_latest = {"at": 0.0, "data": None}


def collect(w, stop):
    """The slow part, on its own clock."""
    while not stop.is_set():
        if w.watching() != 0:               # nobody there: gather nothing
            try:
                _latest["data"] = gather()  # files, network, whatever
                _latest["at"] = time.time()
            except Exception as e:
                log(f"gather failed: {e}")  # keep the last good answer
        stop.wait(20)


def render(cols, rows):
    data = _latest["data"]
    if data is None:
        return waiting(cols, rows)          # nothing gathered yet
    age = time.time() - _latest["at"]
    ...                                     # lay out `data`, flag if stale
```

How often to collect is a question about the data, not about the wall.
Activity counts every twenty or thirty seconds is plenty; a clock is the
only thing that needs to be recomputed every frame.

When the source is slow or missing, **show the last good numbers with a
staleness marker**. A wall that freezes looks identical to a wall that is
working, which is the one failure a viewer cannot see.

**A terminal program: you own your own clock.** bitwall waits on
the pty and only ever sees what you chose to write. If a refresh takes
three seconds the wall keeps showing the previous picture until you are
done; nothing stalls, because nothing is waiting on you.

### Two front ends

A program can have two front ends. A core computes a grid, a `pane-*`
script draws that grid to a terminal for use at a desk, and the wall
sender calls the same function directly. If your dashboard should also run
in a terminal, split it that way: a core returning a grid, and two thin
front ends. Never let the wall path go through the terminal path. Games
are built this way (section 11).

---

## 4. What you are told, and when

| Call | Gives you | When |
|---|---|---|
| `w.area()` | current `(cols, rows)` | whenever you compose |
| `w.set_font_size(n)` | blocks until the wall is at that size | startup, or a scene change |
| `w.grid(cols, rows)` | asks for a number of cells; returns the area there is | a fixed board |
| `w.font_size` | the font the wall says its area is drawn at | rarely |
| `w.meta(name=, description=)` | says what this is, for a controller or a page | once, at startup |
| `w.screen()` | `colour`, `rate`, `glyphs`, `cell` (px) as the wall said them | if your look depends on them |
| `w.can("▀")` | whether the wall draws those characters | before drawing anything but text |
| `w.offers("kitty")` | whether the wall's `caps` say a word | before kitty graphics |
| `w.box((x, y, w, h))` | a rectangle of cells as glass pixels | placing a picture over cells |
| `w.want(touch=True, keyboard=True)` | asks for input | startup, if you need presses |
| `w.capabilities()` | what input is actually on | after `want()` |
| `w.input()` | the next press, once, or `None` | polled in your loop |
| `w.watching()` | viewers, or `None` if not counted | before gathering data |
| `w.sessions()` | every session on the wall | before taking a wall |
| `w.passthrough(bytes)` | carries bytes to the wall's terminal untouched | kitty graphics |
| `w.detach()` | leaves cleanly | last, on exit |

Several things happen without you asking:

**Nothing is sent when nothing changes.** Only cells differing from the
last frame go out. A static screen sends no diffs at all, indefinitely.

**A full repaint goes out when a receiver asks for one**, which it does
when a tab joins late or the area changes, plus a backstop every 30
seconds against drift. On terminal-shaped content a full frame costs about
4 bytes a cell.

**A receiver may ask whether you are still there.** After a few seconds of
quiet it sends `ping` and the library answers `pong` for you. This is how
a static wall stays distinguishable from a dead sender, and it is why you
never need to send anything to prove you are alive.

**A wall that goes away and comes back is attached to again**, restarted
or made again: the library says who you are and what you asked for once
more and repaints in full. A sender displaced by another is told so, stays
off, and says so.

**The screen's colour and frame rate are applied for you.** A receiver
says what it can show (`mono`, `grey`, `16`, `256` or `true`, and the
frames a second it wants), every colour is reduced on the way out, and the
rate becomes a ceiling on your drawing. A sender written for truecolour
therefore works on a two-ink screen without knowing. You can still do
better than be reduced: a gradient quantised to two colours is banding,
where a design chosen for two colours is a picture. Read `w.screen()` if
your look depends on colour.

**A dark wall stops you composing.** The wall follows the presence of the
room it is in, and a wall nobody is in front of says `watching 0`. The
render loop then skips composing entirely and repaints in full when the
wall lights. A sender that gathers data for its picture should stop
gathering while `w.watching()` is 0 too, as above.

**Which glyphs exist is the one thing you have to act on yourself.** Ask
`w.can("▀")` or `w.can("⠿")` before building a picture out of them,
because nothing can substitute an ASCII alphabet for a braille one on your
behalf: only you know what the picture was. A wall that has not said is a
yes. `w.glyphs` is everything it says it draws.

**A sender does nothing about who is in the room.** Never blank or unblank
a wall from a sender to follow the room: that is presence, the wall does
it, and a sender doing it fights the wall. What tells the wall is a
controller of its own (`controllers/presence-example` is a skeleton).

### Input

A press arrives as one of:

```python
{"kind": "tap",    "cell": [x, y], "area": [cols, rows]}
{"kind": "key",    "key": "a", "mods": ["ctrl"]}
{"kind": "button", "n": 1}
```

A key is **one printable character**, or a name from a fixed set:
`Return BackSpace Tab Escape space Delete Insert Up Down Left Right Home
End PageUp PageDown F1`-`F12`. Modifiers are `ctrl alt shift meta`.
Anything else never reaches you: a receiver is not trusted, and a key
name becomes an action in your program.

`input()` returns each press once, in the order they came, and only of
the kinds you asked for with `want()`. The receiver decides whether it is
willing; `capabilities()` tells you what is really on. Whether an input is
enabled is the receiver's page or hardware to decide, not the sender's.

The `area` on a tap is the grid it was taken against. Compare it to yours
before acting: a press that crossed a resize refers to a screen that no
longer exists.

---

## 5. Font size and grid: who decides what

**You ask for a font size. The receiver decides the grid.** Only the
receiver knows how many pixels it has, and it may decline: a plain shell
cannot change its font and will report the area it already had.

```python
w = bitwall.attach("pi.local", min_frame=1/20)
w.set_font_size(16)          # a request
cols, rows = w.area()        # what that actually produced
```

Never assume an area, never hard-code one. `set_font_size` blocks until
the receiver confirms it is rendering at that size, so the `area()` you
read next belongs to the font you asked for. `w.grid(cols, rows)` asks for
a number of cells the same way, and returns the area there actually is.

Measured cells (MesloLGS Nerd Font Mono, the font the painting receivers
use) and the grid they produce:

| font | cell px | 1920×1080 | 2560×1440 | 2560×1600 |
|---|---|---|---|---|
| 4 | 3×5 | 640×216 | 853×288 | 853×320 |
| 8 | 5×10 | 384×108 | 512×144 | 512×160 |
| 10 | 7×13 | 274×83 | 365×110 | 365×123 |
| 12 | 8×15 | 240×72 | 320×96 | 320×106 |
| 14 | 9×18 | 213×60 | 284×80 | 284×88 |
| 16 | 10×21 | 192×51 | 256×68 | 256×76 |
| 20 | 13×26 | 147×41 | 196×55 | 196×61 |

Choosing a font size is choosing information density:

| Font | Grid on a 2K screen | Right for |
|---|---|---|
| 18–24 | ~150×45 | read across a room; a few big numbers |
| 12–16 | ~280×80 | a dashboard you walk up to |
| 8–10 | ~450×130 | dense tables, logs, many panels |
| 4–6 | ~800×300 | pictures, not text |

**A cell is about twice as tall as it is wide.** A layout that ignores
this comes out squashed. For a square-looking region ask for roughly twice
as many columns as rows.

### Changing scene

Wrap a font change and a source change together so neither is seen alone:

```python
with w.transition():
    w.set_font_size(18)
    w.set_source(my_source)
```

Inside a transition the render loop stands still, and one full repaint
goes out when both have landed. Without it the wall paints a source built
for the old area at the new one: one visibly wrong frame.

### Limits

`MAX_COLS` 2000 · `MAX_ROWS` 1000 · `MAX_CELLS` 1,000,000 · `MAX_FONT` 400.

An area outside these is refused and the previous one kept. You will not
meet them on a real screen; they exist so a receiver cannot ask the
sender to allocate arbitrary memory.

---

## 6. Resize

The area can change at any moment: someone drags a window, presses a zoom
key, plugs in a different screen. It is not an error and there is no event
to subscribe to.

**Pure Python: there is nothing to do.** `render(cols, rows)` is called
with the current area every frame. Honour the arguments and you are
correct by construction. The trap is caching: a layout built once at
startup and reused has hard-coded an area. Recompute, or cache keyed on
`(cols, rows)`.

**A terminal program hosted in tmux.** The library resizes the tmux
window, then the pty (`TIOCSWINSZ`), then its emulator's screen; the
window first, because tmux stalls redraws on a client that cannot draw the
window. Your program inside receives **SIGWINCH** and should redraw at its
new size exactly as it would in a real terminal. A program that ignores
SIGWINCH looks wrong on a wall, and that is a bug in the program.

**A terminal program run by `CommandSource`.** The library sets the pty's
size (`TIOCSWINSZ`) and its emulator's screen, with no tmux between, and the
program receives **SIGWINCH** as above.

**A terminal program run directly at a desk.** No wall involved. It reads
`os.get_terminal_size()` and handles SIGWINCH itself. Turn autowrap off
(`ESC[?7l`) if your grid is exactly as wide as the screen, or the last
cell of a line pushes the cursor to the next one.

---

## 7. Sources

Every source ends at the same methods, which is why nothing downstream can
tell them apart:

```python
source.start(cols, rows)     # once
source.resize(cols, rows)    # whenever the area changes
source.cells(cols, rows)     # -> a grid, every frame
source.fileno()              # an fd to wait on, or None
source.pump()                # read what arrived, if there is an fd
source.close()               # when it is replaced or the sender leaves
```

| Wrapper | `fileno()` | Idle cost | `resize()` does |
|---|---|---|---|
| `FunctionSource` | `None` | polls every `min_frame` | nothing; `render` is told the area |
| `CommandSource(argv)` | the pty | nothing; blocks in `select` | TIOCSWINSZ |
| `TmuxSource(session, sock)` | the pty | nothing; blocks in `select` | resize-window, then TIOCSWINSZ |

`exited()` says whether a terminal source's program has ended, by itself
or otherwise; `close()` ends it if it has not. One that ends leaves its
last screen on the wall and is reaped, and from then its `fileno()` is
`None`, so the wall polls it as it would a `FunctionSource`.

A plain `render(cols, rows)` function is accepted anywhere a source is,
and is wrapped in `FunctionSource` for you. `set_source` swaps what feeds
the wall without the wall noticing: no reconnect and no gap.

---

## 8. Layers: pictures, bands and films over the cells

The base is one sender's grid. Over it a wall holds up to seven more
**layers**, blended bottom to top, each a session of its own, so a layer
draws over whatever the base shows without taking the base from anyone.
A layer asks for a place, not a number: `place="above"` goes on top of
those placed from below, `place="top"` under those placed from the top,
and a number at that slot. `layer.z` is the one the wall gave; the stack
has no gaps, so a layer may move, and it is never taken from its holder.
A full wall refuses.

### A video layer: pictures in pixels

```python
layer = bitwall.layer("pi.local", place="above")
layer.frame(bitwall.video.qoi(rgba), at=(40, 900), hold=1000)
layer.clear()
layer.close()
```

A frame is a whole QOI, PNG or JPEG image, its top left at `at` in the
glass's pixels, scaled to `size=(w, h)` where given, shown for `hold` ms
or, with `hold="still"`, until the next frame. `layer.pixels()` is the
glass's size, and `w.box(cells)` turns a rectangle of the base's cells
into the pixels it covers.

- **QOI every video wall draws.** `bitwall.video.qoi(rgba)` encodes a
  (height, width, 4) uint8 numpy array, alpha exact. Its work goes with
  how much is drawn rather than with the size of the frame: a couple of
  milliseconds for a 1920x1080 frame with a little drawn on it, so a whole
  glass that is mostly transparent costs little.
- **A photograph goes as the JPEG it is** where `layer.takes("jpeg")`
  says the wall draws one (`images=` in its `caps`).
- **You pace, and a full pipe drops a frame rather than queue it.**
  `frame()` returns False when the frame was dropped, so a wall that falls
  behind costs frames rather than a growing delay.
- **Hold each frame a little longer than you mean to send the next**, so
  a sender that stalls leaves nothing frozen on the wall.
- **Draw at a fraction of the glass and let the wall scale it** where the
  picture is soft: `senders/fireflies` draws at most 540 lines and sends
  only the box round what is lit, which is the example to start from.

### A cells layer: the grid again, at a place of its own

Where the wall's `caps` say `cells` in `above=`,
`bitwall.CellsLayer(target, place="above").attach()` (or
`bitwall.layer(target, type="cells")`) is the wall's grid again among the
layers, written to with `set_source` as a Wall is. Every cell left in the
default colours is transparent, and a colour may carry an alpha,
`'rrggbbaa'`, so a band can be translucent over a film. Its grid is the
base's until it asks `set_font_size` or `grid` for one of its own, which
it keeps through a change of glass; `follow_base()` goes back.
`senders/ticker` is the example.

### A film, decoded on the wall

Any video layer may carry a stream on a fifo of its own, `data`: HEVC or
H.264 Annex B, or QOI or JPEG back to back.

```python
film = bitwall.layer("pi.local", place=0, take=True, data=True,
                     codec="hevc", fps=24, start=int(time.time() * 1000) + 2000)
film.data_write(chunk)       # blocks while the wall is full
film.data_end()              # the stream is over
```

The wall decodes it (its `caps` say `codecs=`) and plays frame i at
start + i/fps itself, so you never pace a film, and walls whose clocks
agree play one together. `place=0, take=True` makes the base itself a
video base. A layer that loses its wall is opened again on its own, and
its stream starts again from its first decoded frame; a sender that keeps
its own clock is told by `when_lost(fn)` and says when with
`start_at(ms)`.
`senders/film` does all of this, on one wall or cut across several.

A film fills the glass unless it is given a box, in the glass's pixels:

```python
inset = bitwall.layer("pi.local", data=True, codec="hevc", fps=25,
                      box=(1200, 80, 640, 360))
inset.box(40, 600, 480, 270)   # from the next frame, mid-film or between films
inset.box()                    # the whole glass again
```

Its frames keep their shape within the box, `align` placing them, and
the layer is transparent outside it, so the film plays as an inset over
whatever is beneath. The box is said again whenever the layer opens, and
kept through a change of glass; what falls off the glass is not drawn,
so on `repaint_asked()` work out a new one from `pixels()`. Where
`inset.offers("box")` is False, the wall fills the glass: send the
film's frames as JPEGs on `in` instead, each with `at` and `size`.
`offers` waits a moment for the wall's `caps`, which a layer hears just
after it opens.

ffmpeg reads its input seconds ahead of what it has decoded, so the writer
finishes well before the film does: wait for start + length before calling
it over.

### Kitty graphics on the grid

Where `w.offers("kitty")`, the wall's grid draws kitty graphics (the
painter and the web receiver themselves, and the shell receiver in a
terminal that does). Hand the escape strings to `w.passthrough(bytes)`,
which carries them to the wall untouched, between frames, and never drops
them. Place a picture in cells, leave the cells under it blank, send with
`q=2`, and delete it before leaving: the terminal keeps it after the
sender goes. PROTOCOL.md, "A grid may carry kitty graphics", has the
details.

So a picture goes, best first, as a JPEG or PNG on a layer, then as kitty
graphics on the base, then in cells. `senders/picture` does all three.

---

## 9. Drawing at this resolution

A cell is about twice as tall as it is wide. The options, in order of
colour fidelity against detail:

| technique | pixels per cell | colours per cell |
|---|---|---|
| half block `▀` | 1 × 2 | 2, both full |
| quadrants `▘▚▐` | 2 × 2 | 2 |
| sextants | 2 × 3 | 2 |
| octants | 2 × 4 | 2 |
| braille | 2 × 4 | 1 |
| ascii ramp | 1 × 1 | 1 |
| symbol matching | 2 × 4 | 2, best fit searched |

**Half blocks are the default choice**: `▀` with the foreground as the
top pixel and the background as the bottom gives two square-ish
full-colour pixels per cell.

### What the receivers draw as geometry

The painter and the web receiver draw these code points as shapes of their
own rather than asking the font, so they reach the cell's edges, meet their
neighbours with no seam at any size, and look the same on every wall. The
term and shell receivers write cells to a terminal, whose font draws every
one of them as that terminal draws them.

| characters | drawn as |
|---|---|
| `█ ▀ ▄ ▌ ▐`, the eighths `▁`-`▇`, `▉`-`▏`, `▔` and `▕` | rectangles |
| quadrants `▖ ▗ ▘ ▝ ▙ ▚ ▛ ▜ ▞ ▟` | rectangles |
| sextants U+1FB00-1FB3B, octants U+1CD00-1CDE5 | whole parts of a cell cut 2x3 or 2x4 |
| the eighth lines and fractions U+1FB70-1FB8B, the middle and centred quarters U+1FBE4-1FBE7, the quarter bars U+1CEA0-1CEAF | rectangles |
| box lines, light, heavy and double: `─ │ ┌ ┼ ━ ║ ╬` and the rest | lines from the middle of the cell to its edges |
| rounded corners `╭ ╮ ╯ ╰` | a quarter ellipse from the middle of one edge to the middle of the next, so it runs into `─` and `│` |
| diagonal halves `◢ ◣ ◤ ◥` | half the cell, corner to corner, so a row of them is one slope |
| shades `░ ▒ ▓` | the whole cell in a mix of fg and bg |
| braille `⠁`-`⣿` | dots on an even grid; with `faint` the unlit dots show too, in bg |
| `▪` | an LED emitter, inset so a grid of them reads as separate lamps |

Everything else comes from the font, MesloLGS Nerd Font on both. Dashed
and diagonal box lines (`╌ ╱ ╲`) are the font's and stop short of the
cell's edge. The smooth mosaic slopes (U+1FB3C-1FB6F) and the rest of
Symbols for Legacy Computing are not in the font and not drawn: do not
use them. So a table or a maze drawn with box characters has lines that
actually join; use them rather than ASCII art.

**Every pattern of a cell cut two across and four down is one
character**, so a two-colour picture can have eight pixels a cell, solid
and touching. Most are octants, in order of the pattern with the other 26
skipped; those 26 are the blank and full cells, the halves, quadrants,
`▂ ▆`, U+1FB82, U+1FB85, the corner quarter bars and U+1FBE6/7. The
sextants are the same for 2x3. A cell's pattern to its character is a
table a sender builds once; ask `w.can("\U0001CD00")` first, since a wall
that says it cannot shows nothing for them.

**Ask before drawing anything but plain text.** `w.can("◢◣◤◥")` says
whether the wall draws those characters. Decide what to draw instead when
the answer is no; `letters` does it for its fonts.

**Nerd Font icons are characters too.** The painter, the web receiver
and the e-ink receiver claim the icon sets their Meslo has, at the code
points Nerd Fonts gives them: Font Awesome U+F000-F2E0, Material Design
U+F0001-F1AF0, Codicons U+EA60-EC1E, Octicons U+F400-F533, Weather
U+E300-E3E3 and Powerline. An icon is one cell, in the cell's ink, so
`w.can("\uf05a")` and then the icon, or a letter or a symbol where the
answer is no (a terminal's font is not known, so the term receiver says
nothing of them).

### Techniques

**A panel with its corners cut.** Fill the panel's cells with its colour
as ground, then put a diagonal half in each corner cell, the panel's
colour as ink over whatever is behind as ground. Text in the panel takes
the panel's colour as its bg, or it punches a hole.

| corner | character |
|---|---|
| top left | `◢` |
| top right | `◣` |
| bottom left | `◥` |
| bottom right | `◤` |

```python
if w.can("◢◣◤◥"):
    for x, y, ch in ((0, 0, "◢"), (cols - 1, 0, "◣"),
                     (0, rows - 1, "◥"), (cols - 1, rows - 1, "◤")):
        grid[top + y][left + x] = bitwall.cell(ch, panel, page)
```

The cut is always a whole cell, and a cell is twice as tall as wide, so
the slope is steep, about 63 degrees. **On a band two rows deep the top
and bottom cuts meet, and each end comes to a point**: a header with
arrow ends, for free. sysinfo's header band is this (`View.corners` in
`senders/sysinfo/main.py`). A band one row deep has one cell at each end
and can lose only its top corners.

**A box with rounded corners.** `╭─╮`, `│ │`, `╰─╯`. The arcs are exactly
one cell, so the rounding is small. Curves bigger than a cell are in
SHAPES.md.

**A glow is drawn by the sender.** Light round a lit shape is the cells
next to it given a dim version of its colour as their ground, fading over
a cell or two. A receiver effect that paints across cells, such as a
bloom, breaks the rule that each cell draws only itself; do not ask for
one.

**Approximate a shape with what is there.** To draw a curve the wall has
no character for, sample each cell of it (8x16 is plenty) and take the
drawn character with the fewest pixels wrong, from `█ ▀ ▄ ▌ ▐`, the
quadrants, the sextants and octants and `◢◣◤◥`. For a curved outline,
give each cell the curve crosses the box piece that joins the sides it
crosses: `─ │ ╭ ╮ ╰ ╯`. It reads as round, only coarser.

**Fall back in steps, not all at once.** New shapes where the wall has
them, then the approximation above, then plain `█` and `┌─┐`. Every level
is still cells, so each looks deliberate.

### Big text is `bitwall.letters`

Use it rather than drawing your own. Eight fonts, each a look of its own:

- `BLOCK` and `HALF`: a 3x5 bitmap, bold, any wall; `HALF` twice as fine
  top to bottom where half blocks draw.
- `DOTS`: a 5x7 dot-matrix in braille, upper and lower case,
  proportional. Small, sharp, readable text among ordinary text: a host
  name, a status line.
- `SEGMENT` (a 14-segment display, letters and digits) and `SEGMENT7` (a
  7-segment display, the clock font), and `SEGMENT_CUT` and
  `SEGMENT7_CUT`, the same with 45-degree diagonals.
- `TILE`: the same 3x5 bitmap as a panel of lit tiles with hairline gaps
  and rounded turns, drawn as one kitty picture over the cells it takes,
  so it is exact at any cell size and fills a height from two rows up. It
  needs a wall that offers kitty, and Pillow (`uv run --with pillow`, or
  the sender's own script dependencies); `draw` falls back to BLOCK with
  its corners cut, and `fit` to the next font.

`WHITE_RABBIT`, `BYTES256` and `VT323` are outline fonts drawn the same
way, for a heading in a look cells cannot give (licences in
`bitwall/fonts/`). These four are pictures, several kilobytes each and
sent again whenever the text changes: use them for what stands still,
never for a ticking clock, a counter or animation, which stay in the cell
fonts.

`rows`, `size`, `draw`, `fit` and `centred` take `scale` or `height`,
`spacing` (`"proportional"` or `"mono"`; digits are one width either way,
so a number that changes does not shuffle), `turn` (`"up"`, `"down"`),
`edges` (`"square"`, `"cut"`), `smooth`, and `ghost` (the unlit segments,
or dim dots behind `DOTS`).

**Pass the wall, `wall=w`, and let the library judge the screen.** It
reads what the wall can draw, its colour depth and its pixels per cell,
and chooses by them: smoothing only where strokes are wide enough in real
pixels for the shading to help, `DOTS` only where the dots are distinct,
a `SEGMENT` too small to read only as a last resort, the block fonts
widened by the cell's true shape. So `fit(text, cols, rows, fonts=[...],
wall=w)` with the fonts in order of preference, then `draw(grid, **got)`,
is usually all a sender needs. Given the wall, `size`, `rows` and `draw`
draw a font it cannot as the nearest it can (DOTS as HALF, then BLOCK; a
cut segment font square), so a sender that draws directly need not ask
`usable` first.

As a start rather than a rule: crisp for anything big and bold;
`smooth=True` on segments at wall font 10 or below; `DOTS` at wall fonts
of roughly 6 to 14 (6 is the one size whose dots fall on an exact grid);
`SEGMENT7` for time and counters. Run `senders/letters/main.py` on the
wall in question and look before deciding: it shows every font, size and
option, labelled.

### Bandwidth

**A framebuffer defeats the diff.** Content changing a few cells costs
almost nothing. Anything repainting every cell costs real bandwidth, so
cap the frame rate and measure. Rough shape, per full frame:

| grid | cells | terminal-ish | every cell coloured |
|---|---|---|---|
| 120×34 | 4,080 | 17 KB | 153 KB |
| 302×87 | 26,274 | 108 KB | 987 KB |

The raycaster at 194x106 pixels produced 320-470 KiB/s raw, 14-30 KiB/s
over a compressed ssh, at 12fps.

---

## 10. Running under a controller

A controller (`controllers/bitwall-senders`, CONTROL.md) starts, stops and
moves senders on a host. To be one it can offer, a sender needs a
catalogue entry and a few manners.

### The catalogue entry

A `.json` beside the others, `senders/catalogue/` for bitwall's own, or
in a directory of your own linked into `~/.bitwall/senders/`:

```json
{
  "label": "Snake",
  "description": "A snake that plays itself, eating its way round the board",
  "start": "../games/snake/main.py",
  "moves": true,
  "options": {
    "font": {"type": "font", "label": "Font size",
             "description": "Cell size on the wall", "default": 18, "min": 6, "max": 40}
  }
}
```

Every option is passed as `--<name> [value]`, with the wall last as
`host:wall`. Option types are `bool`, `choice`, `font` and `walls`
(CONTROL.md, *Options*). A layer says `"type": "video", "place": "above"`
(or `"type": "cells"` for a cells layer) and has no font option if it is
video; the sender is told its place in `BITWALL_PLACE`, which
`bitwall.layer()` reads.

A sender that can run only where something is -- a capture dongle, a
camera, a program -- says so with `"available": "./available"`, a script
found as `start` is, run each time the catalogue is walked. Exit 0 and
it is offered; exit anything else and it is not, the first line it
prints being the reason the controller shows; it has two seconds, so it
looks rather than tries. `senders/hdmi/available` is one:

```sh
#!/bin/sh
for dev in /dev/v4l/by-id/usb-*-video-index0; do
    [ -e "$dev" ] && exit 0
done
echo "no USB capture device"
exit 1
```

A shell script is never run on Windows, so there the sender is
unavailable; write it in Python, the standard library alone, if the
sender runs there too (CONTROL.md,
*Whether it can run here*).

### What a sender prints is its log

A sender a controller starts has its stdout and stderr in
`~/.bitwall/running/<id>.log` on the host it runs on, and the control page
shows the end of it. Start each line with the time
(`time.strftime("%H:%M:%S")`, or a date as well for a sender that runs for
days): a log with no times cannot say whether a line is from a minute ago
or last week. Print what someone would want when it has gone wrong (what
it attached to, what it chose, what failed and why) and flush, since a
pipe is buffered and a crash loses the rest. A restart starts a fresh
log.

Print events, not a running commentary. A wall runs for weeks: a line a
frame is a million lines a day and a full disk. Say when something
changes (attached, resized, next film, lost the feed) and never on every
frame, tick or poll.

### Stop when told, and say so

A controller stops a sender with SIGTERM to its whole process group, and
SIGKILL five seconds later. Catch SIGTERM as well as SIGINT, stop drawing,
clean up, and call `w.detach()` last (section 2). On Windows, where there
is no SIGTERM to send, the library raises the signal the sender stops on
for it.

### Moving

`bitwall-senders move` moves a running sender to another wall, the same
process with all it holds, when its entry says `"moves": true`. The
controller writes the new wall into the sender's record and sends it
SIGUSR1, and the library retargets every Wall and Layer of the process.
A sender built on `bitwall.Wall` or `bitwall.layer()` usually needs
nothing. One that kept something from its first wall (the glass size, a
choice made from `capabilities()` at start) or spans several walls does
not move. Say `moves` only once the sender has been moved and looked at,
since SIGUSR1 ends a process that does not catch it.

### Before you put a sender on a wall

**Ask the wall's `sessions`** (`w.sessions()`, or
`controllers/bitwall-ctl pi.local sessions`). `attach()` takes the base
from whoever holds it, so a sender started on a wall someone is using
takes it from them. A controller's `start` asks for you, and refuses a
held base unless told `--replace`.

### Looking for something to show

**A dashboard sender may already exist as a TRMNL plugin; look there
first.** TRMNL (trmnl.com) is a catalogue of mostly static information
screens (weather, calendars, transit, moon, parcels, air quality) for an
800x480 e-ink panel, each drawn at full, half and quadrant size. Where a
sender shows the same thing, its plugin says which API to call, which
fields are worth showing, how it authenticates and how often it is worth
refreshing: `usetrmnl/plugins` on GitHub holds about 30 of their own
(Ruby for the data, ERB for the views) and its README lists about 55
community ones, and trmnl.com/integrations shows how many people use
each, which says what is worth building. It is inspiration for layout,
data and API, and nothing more: their plugins carry no licence, the
community ones each carry their own, and a sender draws in cells for
itself.

---

## 11. Writing a game

Follow `senders/games/snake` **structurally**, and nothing else about it:

- `game.py`: the pure core. `frame(cols, rows)` in, a grid of
  `Glyph(ch, fg, bg, bold)` out. Knows nothing of bitwall or terminals.
- `main.py`: the bitwall front end. Glyph to cell, `set_source`.
- `pane-<name>`: the terminal front end, sized from
  `os.get_terminal_size()`, writing diffed ANSI to stdout.

`frame(cols, rows)` is deliberately the same shape as bitwall's source
contract, so the adapter is trivial and the core stays testable.

**Do not inherit another game's visuals.** Copying snake's block score
header and title wordmark into another game is spotted immediately.
Derive presentation from the thing you are imitating.

These are **self-playing wall displays**. Nobody plays them and nobody
reads the score, so fidelity means how it looks and moves, not scoring
tables or arcade-accurate timings. Approximate freely wherever it is not
visible. Spend the effort on motion and colour.

---

## 12. Things that cost time if nobody says them

- **`render()` is called every tick**, static screen or not. Expensive work
  belongs behind a cache or on its own thread, not in the hot path.
- **An exception inside `render()` paints its message on the top row**
  rather than killing the wall. Look there when a panel goes blank.
- **Do not assume your font request was honoured.** Read the area back.
- **`BITWALL_TRACE=1`** writes the library's side of the conversation to
  stderr, with times: what was asked, what the wall said, every hold and
  repaint.
- **`pkill -f` matches your own command line.** Use a bracket:
  `pkill -f '[m]y-sender'`.
- **`pgrep -f` cannot see environment variables.** A sender pointed at a
  wall by `BITWALL_RUN` looks identical to any other in `ps`. Ask the
  wall's `sessions` rather than the process list whether something is
  already on it.
- **Producer and consumer rates have to agree.** Pacing something at a
  fixed rate into a wall that cannot keep up does not drop frames: a queue
  fills, the producer blocks, and it plays in slow motion. Drop what you
  cannot deliver rather than queueing it. Slow motion is much harder to
  spot than a dropped frame.
- **ffmpeg decodes as fast as the pipe drains.** `-r` sets the output
  frame rate, not the speed. Without `-re` the film fast-forwards.
- **`ffmpeg -vf eq` after `pad` breaks odd column counts**, silently, if
  stderr is discarded. Order the chain `unsharp, eq, scale, pad,
  format=rgb24` and always capture ffmpeg's stderr.
- **Some films open with minutes of black.** Lawrence of Arabia has a 4.5
  minute overture at brightness exactly 0. A static black grid looks
  identical to a broken sender. Seek in before concluding anything is
  wrong.
- **A raw pipe read returns what has arrived, not what you asked for.**
  With `bufsize=0`, `read(n)` may come back short. Treating that as EOF
  exits the reader and freezes the picture for good. Loop until the frame
  is full.
