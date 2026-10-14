"""The golden letters for `letters.rs`: what `bitwall.letters` draws.

Each case is a call -- `rows` (with `size`), `draw`, `fit`, `centred` or
`plain` -- with its text, its options as a style and perhaps a wall, and
what Python gave back. The Rust test makes the same calls and asks for the
same cells.

    python3 bitwall/rust/tests/golden/letters.py

The wall is a fake with what letters reads of one: `can`, `offers`,
`screen()["cell"]`, `font_size` and `depth`. TILE and the faces are drawn
as Python draws them without Pillow, which is how Rust draws them always.
Every case starts with letters' caches of drawn text empty, so each answer
is the one its call makes and not one remembered from another.

The file, `letters.json`, is a list of cases:

    {"op", "text", "style": {font, fg, bg, scale, height, spacing, turn,
     align, smooth, depth, cell, ghost, edges, px}, "wall": null or
     {glyphs, depth, cell, font_size, kitty}, ..., "want"}

Colours are 0xRRGGBB, -1 for the terminal's own, null for none. Drawn
cells are a palette, [cp, fg, bg, faint] with entry 0 null, and rows as
strings, a character a cell: chr(0x30 + its palette index).
"""
import json
import os
import random
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", ".."))

from bitwall import letters as L                  # noqa: E402
from bitwall import protocol                      # noqa: E402
from bitwall.cells import cell as _cell           # noqa: E402

L._pillow_ok = False            # draw TILE and the faces as Rust does
rng = random.Random(20261011)

CELL_FONTS = ["block", "half", "dots", "segment", "segment_cut", "segment7",
              "segment7_cut"]
PICTURED = ["tile", "white_rabbit", "256_bytes", "vt323"]

TEXTS = ["12:30", "Hello there", "ON AIR", "Cezanne café", "Straße ﬁx",
         "a\nbc", "", " ", "↑↓←→", "Ab9!?%", "WiiW mm", "0123456789",
         "“Quote” — dash… ×·•", "x", "Line one\nLine two", "HELLO, WORLD!",
         "q.j:g;y'", "-_+*/\\=<>()[]", "$#&@\"", "ÀÉÎÕÜ ñ ç", "21.5°C",
         "3:07 PM", "日本", "tab\there", "1\n\n2", "  pad  ", "I", "88:88"]

ASCII = [32, 126]
HALVES = [0x2580, 0x259F]
DIAG = [0x25E2, 0x25E5]
BRAILLE = [0x2800, 0x28FF]
WALLS = [
    None,
    {"glyphs": [], "depth": "true", "cell": None, "font_size": None, "kitty": False},
    {"glyphs": [ASCII, HALVES], "depth": "256", "cell": [8, 16], "font_size": None, "kitty": False},
    {"glyphs": [ASCII, HALVES, DIAG, BRAILLE], "depth": "true", "cell": [4, 8], "font_size": None, "kitty": False},
    {"glyphs": [ASCII], "depth": "16", "cell": None, "font_size": 14, "kitty": False},
    {"glyphs": [], "depth": "grey", "cell": None, "font_size": 9, "kitty": False},
    {"glyphs": [], "depth": "mono", "cell": [12, 26], "font_size": None, "kitty": True},
    {"glyphs": [ASCII, HALVES, BRAILLE], "depth": "256", "cell": None, "font_size": 20, "kitty": False},
]


class Fake:
    """A wall as letters reads one."""

    def __init__(self, d):
        self.spans = [tuple(r) for r in d["glyphs"]]
        self.depth = d["depth"]
        self.cell = d["cell"]
        self.font_size = d["font_size"]
        self.kitty = d["kitty"]

    def can(self, *text):
        return all(protocol.covers(self.spans, t) for t in text)

    def offers(self, word):
        return word == "kitty" and self.kitty

    def screen(self):
        return {"cell": tuple(self.cell)} if self.cell else {}


def hexed(c):
    return None if c is None else "default" if c == -1 else "%06x" % c


def unhex(c):
    if c is None:
        return None
    if c == "default":
        return -1
    return int(c, 16)


def style(**over):
    s = {"font": "block", "fg": 0xffffff, "bg": None, "scale": 1,
         "height": None, "spacing": "proportional", "turn": None,
         "align": None, "smooth": False, "depth": None, "cell": None,
         "ghost": None, "edges": None, "px": None}
    s.update(over)
    return s


def kw(st, wall, with_wall=True):
    """A style as Python's keywords, given only where it says."""
    o = {"scale": st["scale"], "spacing": st["spacing"], "smooth": st["smooth"]}
    for k in ("height", "turn", "align", "depth", "cell", "edges"):
        if st[k] is not None:
            o[k] = st[k]
    if st["ghost"] is not None:
        o["ghost"] = hexed(st["ghost"])
    if st["px"] is not None:
        o["px"] = tuple(st["px"])
    if with_wall and wall is not None:
        o["wall"] = Fake(wall)
    return o


def cells(out):
    pal, index, grid = [None], {None: 0}, []
    for line in out:
        s = []
        for c in line:
            key = None if c is None else (ord(c[0]), unhex(c[1]), unhex(c[2]),
                                          bool(len(c) > 3 and c[3]))
            if key not in index:
                index[key] = len(pal)
                pal.append(list(key))
            assert index[key] < 0xD000
            s.append(chr(0x30 + index[key]))
        grid.append("".join(s))
    return {"pal": pal, "grid": grid}


def fresh():
    L._drawn.clear()
    L._cells.clear()


# The frame draw and centred start from: '.' in grey on a ground of a few
# colours and the terminal's own, by (x + 3y) % len(BGS).
BGS = [0x000000, 0x203040, -1, 0x806020]


def grid_of(cols, rows):
    return [[_cell(".", "808080", hexed(BGS[(x + 3 * y) % len(BGS)]))
             for x in range(cols)] for y in range(rows)]


def norm(c):
    return [ord(c[0]), unhex(c[1]), unhex(c[2]), 32 if len(c) > 8 and c[8] else 0]


def changes(grid, cols, rows):
    before = grid_of(cols, rows)
    out = []
    for y in range(rows):
        for x in range(cols):
            a, b = norm(before[y][x]), norm(grid[y][x])
            if a != b:
                out.append([y * cols + x] + b)
    return out


def fitted(got):
    if got is None:
        return None
    return {"text": got["text"], "font": got["font"], "height": got["height"],
            "px": list(got["px"]) if got.get("px") else None,
            "depth": got.get("depth"), "align": got.get("align")}


def pick(choices, weights=None):
    return rng.choices(choices, weights)[0]


def random_style(font):
    return style(
        font=font,
        fg=pick([0xffd75f, 0xffffff, 0x00ff00, 0x123456, -1], [4, 4, 2, 2, 1]),
        bg=pick([None, 0x000000, 0x202040, -1], [6, 2, 2, 1]),
        scale=pick([1, 2, 3], [7, 3, 1]),
        height=pick([None, rng.randint(1, 14)], [1, 1]),
        spacing=pick(["proportional", "mono"]),
        turn=pick([None, "up", "down"], [6, 1, 1]),
        align=pick([None, "left", "centre", "right"], [6, 1, 2, 1]),
        smooth=rng.random() < 0.4,
        depth=pick([None, "true", "256", "16", "grey", "mono"], [6, 1, 2, 1, 1, 1]),
        cell=pick([None, 1.5, 2.0, 2.2, 2.75], [8, 1, 1, 1, 1]),
        ghost=pick([None, 0x332211, 0x404040, 0x101010, 0x806040, -1], [6, 2, 2, 1, 1, 1]),
        edges=pick([None, "square", "cut"], [6, 1, 3]),
        px=pick([None, [6, 13], [8, 16], [10, 20], [4, 8], [12, 26], [5, 10]], [10, 1, 1, 1, 1, 1, 1]),
    )


def small_enough(st, text):
    """Keep the file small: big segment text only now and then. Text with
    no characters, turned, Python cannot measure (its height never grows)."""
    if st["turn"] and not text.replace("\n", ""):
        return False
    lines = text.split("\n")
    n = max(len(line) for line in lines)
    if st["font"] in ("segment", "segment_cut", "segment7", "segment7_cut"):
        if st["height"] is None and st["scale"] > 1 and n > 5:
            return False
    return True


cases = []


def case_rows(text, st, wall):
    fresh()
    o = kw(st, wall)
    got = L.rows(text, st["font"], hexed(st["fg"]), hexed(st["bg"]), **o)
    fresh()
    sz = L.size(text, st["font"], **o)
    cases.append({"op": "rows", "text": text, "style": st, "wall": wall,
                  "want": cells(got), "size": list(sz)})


def case_draw(text, st, wall, x, y, cols, rows):
    fresh()
    grid = grid_of(cols, rows)
    got = L.draw(grid, text, x, y, hexed(st["fg"]), hexed(st["bg"]),
                 font=st["font"], **kw(st, wall))
    cases.append({"op": "draw", "text": text, "style": st, "wall": wall,
                  "x": x, "y": y, "cols": cols, "rows": rows,
                  "want": changes(grid, cols, rows), "size": list(got)})


def case_fit(text, st, wall, fonts, cols, rows, wrap):
    fresh()
    o = kw(st, None)
    got = L.fit(text, cols, rows, fonts=tuple(fonts), wall=None if wall is None else Fake(wall),
                wrap=wrap, **o)
    drawn = None
    if got is not None:
        fresh()
        rest = {k: v for k, v in got.items() if k not in ("text", "font")}
        drawn = cells(L.rows(got["text"], got["font"], hexed(st["fg"]), hexed(st["bg"]), **rest))
    cases.append({"op": "fit", "text": text, "style": st, "wall": wall,
                  "fonts": fonts, "cols": cols, "rows": rows, "wrap": wrap,
                  "want": fitted(got), "drawn": drawn})


def case_centred(text, st, wall, fonts, cols, rows, margin, wrap):
    fresh()
    grid = grid_of(cols, rows)
    o = kw(st, None)
    got = L.centred(grid, text, hexed(st["fg"]), hexed(st["bg"]), fonts=tuple(fonts),
                    wall=None if wall is None else Fake(wall), margin=margin,
                    wrap=wrap, **o)
    cases.append({"op": "centred", "text": text, "style": st, "wall": wall,
                  "fonts": fonts, "cols": cols, "rows": rows, "margin": margin,
                  "wrap": wrap, "want": changes(grid, cols, rows),
                  "fitted": fitted(got)})


# plain: every text in every font, and every character that folds.
folds = "".join(chr(cp) for cp in range(0x80, 0x110000)
                if unicodedata.normalize("NFKD", chr(cp)).encode("ascii", "ignore"))
for font in CELL_FONTS + PICTURED:
    for text in TEXTS + [folds, "ŉ ǰ ﬀ ß ſ ǅ İ"]:
        cases.append({"op": "plain", "text": text, "font": font, "want": L.plain(text, font)})

# Each font plainly, and with each option alone, on a few texts.
ALONE = [{}, {"edges": "cut"}, {"edges": "square"}, {"ghost": 0x332211},
         {"smooth": True}, {"turn": "up"}, {"turn": "down"},
         {"spacing": "mono"}, {"height": 7}, {"height": 12}, {"scale": 2},
         {"align": "centre"}, {"align": "right"}, {"cell": 2.75},
         {"px": [6, 13]}, {"depth": "256", "ghost": 0x404040},
         {"depth": "grey", "ghost": 0x404040}, {"depth": "16", "ghost": 0x404040},
         {"bg": 0x202040, "smooth": True}, {"spacing": "mono", "ghost": 0x333333},
         {"smooth": True, "height": 5}, {"smooth": True, "height": 15, "px": [10, 20]},
         {"edges": "cut", "turn": "up"}, {"edges": "cut", "ghost": 0x222222}]
for font in CELL_FONTS:
    for extra in ALONE:
        for text in ("12:30", "Hi\nthere", "KWXYZ", "AB&?"):
            st = style(font=font, **extra)
            if small_enough(st, text):
                case_rows(text, st, None)

# Every font at every height, upright and turned.
for font in CELL_FONTS:
    for h in range(1, 21):
        for turn in (None, "up"):
            case_rows("Ag8:" if turn is None else "Ag", style(font=font, height=h, turn=turn), None)

# Every character each font has, plain and cut.
for font in CELL_FONTS:
    table = L._chars(font)
    chars = "".join(sorted(table))
    for k in range(0, len(chars), 12):
        for edges in ("square", "cut"):
            case_rows(chars[k:k + 12], style(font=font, edges=edges, ghost=0x303030), None)
            case_rows(chars[k:k + 12], style(font=font, edges=edges, spacing="mono", height=9), None)

# Random styles, walls and texts.
for _ in range(1600):
    font = pick(CELL_FONTS)
    st = random_style(font)
    text = pick(TEXTS)
    if not small_enough(st, text):
        continue
    case_rows(text, st, pick(WALLS))

# draw: into a frame, transparent and not, clipped at every side, and the
# pictured fonts drawn instead.
for _ in range(400):
    font = pick(CELL_FONTS + PICTURED, [3] * 7 + [2, 1, 1, 1])
    st = random_style(font)
    text = pick(TEXTS)
    if not small_enough(st, text):
        continue
    cols, rows = rng.randint(4, 60), rng.randint(2, 24)
    x, y = rng.randint(-6, cols), rng.randint(-4, rows)
    case_draw(text, st, pick(WALLS), x, y, cols, rows)

# fit, and what it draws.
FONT_LISTS = [["half", "block"], ["dots", "half", "block"], ["segment", "block"],
              ["segment7_cut", "segment7"], ["tile", "segment_cut", "dots"],
              ["vt323", "dots", "block"], ["segment_cut", "half"], ["block"],
              ["dots"], ["segment"]]
for _ in range(700):
    st = random_style("block")
    st["height"] = None
    st["scale"] = 1
    if rng.random() < 0.7:
        st["turn"] = None
    text = pick(TEXTS)
    if not small_enough(st, text):
        continue
    cols, rows = rng.randint(1, 70), rng.randint(1, 30)
    case_fit(text, st, pick(WALLS), pick(FONT_LISTS), cols, rows, rng.random() < 0.5)

# centred.
for _ in range(200):
    st = random_style("block")
    st["height"] = None
    st["scale"] = 1
    st["turn"] = None if rng.random() < 0.85 else st["turn"]
    text = pick(TEXTS)
    if not small_enough(st, text):
        continue
    cols, rows = rng.randint(3, 60), rng.randint(1, 24)
    case_centred(text, st, pick(WALLS), pick(FONT_LISTS), cols, rows,
                 pick([0, 0, 1, 2]), rng.random() < 0.7)

path = os.path.join(HERE, "letters.json")
with open(path, "w") as f:
    json.dump(cases, f, ensure_ascii=False, separators=(",", ":"))
print(f"{len(cases)} cases, {os.path.getsize(path)} bytes, {path}")
