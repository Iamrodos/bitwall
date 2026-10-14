#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]
# ///
"""Fireflies drifting over whatever the wall shows.

    senders/fireflies/main.py pi.local
    senders/fireflies/main.py --colour blue --count 20 pi.local

A video layer: it opens a layer above whoever holds the base and sends
QOI frames with alpha, transparent everywhere but the fireflies' light.

Each firefly wanders on a path of its own, a few slow sines summed, rising
a little over time and leaving by the top to come back in below. It
blinks as a real one does: a quick swell, a slow fade and a while dark,
on a rhythm of its own that drifts from flash to flash, and its flash
lifts it a little, the J a firefly draws in the dark. Now and then the
ones near one of them fall into step with it, the flash running out from
it, for a few beats before their rhythms pull apart again. Most are far
and small; a few are near, bigger, brighter and softer, and move faster
across the glass.

`--colour warm` is yellow-green with some amber, `green` a cooler green,
and `blue` the blue ghost firefly, which glows low and steadily for
seconds at a time rather than blinking, and never falls into step.

The picture is drawn at a whole fraction of the glass, at most 540 lines,
and the wall scales it up (`size=`): a glow is soft, and on a 1080-line
glass the frame is a quarter of the pixels to make, send, decode and
upload. Each frame is
only the box round what is lit, the rest of the glass left transparent,
and when nothing is lit the layer is cleared and nothing is sent. Each
frame is held for a second, so a sender that stalls leaves no frozen
fireflies on the wall.
"""
import argparse
import math
import os
import random
import signal
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import bitwall                                                  # noqa: E402
from bitwall.video import qoi                                   # noqa: E402

#: How long each frame stays up if no other follows.
HOLD_MS = 1000
#: The most lines the picture is drawn in: each is a whole number of the
#: glass's lines, the fewest that keep it to this.
DRAW_LINES = 540
#: Fireflies on a 1920x1080 glass; others by the square root of their area.
COUNT_1080 = 40

#: Each palette's colours, with how often each is chosen, and the colour
#: the very centre of a flash runs to.
PALETTES = {
    "warm": ([((218, 255, 78), 5), ((190, 255, 70), 3), ((255, 196, 70), 2)],
             (255, 252, 214)),
    "green": ([((140, 255, 110), 4), ((104, 255, 150), 3), ((178, 255, 92), 2)],
              (236, 255, 228)),
    "blue": ([((118, 212, 255), 4), ((150, 232, 255), 3), ((96, 182, 255), 2)],
             (228, 246, 255)),
}


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} fireflies {msg}", flush=True)


def smoothstep(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class Firefly:
    """One firefly. Its place is in glass heights: y from 0 at the top to 1
    at the bottom, x from 0 to the glass's aspect."""

    def __init__(self, swarm, anywhere=True):
        self.swarm = swarm
        blue = swarm.blue
        # Depth: most far, a few near, which are drawn bigger and softer.
        self.z = random.uniform(0.85, 1.0) if random.random() < 0.12 \
            else random.random() ** 1.6 * 0.6
        near = self.z
        self.size = 0.9 + 2.4 * near ** 2
        self.bright = 0.72 + 0.28 * near
        self.soft = 0.2 + 0.32 * near ** 2       # the core's share of the halo
        self.parallax = 0.6 + 1.1 * near
        colours, _ = swarm.palette
        self.colour = np.array(random.choices([c for c, _ in colours],
                                              [w for _, w in colours])[0], np.float32)
        # The wander: three slow sines a side.
        self.waves = [(random.uniform(0.02, 0.22) * 2 * math.pi, random.uniform(0, 2 * math.pi),
                       random.uniform(0.02, 0.22) * 2 * math.pi, random.uniform(0, 2 * math.pi))
                      for _ in range(3)]
        self.speed = random.uniform(0.022, 0.04) * (0.6 if blue else 1.0)
        self.rise = random.uniform(0.002, 0.008) * (0.2 if blue else 1.0)
        self.t = random.uniform(0, 100)
        self.place(anywhere)
        self.new_rhythm()
        self.cycle = random.uniform(0, self.period)
        self.peak = self.next_peak()
        self.lead = None            # the firefly it is in step with, and until when

    def place(self, anywhere):
        s = self.swarm
        m = s.margin
        self.x = random.uniform(-m, s.aspect + m)
        if self.swarm.blue:
            self.y = random.uniform(0.45, 1.0) if anywhere else 1 + m
        else:
            self.y = 1 - random.random() ** 1.3 if anywhere else 1 + m

    def new_rhythm(self):
        if self.swarm.blue:
            self.rise_s = random.uniform(1.2, 2.2)
            self.hold_s = random.uniform(2.0, 6.0)
            self.fade_s = random.uniform(1.0, 2.0)
            self.period = self.rise_s + self.hold_s + 2.5 * self.fade_s + random.uniform(2, 9)
        else:
            self.rise_s = random.uniform(0.09, 0.16)
            self.hold_s = random.uniform(0.03, 0.08)
            self.fade_s = random.uniform(0.3, 0.5)
            self.period = random.uniform(3.0, 6.5)
        self.base_period = self.period

    def next_peak(self):
        if not self.swarm.blue and random.random() < 0.15:
            return 0.0                      # a beat skipped
        return random.uniform(0.45, 0.75) if self.swarm.blue else random.uniform(0.65, 1.0)

    def glow(self):
        """How lit it is, 0 to 1, at this point in its cycle."""
        c = self.cycle
        if c < self.rise_s:
            e = smoothstep(c / self.rise_s)
        elif c < self.rise_s + self.hold_s:
            e = 1.0
        else:
            # The fade: slow at first and gone cleanly, no long dim tail.
            u = (c - self.rise_s - self.hold_s) / (2.5 * self.fade_s)
            e = 1 - smoothstep(u) if self.swarm.blue else max(0.0, 1 - u) ** 2
        return e * self.peak

    def step(self, dt):
        s = self.swarm
        self.t += dt
        t = self.t
        vx = sum(math.sin(t * fa + pa) for fa, pa, _, _ in self.waves) / 3
        vy = sum(math.sin(t * fb + pb) for _, _, fb, pb in self.waves) / 3
        lit = self.glow()
        lift = 0.0 if s.blue else 0.035 * lit
        self.x += (vx * self.speed) * self.parallax * dt
        self.y += (vy * self.speed * 0.7 - self.rise - lift) * self.parallax * dt

        # In step: pulled toward the leader's cycle, a little behind it
        # with distance, so the flash runs out across the group.
        if self.lead is not None:
            lead, until, delay = self.lead
            if s.clock > until:
                self.lead = None
                self.period = self.base_period = random.uniform(3.5, 7.5)
            else:
                self.period = lead.period
                want = (lead.cycle - delay) % self.period
                diff = (want - self.cycle + self.period / 2) % self.period - self.period / 2
                self.cycle += diff * min(1.0, 2.5 * dt)

        self.cycle += dt
        if self.cycle >= self.period:
            self.cycle -= self.period
            if self.lead is None:
                self.period = self.base_period * random.uniform(0.88, 1.12)
            self.peak = self.next_peak() if self.lead is None else random.uniform(0.75, 1.0)

        m = s.margin
        if self.y < -m:
            self.place(anywhere=False)
        elif self.y > 1 + m:
            self.y = -m
        if self.x < -m:
            self.x += s.aspect + 2 * m
        elif self.x > s.aspect + m:
            self.x -= s.aspect + 2 * m


class Swarm:
    """Every firefly, and the picture they make on one glass."""

    def __init__(self, colour, count):
        self.blue = colour == "blue"
        self.palette = PALETTES[colour]
        self.white = np.array(self.palette[1], np.float32)
        self.asked = count
        self.clock = 0.0
        self.next_sync = random.uniform(12, 30)
        self.flies = []
        self.glass = None
        self.margin = 0.08

    def fit(self, glass):
        """Draw for `glass` from now on: its picture's size, and as many
        fireflies as it is given, kept where they are."""
        gw, gh = glass
        self.glass = glass
        self.scale = max(1, -(-gh // DRAW_LINES))
        self.w, self.h = -(-gw // self.scale), -(-gh // self.scale)
        self.aspect = gw / gh
        self.acc = np.zeros((self.h, self.w, 4), np.float32)
        self.out = np.zeros((self.h, self.w, 4), np.uint8)
        self.boxes = []
        want = self.asked or max(6, round(COUNT_1080 * (0.5 if self.blue else 1)
                                          * math.sqrt(gw * gh / (1920 * 1080))))
        while len(self.flies) < want:
            self.flies.append(Firefly(self))
        del self.flies[want:]
        for f in self.flies:
            if f.x > self.aspect + self.margin:
                f.x = random.uniform(0, self.aspect)
        self.flies.sort(key=lambda f: f.z)
        return want

    def sync(self):
        """Some near one firefly fall into step with it for a few beats."""
        if self.blue:
            return
        lead = random.choice(self.flies)
        reach = 0.38
        near = [(f, math.hypot(f.x - lead.x, f.y - lead.y)) for f in self.flies
                if f is not lead and f.lead is None]
        near = [(f, d) for f, d in near if d < reach]
        if len(near) < 3:
            return
        until = self.clock + lead.period * random.uniform(3.5, 5.5)
        for f, d in near:
            f.lead = (lead, until, d / reach * 0.35)
        lead.lead = None
        return len(near) + 1

    def frame(self, dt):
        """Move everything on by `dt` seconds and draw it: the QOI of the
        box round what is lit, and where that box goes on the glass and how
        big, or None when nothing is lit."""
        self.clock += dt
        synced = None
        if self.clock >= self.next_sync:
            synced = self.sync()
            self.next_sync = self.clock + random.uniform(20, 50)
        for f in self.flies:
            f.step(dt)

        acc, out, H = self.acc, self.out, self.h
        for y0, y1, x0, x1 in self.boxes:
            acc[y0:y1, x0:x1] = 0
            out[y0:y1, x0:x1] = 0
        boxes = []
        for f in self.flies:
            g = f.glow() * f.bright
            if g < 0.01:
                continue
            halo = 0.012 * f.size * H
            core = max(0.8, halo * f.soft)
            cx, cy = f.x * H, f.y * H
            r = int(halo * 3.2) + 1
            x0, x1 = max(int(cx) - r, 0), min(int(cx) + r + 2, self.w)
            y0, y1 = max(int(cy) - r, 0), min(int(cy) + r + 2, H)
            if x0 >= x1 or y0 >= y1:
                continue
            dx = np.arange(x0, x1, dtype=np.float32) + 0.5 - cx
            dy = np.arange(y0, y1, dtype=np.float32)[:, None] + 0.5 - cy
            d2 = dx * dx + dy * dy
            hot = np.exp(d2 * (-0.5 / (core * core)))
            a = g * (hot + 0.55 * np.exp(d2 * (-0.5 / (halo * halo))))
            w = (0.75 * g * hot)[..., None]
            rgb = f.colour * (1 - w) + self.white * w
            box = acc[y0:y1, x0:x1]
            box[..., :3] += rgb * a[..., None]
            box[..., 3] += a
            boxes.append((y0, y1, x0, x1))
        for y0, y1, x0, x1 in boxes:
            b = acc[y0:y1, x0:x1]
            alpha = np.minimum(b[..., 3], 1.0)
            o = out[y0:y1, x0:x1]
            o[..., :3] = np.clip(b[..., :3] / np.maximum(b[..., 3:], 1e-6), 0, 255)
            o[..., 3] = alpha * 255
            # Below a sliver of alpha it is transparent outright: no
            # colour left in pixels nobody sees, and longer runs.
            o[o[..., 3] < 3] = 0
        self.boxes = boxes
        if not boxes:
            return None, synced
        y0 = min(b[0] for b in boxes)
        y1 = max(b[1] for b in boxes)
        x0 = min(b[2] for b in boxes)
        x1 = max(b[3] for b in boxes)
        s = self.scale
        return (qoi(out[y0:y1, x0:x1]), (x0 * s, y0 * s), ((x1 - x0) * s, (y1 - y0) * s)), synced


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=lambda v: 0 if v == "auto" else int(v), default=0,
                    help=f"how many fireflies, or auto: {COUNT_1080} on a 1920x1080 glass, "
                         "by its size on others, and half that of blue (default auto)")
    ap.add_argument("--fps", type=float, default=15,
                    help="frames a second sent (default %(default)s)")
    ap.add_argument("--colour", choices=sorted(PALETTES), default="warm",
                    help="warm yellow-green, a cooler green, or blue ghosts "
                         "(default %(default)s)")
    ap.add_argument("target", nargs="?", default="localhost",
                    help="the wall to drift over, host:wall (default %(default)s)")
    args = ap.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    try:
        layer = bitwall.layer(args.target)
    except Exception as e:                             # noqa: BLE001
        log(f"could not open a layer on {args.target}: {e}")
        return 1
    if not layer.open_now:
        log(f"no layer on {args.target}: {layer.stopped or 'the wall did not answer'}")
        layer.close()
        return 1
    layer.meta(name="Fireflies", description="Fireflies drifting and blinking over the wall")
    until = time.monotonic() + 3
    while layer.pixels() is None and time.monotonic() < until and not stop.is_set():
        time.sleep(0.05)
    log(f"opened a video layer on {args.target}, {args.fps:g}fps, {args.colour}")

    layer.repaint_asked()           # the one every session is told on connecting
    swarm = Swarm(args.colour, args.count)
    period = 1.0 / args.fps
    sent = dropped = made = 0
    sent_bytes = 0
    encode = 0.0
    shown = False
    last = start = first = time.monotonic()
    next_at = start
    idle = False
    while not stop.is_set():
        glass = layer.pixels() or swarm.glass or (1920, 1080)
        if glass != swarm.glass:
            n = swarm.fit(glass)
            log(f"glass {glass[0]}x{glass[1]}: {n} fireflies, drawn at "
                f"{swarm.w}x{swarm.h} and scaled x{swarm.scale}")
        if layer.repaint_asked():
            shown = True            # whatever is there, say it again
        if layer.stopped:
            log(f"stopping: {layer.stopped}")
            break
        if layer.watching() == 0:
            if not idle:
                log("nobody watching; resting")
                idle = True
            stop.wait(0.5)
            last = next_at = time.monotonic()
            continue
        if idle:
            log("watched again")
            idle = False

        now = time.monotonic()
        t0 = time.perf_counter()
        got, synced = swarm.frame(min(now - last, 0.25))
        encode += time.perf_counter() - t0
        made += 1
        last = now
        if synced:
            log(f"{synced} fireflies fall into step")
        if got is None:
            if shown:
                layer.clear()
                shown = False
        elif layer.frame(got[0], at=got[1], size=got[2], hold=HOLD_MS):
            shown = True
            if not sent:
                first = time.monotonic()
            sent += 1
            sent_bytes += len(got[0])
            if sent == 300:
                log(f"first 300 frames: {sent_bytes / sent / 1024:.1f} KB a frame, "
                    f"{encode / made * 1000:.1f} ms to make one")
        else:
            dropped += 1

        next_at += period
        wait = next_at - time.monotonic()
        if wait < -period:
            next_at = time.monotonic()      # far behind: start the clock again
        elif wait > 0:
            stop.wait(wait)

    took = time.monotonic() - start
    rate = (sent - 1) / max(time.monotonic() - first, 1e-3) if sent > 1 else 0.0
    log(f"stopping after {took:.0f}s: {sent} frames sent, {rate:.1f}fps, {dropped} dropped")
    layer.close()
    log("closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
