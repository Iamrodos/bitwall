#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy"]
# ///
"""A ball and a spinning bar bouncing across four walls as one screen.

    senders/quad/main.py --tl pi-a.local:1 --tr pi-a.local \\
                         --bl pi-b.local --br pi-b.local:1 \\
                         --gap-x 21 --gap-y 18 [--nudge br=0,-2] [--tune]

The four walls are a two by two grid. Every wall is drawn at the smallest
area any of them reports, so the grid is one canvas twice that wide and
twice that tall. The motion runs on its own thread against the monotonic
clock and each wall draws its quarter of it when asked, so the four stay
in step whatever their frame rates. It is drawn in half blocks, two
square pixels a cell, so the ball is round.

**The bezels are part of the canvas.** The gaps between panels, glass to
glass in millimetres, sit between the quarters as pixels nobody sees, so
a line crossing a join carries on straight and the ball passes behind a
bezel rather than jumping over it. A nudge moves one wall's quarter of the
canvas by a few millimetres, for panels that do not sit quite level.
Millimetres become pixels by the panel's width, `--panel-mm`, over the
columns the wall shows.

**`--tune` shows an alignment pattern and takes keys** in the terminal it
runs in: the diagonals, a circle and a line across each join, still, so a
kink at a join is the thing to fix. Arrows move the gaps; 1 to 4 pick a
wall (top left, top right, bottom left, bottom right) and the arrows then
nudge it; 0 goes back to the gaps; f toggles steps of one pixel or one
millimetre; q quits and prints the flags that give what is on the glass.
"""
import argparse
import math
import os
import select
import signal
import sys
import termios
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import bitwall                                                 # noqa: E402

FPS = 25
TICK = 1 / 120
GROUND, BALL, BAR = 0x06070c, 0xff8c2a, 0x5fd7ff
QUADS = ("tl", "tr", "bl", "br")


def say(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for q in ("tl", "tr", "bl", "br"):
        ap.add_argument(f"--{q}", required=True, metavar="WALL")
    ap.add_argument("--font", type=int, default=14)
    ap.add_argument("--gap-x", type=float, default=0.0, metavar="MM",
                    help="glass to glass between the left and right panels")
    ap.add_argument("--gap-y", type=float, default=0.0, metavar="MM",
                    help="glass to glass between the top and bottom panels")
    ap.add_argument("--nudge", action="append", default=[], metavar="Q=DX,DY",
                    help="move a wall's quarter, in mm, e.g. br=0,-2")
    ap.add_argument("--panel-mm", type=float, default=597.6,
                    help="a panel's lit width (a 27-inch 16:9 is 597.6)")
    ap.add_argument("--tune", action="store_true",
                    help="an alignment pattern, adjusted from the keyboard")
    args = ap.parse_args()
    targets = [args.tl, args.tr, args.bl, args.br]
    nudge = [[0.0, 0.0] for _ in QUADS]
    for n in args.nudge:
        q, _, xy = n.partition("=")
        dx, dy = (float(v) for v in xy.split(","))
        nudge[QUADS.index(q)] = [dx, dy]

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    walls = [bitwall.attach(t, min_frame=1 / FPS) for t in targets]
    lock = threading.Lock()
    # In canvas pixels once the canvas is known, shares of it until then.
    s = {"w": 0, "h": 0, "bx": 0.3, "by": 0.4, "bvx": 1.0, "bvy": 0.7,
         "cx": 0.7, "cy": 0.6, "cvx": -0.6, "cvy": 0.8, "a": 0.0, "spin": 1.1,
         "gx": args.gap_x, "gy": args.gap_y, "nudge": nudge}

    def area():
        return (min(w.area()[0] for w in walls),
                min(w.area()[1] for w in walls))

    def layout(d, cols, rows):
        """The canvas size in pixels, and where each wall's top left sits
        on it, with the gaps and nudges in millimetres made pixels."""
        mm = args.panel_mm / cols
        gx, gy = d["gx"] / mm, d["gy"] / mm
        at = [((k % 2) * (cols + gx) + d["nudge"][k][0] / mm,
               (k // 2) * (2 * rows + gy) + d["nudge"][k][1] / mm)
              for k in range(4)]
        return 2 * cols + gx, 4 * rows + gy, at

    def simulate():
        last = time.monotonic()
        while not stop.wait(TICK):
            now = time.monotonic()
            dt, last = min(now - last, 0.25), now
            cols, rows = area()
            with lock:
                W, H, _ = layout(s, cols, rows)    # a cell is 1x2 pixels
                if (W, H) != (s["w"], s["h"]):
                    fx, fy = (W / s["w"], H / s["h"]) if s["w"] else (W, H)
                    s["bx"], s["cx"] = s["bx"] * fx, s["cx"] * fx
                    s["by"], s["cy"] = s["by"] * fy, s["cy"] * fy
                    s["w"], s["h"] = W, H
                    say(f"canvas {W:.0f}x{H:.0f} pixels, "
                        f"each wall {cols}x{rows}")
                r, half, speed = 0.09 * min(W, H), W / 8, 0.15 * W
                s["a"] += s["spin"] * dt
                # The bar's reach turns with it, so a swinging end bounces
                # it too; the bar spins the other way off every edge.
                reach = {"b": (r, r),
                         "c": (half * abs(math.cos(s["a"])) + 1,
                               half * abs(math.sin(s["a"])) + 1)}
                for o in ("b", "c"):
                    for ax, size, ex in (("x", W, reach[o][0]),
                                         ("y", H, reach[o][1])):
                        p, v = o + ax, o + "v" + ax
                        s[p] += s[v] * speed * dt
                        if s[p] < ex or s[p] > size - ex:
                            low = s[p] < ex
                            s[p] = ex if low else size - ex
                            s[v] = abs(s[v]) if low else -abs(s[v])
                            if o == "c":
                                s["spin"] = -s["spin"]

    def pattern(x, y, W, H):
        """Lines that each cross a join: the two diagonals, a circle round
        the middle, and a line along the middle of each row and column of
        panels. Straight across every bezel when the gaps are right."""
        n = math.hypot(W, H)
        line = (np.abs(x * H - y * W) / n < 0.7) | \
               (np.abs(x * H + y * W - W * H) / n < 0.7)
        ring = np.abs(np.hypot(x - W / 2, y - H / 2) - 0.4 * min(W, H)) < 0.7
        rows_ = (np.abs(y - H / 4) < 0.7) | (np.abs(y - 3 * H / 4) < 0.7)
        cols_ = (np.abs(x - W / 4) < 0.7) | (np.abs(x - 3 * W / 4) < 0.7)
        return line | ring | rows_ | cols_

    def flags():
        out = f"--gap-x {s['gx']:g} --gap-y {s['gy']:g}"
        for q, (dx, dy) in zip(QUADS, s["nudge"]):
            if dx or dy:
                out += f" --nudge {q}={dx:g},{dy:g}"
        return out

    def tuner():
        """Keys from the terminal: arrows move the gaps or a wall."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        pick, fine = None, False
        say("tuning: arrows move the gaps, 1-4 pick a wall, 0 the gaps, "
            "f fine, q quit")
        try:
            while not stop.is_set():
                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                key = os.read(fd, 8).decode(errors="ignore")
                if key == "q":
                    stop.set()
                    break
                if key in "01234" and len(key) == 1:
                    pick = None if key == "0" else int(key) - 1
                elif key == "f":
                    fine = not fine
                arrow = {"\x1b[A": (0, -1), "\x1b[B": (0, 1),
                         "\x1b[C": (1, 0), "\x1b[D": (-1, 0)}.get(key)
                with lock:
                    if arrow:
                        step = 1.0 if fine else args.panel_mm / area()[0]
                        if pick is None:
                            s["gx"] = max(0.0, s["gx"] + arrow[0] * step)
                            s["gy"] = max(0.0, s["gy"] + arrow[1] * step)
                        else:
                            s["nudge"][pick][0] += arrow[0] * step
                            s["nudge"][pick][1] += arrow[1] * step
                    what = "gaps" if pick is None else QUADS[pick]
                    say(f"[{what}{', fine' if fine else ''}] {flags()}")
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old)

    def painter(k):
        def render(_cols, _rows):
            cols, rows = area()
            with lock:
                d = dict(s, nudge=[list(n) for n in s["nudge"]])
            W, H, at = layout(d, cols, rows)
            x = at[k][0] + np.arange(cols) + 0.5
            y = (at[k][1] + np.arange(2 * rows) + 0.5)[:, None]
            px = np.full((2 * rows, cols), GROUND, np.int32)
            if args.tune:
                px[pattern(x, y, W, H)] = BAR
            elif d["w"]:
                half = d["w"] / 8
                ux, uy = half * math.cos(d["a"]), half * math.sin(d["a"])
                ax, ay = d["cx"] - ux, d["cy"] - uy
                t = np.clip(((x - ax) * ux + (y - ay) * uy)
                            / (2 * half * half), 0, 1)
                bar = np.hypot(x - ax - 2 * t * ux, y - ay - 2 * t * uy)
                px[bar < 0.8] = BAR
                ball = np.hypot(x - d["bx"], y - d["by"])
                px[ball < 0.09 * min(d["w"], d["h"])] = BALL
            return (np.full((rows, cols), ord("▀"), np.int32),
                    px[0::2], px[1::2])
        return render

    for k, w in enumerate(walls):
        w.meta(name="Quad", description="A ball and a bar across four walls")
        with w.transition():
            w.set_font_size(args.font)
            w.set_source(bitwall.FunctionSource(arrays=painter(k)))
        say(f"{targets[k]}: {w.area()[0]}x{w.area()[1]} at font {args.font}")

    threading.Thread(target=simulate, daemon=True).start()
    if args.tune and sys.stdin.isatty():
        threading.Thread(target=tuner, daemon=True).start()
    stop.wait()
    say("stopping")
    if args.tune:
        with lock:
            say(f"keep: {flags()}")
    for w in walls:
        w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
