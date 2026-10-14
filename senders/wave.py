#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""A whole sender: colour flowing across a wall.  senders/wave.py pi.local"""
import colorsys, math, os, signal, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bitwall


def colour(x, y, t):
    hue = x / 90 + math.sin(y / 10 + t) / 5 + t / 12
    r, g, b = colorsys.hsv_to_rgb(hue % 1, 0.65, 1)
    return f"{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def render(cols, rows):                 # called every frame, any size
    t = time.time()
    return [[bitwall.cell("▀", colour(x, 2 * y, t), colour(x, 2 * y + 1, t))
             for x in range(cols)] for y in range(rows)]


stop = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.set())
w = bitwall.attach(sys.argv[1], min_frame=1 / 15)
w.meta(name="Wave", description="Colour flowing across the wall")
with w.transition():
    w.set_font_size(12)
    w.set_source(render)
print(time.strftime("%H:%M:%S"), "wave on", sys.argv[1], w.area(), flush=True)
stop.wait()
w.detach()
