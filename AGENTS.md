# Working on bitwall

What bitwall is, how it is put together, and the traps that have cost real
time, for anyone changing bitwall itself, person or agent. Read this before
writing anything.

**Writing a sender is `docs/writing-a-sender.md`**: the API, drawing at
this resolution, big text, layers, the catalogue, logging and stopping,
games. This file does not repeat it. PROTOCOL.md is the contract between
senders and walls, CONTROL.md the controllers, README.md setting a wall up.

## The system

A **sender** computes everything and streams it to a **receiver**, which
paints it and knows nothing else. All credentials and all data stay on the
sender; a receiver is a screen on a dumb machine.

The **receiver contract is three things**:

1. accept a font size request
2. publish the area it can offer, in cells
3. consume a stream and paint it

That is the whole interface. `bitwall/receivers/pi` is the painter: a
Raspberry Pi's screens straight through KMS, and on a Mac or Windows a
window, or no screen at all, its glass a size a controller gives it
(`--glass`, `glass WxH`) and streamed raw to whoever asks (README.md,
*The painter*); `bitwall/receivers/web` puts a wall in
a browser tab, and `bitwall/receivers/mac` in a window of its own;
`bitwall/receivers/term` is a wall on the terminal it runs in, one Rust
program for Windows, a Mac or Linux, that reads the stream into cells and
stacks cells layers over them, with no pixels of its own and so no video
layer and no glass to picture (its README.md); `bitwall/receivers/shell`
is the least a receiver can be, `bitwall-shell` on a POSIX terminal and
`bitwall-ps.ps1` on a Windows console, copying the base's stream to the
terminal unread. Anything meeting those three points is a receiver.

**The painter is the reference.** Where the protocol leaves a choice --
when `area`, `repaint` and `screen` are said, how capture is framed, the
measure fields, reaping, backpressure, how the overlay looks -- the web
receiver does what `bitwall-paint.c` does, and `tests/bitwall-contracts`
holds both, and the fake wall, the shell receiver and the term receiver,
to the same clauses.

**A web wall is a named wall with a tab for its glass.**
`bitwall/receivers/web/bitwall-web kitchen` keeps `<run>/bitwall/kitchen/`
with its door and sessions (the receiver core, `bitwall/receivers/core.py`) and hands each layer to every tab
open on `/r/kitchen`, which stacks and paints them (`bitwall-screen.js`):
the base's cells on one canvas, each video layer's QOI on its own. One tab
decides the wall -- the page marked `primary`, else the one most lately
brought into view -- and its box in device pixels is the wall's glass: it
says the box and a cell's size at every font once a resize has settled,
and the wall works out its area as the painter does from its font; it
answers `picture glass` and `mirror glass` with a look at its canvases.
Walls after startup are made with `make <name> [presence=<name>]` on
`<run>/bitwall-web.in`. `bitwall-web` listens on 127.0.0.1 unless given
`--host`, and its websockets take only pages of its own origin. It runs
under `uv run`, which does not pass SIGKILL on: to kill it outright, kill
its process group.

The wire carries **cells**: `(text, fg, bg, bold, italics, underscore,
strikethrough, reverse)`, and `faint` ninth where it is set. Colours are
xterm-256 indices, the string `'rrggbb'` for truecolor, `'rrggbbaa'` on a
cells layer, or `"default"`. `bitwall/cells.py` says what a cell is.

### Transport

A wall is a directory, `bitwall/<wall>/` in its host's run directory, with
a **door**: a fifo, `door.in`, that only takes requests. A caller asks at
the door for a **session**, `open <name> layer` or `open <name>
controller`, and the wall makes `s/<name>/` with two fifos of its own:
`in`, which the caller writes, and `out`, which it reads. So the transport
needed no code: `ssh host 'cat > .../s/<name>/in'`. ssh moves bytes, `cat`
writes them into the pipe. On Windows the fifos are named pipes and a
relay stands in for `cat` (WINDOWS.md).

Frames and commands share a session's `in`. Commands travel as APC
strings, which a terminal ignores by design, so the stream stays
paintable. Each session hears only what concerns it, on its own `out`, so
two callers never read each other's answers. See `PROTOCOL.md`.

The sender is the **base**, layer 0. `attach()` opens it with `take=1`,
which displaces whoever held it; the one displaced is told `closed
displaced`, stays out, and says so. What a sender asks before the wall
has said `hello` is held and sent once it has. A wall that goes away and
comes back -- restarted, or a web wall made again -- is attached to again
by the sender on its own, which says who it is and what it asked for once
more and repaints in full.

Over the base a wall holds up to seven **layers**, blended bottom to top,
each a session of its own: placed `above` (on top of those placed from
below), `top` (under those placed from the top) or at a number, the
layers above it moving up one. The stacks have no gaps, so a layer may
move and is told `moved`; it is never taken from its holder, and a full
wall refuses. A **video layer** is pictures in pixels over the cells
(`bitwall/video.py`): QOI, PNG or JPEG frames, where the wall's `caps`
say `images=`. A **cells layer**, where the `caps` say `cells` in
`above=` (the painter, the web receiver and the term receiver), is the
wall's grid again at a place among the layers, every default cell
transparent. Neither changes the base's area or frames.

**A film is decoded on the wall.** The base may be video too, and any
video layer may carry a stream on a fifo of its own, `data`: HEVC or
H.264 Annex B, or QOI or JPEG back to back. The wall decodes it (ffmpeg,
on the Pi's own decoder: HEVC on a Pi 5, H.264 on a Pi 3; its `caps`
says `codecs=`) and plays it out itself, frame i at start + i/fps, so the
sender never paces a film and walls whose clocks agree play one together.
`senders/film` is the one that does it, on one wall or cut across several
(`--layout 'pi-a.local:0,pi-a.local:1 / pi-b.local:0,pi-b.local:1'`).
Measured at 1080p: 24fps and nothing dropped, the painter at 15% of a core
on a Pi 5 in HEVC and 47% on a Pi 3 in H.264, whose ffmpeg takes 80% more
copying frames out of the decoder.

**A wall may be cut into panes**, each a wall of its own for any sender:
`senders/panes` on the wall's base, then `split`, `unsplit` and `share`
asked of a pane with `bitwall-ctl` (PANES.md). It is a sender to the real
wall and receiver cores to its panes, so neither the receiver nor the
senders in the panes know. Composing two sources stays a sender's
business; no receiver learns what a pane is.

**One wall's glass shows on others as video** with `senders/mirror`
(MIRROR.md), run on the source's host: the painter streams its glass raw
(`stream` in its `caps`; it runs several at once, `streams=8`, one
for each size and rate sent, each `--to wall@WxH/fps` if it says), ffmpeg
there encodes it, H.264, HEVC or MJPEG as each destination decodes in
hardware (`hw=`), and the same bytes go to every destination of an encode on its
video layer's `data`, `--lead` seconds behind the source
on their clocks, or as decoded with `--lead 0`. Its catalogue entry is
placed `controller`: it holds only a controller session on the wall it
is started on, which carries its instance. Its defaults come from walls
measured and are worked out per destination: 3 Mb/s at 720p25 scaled by
the pixels a second, and a lead of 0.25 s, or 0.5 s where a frame is held
on its way (a VideoToolbox encoder at the source, or a destination
decoding on more than two threads; the painter runs one by default), or
0.8 s above 720p on a Pi 3, the largest of them for every destination;
and a destination's base where nobody holds it, else above it. Setting
one up, its options, the rates measured and what to do when it goes
wrong are in MIRROR.md, *Using a mirror*. Its source and its
destinations are Pis, Macs or Windows: on Windows it reads the glass
through `bitwall/local.py` and encodes on Quick Sync, and a Windows
host's clock is asked through `relay clock` (MIRROR.md, *A Windows
host*).

```sh
controllers/bitwall-senders start bitwall/mirror pi-a.local:0 --to pi-b.local:0,pi-b.local:1
senders/mirror/main.py localhost:0 --to pi.local:0 --codec h264 --lead 0
```

`ssh -C` is on for a cell stream and buys about **10x**: 70 KB/s on the
wire became 6.9 KB/s, measured at a Pi's interface. When testing it, kill
the ControlMaster first — a new `-C` connection multiplexes over an
uncompressed master and compresses nothing, which looks exactly like
compression not working.

### Input

A receiver says what a viewer may give it, and what it was given, and
bitwall carries both without interpreting either.

    accepts touch keyboard     what this receiver will pass on, when asked
    input tap cell=3,4 ...     something a viewer did, said as it happens

By convention, not by rule: `touch` reports `tap` with a cell and the area
it was taken against; `keyboard` reports `key` with a character; `buttons`
reports `button` with a number. The area travels with a tap because a press
that crosses a resize would otherwise land on a grid that no longer exists.
The library rebuilds every press from checked values and hands a sender
only the kinds it asked for (`bitwall/__init__.py`, `clean_input`).

Whether an input is *enabled* is the receiver's page or hardware to decide,
not the sender's. A browser capturing taps has to swallow scroll and pinch,
which is a choice about that page, not about the wall.

### Drawing

What the painter and the web receiver draw as geometry rather than from
the font -- blocks, quadrants, sextants, octants, box lines, rounded
corners, diagonal halves, shades, braille and `▪` -- is tabled in
`docs/writing-a-sender.md`, *What the receivers draw as geometry*. A
receiver that changes what it draws changes that table and says so in its
`screen glyphs=`; SHAPES.md is what more could be drawn.

## Where things live

The design holds together because each layer knows very little. When in
doubt about where something belongs, it belongs further out than feels
convenient.

**bitwall** is the cell format, the transport, and the contract. It carries
things it never interprets: what a font size means, what a menu offers,
what a button press wants. That ignorance is the feature -- it is why a
terminal that cannot change font is still a receiver.

**A receiver** owns a screen, or a window, or a box on a page. It publishes
the area it can offer, honours the font request or does not, paints what it
is given, and blanks when asked. It publishes its input if it has any. It
never learns what a pane is, what a game is, or where its content came
from. Not every receiver takes every command: the shell receiver takes
four controllers that may ask only `sessions`, `report` and `ping`, which
is why `caps` says what a wall takes.

**A sender** is not part of bitwall. It owns all the content and all the
credentials, composes whatever it likes into one grid -- two games side by
side is a sender's business -- decides what a font size means for its
layout, publishes what it can do, and reads what came back. The senders
in `senders/` are there to show what the system can do.

**A page**, for the browser receiver, owns layout, chrome and controls. It
sizes the box and the component reacts. It renders whatever menu the sender
published without knowing the vocabulary.

**A controller** starts, stops and assigns senders, which is process
management and nothing to do with bitwall; and it talks to walls directly,
without going through their senders. It opens a controller session at a
wall's door, sends commands on its own `in` and hears the answers and the
wall's state on its own `out` (PROTOCOL.md, "What a controller hears").
`controllers/bitwall-ctl` is the one for a terminal:

```sh
controllers/bitwall-ctl pi.local:1                     # what the wall is and can do
controllers/bitwall-ctl pi.local stats 1 show=all      # a command, and its answer
controllers/bitwall-ctl --follow pi.local follow in    # everything it says and hears
controllers/bitwall-ctl --follow pi.local:1 measures 500 # the glass, then a line a session
```

A wall cut into panes (`senders/panes`, PANES.md) is driven the same way,
each command asked of a pane, which is a wall named `<wall>-<pane>`:

```sh
controllers/bitwall-ctl localhost:web-snake layout              # every pane, a line each
#   pane snake 62x43 at=0,0 wall=web-snake
#   pane maze 62x43 at=62,0 wall=web-maze
#   panes 2
controllers/bitwall-ctl localhost:web-snake share 60            # snake's share of its cut
controllers/bitwall-ctl localhost:web-maze split down letters 30   # a new pane under maze
controllers/bitwall-senders start bitwall/letters localhost:web-letters
controllers/bitwall-ctl localhost:web-letters unsplit           # its space back to maze
```

None restarts a sender: a pane whose size changes is told its `area`. To
find which walls are panes and of what, ask a wall's `caps` for `split`,
or `layout` of any pane.

`controllers/bitwall-control` is the same on a web page: every wall on
every device beside each other, with font size, blank, the stats overlay
and live measures as controls, a picture of each wall on request or on a
timer, and each wall's sessions and traffic. Its devices come from
`--devices FILE`, `--device NAME=HOST`, `$BITWALL_DEVICES` or
`~/.bitwall/devices.json` (`controllers/devices.example.json`). It holds a
controller session on every wall and listens on 127.0.0.1 and the
machine's Tailscale address unless given `--host`, since whoever reaches
it can change every wall. It never talks to a sender: what it knows is
what the walls say.

A presence feeder is a controller too: it holds a session on each wall
and says `presence 0|1 from=<who>` to the walls following its room
(PROTOCOL.md). `controllers/presence-example` is a skeleton of one, its
check a stand-in to replace. A wall follows the presence it was started with
(`--presence 1=lounge` to the painter, `make kitchen presence=lounge` to
the web receiver); a sender never does.

`controllers/bitwall-senders` runs senders on a sender host (CONTROL.md):

```sh
controllers/bitwall-senders list                        # what can run here
controllers/bitwall-senders start bitwall/snake pi.local --font 20
controllers/bitwall-senders start bitwall/fireflies pi.local    # a layer, placed as its entry says
controllers/bitwall-senders start --replace bitwall/fluid pi.local
controllers/bitwall-senders status                      # what is running, on which wall and layer
controllers/bitwall-senders move mac-7f3a2c pi.local:1  # to another wall, still running
controllers/bitwall-senders stop mac-7f3a2c
```

`move` asks the new wall for room as `start` does (`--replace` onto a
held base), writes the wall into the sender's record and sends it
SIGUSR1, which the library catches and retargets every Wall and Layer of
the sender (`bitwall/moving.py`; CONTROL.md, "Moving a sender"). On
Windows the record itself is the signal. Only an entry that says
`"moves": true` is moved; any other is refused.

What can run is the host's catalogue, `~/.bitwall/senders/`, each sender a
`.json` with its label, its `start` script, the options a controller may
give it and where it goes (CONTROL.md, *The catalogue*). bitwall's own are
in `senders/catalogue/`, linked into a host's catalogue as `bitwall`; a
sender added there is one a controller can offer. Senders kept elsewhere
are linked in beside it, as a directory of entries of their own. A start
asks the wall's `sessions` whether the place is free: a base start refuses
a held base unless told `--replace`, and a layer start a wall with no
layer free. Every session a controller started says its `instance`, on
its line in `sessions`.

A sender can run on a Pi as well, from a copy of the library and of the
senders on it, laid out as in the repo, each Pi with a catalogue of its
own. The copy does not follow the repo, so after changing the library or
such a sender, copy it again; a sender already running keeps what it
started with until it is restarted. A Pi runs `python3` on that copy and
needs nothing fetched at run time. A sender runs on Windows too, with
Python, uv and ssh (README.md, "On Windows"), and a Windows machine is a
sender host for `bitwall-senders`, its catalogue directory junctions in
place of links, a sender started through WMI and stopped and moved
through its record (WINDOWS.md, *A sender host*).

The leaks to watch for, in the order they are likely:

- a receiver growing an idea of panes, because splitting a wall looks like
  a receiver problem. It is a sender composing two sources.
- a component drawing its own chrome, at which point it has decided
  something about appearance that belongs to the page.
- bitwall interpreting a payload it was only supposed to carry.
- a sender assuming its font request was honoured, rather than reading the
  area that came back.

## Traps that have cost time

The sender's own traps (ffmpeg's pace, films that open in black, rates
that do not agree) are in `docs/writing-a-sender.md`. These are the ones
for whoever works on the library, a receiver or a controller.

- **`pkill -f` matches your own command line.** Use a bracket,
  `pkill -f "[f]oo"`, and never put the unbracketed string anywhere else in
  the same command. This has killed the wrong process repeatedly, including
  an ssh session mid-command.
- **A raw pipe read returns what has arrived, not what you asked for.** With
  `bufsize=0`, `read(n)` may come back short. Treating that as EOF exits the
  reader and freezes the picture for good. Loop until the frame is full.
- **tmux `set -g` is server-wide, not session-wide.** Two receivers sharing
  one tmux server share pane borders, status and terminal type, and the last
  scene built wins for all of them. Give each receiver its own socket.
- **A tmux pane costs a full-screen terminal emulation per frame.** The game
  renders to ANSI, a pty carries it, pyte parses every cell back. Calling a
  game's `frame()` directly instead was 30% faster while doubling the area.
  Use tmux only when you actually need a real TUI in the scene.
- **A frame bigger than the pipe buffer arrives in pieces.** macOS gives a
  fifo 64KB, so at roughly 24 bytes a cell in truecolor anything past about
  2700 cells is split. Paint a piece as it lands and the picture wipes down
  the screen. **A bare `ESC[0m` -- no other parameter -- is the frame
  delimiter**: `serialise` emits one to close a frame and one to open a full
  repaint, and nowhere else, so a receiver can paint between frames instead.
  Keep a guard for a sender that stops mid-frame.
- **Take the bytes off the fifo as soon as they are written, but only if you
  can keep up.** What sets the frame rate is how soon after a write the pipe
  has room again, so a thread sitting in `read()` is worth several times a
  poll: on a 151x71 film, 14.6fps against 3.0fps at 60Hz. The catch is that a
  full pipe is also the only thing telling a sender to slow down, and a
  sender told to slow down drops frames rather than queueing them. Drain
  eagerly only where the receiver can consume the stream -- the web receiver
  takes the commands out, hands the rest on and the browser paints in a
  few ms. Where it cannot, draining eagerly just buys frames that are parsed
  and never shown: a terminal that reads its fifo on an event loop keeps up,
  and a receiver that polls cannot.
- **Producer and consumer rates have to agree.** A film pacing itself at
  20fps into a sender taking 12 does not drop frames -- its queue fills,
  ffmpeg blocks on a full pipe, and the film plays at sixty per cent speed.
  Slow motion is much harder to spot than a dropped frame.
- **`pgrep -f` cannot see environment variables.** A sender names its wall
  as `host:<wall>` on its command line, where `ps` shows it; one pointed
  somewhere by `BITWALL_RUN` does not, and looks identical to any other.
  Matching on the target finds nothing and you cheerfully start a second
  sender, which takes the wall from the first. Ask the wall's `sessions`.
- **A film on `data` runs ahead of the picture.** ffmpeg reads its input
  seconds ahead of what it has decoded, so the writer finishes well before
  the film does: wait for start + length before calling it over, and do
  not read a finished writer as a finished film.
- **Two Pis' clocks are not a frame's width apart by default.**
  systemd-timesyncd left two Pi 5s 17 ms apart, which is up to one frame
  between walls playing one film; chrony would do better.
- **On Windows, never look at a pipe by its path.** `os.path.exists` on
  a named pipe says False and takes the waiting instance with it, so the
  next caller to open it is refused. List `\\.\pipe\` instead
  (WINDOWS.md).
- **Windows' own ssh, started from a program in an ssh session, never
  exits when its stdout is a pipe.** It does the whole job and hangs at
  the end, so a `Link` asking where a host keeps its walls says it could
  not ask, which looks like a dead Pi. Put Git's `ssh` first on the path
  there, `C:\Program Files\Git\usr\bin` (WINDOWS.md). A wall on the same
  machine needs no ssh, so `localhost` works without it.
- **On Windows, what an ssh session starts dies with it**, `Start-Process`
  too: sshd closes the session's job. Start what must outlive it through
  WMI, and stop it with `taskkill /T /F` (WINDOWS.md, *Keeping it
  running*).
- **MSYS2's bash entered from PowerShell needs `HOME=/home/<user>`.**
  Without it bash reads Git Bash's `.bashrc` in the Windows home, puts
  Chocolatey's MinGW first, and the painter fails to build with
  `jpeglib.h: No such file`. `which gcc` must say `/ucrt64/bin/gcc`
  (README.md, *The painter on Windows*).
- **`uv run --script` fails in an ssh session on Windows** with uv's own
  Python, `UV_PYTHON` or not: give it `--python (Get-Command
  python3).Source`, or run `uv run --no-project --with <deps> python`
  (README.md, *On Windows*).
- **A clock read over a fresh ssh connection is late.** The time is read
  at the end of a round trip of half a second, and taking it as the
  middle put a Windows host 167 ms out. Time a question and its answer on
  a process already running, as `relay clock` answers once a line; and
  that host's own clock ran 0.55 s ahead of NTP's, so never take a host's
  clock to be right.
- **Assert your patches applied.** A `str.replace` that matches nothing
  fails silently. Two edits here reported success and changed nothing, and
  the wrong behaviour was blamed on other code for half an hour.

## House rules

- **Do not `git commit`** unless asked to.
- **Do not modify `bitwall/`** from a sender task. It is the shared tool.
- **Ask a wall's `sessions` before putting a sender on it.** `attach()`
  displaces whoever holds the base, so a sender started on a wall someone
  is using takes it from them.
- Docstrings say what the thing **is** and why it works that way. Never what
  it used to be, never what it does not do, never a changelog. That history
  belongs in the commit message.
- Verify by measuring. If you say it works, have run it and have the number.
