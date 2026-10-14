#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]     # the scene vectorises its samples
# ///
"""A spinning donut on a wall.

    senders/games/torus/main.py pi.local
    senders/games/torus/main.py --shape all --palette ice pi.local
    senders/games/torus/main.py --font 12 --fps 24 localhost

The bitwall front end on `torus.py`. bitwall calls render() whenever it
wants a frame and tells it the receiver's area, which is the contract the
scene already has, so this is the conversion from a Glyph to a cell and
nothing else.

The area is asked for rather than assumed: the font size we request is a
request, and the grid it produces is the receiver's to report. The scene
refits itself to whatever comes back, so a window dragged to a new size
changes nothing but the number of samples.
"""
import argparse
import os
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SENDERS = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_SENDERS))

import bitwall                                                  # noqa: E402
from torus import PALETTES, SHAPES, Scene                       # noqa: E402

#: Small enough that the shading is a gradient rather than a staircase,
#: large enough that a wall is not repainting a hundred thousand cells a
#: second for a picture that is mostly background. Below about 10 the
#: bandwidth is all in cells too small to see from where anyone stands.
DEFAULT_FONT = 14

#: The grid this looks right in, for a page laying out several walls. The
#: shape is 16:9 once a cell's proportions are taken out -- a picture, not a
#: page of text -- and it is this dense because the solid has to keep a hole
#: in it, a highlight on it and a gradient across it in a preview box.
NOMINAL = (128, 36)


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def parse(argv):
    p = argparse.ArgumentParser(
        prog="main.py", description="a lit solid turning on a wall")
    p.add_argument("host")
    p.add_argument("--shape", default="torus",
                   choices=list(SHAPES) + ["all"])
    p.add_argument("--palette", default="ember", choices=sorted(PALETTES))
    p.add_argument("--spin", type=float, default=1.0,
                   help="turns a second, as a multiple of the default")
    p.add_argument("--dwell", type=float, default=25.0,
                   help="seconds a shape holds the wall under --shape all")
    p.add_argument("--font", type=int, default=DEFAULT_FONT)
    p.add_argument("--fps", type=float, default=20.0)
    return p.parse_args(argv)


def main():
    opts = parse(sys.argv[1:])
    scene = Scene(shape=opts.shape, palette=opts.palette, spin=opts.spin,
                  dwell=opts.dwell)

    def render(cols, rows):
        return [[bitwall.cell(g.ch,
                              "default" if g.fg is None else g.fg,
                              "default" if g.bg is None else g.bg)
                 for g in row]
                for row in scene.frame(cols, rows)]

    wall = bitwall.attach(opts.host, min_frame=1.0 / opts.fps)
    with wall.transition():
        wall.set_font_size(opts.font)
        cols, rows = wall.area()
        scene.frame(cols, rows)      # fit before the first frame is asked for
        wall.set_source(render)
    label = "solids" if opts.shape == "all" else opts.shape
    wall.meta(name=label,
              description=f"A lit {'solid' if opts.shape == 'all' else opts.shape}"
                          f" turning, shaded in {opts.palette}")
    say(f"torus -> {opts.host}  {opts.shape} in {opts.palette}  "
        f"{cols}x{rows} at font {opts.font}  framebuffer "
        f"{cols}x{rows * 2}  {scene.samples} samples at {opts.fps:g}fps  "
        f"(ctrl-c to stop)", flush=True)

    stopping = {"now": False}

    def halt(*_):
        stopping["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halt)
    t0 = time.monotonic()
    shape = scene.shape
    try:
        while not stopping["now"]:
            time.sleep(0.5)
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"torus: resized to {cols}x{rows}", flush=True)
            if scene.shape != shape:
                shape = scene.shape
                say(f"torus: now a {shape}", flush=True)
    finally:
        dt = max(1e-6, time.monotonic() - t0)
        say(f"torus: stopped  {wall.frames_sent} frames, "
            f"{wall.bytes_sent} bytes, "
            f"{wall.bytes_sent / dt / 1024:.0f} KiB/s pre-compression",
            flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
