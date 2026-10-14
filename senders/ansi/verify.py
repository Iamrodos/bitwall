#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["pillow"]
# ///
"""Check a parse against the renders the scene itself uses.

    senders/ansi/verify.py media/ansi/pack

16colo.rs publishes a rendered PNG beside every file, made by the tool the
scene agrees is right. That makes this the rare case where correctness is
answerable rather than a matter of opinion: parse a piece, render the
canonical image down to one number per row, and see where they disagree.

Comparing whole images would need our fonts and theirs to match. Comparing
per row -- how much of each row is drawn on -- does not, and it finds the
failures that matter: a row of art we left blank, or content landing a row
out because a wrap went the wrong way.
"""
import os
import sys
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import art                                                    # noqa: E402

#: How much a row may differ before it is called wrong. A cell drawn as a
#: light shade covers a quarter of its box and one drawn solid covers all
#: of it, so the two measures never agree exactly; what matters is a row
#: that is empty in one and not the other.
SLACK = 0.18

CACHE = os.path.join(os.path.dirname(_HERE), "..", "media", "ansi", "png")


def canonical(pack, name):
    """The published render, downloaded once and kept."""
    os.makedirs(CACHE, exist_ok=True)
    at = os.path.join(CACHE, name + ".png")
    if not os.path.exists(at):
        url = ("https://16colo.rs/pack/" + pack + "/x1/"
               + urllib.parse.quote(name) + ".png")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                open(at, "wb").write(r.read())
        except Exception as e:
            return None, f"{e}"
    if open(at, "rb").read(8) != b"\x89PNG\r\n\x1a\n":
        return None, "not a png"
    return at, None


def lit_per_row(path, rows, width):
    """How much of each row of the render is drawn on, as a share.

    The reference is drawn in one of two fonts -- the 8x16 of a VGA in
    graphics mode and the 9x16 of one in text mode, which is the same
    glyphs with a column of gap. Which was used is not recorded anywhere,
    so it is worked out from the width the file declared: a 90 pixel
    column count for an 80 column piece means nine.
    """
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    cell = 9 if width and w % width == 0 and w // width == 9 else 8
    out = []
    for y in range(min(rows, h // 16)):
        band = im.crop((0, y * 16, w, y * 16 + 16))
        lit = sum(n for n, c in band.getcolors(maxcolors=1 << 20)
                  if c != (0, 0, 0))
        out.append(lit / (w * 16))
    return out, w // cell, h // 16


#: How much of its box each glyph covers. The blocks and shades are what
#: this art is made of and they differ enormously -- a full block covers
#: all of its cell and a light shade a quarter -- so one number for "has a
#: glyph" cannot stand for both. Everything else is roughly a third.
COVER = {" ": 0.0, "\u2588": 1.0, "\u2593": 0.75, "\u2592": 0.5,
         "\u2591": 0.25, "\u2580": 0.5, "\u2584": 0.5, "\u258c": 0.5,
         "\u2590": 0.5, "\u25a0": 0.6, "\u25ac": 0.3}
OTHER = 0.33


def ours_per_row(rows, width):
    """The same measure over our cells: how much of each row is lit.

    A cell is its glyph in the foreground over the rest in the background,
    so what is lit is the glyph's share if the ink is not black plus the
    remainder if the paper is not. Matching the reference's own measure is
    the point -- a number that means the same thing on both sides.
    """
    out = []
    for row in rows:
        lit = 0.0
        for ch, fg, bg, _bold in row:
            share = COVER.get(ch, OTHER)
            if fg:
                lit += share
            if bg:
                lit += 1.0 - share
        out.append(lit / width)
    return out


def check(path, pack):
    name = os.path.basename(path)
    raw = open(path, "rb").read()
    rows, width = art.parse(raw)
    png, why = canonical(pack, name)
    if png is None:
        return name, None, f"no reference ({why})"
    theirs, ref_w, ref_h = lit_per_row(png, len(rows) + 8, width)
    mine = ours_per_row(rows, width)
    notes = []
    if ref_w != width:
        notes.append(f"width {width} vs {ref_w}")
    if abs(ref_h - len(rows)) > 1:
        notes.append(f"height {len(rows)} vs {ref_h}")
    bad = [y for y in range(min(len(mine), len(theirs)))
           if abs(mine[y] - theirs[y]) > SLACK]
    return name, bad, "; ".join(notes)


def main():
    where = sys.argv[1] if len(sys.argv) > 1 else "media/ansi/pack"
    pack = sys.argv[2] if len(sys.argv) > 2 else \
        "blocktronics-20th-century-blocks"
    files = sorted(f for f in os.listdir(where) if f.lower().endswith(".ans"))
    worst = 0
    for f in files:
        name, bad, note = check(os.path.join(where, f), pack)
        if bad is None:
            print(f"  {name[:34]:36s} -- {note}")
            continue
        worst = max(worst, len(bad))
        mark = "ok " if not bad else f"{len(bad):3d} rows differ"
        extra = f"  {note}" if note else ""
        print(f"  {name[:34]:36s} {mark}{extra}"
              + (f"  first at {bad[0]}" if bad else ""))
    print(f"\n  worst piece: {worst} rows")
    return 0 if worst == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
