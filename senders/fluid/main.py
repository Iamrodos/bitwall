#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]
# ///
"""A fluid, stirred by wandering jets, in dye or in drifting particles.

    senders/fluid/main.py localhost
    senders/fluid/main.py --mode particles --font 10 pi.local
    senders/fluid/main.py --mode all --secs 600 localhost:1

  dye        Coloured ink poured in by the jets and carried off by the
             flow, two full-colour pixels a cell in half blocks.
  particles  The same flow with no ink: thousands of motes carried on it,
             each leaving a short trail, eight dots a cell in braille,
             each cell coloured by how fast the fluid under it moves.

`--mode all` takes them in turn. Every `--secs` the fluid starts again
with new jets and new colours, whichever mode is up, so one mode on its
own does not settle into one look for a week.

The solver is Stam's stable fluids on a torus: the flow is carried along
itself, then made incompressible exactly with a Fourier projection, with
vorticity confinement putting back the small curls that carrying smears
out. The field is the wall's cells with each one split top and bottom, so
its pixels are square; the particles move in the same space and are drawn
at twice its resolution. The flow runs off one edge and in at the other.

Everything is numpy arrays from the solver to the wall. Nearly every cell
changes every frame, so colours are rounded to steps of eight, which lets
the dark and the settled parts of the picture cost nothing.

Arguments are never fatal. A wall is a screen in another room with nobody
at the keyboard, so a value out of range is clamped, one that is not a
number falls back to its default, and what was done is said on stderr.
"""
import argparse
import math
import os
import signal
import sys
import threading
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))   # repo root

import bitwall                                                 # noqa: E402
from bitwall import arrays, letters                            # noqa: E402

MODES = ("dye", "particles")
ABOUT = {
    "dye": "Coloured ink stirred by wandering jets",
    "particles": "Motes drifting on a stirred fluid, coloured by its speed",
}
DEFAULT_SECS = 60
DEFAULT_FONT = 14
DEFAULT_FPS = 20

HALF = "▀"
BRAILLE = "⣿"
#: The braille bit for each dot of a cell, four down by two across.
DOTS = np.array([[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]],
                np.int64)
GROUND = 0x05060a

#: Motes per cell for particles. One is chosen each cycle.
DENSITIES = (0.1, 0.2, 0.35, 0.5, 0.7)

#: Ramps for particles, slow to fast. One is chosen each cycle.
RAMPS = {
    "ice": ("0b1d4a", "1e7bd8", "7fe3ff", "f4ffff"),
    "ember": ("3a0a00", "c23b00", "ff9a1f", "fff1b0"),
    "neon": ("1d0340", "7a1fd0", "ff4fd8", "ffd6f6"),
    "sea": ("02281f", "0b8a5c", "5ff2a8", "e8fff4"),
    "smoke": ("2a2a2a", "7a7a80", "c8ccd8", "ffffff"),
}


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


# ----------------------------------------------------------------- colour

def hue(h, s=1.0, v=1.0):
    """An HSV colour as floats 0..1, for one jet's ink."""
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    return ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v),
            (v, p, q))[i]


def pack(rgb):
    """(..., 3) floats 0..1 to 0xRRGGBB, rounded to steps of eight."""
    c = (np.clip(rgb, 0, 1) * 255).astype(np.int64) & 0xF8
    return (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]


def ramp_table(stops, n=256):
    """A ramp through hex stops, as n packed colours."""
    pts = np.array([[int(s[i:i + 2], 16) for i in (0, 2, 4)] for s in stops],
                   float) / 255
    at = np.linspace(0, len(stops) - 1, n)
    lo = np.minimum(at.astype(int), len(stops) - 2)
    t = (at - lo)[:, None]
    return pack(pts[lo] * (1 - t) + pts[lo + 1] * t)


# ----------------------------------------------------------------- solver

def sample(f, x, y):
    """f at (x, y), bilinear, wrapping at every edge. f is (h, w) or
    (h, w, c); x and y are arrays of one shape."""
    h, w = f.shape[:2]
    x0 = np.floor(x)
    y0 = np.floor(y)
    fx = x - x0
    fy = y - y0
    x0 = x0.astype(np.int64) % w
    y0 = y0.astype(np.int64) % h
    x1 = (x0 + 1) % w
    y1 = (y0 + 1) % h
    if f.ndim == 3:
        fx = fx[..., None]
        fy = fy[..., None]
    top = f[y0, x0] * (1 - fx) + f[y0, x1] * fx
    bottom = f[y1, x0] * (1 - fx) + f[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


class Jet:
    """A nozzle wandering over the fluid, turning as it goes, pushing in
    bursts and pouring its own colour of ink."""

    def __init__(self, rng, w, h, colour):
        self.w, self.h = w, h
        self.colour = np.array(colour)
        # A wander made of two sines a side at unrelated rates, so the
        # path never quite repeats and never jumps.
        self.fx = rng.uniform(0.01, 0.05, 2)
        self.fy = rng.uniform(0.01, 0.05, 2)
        self.px = rng.uniform(0, 2 * math.pi, 2)
        self.py = rng.uniform(0, 2 * math.pi, 2)
        self.cx, self.cy = rng.uniform(0, w), rng.uniform(0, h)
        self.turn = rng.uniform(-0.4, 0.4)
        self.angle = rng.uniform(0, 2 * math.pi)
        self.pulse = rng.uniform(0.08, 0.2)
        self.phase = rng.uniform(0, 2 * math.pi)

    def at(self, t):
        x = self.cx + self.w * 0.3 * (math.sin(self.fx[0] * t * 6.28 + self.px[0])
                                      + 0.5 * math.sin(self.fx[1] * t * 6.28 + self.px[1]))
        y = self.cy + self.h * 0.3 * (math.sin(self.fy[0] * t * 6.28 + self.py[0])
                                      + 0.5 * math.sin(self.fy[1] * t * 6.28 + self.py[1]))
        a = self.angle + self.turn * t + 0.8 * math.sin(0.3 * t + self.phase)
        # On for most of each beat, off for a breath: bursts leave eddies
        # behind where a steady push leaves one straight stream.
        s = max(0.0, math.sin(self.pulse * t * 6.28 + self.phase)) ** 0.5
        return x % self.w, y % self.h, math.cos(a), math.sin(a), s


class Fluid:
    """Velocity and ink on a w x h torus of square pixels."""

    def __init__(self, w, h, rng, jets):
        self.w, self.h = w, h
        self.u = np.zeros((h, w))
        self.v = np.zeros((h, w))
        self.ink = np.zeros((h, w, 3))
        self.jets = jets
        self.gy, self.gx = np.mgrid[0:h, 0:w].astype(float)
        self.radius = max(1.5, min(w, h) / 22)
        self.force = max(w, h) * 1.6
        ky = np.fft.fftfreq(h)[:, None]
        kx = np.fft.rfftfreq(w)[None, :]
        k2 = kx * kx + ky * ky
        k2[0, 0] = 1.0
        self.kx, self.ky, self.k2 = kx, ky, k2

    def splat(self, t, dt, ink):
        r2 = 2 * self.radius ** 2
        for jet in self.jets:
            x, y, dx, dy, s = jet.at(t)
            if s <= 0:
                continue
            # The nearest way round the torus to the jet.
            ox = (self.gx - x + self.w / 2) % self.w - self.w / 2
            oy = (self.gy - y + self.h / 2) % self.h - self.h / 2
            g = np.exp(-(ox * ox + oy * oy) / r2)
            self.u += g * (dx * self.force * s * dt)
            self.v += g * (dy * self.force * s * dt)
            if ink:
                self.ink += g[..., None] * (jet.colour * (3.0 * s * dt))

    def confine(self, dt, eps=0.35):
        u, v = self.u, self.v
        curl = (np.roll(v, -1, 1) - np.roll(v, 1, 1)
                - np.roll(u, -1, 0) + np.roll(u, 1, 0)) * 0.5
        a = np.abs(curl)
        nx = (np.roll(a, -1, 1) - np.roll(a, 1, 1)) * 0.5
        ny = (np.roll(a, -1, 0) - np.roll(a, 1, 0)) * 0.5
        n = np.sqrt(nx * nx + ny * ny) + 1e-6
        self.u += eps * (ny / n) * curl * dt * 10
        self.v -= eps * (nx / n) * curl * dt * 10

    def project(self):
        """Take out every part of the velocity that compresses: exact on a
        torus, one transform a component."""
        fu = np.fft.rfft2(self.u)
        fv = np.fft.rfft2(self.v)
        along = (self.kx * fu + self.ky * fv) / self.k2
        fu -= self.kx * along
        fv -= self.ky * along
        self.u = np.fft.irfft2(fu, s=(self.h, self.w))
        self.v = np.fft.irfft2(fv, s=(self.h, self.w))

    def step(self, t, dt, ink):
        self.splat(t, dt, ink)
        self.confine(dt)
        self.project()
        bx = self.gx - self.u * dt
        by = self.gy - self.v * dt
        u = sample(self.u, bx, by)
        self.v = sample(self.v, bx, by)
        self.u = u
        self.project()
        fade = math.exp(-dt / 12.0)
        self.u *= fade
        self.v *= fade
        if ink:
            self.ink = sample(self.ink, bx, by) * math.exp(-dt / 7.0)


# ----------------------------------------------------------------- pictures

class Dye:
    """The ink, two pixels a cell."""
    name = "dye"

    def __init__(self, cols, rows, rng):
        self.cols, self.rows = cols, rows
        n = int(np.clip(cols * rows / 2500, 3, 6))
        base = rng.uniform(0, 1)
        spread = rng.choice([0.08, 0.15, 1.0 / n])
        jets = [Jet(rng, cols, rows * 2,
                    hue((base + i * spread) % 1, rng.uniform(0.6, 1.0)))
                for i in range(n)]
        self.fluid = Fluid(cols, rows * 2, rng, jets)
        self.ch = np.full((rows, cols), ord(HALF), np.int64)
        self.accent = int(pack(np.array(jets[0].colour)))
        self.figure = None
        self.counted = 0.0

    def tick(self, t, dt):
        self.fluid.step(t, dt, ink=True)

    def frame(self):
        ink = self.fluid.ink
        # A soft shoulder rather than a clip, so where ink piles up it
        # goes pale instead of flat.
        rgb = 1 - np.exp(-ink * 1.4)
        c = pack(rgb)
        c = np.where(c == 0, GROUND, c)
        now = time.monotonic()
        if now - self.counted >= 1.0:
            # How much of the wall has ink on it you can see, climbing as
            # the jets fill it and easing as the ink fades. Once a second,
            # so the figure can be read rather than flickering.
            self.figure = f"{int(round((rgb.max(axis=2) > 0.12).mean() * 100))}%"
            self.counted = now
        return self.ch, c[0::2], c[1::2]


class Particles:
    """Motes on the flow, eight dots a cell, each cell coloured by speed."""
    name = "particles"

    def __init__(self, cols, rows, rng):
        self.cols, self.rows = cols, rows
        self.rng = rng
        w, h = cols, rows * 2
        n = int(np.clip(cols * rows / 2500, 3, 6))
        jets = [Jet(rng, w, h, (0, 0, 0)) for _ in range(n)]
        self.fluid = Fluid(w, h, rng, jets)
        # How thick the motes are changes each cycle, from a few threads
        # drifting on black to a dense smoke.
        self.count = int(cols * rows * rng.choice(DENSITIES))
        self.figure = str(self.count)
        self.x = rng.uniform(0, w, self.count)
        self.y = rng.uniform(0, h, self.count)
        self.trail = np.zeros((rows * 4, cols * 2), np.float32)
        self.ramp = list(RAMPS)[rng.integers(len(RAMPS))]
        self.table = ramp_table(RAMPS[self.ramp])
        self.accent = int(self.table[200])
        self.slow = 1.0

    def tick(self, t, dt):
        f = self.fluid
        f.step(t, dt, ink=False)
        # Midpoint steps: a mote on a curl goes round it, where one
        # straight step a frame spirals outwards.
        u = sample(f.u, self.x, self.y)
        v = sample(f.v, self.x, self.y)
        mx, my = self.x + u * dt / 2, self.y + v * dt / 2
        self.x = (self.x + sample(f.u, mx, my) * dt) % f.w
        self.y = (self.y + sample(f.v, mx, my) * dt) % f.h
        # A flow bunches motes into its seams over time; a few start
        # again somewhere at random every frame so the picture stays even.
        k = max(1, int(self.count * 0.3 * dt))
        i = self.rng.integers(0, self.count, k)
        self.x[i] = self.rng.uniform(0, f.w, k)
        self.y[i] = self.rng.uniform(0, f.h, k)
        self.trail *= math.exp(-dt / 0.2)
        dx = np.minimum((self.x * 2).astype(np.int64), self.cols * 2 - 1)
        dy = np.minimum((self.y * 2).astype(np.int64), self.rows * 4 - 1)
        self.trail[dy, dx] = 1.0

    def frame(self):
        rows, cols = self.rows, self.cols
        lit = (self.trail > 0.3).astype(np.int64)
        bits = (lit.reshape(rows, 4, cols, 2) * DOTS[None, :, None, :]) \
            .sum(axis=(1, 3))
        ch = np.where(bits == 0, 32, 0x2800 + bits)
        f = self.fluid
        speed = np.hypot(f.u, f.v)
        speed = (speed[0::2] + speed[1::2]) * 0.5
        # Scaled to this frame's fast end, eased towards it, so a calm
        # spell still uses the whole ramp and a gust does not flash.
        top = max(np.percentile(speed, 98), 1e-3)
        self.slow += (top - self.slow) * 0.05
        level = np.clip(speed / self.slow, 0, 1) ** 0.7
        fg = self.table[(level * 255).astype(np.int64)]
        bg = np.full((rows, cols), GROUND, np.int64)
        return ch, fg, bg


PICTURES = {"dye": Dye, "particles": Particles}

# ----------------------------------------------------------------- notes

class Notes:
    """A black bar along the bottom with a line of LED lettering, the way
    sysinfo shows its clock: the dot-matrix font, every letter the same
    width, the unlit dots glowing faintly behind. On a wall with no
    braille it is the half-block font instead."""

    #: How bright the unlit dots are beside the lit ones.
    GLOW = 0.2

    def __init__(self, wall):
        self.dots = letters.usable(letters.DOTS, wall)
        self.made = {}

    def rows(self, rows):
        return 4 if self.dots else 7

    def arrays(self, text, cols, rows, ink):
        """The bar, lettered in `ink` (0xRRGGBB), the colour of whatever
        is flowing above it."""
        key = (text, cols, rows, ink)
        if key not in self.made:
            self.made.clear()
            text = text.upper()
            dim = sum(int(((ink >> k) & 255) * self.GLOW) << k
                      for k in (16, 8, 0))
            grid = [[bitwall.cell(" ", "ffffff", "000000")
                     for _ in range(cols)] for _ in range(rows)]
            if self.dots:
                opts = dict(font=letters.DOTS, spacing="mono",
                            ghost="%06x" % dim)
            else:
                opts = dict(font=letters.HALF, height=5)
            wide, tall = letters.size(text, **opts)
            letters.draw(grid, text, max(0, (cols - wide) // 2),
                         max(0, (rows - tall) // 2), "%06x" % ink, "000000",
                         **opts)

            def colour(v):
                return int(v, 16) if isinstance(v, str) and len(v) == 6 \
                    else 0
            ch = np.array([[ord(c[0][0]) if c[0] else 32 for c in row]
                           for row in grid], np.int64)
            fg = np.array([[colour(c[1]) for c in row] for row in grid],
                          np.int64)
            bg = np.array([[colour(c[2]) for c in row] for row in grid],
                          np.int64)
            at = np.array([[arrays.FAINT if len(c) > 8 and c[8] else 0
                            for c in row] for row in grid], np.int64)
            self.made[key] = (ch, fg, bg, at)
        return self.made[key]


# ----------------------------------------------------------------- front end

def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except ValueError:
            say(f"fluid: {text!r} is not a number, using {default}",
                file=sys.stderr, flush=True)
            return default
    return convert


def clamp(name, value, lo, hi):
    got = min(hi, max(lo, value))
    if got != value:
        say(f"fluid: {name} {value:g} is outside {lo:g}..{hi:g}, using {got:g}",
            file=sys.stderr, flush=True)
    return got


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="fluid", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bad values are clamped or defaulted, never fatal: the wall "
               "keeps drawing and stderr says what changed.")
    ap.add_argument("host", nargs="?", help="receiver to attach to")
    ap.add_argument("--mode", default="all",
                    help="dye, particles, or all in turn (default all)")
    ap.add_argument("--secs", type=number(DEFAULT_SECS), default=DEFAULT_SECS,
                    help=f"seconds before the fluid starts again, and in "
                         f"`all` the next mode (default {DEFAULT_SECS:g})")
    ap.add_argument("--speed", type=number(1.0), default=1.0,
                    help="multiplier on how fast the fluid runs (default 1, "
                         "0.1..5)")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=number(DEFAULT_FPS), default=DEFAULT_FPS,
                    help=f"frames a second at most (default {DEFAULT_FPS:g})")
    ap.add_argument("--notes", action="store_true",
                    help="a black bar along the bottom saying, in big "
                         "letters, what is showing and how")
    ap.add_argument("--seed", type=number(None), default=None,
                    help="random seed, for a run that repeats")
    args, unknown = ap.parse_known_intermixed_args(argv)
    for u in unknown:
        say(f"fluid: ignoring unknown argument {u!r}", file=sys.stderr,
            flush=True)
    if not args.host:
        ap.error("no host given")
    return args


def main():
    args = parse(sys.argv[1:])
    order = list(MODES) if args.mode == "all" else \
        [m for m in args.mode.split(",") if m in MODES]
    if not order:
        say(f"fluid: {args.mode!r} names no mode I have; using all",
            file=sys.stderr, flush=True)
        order = list(MODES)
    secs = clamp("secs", args.secs, 10, 86400)
    font = int(clamp("font", args.font, 4, 400))
    fps = clamp("fps", args.fps, 1, 60)
    speed = clamp("speed", args.speed, 0.1, 5)
    rng = np.random.default_rng(None if args.seed is None else int(args.seed))

    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    if "particles" in order and not wall.can(BRAILLE):
        say("fluid: this screen has no braille; dye only",
            file=sys.stderr, flush=True)
        order = [m for m in order if m != "particles"] or ["dye"]
    if not wall.can(HALF):
        say("fluid: this screen has no half block; the picture will be wrong",
            file=sys.stderr, flush=True)

    step = speed / fps
    bar = Notes(wall) if args.notes else None
    started = time.monotonic()
    showing = {"picture": None, "cycle": None, "last": None, "t": 0.0}

    def render(cols, rows):
        now = time.monotonic()
        cycle = int((now - started) / secs)
        want = order[cycle % len(order)]
        pic = showing["picture"]
        if pic is None or showing["cycle"] != cycle \
                or (pic.cols, pic.whole) != (cols, rows):
            high = rows - (bar.rows(rows) if bar else 0)
            pic = showing["picture"] = PICTURES[want](cols, high, rng)
            pic.whole = rows
            showing["cycle"] = cycle
            showing["last"] = None
            showing["t"] = 0.0
        # Every frame moves the fluid on by the same step, however late it
        # was asked for. A frame held up on its way to the wall then only
        # slows the motion for a moment, where stepping by the time that
        # really passed stalls and then lurches to catch up. Time runs
        # only while frames are wanted, so a wall that went dark comes
        # back to the fluid as it left it.
        dt = 0.0 if showing["last"] is None else step
        showing["last"] = now
        showing["t"] += dt
        if dt > 0:
            pic.tick(showing["t"], dt)
        ch, fg, bg = pic.frame()
        if bar:
            note = pic.name
            if getattr(pic, "ramp", None):
                note += f" {pic.ramp}"
            if pic.figure:
                note += f" {pic.figure}"
            under = bar.arrays(note, cols, rows - pic.rows, pic.accent)
            above = np.zeros(ch.shape, np.int64)
            return tuple(np.vstack([a, b]) for a, b
                         in zip((ch, fg, bg, above), under))
        return ch, fg, bg

    with wall.transition():
        wall.set_font_size(font)
        wall.set_source(bitwall.FunctionSource(arrays=render))
    wall.meta(name="fluid", description="A stirred fluid: " + ", ".join(order))
    cols, rows = wall.area()
    say(f"fluid -> {args.host}  {cols}x{rows} at font {font}  "
        f"{'+'.join(order)} every {secs:g}s  {fps:g}fps  (ctrl-c to stop)",
        flush=True)

    named = None
    try:
        while not stop.wait(0.5):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"fluid: resized to {cols}x{rows}", flush=True)
            pic = showing["picture"]
            key = (pic.name, showing["cycle"]) if pic else None
            if key and key != named:
                if not named or named[0] != key[0]:
                    wall.meta(name=f"fluid {pic.name}",
                              description=ABOUT[pic.name])
                say(f"fluid: {pic.name}, fresh", flush=True)
                named = key
    finally:
        say("fluid: stopped", flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
