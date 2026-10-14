#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = []
# ///
"""The raycaster on a wall.

    senders/games/raycast/main.py pi.local
    senders/games/raycast/main.py --font 14 pi.local

The bitwall front end on `game.py`. bitwall calls render() whenever it wants
a frame and tells it the receiver's area, which is the contract the engine
already has, so this is the conversion from a Glyph to a cell and nothing
else.

The area is asked for rather than assumed: the font size we request is a
request, and the grid it produces is the receiver's to report.

The wall's minimum frame interval is set to the engine's frame rate. Every
cell of a raycast frame differs from the last, so the receiver's damage
tracking saves nothing and each frame is a full repaint on the wire; asking
for them faster than the engine makes them would only spend bandwidth
re-sending a frame that had not changed.
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

DEFAULT_FONT = 14        # small enough that a column of pixels is a column


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def parse(argv):
    p = argparse.ArgumentParser(
        prog="main.py", description="a raycast dungeon walked on a wall")
    p.add_argument("host")
    p.add_argument("--font", type=int, default=DEFAULT_FONT,
                   help=f"receiver font size (default {DEFAULT_FONT})")
    return p.parse_args(argv)


def main():
    opts = parse(sys.argv[1:])
    host, font = opts.host, opts.font

    game = Game()

    def render(cols, rows):
        return [[bitwall.cell(g.ch,
                              "default" if g.fg is None else g.fg,
                              "default" if g.bg is None else g.bg,
                              bold=g.bold)
                 for g in row]
                for row in game.frame(cols, rows)]

    wall = bitwall.attach(host, min_frame=1.0 / Game.FPS)
    with wall.transition():
        wall.set_font_size(font)
        cols, rows = wall.area()
        game.frame(cols, rows)      # build the world before the first frame
        wall.set_source(render)
    wall.meta(name="raycast",
              description="A first-person raycaster exploring a dungeon")
    say(f"raycast -> {host}  {cols}x{rows} at font {font}  "
        f"framebuffer {cols}x{rows * 2} at {Game.FPS}fps  "
        f"(ctrl-c to stop)", flush=True)

    stopping = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.update(now=True))
    t0 = time.monotonic()
    level = game.level
    try:
        while not stopping["now"]:
            time.sleep(0.5)
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"raycast: resized to {cols}x{rows}", flush=True)
            if game.level != level:
                level = game.level
                say(f"raycast: sector {level}", flush=True)
    finally:
        dt = max(1e-6, time.monotonic() - t0)
        say(f"raycast: stopped  {wall.frames_sent} frames, "
            f"{wall.bytes_sent} bytes, "
            f"{wall.bytes_sent / dt / 1024:.0f} KiB/s pre-compression",
            flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
