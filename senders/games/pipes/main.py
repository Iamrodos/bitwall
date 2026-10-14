#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]     # the renderer vectorises its pixels
# ///
"""The 3D pipes screensaver on a wall.

    senders/games/pipes/main.py pi.local
    senders/games/pipes/main.py --font 16 localhost

The bitwall front end on `game.py`. bitwall calls render() whenever it wants
a frame and tells it the receiver's area, which is the contract the renderer
already has, so this is the conversion from a Glyph to a cell and nothing
else.

The area is asked for rather than assumed: the font size we request is a
request, and the grid it produces is the receiver's to report. The room is
built for whatever comes back, at any size.

**Only the rows that changed are converted.** The renderer replaces a row
list when it redraws part of it and leaves the rest alone, so an identity
test per row turns a full grid of conversions into a handful. A pipe growing
touches a dozen rows out of a hundred, and the rest of the wall costs an
`is`.
"""
import argparse
import os
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))

import bitwall                                                  # noqa: E402
from game import Game                                           # noqa: E402

DEFAULT_FONT = 12        # small enough that the room has depth to grow into

# The grid the animation is composed for. A page giving this wall a small
# preview box sizes it from here, and pipes want a wide box: the framebuffer
# is two pixels to a cell, so 132x38 cells is a 132x76 picture, which is
# about the proportions of a screen.
NOMINAL = (132, 38)



def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def parse(argv):
    p = argparse.ArgumentParser(
        prog="main.py", description="the 3D pipes screensaver on a wall")
    p.add_argument("host")
    p.add_argument("--font", type=int, default=DEFAULT_FONT,
                   help=f"receiver font size (default {DEFAULT_FONT})")
    return p.parse_args(argv)


def main():
    opts = parse(sys.argv[1:])
    host, font = opts.host, opts.font

    game = Game()
    cache = {"rows": [], "cells": []}

    def render(cols, rows):
        grid = game.frame(cols, rows)
        old, out = cache["rows"], cache["cells"]
        cells = []
        for y, row in enumerate(grid):
            if y < len(old) and row is old[y]:
                cells.append(out[y])
                continue
            cells.append([bitwall.cell(g.ch,
                                       "default" if g.fg is None else g.fg,
                                       "default" if g.bg is None else g.bg)
                          for g in row])
        cache["rows"], cache["cells"] = grid, cells
        return cells

    wall = bitwall.attach(host, min_frame=1.0 / Game.FPS)
    with wall.transition():
        wall.set_font_size(font)
        cols, rows = wall.area()
        game.frame(cols, rows)      # build the room before the first frame
        wall.set_source(render)
    wall.meta(name="pipes",
              description="The 3D pipes screensaver, growing through a room")
    say(f"pipes -> {host}  {cols}x{rows} at font {font}  framebuffer "
        f"{cols}x{rows * 2}  room {game.dims[0]}x{game.dims[1]}x"
        f"{game.dims[2]}  growing {len(game.pipes)}  (ctrl-c to stop)",
        flush=True)

    stopping = {"now": False}

    def halt(*_):
        stopping["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halt)
    t0 = time.monotonic()
    try:
        while not stopping["now"]:
            time.sleep(0.5)
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"pipes: resized to {cols}x{rows}", flush=True)
    finally:
        dt = max(1e-6, time.monotonic() - t0)
        say(f"pipes: stopped  {wall.frames_sent} frames, "
            f"{wall.bytes_sent} bytes, "
            f"{wall.bytes_sent / dt / 1024:.1f} KiB/s pre-compression",
            flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
