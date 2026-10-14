"""Golden cases for the Rust port of the cells population (`src/life.rs`).

    uv run --no-project --with numpy python senders/games/cells/rust/tests/golden.py

Each case is a population grown by `life.py` from a fixed seed, written as
the state that painting reads (every organism's place, mass, age, state,
hue and nodes) beside the frame `World.arrays` makes of it. The Rust test
loads the state into its own World and paints it: the simulation's random
numbers are its own, so what is held equal is the picture of one state,
cell for cell.

A case may paint at another size than it was grown at, so the resize --
every organism rescaled to the new grid -- is held equal too.

`.state` is text, one token per value and floats as Python's repr, which
reads back to the same double; `.arrays` is ch, fg and bg as little-endian
int32, one after the other.
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from life import Config, World  # noqa: E402

OUT = os.path.join(HERE, "golden")


def dump(name, w, cols, rows):
    """Write the world's state, then resize to cols x rows and write the
    frame Python paints there."""
    lines = [f"world {w.W} {w.H} {w.scale!r} {int(w.cfg.aura)} {cols} {rows}",
             f"orgs {len(w.orgs)}"]
    for o in w.orgs:
        lines.append(" ".join(["org", o.kind.name, o.state] + [repr(float(v)) for v in (
            o.since, o.age, o.lifespan, o.hue, o.x, o.y, o.vx, o.vy,
            o.mass, o.target_mass, o.mass_at_death)] + [str(len(o.nr))]))
        for arr in (o.nx, o.ny, o.nr, o.tint):
            lines.append(" ".join(repr(float(v)) for v in arr))
        lines.append(" ".join(str(int(v)) for v in o.extra))
    with open(os.path.join(OUT, name + ".state"), "w") as f:
        f.write("\n".join(lines) + "\n")
    w.last = None                   # no clock step: paint this state
    ch, fg, bg = w.arrays(cols, rows)
    with open(os.path.join(OUT, name + ".arrays"), "wb") as f:
        for a in (ch, fg, bg):
            f.write(np.ascontiguousarray(a, dtype="<i4").tobytes())
    states = {}
    for o in w.orgs:
        states[o.state] = states.get(o.state, 0) + 1
    lit = int((ch != 0x20).sum() + (bg != -1).sum())
    print(f"{name}: {len(w.orgs)} organisms {states}, {cols}x{rows}, "
          f"{lit} lit")


def grown(cols, rows, secs, seed, **cfg):
    c = Config()
    c.seed = seed
    for k, v in cfg.items():
        setattr(c, k, v)
    c.clamp()
    w = World(c)
    w._resize(cols, rows * 2)
    w.advance(secs)
    return w


def main():
    os.makedirs(OUT, exist_ok=True)
    w = grown(150, 45, 30.0, 7)
    dump("small30", w, 150, 45)
    w.advance(60.0)
    dump("small90", w, 150, 45)
    w.advance(5.0)
    dump("resized", w, 220, 61)       # grown at 150x45, painted rescaled
    w = grown(300, 80, 45.0, 11, aura=False, hue=200.0, spread=30.0,
              population=14)
    dump("noaura", w, 300, 80)
    w = grown(320, 96, 20.0, 3, size=1.6)
    dump("large", w, 320, 96)
    w = grown(150, 45, 40.0, 5, lifespan=0.15)
    dump("brief", w, 150, 45)         # short lives: births and deaths

    # Python's time for the frame at the size the Rust timing uses.
    w = grown(1706, 480, 4.0, 5)
    w.last = None
    w.arrays(1706, 480)
    t0 = time.perf_counter()
    n = 5
    for _ in range(n):
        w.last = None
        w.arrays(1706, 480)
    ms = (time.perf_counter() - t0) / n * 1000
    print(f"python arrays at 1706x480: {ms:.1f} ms a frame, "
          f"{len(w.orgs)} organisms")


if __name__ == "__main__":
    main()
