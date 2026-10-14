#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]     # the field is summed over every pixel
# ///
"""Cells on a wall: a population of soft organisms that live, split, merge
and die.

    senders/games/cells/main.py pi.local
    senders/games/cells/main.py --population 12 --speed 1.5 pi.local
    senders/games/cells/main.py --no-split --no-merge pi.local
    senders/games/cells/main.py --hue 200 --spread 30 pi.local

The bitwall front end on `life.py`. bitwall asks the population for a frame
as arrays of code points and colours, in the receiver's area, the shape
it already answers in: its picture is numpy's from the start, and a cell
apiece would cost more than the field. What is left here is the arguments
that change how the population behaves.

The population reads its own clock, so the frame rate is a cap and not a
speed. Twelve a second is enough: the slowest bodies move a pixel every
few frames and the fastest a pixel or two a frame, and every frame of a
body that moved is a repaint of every cell it covers.

Arguments are never fatal. A wall is a screen in another room with nobody
at the keyboard, so a value out of range is clamped, a value that is not a
number falls back to its default, an unknown flag is ignored, and what was
done is said on stderr where the operator will find it. A slightly wrong
animation beats a stack trace on the wall.
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
from life import KINDS, NOMINAL, Config, World                  # noqa: E402


DEFAULT_FONT = 18        # bodies big enough to read across a room
DEFAULT_FPS = 12.0


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except ValueError:
            say(f"cells: {text!r} is not a number, using {default}",
                file=sys.stderr, flush=True)
            return default
    return convert


def switch(ap, name, help_on):
    """--name and --no-name, so each behaviour can be watched alone or
    switched off on its own."""
    ap.add_argument(f"--{name}", dest=name, action="store_true",
                    default=True, help=argparse.SUPPRESS)
    ap.add_argument(f"--no-{name}", dest=name, action="store_false",
                    help=f"turn off {help_on}")


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="cells", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bad values are clamped or defaulted, never fatal: the wall "
               "keeps drawing and stderr says what changed.")
    ap.add_argument("host", nargs="*", help="receiver to attach to")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=number(DEFAULT_FPS), default=DEFAULT_FPS,
                    help=f"frames a second at most (default {DEFAULT_FPS:g})")
    ap.add_argument("--population", type=number(8), default=8,
                    help="organisms aimed for on a 150x45 grid; scales "
                         "with the area (default 8, 1..60)")
    switch(ap, "split", "splitting: a large body becoming two smaller")
    switch(ap, "merge", "merging: two bodies that meet becoming one")
    switch(ap, "birth", "births, so the population only ever declines")
    switch(ap, "death", "death, so nothing ages out")
    switch(ap, "growth", "growth, so nothing gets big enough to split")
    switch(ap, "aura", "the faint halo outside a body's rim")
    ap.add_argument("--speed", type=number(1.0), default=1.0,
                    help="multiplier on every kind's speed (default 1, "
                         "0.1..5)")
    ap.add_argument("--size", type=number(1.0), default=1.0,
                    help="multiplier on every kind's size (default 1, "
                         "0.3..3)")
    ap.add_argument("--lifespan", type=number(1.0), default=1.0,
                    help="multiplier on every kind's lifespan (default 1, "
                         "0.1..10)")
    ap.add_argument("--hue", type=number(None), default=None,
                    help="base hue in degrees for every body, instead of "
                         "each kind's own palette")
    ap.add_argument("--spread", type=number(40.0), default=40.0,
                    help="hue spread either side of --hue (default 40)")
    ap.add_argument("--kinds", default=",".join(k.name for k in KINDS),
                    help="which kinds are born, comma separated (default "
                         "all: " + ", ".join(k.name for k in KINDS) + ")")
    ap.add_argument("--seed", type=number(None), default=None,
                    help="random seed, for a run that repeats")
    args, unknown = ap.parse_known_intermixed_args(argv)
    # An unknown flag's value lands among the positionals, and a positional
    # after an unknown flag lands among the unknowns, so the host is the
    # last thing anywhere that could be one, and the rest is noise.
    loose = args.host + [u for u in unknown if not u.startswith("-")]
    hosts = [h for h in loose if not h.replace(".", "").isdigit()
             or h.count(".") == 3]
    if not hosts:
        ap.error("no host given")
    for u in [u for u in unknown if u.startswith("-")] \
            + [h for h in loose if h != hosts[-1]]:
        say(f"cells: ignoring unknown argument {u!r}", file=sys.stderr,
            flush=True)
    args.host = hosts[-1]
    return args


def configure(args):
    cfg = Config()
    cfg.population = args.population
    cfg.split = args.split
    cfg.merge = args.merge
    cfg.birth = args.birth
    cfg.death = args.death
    cfg.growth = args.growth
    cfg.aura = args.aura
    cfg.speed = args.speed
    cfg.size = args.size
    cfg.lifespan = args.lifespan
    cfg.hue = args.hue
    cfg.spread = args.spread
    cfg.kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    cfg.seed = None if args.seed is None else int(args.seed)
    for note in cfg.clamp():
        say(f"cells: {note}", file=sys.stderr, flush=True)
    return cfg


def main():
    args = parse(sys.argv[1:])
    cfg = configure(args)
    font = int(min(400, max(4, args.font)))
    fps = min(60.0, max(1.0, args.fps))
    if font != args.font or fps != args.fps:
        say(f"cells: font {font}, fps {fps:g}", file=sys.stderr, flush=True)

    world = World(cfg)

    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with wall.transition():
        wall.set_font_size(font)
        wall.set_source(bitwall.FunctionSource(arrays=world.arrays))
    wall.meta(name="cells",
              description="Soft organisms that drift, split, merge and die")
    cols, rows = wall.area()
    say(f"cells -> {args.host}  {cols}x{rows} at font {font}  "
        f"population {cfg.population}  (ctrl-c to stop)", flush=True)

    try:
        while not stop.wait(0.5):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"cells: resized to {cols}x{rows}", flush=True)
    finally:
        say("cells: stopped", flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
