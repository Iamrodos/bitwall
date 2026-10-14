#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]
# ///
"""An LED matrix on a wall, driven by whatever already drives LED matrices.

    senders/leds/main.py --size 32x16 localhost
    senders/leds/main.py --size 64x32 --layout serpentine --font 12 pi.local
    senders/leds/main.py --size 64x32 --play fire localhost:1

Then point LED software at this machine: xLights, Glediator, Jinx!, LedFx,
a Processing sketch, anything that outputs sACN, Art-Net or DDP. Nothing
here knows what the pixels mean; it takes a frame of RGB and shows it.
`--play` runs one of the generators beside this file instead, sized to the
panel and aimed at this sender, for a wall that should show something with
no LED software anywhere.

**Nothing about bitwall is LED-shaped, and that is the point.** This is a
transmitter like any other: it reads UDP, composes cells and hands them
over. The three protocols it speaks are the ones that ecosystem already
uses, so decades of effects work arrives without a line of it being
ported, and a wall becomes a preview rig for a panel nobody has wired yet.

    sACN (E1.31)   udp/5568   universes of 170 pixels, the common tongue
    Art-Net        udp/6454   the older one, same shape
    DDP            udp/4048   WLED's, and the simplest of the three

**A pixel is drawn as an emitter, not a fill.** One LED is a lit block
with dark around it, because a grid of touching blocks is a picture where
a grid of separated ones is a panel. At one cell per LED the gap comes
from the glyph; given more room each LED grows into a block of cells and
keeps a dark cell between itself and the next.

**What is not lit is still there.** An unlit LED is drawn dim rather than
absent, so the panel has its shape before anything is sent to it, and a
source that stops looks like a panel that went dark rather than like a
wall that broke.

**The panel is an array and so is the frame.** Every cell of a wall this
fills changes whenever the picture does, which at font 8 is fifty thousand
cells thirty times a second. Each layout is an index expression over the
whole panel, handed to the wall as `bitwall.arrays` would have it, so
nothing here or in bitwall touches a cell from python.
"""
import argparse
import os
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))   # repo root

import bitwall                                                 # noqa: E402

#: Ports the three protocols are found on. Each is the registered one, and
#: each piece of software lets you change it, so all three are settable.
PORTS = {"sacn": 5568, "artnet": 6454, "ddp": 4048}

#: sACN carries 512 channels per universe, which is 170 whole RGB pixels
#: with two channels left over. Every sequencer packs them this way.
PER_UNIVERSE = 170

#: A lit emitter, and the fill to fall back to on a screen that has no
#: glyph for one. `▪` is inset on all sides by the web receiver, which
#: draws these as rectangles rather than asking a font.
EMITTER = "▪"
SOLID = "█"
#: Two LEDs in one cell: the foreground is the upper, the background
#: the lower. A half cell is as wide as it is tall, so each LED is
#: exactly square and the panel has twice the resolution -- at the
#: cost of the two in a cell touching, so the gap survives left and
#: right but not up and down. A denser panel that reads as a picture,
#: against a sparser one that reads as lamps.
HALF = "▀"

#: How long a panel goes on showing its last frame before it is called
#: dark. Long enough that a sequencer between cues is not reported as
#: gone, short enough to be the truth.
STALE = 3.0

FONT = 12
SECS = 60
FPS = 30.0

#: Generators `--play` can start: the effects by name, the whole rotation
#: of them, or the plasma. Each is a script beside this one, so it goes
#: wherever this sender is deployed.
EFFECTS = ("fire", "pacifica")
PLAY = ("none", "effects", *EFFECTS, "plasma")


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def layout_index(kind, w, h, i):
    """Which (x, y) the i-th pixel of a strip lands on.

    Matrices are wired one of two ways and the difference is invisible
    until the picture comes out mirrored on alternate rows. Serpentine --
    every other row wired backwards -- is the common one, because it is
    the shorter run of wire.
    """
    if not (0 <= i < w * h):
        return None
    y, x = divmod(i, w)
    if kind == "serpentine" and y % 2:
        x = w - 1 - x
    return x, y


class Panel:
    """The pixels, and where they came from.

    Written by the listener threads and read by the render loop, under a
    lock: a half-applied frame is a torn picture, and the whole point of
    this is to show what was sent.
    """

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.pixels = np.zeros((w * h, 3), np.uint8)
        self.lock = threading.Lock()
        #: When a packet last arrived, what spoke and how many frames it
        #: has sent. A panel showing nothing and a panel nobody is driving
        #: are different states and the wall says which.
        self.at = 0.0
        self.source = None
        self.frames = 0
        self.warned = False

    def take(self, first, data, source):
        """Put a run of RGB channel bytes in, starting at pixel `first`.

        A source sending more pixels than this panel holds is the mistake
        that looks like a bug in everything else: the picture arrives
        sheared, because every row after the first lands at the wrong
        offset. It costs one line to say so, and silence costs an hour.
        """
        n = len(data) // 3
        if first + n > len(self.pixels) and not self.warned:
            self.warned = True
            say(f"leds: {source} is sending past pixel "
                  f"{len(self.pixels)} -- this panel is {self.w}x{self.h}, "
                  f"so the picture will not line up", file=sys.stderr,
                  flush=True)
        run = np.frombuffer(data, np.uint8, n * 3).reshape(n, 3)
        lo, hi = max(0, first), min(len(self.pixels), first + n)
        with self.lock:
            if lo < hi:
                self.pixels[lo:hi] = run[lo - first:hi - first]
            self.at = time.time()
            self.source = source
            self.frames += 1

    def read(self):
        with self.lock:
            return self.pixels.copy(), self.at, self.source, self.frames


def sacn(data, first_universe):
    """One sACN packet as (first pixel, channel bytes), or None.

    The header is 126 bytes of nested ACN layers; what matters is the
    identifier, so another service on the port is not read as pixels, the
    universe, and where the channel data starts.
    """
    if len(data) < 126 or data[4:16] != b"ASC-E1.17\x00\x00\x00":
        return None
    universe = struct.unpack(">H", data[113:115])[0]
    if universe < first_universe:
        return None
    return ((universe - first_universe) * PER_UNIVERSE,
            data[126:])


def artnet(data, first_universe):
    """One Art-Net ArtDmx packet as (first pixel, channel bytes), or None.

    Art-Net counts universes from zero where sACN counts from one, and the
    two are used interchangeably for the same panel -- sACN universe 1 is
    Art-Net universe 0. So the number asked for is read in whichever
    convention the packet arrived in, rather than making an operator keep
    two settings that mean the same thing.
    """
    if len(data) < 18 or data[0:8] != b"Art-Net\x00":
        return None
    if struct.unpack("<H", data[8:10])[0] != 0x5000:       # ArtDmx
        return None
    first_universe = max(0, first_universe - 1)
    universe = struct.unpack("<H", data[14:16])[0]
    if universe < first_universe:
        return None
    length = struct.unpack(">H", data[16:18])[0]
    return ((universe - first_universe) * PER_UNIVERSE,
            data[18:18 + length])


def ddp(data, _first_universe):
    """One DDP packet as (first pixel, channel bytes), or None.

    DDP addresses bytes rather than universes, so the offset is already
    where in the strip this belongs; dividing by three gives the pixel.
    """
    if len(data) < 10 or (data[0] & 0xC0) != 0x40:          # version 1
        return None
    head = 14 if data[0] & 0x10 else 10                     # timecode
    if len(data) < head:
        return None
    offset = struct.unpack(">I", data[4:8])[0]
    length = struct.unpack(">H", data[8:10])[0]
    return (offset // 3, data[head:head + length])


READERS = {"sacn": sacn, "artnet": artnet, "ddp": ddp}


def listen(name, port, panel, first_universe, stop, sock=None):
    """Take one protocol's packets until told to stop, on its public
    port or on a socket already bound.

    Never raises: a malformed packet from anywhere on the network must
    cost one frame rather than the wall, and a port already in use is
    worth saying once rather than exiting over.
    """
    read = READERS[name]
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError as e:
            say(f"{name}: udp/{port}: {e.strerror or e}", file=sys.stderr,
                flush=True)
            return
    # sACN and Art-Net are usually multicast; joining the groups a
    # sequencer would send to means it works without being told a unicast
    # address. Failing to join is not fatal -- unicast still arrives.
    if name == "sacn":
        for universe in range(first_universe, first_universe + 16):
            group = socket.inet_aton(f"239.255.{universe >> 8}."
                                     f"{universe & 0xFF}")
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                group + socket.inet_aton("0.0.0.0"))
            except OSError:
                pass
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            got = read(data, first_universe)
        except Exception:
            continue                 # one bad packet, not a wall down
        if got is not None and len(got[1]) >= 3:
            panel.take(got[0], got[1], name)
    sock.close()


def colours(rgb, off, dark):
    """(..., 3) pixels as the packed colours a frame carries.

    A pixel that is black, or dimmed to black, is an unlit emitter and
    takes the unlit colour, so the panel keeps its shape.
    """
    if dark:
        rgb = rgb >> 2                   # a quarter, as the eye sees dark
    packed = ((rgb[..., 0].astype(np.int32) << 16)
              | (rgb[..., 1].astype(np.int32) << 8) | rgb[..., 2])
    return np.where(packed == 0, off, packed)


def blank(cols, rows):
    """An empty frame: spaces on the terminal's own colours."""
    return (np.full((rows, cols), ord(" "), np.int32),
            np.full((rows, cols), -1, np.int32),
            np.full((rows, cols), -1, np.int32))


def note(frame, cols, rows, at, source, dark, w, h):
    """Say what a wall showing nothing is showing nothing because of."""
    line = None
    if not at:
        line = f"waiting for pixels  {w}x{h}  sacn/artnet/ddp"
    elif dark:
        line = f"no {source} for {int(time.time() - at)}s"
    if line and rows > 1:
        text = [ord(c) for c in line[:cols]]
        ch, fg, bg = frame
        ch[rows - 1, :len(text)] = text
        fg[rows - 1, :len(text)] = 0x666672
        bg[rows - 1, :len(text)] = -1


def stacked(panel, off, kind):
    """Two LEDs to a cell, one above the other in a half block."""
    def render(cols, rows):
        pixels, at, source, _ = panel.read()
        w, h = panel.w, panel.h
        frame = blank(cols, rows)
        dark = at and (time.time() - at) > STALE
        ox = max(0, (cols - w) // 2)
        oy = max(0, (rows - (h + 1) // 2) // 2)
        # The panel's rows in pairs, an odd last row paired with black.
        pairs = (h + 1) // 2
        grid = np.zeros((pairs * 2, w, 3), np.uint8)
        grid[:h] = pixels.reshape(h, w, 3)
        top = colours(grid[0::2], off, dark)
        bottom = colours(grid[1::2], off, dark)
        ph, pw = min(pairs, rows - oy), min(w, cols - ox)
        if ph > 0 and pw > 0:
            ch, fg, bg = frame
            ch[oy:oy + ph, ox:ox + pw] = ord(HALF)
            fg[oy:oy + ph, ox:ox + pw] = top[:ph, :pw]
            bg[oy:oy + ph, ox:ox + pw] = bottom[:ph, :pw]
        note(frame, cols, rows, at, source, dark, w, h)
        return frame
    return render


def strip_index(kind, w, sy, sx):
    """The strip index of the LED at (sx, sy), for arrays of either.

    The inverse of the wiring rather than the wiring itself: here a place
    on the panel is known and the pixel that belongs to it is wanted,
    which is the other direction from laying a strip out.
    """
    flip = (kind == "serpentine") & (sy % 2 == 1)
    return sy * w + np.where(flip, w - 1 - sx, sx)


def fitted(panel, lit_glyph, off, kind):
    """One emitter per cell, the whole area, whatever the matrix is.

    The wall's grid is the panel here, and the matrix only supplies
    colour: each cell takes the source pixel nearest to it. So the panel
    fills the screen at every font size and follows a resize without
    anything being told, which a fixed matrix in the middle of a larger
    grid cannot do.

    A matrix smaller than the wall therefore comes out as blocks of
    emitters sharing a colour, which still reads as lamps because every
    cell keeps its own gap -- the difference from simply scaling the
    picture up, where the gaps would scale too and stop being gaps.
    """
    def render(cols, rows):
        pixels, at, source, _ = panel.read()
        w, h = panel.w, panel.h
        dark = at and (time.time() - at) > STALE
        sy = (np.arange(rows) * h // rows)[:, None]
        sx = (np.arange(cols) * w // cols)[None, :]
        frame = (np.full((rows, cols), ord(lit_glyph), np.int32),
                 colours(pixels[strip_index(kind, w, sy, sx)], off, dark),
                 np.full((rows, cols), -1, np.int32))
        note(frame, cols, rows, at, source, dark, w, h)
        return frame
    return render


def painter(panel, lit_glyph, off, kind, want_scale=0):
    """A render function over this panel.

    **The panel fills the wall**, because a panel in the middle of a
    larger grid is a picture someone has to walk up to. So an LED takes
    `2k` cells across and `k` down, for the largest `k` the area allows,
    and how large a lamp that makes depends entirely on how many LEDs are
    being sent: a 32x16 matrix on a fine grid is sixteen enormous lamps,
    and the same grid driven at 600x180 is a photograph.

    **At one cell per LED the gap comes from the glyph.** A cell is twice
    as tall as it is wide, and the emitter is inset further top and bottom
    than left and right, so what is lit inside it is square and the dark
    around it is the space between lamps. Above that the emitter is built
    from whole cells with a dark row and column between.

    `--scale k` pins it rather than filling, for a wall where the lamps
    should stay a certain size whatever the area.
    """
    def render(cols, rows):
        pixels, at, source, _ = panel.read()
        w, h = panel.w, panel.h

        # One LED is (2k x k) cells, of which the last column and, when
        # there is a row to spare, the last row are the dark gap.
        fits = max(1, min(cols // (2 * w) if w else 1,
                          rows // h if h else 1))
        k = fits if want_scale == 0 else min(want_scale, fits)
        if k == 1:
            ew = eh = 1              # one cell, one LED, gap in the glyph
        else:
            ew, eh = 2 * k, k
        lit_w = max(1, ew - 1) if ew > 1 else 1
        lit_h = eh - 1 if eh > 1 else 1
        # At one row per emitter the gap cannot be a row, so it comes from
        # the glyph, which is inset on every side.
        glyph = lit_glyph if eh == 1 else SOLID

        ox = max(0, (cols - w * ew) // 2)
        oy = max(0, (rows - h * eh) // 2)
        dark = at and (time.time() - at) > STALE

        # Which LED each cell belongs to, and whether it falls in the lit
        # part of that LED's block or in the gap around it. An unlit
        # emitter is drawn, not skipped: the panel keeps its shape before
        # anything has been sent to it.
        gx, gy = np.arange(cols) - ox, np.arange(rows) - oy
        px, dx = gx // ew, gx % ew
        py, dy = gy // eh, gy % eh
        on_x = (gx >= 0) & (px < w) & (dx < lit_w)
        on_y = (gy >= 0) & (py < h) & (dy < lit_h)
        lit = on_y[:, None] & on_x[None, :]
        spot = strip_index(kind, w, np.clip(py, 0, h - 1)[:, None],
                           np.clip(px, 0, w - 1)[None, :])
        colour = colours(pixels[spot], off, dark)

        ch, fg, bg = frame = blank(cols, rows)
        ch[lit] = ord(glyph)
        fg[lit] = colour[lit]
        note(frame, cols, rows, at, source, dark, w, h)
        return frame
    return render


class Source:
    """A generator run as a child, and paused when nobody is looking.

    A source that talks a protocol cannot be told about watchers -- real
    LED software will never ask -- so the one thing that does know says it
    on the source's behalf. Stopped rather than killed: the effects carry
    state a restart would lose, and SIGCONT costs nothing where starting a
    python process again costs a second of blank wall.

    Signalled as a group rather than as a process. A generator run
    through a launcher -- `uv run`, a shell wrapper -- is a child that
    spawns the thing doing the work, and stopping the launcher leaves the
    work running. Its own session means one signal reaches all of it.

    Only for a source this sender started. Something already running, or
    on another machine, is nobody's to suspend.
    """

    def __init__(self, command):
        self.command = command
        self.child = None
        self.paused = False

    def start(self):
        if not self.command:
            return
        try:
            self.child = subprocess.Popen(shlex.split(self.command),
                                          start_new_session=True)
        except OSError as e:
            say(f"leds: cannot run {self.command!r}: {e}",
                file=sys.stderr, flush=True)

    def signal(self, sig):
        """One signal to everything the source turned into."""
        try:
            os.killpg(os.getpgid(self.child.pid), sig)
        except (OSError, ProcessLookupError):
            pass

    def wanted(self, yes):
        if self.child is None or self.child.poll() is not None:
            return
        if yes and self.paused:
            self.signal(signal.SIGCONT)
            self.paused = False
        elif not yes and not self.paused:
            self.signal(signal.SIGSTOP)
            self.paused = True

    def stop(self):
        """End the generator, killing it if it has not gone in 1.5 s.

        Short, because the sender is on a clock of its own: a controller
        kills the sender five seconds after asking, and the wall still has
        to be told it is leaving after this.
        """
        if self.child is None:
            return
        # Continued first: a stopped process cannot act on being asked to
        # finish, and would be left behind.
        if self.paused:
            self.signal(signal.SIGCONT)
        self.signal(signal.SIGTERM)
        try:
            self.child.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            self.signal(signal.SIGKILL)
            self.child.wait()


def played(play, w, h, port):
    """The command line for a `--play` generator, aimed at this sender
    alone: DDP to 127.0.0.1 on the port only the two of them know, at the
    panel's own size."""
    protocol = "ddp"
    size = f"{w}x{h}"
    if play == "plasma":
        argv = [os.path.join(_HERE, "pattern"), "--size", size,
                "--port", str(port), "127.0.0.1"]
    else:
        argv = [os.path.join(_HERE, "effects"), "--size", size,
                "--protocol", protocol, "--port", str(port)]
        if play != "effects":
            argv += ["--effect", play]
        argv.append("127.0.0.1")
    return shlex.join(argv)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("--size", default="32x16",
                    help="the matrix, as COLSxROWS (default 32x16)")
    ap.add_argument("--layout", choices=("rows", "serpentine"),
                    default="serpentine",
                    help="how the strip is wired (default serpentine)")
    ap.add_argument("--protocol", default="all",
                    help="sacn, artnet, ddp, or all (default all)")
    ap.add_argument("--universe", type=int, default=1,
                    help="first universe, as sACN counts them (default 1); "
                         "Art-Net is read one lower, being zero-based")
    ap.add_argument("--off", default="0d0d10",
                    help="colour of an unlit emitter (default 0d0d10)")
    ap.add_argument("--scale", type=int, default=0,
                    help="cells per LED, with --no-fit; 0 sizes to fit")
    ap.add_argument("--no-fit", action="store_true",
                    help="draw the matrix at its own size and centre it, "
                         "rather than letting it fill the wall")
    ap.add_argument("--pixels", type=int, choices=(1, 2), default=1,
                    help="LEDs per cell: 1 keeps the gap all round, "
                         "2 stacks them in a half block for twice the "
                         "resolution and no gap up and down (default 1)")
    ap.add_argument("--font", type=int, default=FONT)
    ap.add_argument("--fps", type=float, default=FPS)
    ap.add_argument("--source",
                    help="a generator to run, and to suspend whenever "
                         "nobody is watching this wall")
    ap.add_argument("--play", choices=PLAY, default="none",
                    help="a generator from beside this file: effects is "
                         "the whole rotation, fire and pacifica one each, "
                         "plasma the pattern; none waits for real LED "
                         "software (default none); --source wins over it")
    for name, port in PORTS.items():
        ap.add_argument(f"--{name}-port", type=int, default=port,
                        help=f"udp port for {name} (default {port})")
    args = ap.parse_args()

    # A wall is a screen in another room. Nothing here is worth refusing to
    # draw over: clamp, fall back, and say so where an operator can see it.
    def spoke(what, value):
        say(f"leds: {what}; using {value}", file=sys.stderr, flush=True)
        return value

    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
        # Bounded by what the pixels cost rather than by a panel anyone
        # sells: a wall is a few hundred cells across and a matrix drawn
        # one LED to a cell is that big, which is larger than any real
        # panel and perfectly reasonable here.
        if not (0 < w <= 4096 and 0 < h <= 4096 and w * h <= 1_000_000):
            raise ValueError
    except ValueError:
        w, h = spoke(f"{args.size!r} is not a matrix size", (32, 16))
    try:
        off = int(args.off, 16) if len(args.off) == 6 else None
    except ValueError:
        off = None
    if off is None:
        off = int(spoke(f"{args.off!r} is not a colour", "0d0d10"), 16)
    fps = args.fps if 0.1 <= args.fps <= 120 else spoke(
        f"{args.fps} frames a second is not sensible", FPS)
    want = ("sacn", "artnet", "ddp") if args.protocol == "all" \
        else tuple(p for p in args.protocol.split(",") if p in READERS)
    if not want:
        want = spoke(f"{args.protocol!r} names no protocol I speak",
                     ("sacn", "artnet", "ddp"))

    panel = Panel(w, h)
    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    # The emitter is a shape a receiver either draws or does not have, and
    # this is a look built out of it, so it is worth asking rather than
    # hoping: a wall of replacement boxes is not a panel.
    lit = EMITTER if wall.can(EMITTER) else SOLID

    # A controller stops a sender with SIGTERM, and the generator is in a
    # session of its own where that never reaches it, so both signals end
    # the loop and the generator is stopped below, then the wall told.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    # A panel that plays its own generator hears it on a port of its own,
    # on this machine only, so any number of them run side by side; the
    # public ports, which one sender on a machine can hold, are for
    # outside LED software to find.
    own = None
    if args.play != "none" and not args.source:
        own = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        own.bind(("127.0.0.1", 0))
        want = ("ddp",)
        threading.Thread(target=listen, daemon=True,
                         args=("ddp", 0, panel, args.universe, stop, own)).start()
    else:
        for name in want:
            threading.Thread(target=listen, daemon=True,
                             args=(name, getattr(args, f"{name}_port"), panel,
                                   args.universe, stop)).start()

    two = args.pixels == 2 and wall.can(HALF)
    if args.pixels == 2 and not two:
        say("leds: this screen has no half block; one LED to a cell",
            file=sys.stderr, flush=True)
    with wall.transition():
        wall.set_font_size(args.font)
        if two:
            source = stacked(panel, off, args.layout)
        elif args.no_fit:
            source = painter(panel, lit, off, args.layout,
                             max(0, args.scale))
        else:
            source = fitted(panel, lit, off, args.layout)
        wall.set_source(bitwall.FunctionSource(arrays=source))

    # The grid it wants is the matrix at two cells per emitter, which is
    # the shape of the panel rather than the shape of any screen.
    wall.meta(name=f"leds {w}x{h}")

    cols, rows = wall.area()
    say(f"leds -> {args.host}  {w}x{h} {args.layout}  "
        f"{'+'.join(want)}{' on udp/%d, its own' % own.getsockname()[1] if own else ''}  "
        f"{cols}x{rows} at font {args.font}  "
        f"{'2 per cell' if two else ('fitted' if not args.no_fit
                                      else 'scale=' + str(args.scale))}  "
        f"{'emitter' if lit == EMITTER else 'solid'}  (ctrl-c to stop)",
        flush=True)

    command = args.source
    if args.source and args.play != "none":
        say(f"leds: --source given, so it runs and --play {args.play} "
            f"does not", flush=True)
    elif args.play != "none":
        command = played(args.play, w, h, own.getsockname()[1])
    if command:
        say(f"leds: running {command}", flush=True)
    source = Source(command)
    source.start()
    try:
        while not stop.is_set():
            # None means the receiver does not count viewers, which is not
            # the same as counting none: a screen on a wall has nobody to
            # count, and a source stopped for that would never run.
            seen = wall.watching()
            source.wanted(seen is None or seen > 0)
            stop.wait(1.0)
    finally:
        stop.set()
        source.stop()
        wall.detach()
        say("leds: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
