"""The golden pictures for `pixels.rs`: framebuffers, and the cells Python
makes of them.

The reference is the video sender's painting,
`senders/games/cellfilm/player.py`, which `senders/picture` paints with
too: `half`, `quad`, `braille` and `fine` are its own methods, called as
they are. Sextants and octants it does not draw, so they are its `fine`
search -- every brightness-ordered split of the tile, scored by the error
it leaves -- over a 2x3 or 2x4 tile, with the one character that draws
each pattern (`octant` below is `senders/sysinfo`'s, the receivers'
order). That search is written here for any tile, and checked against
`fine` before it is trusted.

    uv run --no-project --with numpy python bitwall/rust/tests/golden/pixels.py

The file, `pixels.bin`, little-endian throughout:

    b"BWPX"  u32 cases
    case:    u16 name length, name
             u8 mode, u32 w, u32 h, u32 cols, u32 rows
             rgb[w * h * 3]
             u32 ch[n], i32 fg[n], i32 bg[n], row-major, n = cols * rows

mode is an index into MODES below, which is `Mode`'s order in Rust.
"""
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "senders",
                                "games", "cellfilm"))

import player                                    # noqa: E402

MODES = ("flat", "half", "quad", "sextant", "octant", "braille", "fine")
TILE = {"half": (1, 2), "quad": (2, 2), "sextant": (2, 3), "octant": (2, 4),
        "braille": (2, 4), "fine": (2, 4)}
rng = np.random.default_rng(20261011)

#: The 2x4 patterns other characters draw, which the octants skip
#: (`senders/sysinfo/main.py`). Bit 2r+c is row r, column c.
NOT_OCTANTS = {0: " ", 1: "\U0001CEA8", 2: "\U0001CEAB", 3: "\U0001FB82", 5: "▘",
               10: "▝", 15: "▀", 20: "\U0001FBE6", 40: "\U0001FBE7",
               63: "\U0001FB85", 64: "\U0001CEA3", 80: "▖", 85: "▌", 90: "▞",
               95: "▛", 128: "\U0001CEA0", 160: "▗", 165: "▚", 170: "▐", 175: "▜",
               192: "▂", 240: "▄", 245: "▙", 250: "▟", 252: "▆", 255: "█"}


def octant(bits):
    if bits in NOT_OCTANTS:
        return NOT_OCTANTS[bits]
    return chr(0x1CD00 + bits - sum(1 for b in NOT_OCTANTS if b < bits))


def sextant(bits):
    """The receivers' order: every 2x3 pattern but the four others draw."""
    other = {0: " ", 21: "▌", 42: "▐", 63: "█"}
    if bits in other:
        return other[bits]
    return chr(0x1FB00 + bits - 1 - (bits > 21) - (bits > 42))


OCTANT_IX = np.array([player._g(octant(m)) for m in range(256)], np.int64)
SEXTANT_IX = np.array([player._g(sextant(m)) for m in range(64)], np.int64)
POINTS = np.array([ord(c) for c in player.GLYPHS], np.int64)


class Painter(player.Player):
    """The player's painting, without a film."""

    def __init__(self, tile):
        self.tile = tile

    def pack_best(self, data, cols, rows, table):
        """`_pack_fine` for a tile of any size."""
        t = self._tiles(data, cols, rows)
        n = t.shape[2]
        lum = self._luma(t)
        order = np.argsort(lum, axis=2)
        ts = np.take_along_axis(t, order[..., None], axis=2).astype(np.int64)
        cum = ts.cumsum(2)
        tot = cum[:, :, n - 1, :]
        dark = cum[:, :, :n - 1, :]
        light = tot[:, :, None, :] - dark
        nd = np.arange(1, n, dtype=np.int64)
        score = ((dark * dark).sum(3) / nd + (light * light).sum(3) / (n - nd))
        bn = score.argmax(2) + 1
        rank = np.empty_like(order)
        np.put_along_axis(rank, order, np.arange(n, dtype=order.dtype), axis=2)
        sel = rank >= bn[..., None]
        flat, mask, fg, bg, mean = self._split(t, lum, sel)
        return self._finish(flat, mask, fg, bg, mean, table)


def flat(data, cols, rows):
    a = np.frombuffer(data, np.uint8).reshape(rows, cols, 3).astype(np.int64)
    return (np.zeros((rows, cols), np.int64) << player._CH) | \
        ((a[..., 0] << 16) + (a[..., 1] << 8) + a[..., 2] + 1)


def paint(mode, data, cols, rows):
    if mode == "flat":
        return flat(data, cols, rows)
    p = Painter(TILE[mode])
    if mode == "sextant":
        return p.pack_best(data, cols, rows, SEXTANT_IX)
    if mode == "octant":
        return p.pack_best(data, cols, rows, OCTANT_IX)
    return getattr(p, "_pack_" + mode)(data, cols, rows)


def unpack(key):
    ch = POINTS[key >> player._CH]
    fg = ((key >> player._FG) & player._MASK25) - 1
    bg = (key & player._MASK25) - 1
    return ch.astype(np.uint32), fg.astype(np.int32), bg.astype(np.int32)


# ------------------------------------------------------------- pictures

def noise(w, h):
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def palette(w, h, k):
    pal = rng.integers(0, 256, (k, 3), dtype=np.uint8)
    return pal[rng.integers(0, k, (h, w))]


def ties(w, h):
    """Colours of one brightness but different hues, so a split falls
    inside a run of equal brightness and the order among them counts."""
    out = []
    for target in (40, 90, 140, 200):
        for _ in range(4):
            for _ in range(100):
                r, b = rng.integers(0, 256, 2)
                g = (target * 256 + 128 - 77 * r - 28 * b) // 151
                if 0 <= g < 256 and (77 * r + 151 * g + 28 * b) >> 8 == target:
                    out.append((r, g, b))
                    break
    pal = np.array(out, np.uint8)
    return pal[rng.integers(0, len(pal), (h, w))]


def gradient(w, h):
    yy, xx = np.mgrid[0:h, 0:w]
    r = xx * 255 // max(1, w - 1)
    g = yy * 255 // max(1, h - 1)
    b = (xx + yy) * 2 % 256
    return np.stack([r, g, b], -1).astype(np.uint8)


def shapes(w, h):
    """Hard edges on black: discs and boxes, the way a game draws."""
    a = np.zeros((h, w, 3), np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(12):
        c = rng.integers(0, 256, 3, dtype=np.uint8)
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(1, 2 + max(w, h) // 4)
        if rng.random() < 0.5:
            a[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = c
        else:
            a[max(0, cy - r):cy + r, max(0, cx - r):cx + r] = c
    return a


def solid(w, h, c):
    return np.broadcast_to(np.array(c, np.uint8), (h, w, 3)).copy()


def soft(w, h):
    """A gradient with a little noise, so cells sit either side of FLAT."""
    g = gradient(w, h).astype(np.int32) + rng.integers(-6, 7, (h, w, 3))
    return np.clip(g, 0, 255).astype(np.uint8)


PICTURES = {
    "noise": noise,
    "palette2": lambda w, h: palette(w, h, 2),
    "palette5": lambda w, h: palette(w, h, 5),
    "ties": ties,
    "gradient": gradient,
    "soft": soft,
    "shapes": shapes,
    "black": lambda w, h: solid(w, h, (0, 0, 0)),
    "white": lambda w, h: solid(w, h, (255, 255, 255)),
}
SIZES = [(1, 1), (7, 3), (40, 12), (61, 17)]


def main():
    # The search written for any tile is `fine` at 2x4.
    p = Painter((2, 4))
    for name, make in PICTURES.items():
        data = make(80, 80).tobytes()
        assert np.array_equal(p.pack_best(data, 40, 20, player.FINE_IX),
                              p._pack_fine(data, 40, 20)), name

    cases = []
    for mode in MODES:
        tw, th = TILE.get(mode, (1, 1))
        for pname, make in PICTURES.items():
            for cols, rows in SIZES:
                if pname in ("black", "white") and cols > 7:
                    continue
                w, h = cols * tw, rows * th
                rgb = make(w, h)
                key = paint(mode, rgb.tobytes(), cols, rows)
                cases.append(("%s-%s-%dx%d" % (mode, pname, cols, rows),
                              MODES.index(mode), w, h, cols, rows,
                              rgb.tobytes(), unpack(key)))

    out = bytearray(b"BWPX")
    out += struct.pack("<I", len(cases))
    for name, mode, w, h, cols, rows, rgb, (ch, fg, bg) in cases:
        nb = name.encode()
        out += struct.pack("<H", len(nb)) + nb
        out += struct.pack("<BIIII", mode, w, h, cols, rows)
        out += rgb
        out += ch.astype("<u4").tobytes()
        out += fg.astype("<i4").tobytes()
        out += bg.astype("<i4").tobytes()
    path = os.path.join(HERE, "pixels.bin")
    with open(path, "wb") as f:
        f.write(out)
    print("%d cases, %d bytes, %s" % (len(cases), len(out), path))


if __name__ == "__main__":
    main()
