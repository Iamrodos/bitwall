#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Matrix rain on a wall.

    senders/games/matrix/main.py pi.local
    senders/games/matrix/main.py --font 12 --density 0.65 pi.local
    senders/games/matrix/main.py --glyphs ascii pi.local

The other front end on `rain.py`. bitwall calls render() when it wants a
frame and tells it the receiver's area, which is the shape the rain already
answers in, so this is the conversion from a Glyph to a cell and the
arguments that change how the rain looks.

The rain reads its own clock, so the frame rate is a cap and not a speed:
asking for fewer frames a second makes a stream cross a row later, never
slower. Ten is enough for rain, because the fastest stream falls at about a
row a frame at that rate -- the smoothest a character grid can be, since a
stream moves in whole cells and any more frames only repeat the last one.

Cells are left on the receiver's own background rather than painted black.
A wall is black already, a blank cell is the cheapest thing on the wire, and
a tail that dies to a dark green is indistinguishable from one that dies to
the paper it is drawn on.
"""
import argparse
import os
import signal
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SENDERS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_SENDERS))

import bitwall                                                  # noqa: E402
from rain import ASCII_GLYPHS, GLYPHS, Config, Rain             # noqa: E402

DEFAULT_FONT = 18        # a stream you can read a glyph in, across a room

#: The grid the rain is composed for, which a page showing several walls
#: uses to give a small preview the same shape at a smaller font. 120x34 is
#: about 16:9 on a screen, since a cell is roughly twice as tall as it is
#: wide, and 34 rows is enough for a trail to be a trail.
NOMINAL = (120, 34)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--font", type=int, default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=float, default=10.0,
                    help="frames a second at most (default 10)")
    ap.add_argument("--density", type=float, default=0.6,
                    help="share of columns raining at once (default 0.6)")
    ap.add_argument("--flicker", type=float, default=5.0,
                    help="glyph swaps a second per stream (default 5)")
    ap.add_argument("--glyphs", choices=("katakana", "ascii", "auto"),
                    default="auto",
                    help="the alphabet; auto asks the screen (default)")
    args = ap.parse_args()

    cfg = Config(density=max(0.02, min(1.0, args.density)),
                 flicker=max(0.0, args.flicker))
    rain = Rain(cfg)

    def render(cols, rows):
        return [[bitwall.cell(g.ch,
                              "default" if g.fg is None else g.fg,
                              "default" if g.bg is None else g.bg,
                              bold=g.bold)
                 for g in row]
                for row in rain.frame(cols, rows)]

    wall = bitwall.attach(args.host, min_frame=1.0 / max(1.0, args.fps))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    # The alphabet, once the receiver has had its say. Katakana is the
    # film's, and a screen whose font lacks it draws a wall of empty boxes
    # -- which looks like a fault rather than like rain. A receiver that
    # claims nothing is taken at its word and gets the katakana.
    want = args.glyphs
    if want == "auto":
        want = "katakana" if wall.can("\uff66\uff9d") else "ascii"
    # Set before the first frame, and a column picks its characters when
    # it starts falling, so every stream gets the chosen alphabet.
    cfg.glyphs = GLYPHS if want == "katakana" else ASCII_GLYPHS

    with wall.transition():
        wall.set_font_size(args.font)
        wall.set_source(render)
    wall.meta(name="matrix")
    cols, rows = wall.area()
    print(f"{time.strftime('%H:%M:%S')} matrix -> {args.host}  "
          f"{cols}x{rows} at font {args.font}  (ctrl-c to stop)", flush=True)

    try:
        stop.wait()
    finally:
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
