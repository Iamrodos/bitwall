"""The golden frames for `frame.rs`: inputs, and the bytes Python makes.

Each case is a frame as arrays, perhaps the frame before it, and the bytes
`bitwall.stream.serialise` writes for it at each depth, diffed and in
full. The Rust test reads them back and asks for the same bytes.

    uv run --no-project --with numpy python bitwall/rust/tests/golden/frame.py

The file, `frame.bin`, little-endian throughout:

    b"BWFR"  u32 cases
    case:    u16 name length, name
             u32 cols, u32 rows, cur
             u8 has prev; if so u32 cols, u32 rows, prev
             u8 outputs; each u8 full, u8 depth, u32 length, bytes
    frame:   u32 ch[n], i32 fg[n], i32 bg[n], u8 attrs[n], row-major

depth is an index into DEPTHS below, which is `Depth`'s order in Rust.
"""
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", ".."))

from bitwall import stream                      # noqa: E402
from bitwall.arrays import Frame                 # noqa: E402
from bitwall.grid import wide                    # noqa: E402

DEPTHS = ("true", "256", "16", "grey", "mono")
rng = np.random.default_rng(20261011)


def blank(cols, rows):
    return (np.full((rows, cols), 32, np.int32),
            np.full((rows, cols), -1, np.int32),
            np.full((rows, cols), -1, np.int32),
            np.zeros((rows, cols), np.uint8))


def frame(a):
    ch, fg, bg, at = a
    rows, cols = ch.shape
    return Frame(ch, fg, bg, at, cols, rows)


def copy(a):
    return tuple(x.copy() for x in a)


def colours(shape, p_default=0.1):
    c = rng.integers(0, 1 << 24, shape, dtype=np.int32)
    c[rng.random(shape) < p_default] = -1
    return c


CHARS = np.array([ord(c) for c in
                  " abcXYZ019.:#█▀▄▌▐▖▗▘▝░▒▓─│┌┼╭╮◢◣⠁⣿▪é€"]
                 + [0x1CD00, 0x1CD10, 0x1FB00, 0x10348], np.int32)
WIDE = [0x4E00, 0x4E8C, 0xAC00, 0xFF21, 0x1F600, 0x1F308, 0x1F914, 0x3042]


def randomframe(cols, rows, p_default=0.1, attrs=True, wides=0.0):
    ch = CHARS[rng.integers(0, CHARS.size, (rows, cols))]
    at = (rng.integers(0, 64, (rows, cols), dtype=np.uint8) if attrs
          else np.zeros((rows, cols), np.uint8))
    a = (ch.astype(np.int32), colours((rows, cols), p_default),
         colours((rows, cols), p_default), at)
    if wides:
        put_wides(a, wides)
    return a


def put_wides(a, p):
    ch = a[0]
    rows, cols = ch.shape
    for y in range(rows):
        x = 0
        while x < cols - 1:
            if rng.random() < p:
                ch[y, x] = WIDE[rng.integers(0, len(WIDE))]
                ch[y, x + 1] = 0
                x += 2
            else:
                x += 1


def scatter(a, n, wides=False):
    """n cells changed at random, in one of their arrays each."""
    a = copy(a)
    rows, cols = a[0].shape
    for _ in range(n):
        y, x = rng.integers(0, rows), rng.integers(0, cols)
        k = rng.integers(0, 4)
        if a[0][y, x] == 0 and k == 0:
            k = 1                       # keep the tail a tail
        if k == 0:
            if not (x + 1 < cols and a[0][y, x + 1] == 0):
                a[0][y, x] = CHARS[rng.integers(0, CHARS.size)]
        elif k == 1:
            a[1][y, x] = rng.integers(-1, 1 << 24)
        elif k == 2:
            a[2][y, x] = rng.integers(-1, 1 << 24)
        else:
            a[3][y, x] = rng.integers(0, 64)
    return a


def gradient(cols, rows, t):
    """A film-like frame: half blocks over a moving gradient."""
    yy, xx = np.mgrid[0:rows, 0:cols]
    r = ((xx * 255 // max(1, cols - 1)) + t) % 256
    g = ((yy * 2 * 255 // max(1, 2 * rows - 1)) + t // 2) % 256
    b = ((xx + yy + t) * 3) % 256
    g2 = ((((yy * 2 + 1) * 255) // max(1, 2 * rows - 1)) + t // 2) % 256
    fg = (r << 16) | (g << 8) | b
    bg = (r << 16) | (g2 << 8) | b
    ch = np.full((rows, cols), ord("▀"), np.int32)
    return (ch, fg.astype(np.int32), bg.astype(np.int32),
            np.zeros((rows, cols), np.uint8))


def text(a, x, y, s, fg=-1, bg=-1, at=0):
    """Text as `Frame::text` writes it."""
    ch = a[0]
    rows, cols = ch.shape
    if y >= rows:
        return
    for c in s:
        if x >= cols:
            break
        ch[y, x], a[1][y, x], a[2][y, x], a[3][y, x] = ord(c), fg, bg, at
        if wide(ord(c)):
            if x + 1 < cols:
                ch[y, x + 1] = 0
                a[1][y, x + 1], a[2][y, x + 1], a[3][y, x + 1] = fg, bg, at
            x += 2
        else:
            x += 1


def dashboard(t):
    a = blank(120, 40)
    text(a, 2, 1, "Kitchen  %02d:%02d" % (9 + t // 60, t % 60), 0xffcc00, -1, 1)
    text(a, 2, 3, "温度 21.5°C  湿度 48%  🌤  晴れ", 0x88ccff, 0x102030)
    for i in range(10):
        text(a, 4, 6 + i, "%-20s %6.1f" % ("row %d" % i, (i * 7.3 + t) % 100),
             -1 if i % 2 else 0xd0d0d0, 0x202020 if i % 3 == 0 else -1,
             (i * 5) % 64)
    text(a, 0, 39, "─" * 120, 0x444444)
    text(a, 110, 20, "端の文字列", 0xff00ff)         # clipped at the edge
    return a


cases = []
DIFF = [(False, d) for d in range(5)]


def add(name, cur, prev=None, outs=None):
    if outs is None:
        outs = [(full, d) for full in (False, True) for d in range(5)]
    cases.append((name, cur, prev, outs))


# Every colour the reducers meet at a tie, and the greys.
def colour_sweep():
    vals = []
    for v in range(256):
        vals.append((v << 16) | (v << 8) | v)
    for lv in (0, 47, 48, 95, 115, 135, 155, 175, 195, 215, 235, 255):
        for o in (lv, 0, 255, 128):
            vals.append((lv << 16) | (o << 8) | (255 - lv))
            vals.append((o << 16) | (lv << 8) | o)
    for i in range(16):
        r, g, b = stream._BASIC[i]
        vals.append((r << 16) | (g << 8) | b)
    vals += rng.integers(0, 1 << 24, 3000).tolist()
    vals.append(-1)
    n = len(vals)
    cols = 64
    rows = (n + cols - 1) // cols
    a = blank(cols, rows)
    flat = np.array(vals + [-1] * (cols * rows - n), np.int32)
    a[1][:] = flat.reshape(rows, cols)
    a[2][:] = np.roll(flat, 7).reshape(rows, cols)
    a[0][:] = ord("x")
    return a


add("zero", blank(0, 0))
add("one", randomframe(1, 1))
add("small", randomframe(10, 5))
f = randomframe(80, 24, wides=0.05)
add("tty", f)
add("tty-same", f, copy(f), DIFF + [(True, 0)])
add("tty-sparse", scatter(f, 40), f, DIFF)
add("tty-dense", scatter(f, 1500), f, DIFF)
g = copy(f)
g[0][0, 0] = ord("Q")
add("corner-tl", g, f, DIFF)
g = copy(f)
g[3][23, 79] ^= 1
add("corner-br", g, f, DIFF)
g = copy(f)
g[1][12, 40] = -1 if g[1][12, 40] != -1 else 5
add("middle", g, f, DIFF)
# A run of only a wide glyph's second half.
w = blank(30, 3)
text(w, 4, 1, "漢字🌈", 0xffffff)
w2 = copy(w)
w2[2][1, 5] = 0x123456
add("tail-only", w2, w)
w3 = copy(w)
text(w3, 4, 1, "ab", 0xffffff)
add("wide-to-narrow", w3, w)
# Gaps of 5 to 9 unchanged cells between changes, which join or split.
gp = blank(80, 6)
gp2 = copy(gp)
for y, gap in enumerate((5, 6, 7, 8, 9, 0)):
    gp2[0][y, 2] = ord("A")
    gp2[0][y, 3 + gap] = ord("B")
    gp2[0][y, 3 + gap + gap + 1] = ord("C")
gp2[0][5, 79] = ord("E")
add("gaps", gp2, gp)
add("reshaped", randomframe(20, 6), randomframe(21, 6))
add("no-attrs", randomframe(40, 10, p_default=0.5, attrs=False))
add("all-default", blank(50, 8), randomframe(50, 8))
d0 = dashboard(5)
add("dashboard", d0)
add("dashboard-tick", dashboard(6), d0, DIFF)
add("colours", colour_sweep(), None, DIFF)
add("supplementary", randomframe(25, 4, wides=0.3))
big = randomframe(300, 40, wides=0.02)
add("big-sparse", scatter(big, 500), big, DIFF)
add("big-full", big, None, [(True, 0)])
g0 = gradient(160, 50, 0)
add("film", gradient(160, 50, 3), g0, [(False, 0), (False, 1), (False, 2)])
r = randomframe(120, 40, wides=0.01)
add("random-mid", scatter(r, 2000), r, DIFF)


def write_frame(out, a):
    ch, fg, bg, at = a
    out.write(np.ascontiguousarray(ch, "<u4").tobytes())
    out.write(np.ascontiguousarray(fg, "<i4").tobytes())
    out.write(np.ascontiguousarray(bg, "<i4").tobytes())
    out.write(np.ascontiguousarray(at, "u1").tobytes())


path = os.path.join(HERE, "frame.bin")
total = 0
with open(path, "wb") as out:
    out.write(b"BWFR" + struct.pack("<I", len(cases)))
    for name, cur, prev, outs in cases:
        nb = name.encode()
        out.write(struct.pack("<H", len(nb)) + nb)
        rows, cols = cur[0].shape
        out.write(struct.pack("<II", cols, rows))
        write_frame(out, cur)
        out.write(struct.pack("<B", prev is not None))
        if prev is not None:
            prows, pcols = prev[0].shape
            out.write(struct.pack("<II", pcols, prows))
            write_frame(out, prev)
        out.write(struct.pack("<B", len(outs)))
        fc = frame(cur)
        fp = frame(prev) if prev is not None else None
        for full, d in outs:
            got = stream.serialise(fc, fp, full, cols, rows,
                                   DEPTHS[d]).encode("utf-8")
            # The arrays path is held to the grid path while we are here,
            # where the two agree: a reshaped prev the arrays path redraws.
            ref = stream.serialise(fc.cells(), fp.cells() if fp else None,
                                   full, cols, rows, DEPTHS[d]).encode("utf-8")
            assert got == ref or (fp is not None
                                  and fp.ch.shape != fc.ch.shape), (name, full, d)
            out.write(struct.pack("<BBI", full, d, len(got)) + got)
            total += 1
print("%d cases, %d outputs, %d bytes" % (len(cases), total,
                                          os.path.getsize(path)))
