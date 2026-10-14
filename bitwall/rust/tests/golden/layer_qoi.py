"""The golden QOI images for `qoi.rs`: pixels, and the bytes Python makes.

Each case is an RGBA picture and what `bitwall.video.qoi` writes for it.
The Rust test reads them back and asks for the same bytes.

    uv run --no-project --with numpy python bitwall/rust/tests/golden/layer_qoi.py

The file, `layer_qoi.bin`, little-endian throughout:

    b"BWQO"  u32 cases
    case:    u16 name length, name
             u32 width, u32 height, rgba[width * height * 4]
             u32 length, the QOI's bytes
"""
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", ".."))

from bitwall.video import qoi                    # noqa: E402

rng = np.random.default_rng(20261011)


def solid(w, h, rgba):
    a = np.zeros((h, w, 4), np.uint8)
    a[:] = rgba
    return a


def cases():
    yield "one black", solid(1, 1, (0, 0, 0, 255))
    yield "one clear", solid(1, 1, (0, 0, 0, 0))
    yield "one red", solid(1, 1, (255, 0, 0, 255))
    for n in (61, 62, 63, 124, 125, 200):
        yield f"run {n}", solid(n, 1, (0, 0, 0, 255))
        yield f"run {n} after", np.concatenate(
            [solid(1, 1, (7, 8, 9, 255)), solid(n, 1, (10, 10, 10, 255))], axis=1)
    yield "solid 40x30", solid(40, 30, (30, 60, 90, 200))
    g = np.zeros((32, 64, 4), np.uint8)
    g[..., 0] = np.arange(64)[None, :] * 4
    g[..., 1] = np.arange(32)[:, None] * 8
    g[..., 2] = 128
    g[..., 3] = 255
    yield "gradient", g
    yield "noise", rng.integers(0, 256, (24, 33, 4), dtype=np.uint8)
    n = rng.integers(0, 256, (20, 20, 4), dtype=np.uint8)
    n[..., 3] = 255
    yield "noise opaque", n
    # Small steps from pixel to pixel, wrapping, in every band QOI has.
    for name, lo, hi in (("diff", -2, 2), ("luma", -40, 40), ("wide", -128, 128)):
        steps = rng.integers(lo, hi, (50 * 30, 3))
        px = np.cumsum(steps, axis=0) % 256
        a = np.zeros((30 * 50, 4), np.uint8)
        a[:, :3] = px
        a[:, 3] = 255
        yield f"steps {name}", a.reshape(30, 50, 4)
    # Alpha that changes now and then, and runs among the changes.
    a = np.repeat(rng.integers(0, 256, (100, 4), dtype=np.uint8),
                  rng.integers(1, 90, 100), axis=0)
    a[rng.random(len(a)) < 0.3, 3] = 255
    a = a[: (len(a) // 37) * 37]
    yield "alpha runs", a.reshape(-1, 37, 4)
    # Something like a picture: a sprite on a clear ground.
    s = np.zeros((48, 48, 4), np.uint8)
    yy, xx = np.mgrid[:48, :48]
    disc = (yy - 24) ** 2 + (xx - 24) ** 2 < 400
    s[disc] = (250, 200, 40, 255)
    s[disc & (xx > 24)] = (240, 180, 30, 255)
    yield "sprite", s


def main():
    out = [b"BWQO"]
    all_ = list(cases())
    out.append(struct.pack("<I", len(all_)))
    for name, rgba in all_:
        rgba = np.ascontiguousarray(rgba, np.uint8)
        h, w = rgba.shape[:2]
        q = qoi(rgba)
        nb = name.encode()
        out.append(struct.pack("<H", len(nb)) + nb)
        out.append(struct.pack("<II", w, h) + rgba.tobytes())
        out.append(struct.pack("<I", len(q)) + q)
    path = os.path.join(HERE, "layer_qoi.bin")
    with open(path, "wb") as f:
        f.write(b"".join(out))
    print(f"{len(all_)} cases to {path}")


if __name__ == "__main__":
    main()
