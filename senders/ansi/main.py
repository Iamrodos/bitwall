#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# ///
"""ANSI art on a wall, a piece at a time.

    senders/ansi/main.py localhost
    senders/ansi/main.py --secs 20 --font 10 media/ansi/pack pi.local

The pieces are `.ans` files in a directory, or one file, and without one
they are the pack in `media/ansi/pack`, which `media/ansi/README.md` says
how to fetch.

Forty years of art made for exactly this medium. A `.ans` file is a stream
of CP437 bytes and escape sequences -- which is to say it is nearly what
bitwall puts on the wire already, in a different encoding -- so showing one
is mostly a matter of reading it correctly rather than of drawing anything.

The reading is `art.py`, which is its own renderer rather than a borrowed
terminal emulator: the two disagree about wrapping and margins, and that
disagreement is where art lives. `verify.py` checks a parse against the
renders 16colo.rs publishes, so the question of whether a piece is read
correctly has an answer.

**A piece is 80 columns and has its own height**, which is a fact about
the art rather than about any screen. So it is centred in whatever area
the wall has, and a piece taller than the screen is panned down it slowly
over the time it is shown, which is how these were meant to be read: they
were made to scroll past on a BBS at the speed of a modem.
"""
import argparse
import os
import random
import signal
import struct
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)                                     # repo root

sys.path.insert(0, _HERE)

import bitwall                                                # noqa: E402
import art                                                    # noqa: E402

FONT = 12
SECS = 10.0
FPS = 10.0
#: Rows a second when moving down a piece taller than the screen. Slow
#: enough to read the part going by, which is the speed these were drawn
#: to be seen at -- they arrived down a modem.
PAN = 6.0
PACK = os.path.join(_ROOT, "media", "ansi", "pack")


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def cells_of(path):
    """One `.ans` file as (rows of bitwall cells, the width it was drawn)."""
    return art.cells(open(path, "rb").read(), bitwall.cell)


class Gallery:
    """The pieces, and which one is up.

    Loaded as it goes rather than all at once: a pack is a few dozen files
    and parsing one is milliseconds, but a wall should start showing
    something immediately rather than after reading everything.
    """

    #: Held still at the top before a pan starts and at the bottom after
    #: it ends. Without them a tall piece begins moving before it has been
    #: seen, and changes in the instant it arrives.
    HOLD = 2.0

    def __init__(self, paths, secs, pan, shuffle, rng):
        self.paths = list(paths)
        if shuffle:
            rng.shuffle(self.paths)
        self.secs = secs
        self.pan = pan
        self.at = -1
        self.since = 0.0
        self.grid = [[bitwall.cell(" ")]]
        self.name = ""
        self.next()

    def next(self):
        for _ in range(len(self.paths)):
            self.at = (self.at + 1) % len(self.paths)
            path = self.paths[self.at]
            try:
                self.grid, _ = cells_of(path)
            except Exception as e:
                say(f"ansi: {os.path.basename(path)}: {e}",
                    file=sys.stderr, flush=True)
                continue
            self.name = os.path.basename(path)
            self.since = time.monotonic()
            say(f"ansi: {self.name}  {len(self.grid[0])}x{len(self.grid)}",
                flush=True)
            return
        say("ansi: nothing here could be read", file=sys.stderr, flush=True)

    def showing(self, rows):
        """How long this piece is up for, and how long it pans within it.

        A piece that fits is a still picture and gets the time asked for.
        One that does not is read by moving down it, and how long that
        takes is a fact about the piece rather than a setting: a piece
        twice as tall takes twice as long, at a speed an eye can follow.
        """
        over = max(0, len(self.grid) - rows)
        if not over:
            return self.secs, 0.0
        travel = over / max(0.5, self.pan)
        return self.HOLD + travel + self.HOLD, travel

    def render(self, cols, rows):
        now = time.monotonic()
        total, travel = self.showing(rows)
        if now - self.since >= total:
            self.next()
            total, travel = self.showing(rows)
        art_h, art_w = len(self.grid), len(self.grid[0])
        out = [[bitwall.cell(" ")] * cols for _ in range(rows)]

        # Narrower than the screen, so centre it; wider, so take the
        # middle of it. Taking the left of a wide piece cuts one edge off
        # entirely, and a composition is built around its centre.
        left = max(0, (cols - art_w) // 2)
        cut = max(0, (art_w - cols) // 2)
        if art_h <= rows:
            top_of_art, top_of_screen = 0, (rows - art_h) // 2
        else:
            # Still at the top, then down at a readable pace, then still
            # at the bottom -- so the end of a piece is seen rather than
            # arrived at as it is replaced.
            gone = (now - self.since) - self.HOLD
            share = min(1.0, max(0.0, gone / max(0.1, travel)))
            top_of_art = int(share * (art_h - rows))
            top_of_screen = 0

        for y in range(min(rows - top_of_screen, art_h - top_of_art)):
            src = self.grid[top_of_art + y]
            dst = out[top_of_screen + y]
            for x in range(min(cols - left, art_w - cut)):
                dst[left + x] = src[cut + x]
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("where", nargs="?", default=PACK,
                    help="a directory of .ans files, or one file "
                         "(default media/ansi/pack)")
    ap.add_argument("host")
    ap.add_argument("--secs", type=float, default=SECS,
                    help=f"seconds a piece that fits is shown "
                         f"(default {SECS:g}); a taller one is shown for "
                         f"as long as panning it takes")
    ap.add_argument("--pan", type=float, default=PAN,
                    help=f"rows a second when panning (default {PAN:g})")
    ap.add_argument("--font", type=int, default=FONT)
    ap.add_argument("--fps", type=float, default=FPS,
                    help="only a panning piece moves, so this is low")
    ap.add_argument("--in-order", action="store_true",
                    help="show them in name order rather than shuffled")
    args = ap.parse_args()

    def spoke(what, value):
        say(f"ansi: {what}; using {value}", file=sys.stderr, flush=True)
        return value

    if not os.path.exists(args.where):
        say(f"ansi: {args.where}: not found", file=sys.stderr, flush=True)
        return 2
    if os.path.isdir(args.where):
        paths = sorted(os.path.join(args.where, n)
                       for n in os.listdir(args.where)
                       if n.lower().endswith(".ans"))
    else:
        paths = [args.where]
    if not paths:
        say(f"ansi: no .ans files in {args.where}", file=sys.stderr,
            flush=True)
        return 2
    secs = args.secs if 0.5 <= args.secs <= 3600 else spoke(
        f"{args.secs} seconds a piece is not sensible", SECS)
    fps = args.fps if 0.1 <= args.fps <= 60 else spoke(
        f"{args.fps} frames a second is not sensible", FPS)

    pan = args.pan if 0.5 <= args.pan <= 60 else spoke(
        f"{args.pan} rows a second is not a pace", PAN)
    gallery = Gallery(paths, secs, pan, not args.in_order, random.Random())
    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with wall.transition():
        wall.set_font_size(args.font)
        wall.set_source(gallery.render)
    wall.meta(name="ansi art",
              description="ANSI art from the BBS scene, a piece at a time")
    cols, rows = wall.area()
    say(f"ansi -> {args.host}  {len(paths)} pieces, {secs:g}s each  "
        f"{cols}x{rows} at font {args.font}  (ctrl-c to stop)", flush=True)
    try:
        while not stop.wait(0.5):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"ansi: resized to {cols}x{rows}", flush=True)
    finally:
        say("ansi: stopped", flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
