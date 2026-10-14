#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = []
# ///
"""Snake on a wall.

    senders/games/snake/main.py pi.local
    senders/games/snake/main.py --font 14 pi.local

The other front end on `game.py`. bitwall calls render() whenever it wants a
frame and tells it the receiver's area, which is the same contract the game
already has, so this is only the conversion from a Glyph to a cell and back
out of the way.

The area is asked for rather than assumed: the font size we request is a
request, and the grid it produces is the receiver's to report.
"""
import argparse
import os
import signal
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))

import bitwall                                                  # noqa: E402
from game import Game                                           # noqa: E402

DEFAULT_FONT = 18        # big enough that the board reads across a room
DESCRIPTION = "A snake that plays itself, eating its way round the board"


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, flush=True, **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host", help="the wall to draw on, as host:wall")
    ap.add_argument("--font", type=int, default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    args = ap.parse_args()

    game = Game()

    def render(cols, rows):
        return [[bitwall.cell(g.ch,
                              "default" if g.fg is None else g.fg,
                              "default" if g.bg is None else g.bg,
                              bold=g.bold)
                 for g in row]
                for row in game.frame(cols, rows)]

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    wall = bitwall.attach(args.host)
    with wall.transition():
        wall.set_font_size(args.font)
        cols, rows = wall.area()
        game.frame(cols, rows)      # lay the board out before the first frame
        wall.set_source(render)
    wall.meta(name="snake", description=DESCRIPTION)
    say(f"snake -> {args.host}  {cols}x{rows} at font {args.font}  "
        f"board {game.gw}x{game.gh}")

    try:
        while not stop.wait(1.0):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"resized to {cols}x{rows}")
    finally:
        say("snake: stopping")
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
