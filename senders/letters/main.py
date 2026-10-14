#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""Big text on a wall: bitwall's letters, shown off a scene at a time.

    senders/letters/main.py localhost
    senders/letters/main.py --secs 20 --font 8 pi.local
    senders/letters/main.py --scene spec-segment --hold localhost:letters

A rolling demonstration of `bitwall.letters`: every font at a few sizes,
proportional against mono, turned up and down, a height filled exactly,
the segment font plain, smoothed and with its unlit segments showing,
braille letters beside ordinary text, a clock and a counter whose digits
hold still, and long text fitted to the wall. A band along the foot says,
in the library's own letters and big enough to read across the room,
which scene it is and what it shows, in the words the library uses, and
the wall's font size and grid.

Standard library only, like the library, so it runs on a Pi as it is.
"""
import argparse
import os
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import bitwall                                                  # noqa: E402
from bitwall import cell, letters                               # noqa: E402

DEFAULT_FONT = 10
DEFAULT_SECS = 8

BG = "0b0e14"
LABEL = "6c7a89"          # what a scene is showing, along the foot
TAG = "8394a8"            # the small words beside a sample
GHOST = "1a2130"          # unlit segments: just off the black
SHADE = "161c26"          # the band behind the imitation header

#: Accents, bright enough to read across a room and none of them shouting.
AMBER, CYAN, ROSE, MINT, LILAC, PEACH = (
    "ffd75f", "5fd7ff", "ff87af", "87e7a0", "b8a0ff", "ffaf87")
HUES = (AMBER, CYAN, ROSE, MINT, LILAC, PEACH)

#: Each font as the library spells it, for the labels.
NAMES = {f: f.upper() for f in letters.FONTS}

#: A character LCD's two looks: lit dots over unlit ones just visible.
LCD_BLUE, LCD_BLUE_DIM = "7fe8ff", "12304a"
LCD_AMBER, LCD_AMBER_DIM = "ffc04a", "3a2a08"

#: The scenes in the order they roll, by the short names `--scene` takes.
SCENES = (
    "fonts", "block-sizes", "half-sizes", "dots-sizes", "segment-sizes",
    "spacing", "turned", "filled", "segment-ways", "segment-cut",
    "block-cut", "lcd", "header", "clock", "families", "resolution",
    "wrapped", "lower",
    "spec-block-cut", "spec-half-cut", "spec-segment", "spec-segment7",
    "spec-dots", "spec-dots-ghost", "tile", "faces",
)

#: Each font's test line, the same wherever the font is shown, chosen for
#: what that font has to get right: for the bitmaps counters (D O 0),
#: diagonal steps (S 2 Z), legs and joins (R K) and mixed corners (5 8);
#: for the fourteen segments every diagonal; for the seven, every digit;
#: for the dots lower case, descenders, a gap and digits.
TEST = {
    letters.BLOCK: "DOSRKZ 2580", letters.HALF: "DOSRKZ 2580",
    letters.SEGMENT: "MNKVWXYZ 0123", letters.SEGMENT_CUT: "MNKVWXYZ 0123",
    letters.SEGMENT7: "0123456789 -:", letters.SEGMENT7_CUT: "0123456789 -:",
    letters.DOTS: "Dots gap Qj 0123",
    letters.TILE: "DOSRKZ 2580",
}

#: Every character TILE has, BLOCK's table, in the groups the TILE scene
#: shows them in.
TILE_SET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789",
            ": . , ; ' \" ! ? % + - = *", "/ _ # & ( ) ← → ↑ ↓")

#: The standard characters the faces page shows of each face.
FACE_SET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz",
            "0123456789", "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

LONG = ("A wall is read from across a room, so anything meant to be read "
        "is drawn: each letter a small picture made of cells.")
PANGRAM = ("The quick brown fox jumps over the lazy dog. "
           "0123456789 (!?) & @ # % $ = + - * / ; : \"'")


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def put(grid, x, y, text, fg=TAG):
    """Ordinary small text, clipped to the grid."""
    if not 0 <= y < len(grid):
        return
    row = grid[y]
    for i, ch in enumerate(text):
        if 0 <= x + i < len(row):
            row[x + i] = cell(ch, fg, row[x + i][2])


class Demo:
    """render(cols, rows): the scene the clock says it is, on the area
    the wall reports."""

    def __init__(self, wall, secs, size, first=0, hold=False):
        self.w = wall
        self.secs = secs
        self.size = size
        self.first = first
        self.hold = hold
        self.start = time.monotonic()
        # Asked once: the screen's answer does not change while it runs.
        self.can = {f: letters.usable(f, wall) for f in letters.FONTS}
        by_name = {
            "fonts": self.every_font,
            "block-sizes": lambda g, c, r: self.sizes(g, c, r, letters.BLOCK),
            "half-sizes": lambda g, c, r: self.sizes(g, c, r, letters.HALF),
            "dots-sizes": lambda g, c, r: self.sizes(g, c, r, letters.DOTS),
            "segment-sizes": lambda g, c, r: self.sizes(g, c, r,
                                                        letters.SEGMENT),
            "spacing": self.spacing,
            "turned": self.turned,
            "filled": self.filled,
            "segment-ways": self.segments,
            "segment-cut": self.cut_segments,
            "block-cut": self.cut_blocks,
            "lcd": self.lcd,
            "header": self.beside,
            "clock": self.clock,
            "families": self.families,
            "resolution": self.resolutions,
            "wrapped": self.wrapped,
            "lower": self.lower,
            "spec-block-cut": lambda g, c, r: self.specimen(
                g, c, r, letters.BLOCK, {"edges": "cut"}),
            "spec-half-cut": lambda g, c, r: self.specimen(
                g, c, r, letters.HALF, {"edges": "cut"}),
            "spec-segment": lambda g, c, r: self.specimen(
                g, c, r, letters.SEGMENT, {"ghost": GHOST}),
            "spec-segment7": lambda g, c, r: self.specimen(
                g, c, r, letters.SEGMENT7, {"ghost": GHOST}),
            "spec-dots": lambda g, c, r: self.specimen(
                g, c, r, letters.DOTS, {}),
            "spec-dots-ghost": lambda g, c, r: self.specimen(
                g, c, r, letters.DOTS, {"ghost": LCD_BLUE_DIM,
                                        "spacing": "mono"}, LCD_BLUE),
            "tile": self.tiles,
            "faces": self.faces,
        }
        self.scenes = [by_name[name] for name in SCENES]
        self.shown = -1
        self.cycle = 0
        self.last = None
        self.kept, self.kept_key, self.counting = None, None, None

    # -------------------------------------------------------------- frame

    def live(self, n):
        """What changes within a scene, as a value that changes with it:
        the clock scene's counter in tenths, the header's second, and for
        every scene a tick every two seconds, so a wall that lost its
        picture has it back soon however still the scene is."""
        now = time.monotonic()
        if SCENES[n] == "clock":
            return int(now * 10)
        if SCENES[n] == "header":
            return int(time.time())
        return int(now / 2)

    def __call__(self, cols, rows):
        at, n = self.current()
        # Nothing that shows has changed, so nothing is drawn: a scene
        # standing still costs a comparison, not a picture.
        key = (n, cols, rows, self.w.depth, self.live(n))
        if key == self.last:
            return None
        self.last = key
        if at != self.shown:
            if n == self.first and at:
                self.cycle += 1
            if self.cycle == 0:
                say(f"scene {n + 1}/{len(self.scenes)}", flush=True)
            self.shown = at
        if SCENES[n] == "clock" and cols >= 8 and rows >= 4:
            # The time and the band change once a second: kept, and each
            # tenth only the counter is drawn, over a copy of them.
            whole = (n, cols, rows, self.w.depth, int(time.time()))
            if self.kept_key != whole:
                self.kept = self.scene(n, cols, rows)
                self.kept_key = whole
            grid = [row[:] for row in self.kept]
            tall, _font = self.band(rows)
            self.count(grid[:rows - tall - 1])
            return grid
        return self.scene(n, cols, rows)

    def scene(self, n, cols, rows):
        """Scene n drawn whole, with the band along its foot."""
        grid = [[cell(" ", TAG, BG) for _ in range(cols)] for _ in range(rows)]
        if cols < 8 or rows < 4:
            return grid
        # The foot is the status band's; the scene has the rows above it
        # and a grid only that tall, so nothing it draws can reach under.
        tall, font = self.band(rows)
        label = self.scenes[n](grid[:rows - tall - 1], cols, rows - tall - 1)
        self.status(grid, cols, rows, tall, font,
                    f"{n + 1}/{len(self.scenes)} {SCENES[n]}: {label}",
                    f"font {self.size}  {cols}x{rows}  {self.believed()}")
        return grid

    def current(self):
        """(how many scene changes since the start, the scene to show):
        the chosen one while holding, otherwise rolling on from it."""
        at = 0 if self.hold else int((time.monotonic() - self.start)
                                     // self.secs)
        return at, (self.first + at) % len(self.scenes)

    def band(self, rows):
        """The status band's height in rows, and the font it is set in.

        About three and a half per cent of the wall, so it reads from as
        far as the scenes do, and never under the two rows DOTS takes at
        its smallest, which is already twice ordinary text.
        """
        tall = max(2, round(rows * 0.035))
        if self.can[letters.DOTS]:
            return tall, letters.DOTS
        if self.can[letters.HALF]:
            return max(3, tall), letters.HALF
        return max(5, tall), letters.BLOCK

    def status(self, grid, cols, rows, tall, font, left, right):
        """Which scene, and the wall's font and grid, along the foot."""
        top = rows - tall
        for y in range(max(0, top - 1), rows):
            for x in range(cols):
                grid[y][x] = cell(" ", TAG, SHADE)
        band = grid[top:]
        got = letters.fit(right, cols - 2, tall, fonts=(font,))
        if not got:
            # On a narrow wall the words go and the numbers stay.
            got = letters.fit(right.replace("font ", ""), cols - 2, tall,
                              fonts=(font,))
        used = 0
        if got:
            c, r = letters.size(**got)
            letters.draw(band, x=cols - c - 1, y=(tall - r) // 2, fg=AMBER,
                         **got)
            used = c + 3
        # The scene's words give way before the font and grid do: cut a
        # word at a time from the end until what is left fits.
        words = left.split()
        while words:
            got = letters.fit(" ".join(words), cols - used - 2, tall,
                              fonts=(font,))
            if got:
                c, r = letters.size(**got)
                letters.draw(band, x=1, y=(tall - r) // 2, fg="d0d6de", **got)
                return
            words.pop()

    def ghost_said(self):
        """Which depth the ghosts are chosen for, and what that did."""
        depth = self.depth()
        got = letters.ghost_for(GHOST, AMBER, depth)
        if got is None:
            return f"no ghost at depth {depth}"
        return f"ghost for depth {depth}" + ("" if got == GHOST else
                                             f" as {got}")

    def believed(self):
        """The cell size in pixels the library works to, for the band."""
        px = letters.resolution(self.w)
        return f"cell {px[0]}x{px[1]}px" if px else "cell unknown"

    def font(self, font):
        """The font, or BLOCK where the wall cannot draw it."""
        return font if self.can[font] else letters.BLOCK

    def missing(self, *fonts):
        gone = [NAMES[f] for f in fonts if not self.can[f]]
        return f" (no {', '.join(gone)}: BLOCK)" if gone else ""

    def depth(self):
        return self.w.depth

    def middle(self, grid, got, cols, rows, fg, top=0, left=0):
        """Draw what `fit` chose in the middle of a region."""
        c, r = letters.size(**got)
        return letters.draw(grid, x=left + (cols - c) // 2,
                            y=top + (rows - r) // 2, fg=fg, **got)

    # ------------------------------------------------------------- scenes

    def every_font(self, grid, cols, rows):
        """Each font at its smallest, one under another, each in itself."""
        samples = [(letters.BLOCK, TEST[letters.BLOCK], AMBER),
                   (letters.HALF, TEST[letters.HALF], CYAN),
                   (letters.DOTS, TEST[letters.DOTS], ROSE),
                   (letters.SEGMENT, TEST[letters.SEGMENT], MINT),
                   (letters.SEGMENT7, TEST[letters.SEGMENT7], PEACH)]
        heights = [letters.size(t, self.font(f))[1] for f, t, _ in samples]
        y = max(0, (rows - sum(heights) - 2 * (len(samples) - 1)) // 2)
        for (font, text, hue), tall in zip(samples, heights):
            if y + tall > rows:
                break
            put(grid, 2, y + tall // 2, NAMES[font].lower())
            letters.draw(grid, text, 12, y, hue, font=self.font(font))
            y += tall + 2
        return ("every font at its smallest, each its test line"
                + self.missing(*letters.FONTS))

    def sizes(self, grid, cols, rows, font):
        """One font at the sizes that fit, smallest first."""
        use = self.font(font)
        text = TEST[use]
        if use == letters.SEGMENT:
            steps = [("height", h) for h in (7, 10, 14, 20, 28)]
        else:
            steps = [("scale", s) for s in (1, 2, 3, 4, 6)]
        placed = []
        for key, v in steps:
            c, r = letters.size(text, use, **{key: v})
            if c <= cols - 16 and sum(p[2] + 1 for p in placed) + r <= rows:
                placed.append((key, v, r))
        y = max(0, (rows - sum(p[2] + 1 for p in placed)) // 2)
        for i, (key, v, r) in enumerate(placed):
            put(grid, 2, y + r // 2, f"{key}={v}")
            letters.draw(grid, text, 14, y, HUES[i % len(HUES)], font=use,
                         smooth=True, depth=self.depth(), **{key: v})
            y += r + 1
        how = ("height, any size" if use == letters.SEGMENT
               else f"scale 1-{placed[-1][1] if placed else 1}")
        return f'{NAMES[use]} by {how}, "{text}"' + self.missing(font)

    def spacing(self, grid, cols, rows):
        """The same words proportional and mono, one over the other."""
        use = self.font(letters.DOTS)
        pairs = [(use, "Illicit willow 1111 0808"), (letters.HALF,
                  "LIT WIT 1111 0808")]
        # As big as the widest of them allows, up to twice the smallest.
        scale = 2
        while scale > 1 and any(
                letters.size(t, f, spacing="mono", scale=scale)[0] > cols - 24
                for f, t in pairs):
            scale -= 1
        tall = sum(letters.size(t, f, scale=scale)[1] + 1
                   for f, t in pairs) * 2 + 1
        y = max(0, (rows - tall) // 2)
        for font, text in pairs:
            for i, spacing in enumerate(("proportional", "mono")):
                c, r = letters.size(text, font, spacing=spacing, scale=scale)
                if y + r > rows:
                    break
                put(grid, 2, y + r // 2, f"{NAMES[font].lower()} {spacing}")
                letters.draw(grid, text, 22, y, (CYAN, AMBER)[i], font=font,
                             spacing=spacing, scale=scale)
                y += r + 1
            y += 1
        return ("proportional vs mono, digits one width"
                + self.missing(letters.DOTS))

    def turned(self, grid, cols, rows):
        """A word up the left, a word down the right, and the rest between."""
        strip = max(6, cols // 5)
        up = letters.fit("READS UP", strip, rows - 2, fonts=(letters.HALF,
                         letters.BLOCK), wall=self.w, turn="up")
        down = letters.fit("AND DOWN", strip, rows - 2,
                           fonts=(letters.SEGMENT, letters.HALF), wall=self.w,
                           turn="down", smooth=True, depth=self.depth())
        if up:
            self.middle(grid, up, strip, rows, ROSE, left=2)
        if down:
            self.middle(grid, down, strip, rows, MINT, left=cols - strip - 2)
        mid = letters.fit("turn=\nup | down", cols - 2 * strip - 8, rows,
                          fonts=(self.font(letters.DOTS), letters.BLOCK),
                          wall=self.w, align="centre")
        if mid:
            self.middle(grid, mid, cols - 2 * strip - 8, rows, AMBER,
                        left=strip + 4)
        said = [f"{NAMES[g['font']]} turn={g['turn']}" for g in (up, down) if g]
        return "turned: " + ", ".join(said)

    def filled(self, grid, cols, rows):
        """A number exactly as tall as the room it is given: its own."""
        use = self.font(letters.SEGMENT)
        high = rows - 2
        opts = dict(font=use, height=high, smooth=True, depth=self.depth(),
                    ghost=GHOST)
        text = str(high)
        if letters.size(text, **opts)[0] > cols - 4:
            got = letters.fit(text, cols - 4, high, fonts=(use,),
                              wall=self.w, **{k: v for k, v in opts.items()
                                              if k not in ("font", "height")})
            if not got:
                return "too small to fill"
            opts = {k: v for k, v in got.items() if k != "text"}
        c, r = letters.size(text, **opts)
        letters.draw(grid, text, (cols - c) // 2, (rows - r) // 2, PEACH,
                     **opts)
        return f"{NAMES[use]} height={opts['height']} fills exactly"

    def segments(self, grid, cols, rows):
        """SEGMENT four ways: plain, smoothed, ghosted and both."""
        use = self.font(letters.SEGMENT)
        ways = [("crisp", {}), ("smooth", {"smooth": True}),
                ("ghost", {"ghost": GHOST}),
                ("smooth + ghost", {"smooth": True, "ghost": GHOST})]
        wide, high = (cols - 3) // 2, (rows - 1) // 2
        for i, (tag, opts) in enumerate(ways):
            left = 1 + (i % 2) * (wide + 1)
            top = (i // 2) * (high + 1)
            used = self.tag(grid, left + 1, top, tag)
            got = letters.fit(TEST[use], wide - 8, high - used - 1,
                              fonts=(use,), wall=self.w, depth=self.depth(),
                              **opts)
            if got:
                self.middle(grid, got, wide, high - used, (MINT, CYAN, AMBER,
                            ROSE)[i], top=top + used, left=left)
        return (f"SEGMENT crisp, smooth, ghost (depth {self.depth()})"
                + self.missing(letters.SEGMENT))

    def tag(self, grid, x, y, text):
        """A small label in DOTS, twice ordinary text, or ordinary text
        where braille cannot be drawn. Returns the rows it took."""
        if self.can[letters.DOTS]:
            return letters.draw(grid, text, x, y, TAG, font=letters.DOTS)[1]
        put(grid, x, y, text)
        return 1

    def pair(self, grid, cols, rows, text, fonts, opts, hues):
        """The same text twice, one under the other: square edges and cut,
        each fitted to half the room and labelled."""
        half = (rows - 2) // 2
        said = []
        for i, (font, edges) in enumerate(fonts):
            top = i * (half + 2)
            name = NAMES[font] if font == letters.SEGMENT_CUT else (
                f"{NAMES[font]} edges={edges}")
            used = self.tag(grid, 2, top, name)
            got = letters.fit(text, cols - 4, half - used, fonts=(font,),
                              wall=self.w, wrap=True, align="centre",
                              **dict(opts, **({} if edges is None
                                              else {"edges": edges})))
            if got:
                self.middle(grid, got, cols, half - used, hues[i],
                            top=top + used)
                said.append(f"{name} h={got['height']}")
        return said

    def cut_segments(self, grid, cols, rows):
        """SEGMENT with square edges against SEGMENT_CUT."""
        if not self.can[letters.SEGMENT_CUT]:
            return "SEGMENT_CUT needs ◢◣◤◥, not drawable here"
        said = self.pair(grid, cols, rows, TEST[letters.SEGMENT],
                         [(letters.SEGMENT, "square"),
                          (letters.SEGMENT_CUT, None)],
                         {"ghost": GHOST}, (AMBER, MINT))
        return (f'SEGMENT square against SEGMENT_CUT, "{TEST[letters.SEGMENT]}"'
                if said else "too small")

    def cut_blocks(self, grid, cols, rows):
        """BLOCK and HALF, square edges beside cut ones."""
        wide = (cols - 3) // 2
        said = []
        for i, (font, scale) in enumerate(((letters.BLOCK, 2),
                                           (letters.HALF, 3))):
            if not self.can[font]:
                continue
            for j, edges in enumerate(("square", "cut")):
                left = 1 + j * (wide + 1)
                top = i * (rows // 2)
                high = rows // 2 - 1
                used = self.tag(grid, left + 1, top,
                                f"{NAMES[font]} edges={edges}")
                got = letters.fit(TEST[font], wide - 12, high - used,
                                  fonts=(font,), wall=self.w, edges=edges)
                if got:
                    self.middle(grid, got, wide, high - used,
                                (PEACH, LILAC)[i], top=top + used, left=left)
                    said.append(f"{NAMES[font]} {edges}")
        return (f'BLOCK and HALF, edges square beside cut, '
                f'"{TEST[letters.BLOCK]}"')

    def lcd(self, grid, cols, rows):
        """DOTS with its unlit dots showing: letters in boxes when mono,
        one field behind the line when proportional."""
        if not self.can[letters.DOTS]:
            return "DOTS ghost needs braille, not drawable here"
        half = (rows - 2) // 2
        for i, (spacing, fg, dim, text) in enumerate((
                ("mono", LCD_BLUE, LCD_BLUE_DIM, TEST[letters.DOTS]),
                ("proportional", LCD_AMBER, LCD_AMBER_DIM, TEST[letters.DOTS]))):
            top = i * (half + 2)
            used = self.tag(grid, 2, top, f"DOTS ghost={dim} {spacing}")
            got = letters.fit(text, cols - 4, half - used,
                              fonts=(letters.DOTS,), wall=self.w,
                              spacing=spacing, ghost=dim, wrap=True)
            if got:
                self.middle(grid, got, cols, half - used, fg, top=top + used)
        return (f'DOTS ghost: mono boxes, proportional field, '
                f'"{TEST[letters.DOTS]}"; ' + self.ghost_said())

    def specimen(self, grid, cols, rows, font, opts, hue=None):
        """The test lines at every size the font has, smallest readable
        first, as many as fit, each with its size beside it."""
        if not self.can[font]:
            return f"{NAMES[font]} is not drawable here"
        if font in (letters.SEGMENT, letters.SEGMENT7):
            steps = [("height", h) for h in
                     (7, 8, 9, 10, 12, 14, 17, 20, 24, 30, 38, 48)]
        elif font == letters.DOTS:
            steps = [("height", h) for h in (2, 3, 4, 5, 6, 8, 10, 12, 16)]
        else:
            steps = [("scale", k) for k in (1, 2, 3, 4, 5, 6, 8)]
        y, shown = 0, []
        for key, v in steps:
            size_opts = dict(opts, **{key: v})
            line = TEST[font]
            c, r = letters.size(line, font, **size_opts)
            if y + r > rows or 16 + c > cols - 1:
                break
            letters.draw(grid, line, 16, y, hue or HUES[len(shown) %
                         len(HUES)], font=font, **size_opts)
            put(grid, 1, y + r // 2, f"{key}={v}")
            shown.append(str(v))
            y += r + 1
        said = ", ".join(f"{k}={v}" for k, v in opts.items())
        return (f"{NAMES[font]} specimen{' ' + said if said else ''}, "
                f'"{TEST[font]}": {steps[0][0]} '
                f"{', '.join(shown) or 'none fits'}")

    def tiles(self, grid, cols, rows):
        """TILE: the bitmap as lit tiles, a picture over its cells, at
        heights from two rows, with ordinary text beside it on its rows.

        Asked each time rather than at the start, since a wall says it
        draws kitty only once its caps have come.
        """
        if not letters.usable(letters.TILE, self.w):
            why = ("no Pillow here" if not letters._pillow()
                   else "the wall does not offer kitty")
            return f"TILE is not drawable here: {why}"
        line = TEST[letters.TILE]
        y, shown = 0, []
        for h, hue, ghost in ((2, AMBER, None), (3, CYAN, None),
                              (5, ROSE, None), (5, MINT, "15251c")):
            opts = dict(height=h, wall=self.w)
            if ghost:
                opts["ghost"] = ghost
            c, r = letters.size(line, letters.TILE, **opts)
            if y + r > rows or 12 + c + 18 > cols:
                continue
            put(grid, 1, y + r // 2, f"height={h}")
            letters.draw(grid, line, 11, y, hue, font=letters.TILE, **opts)
            put(grid, 13 + c, y, "text beside it", "d0d6de")
            put(grid, 13 + c, y + 1, f"{c}x{r} cells", TAG)
            if ghost:
                put(grid, 13 + c, y + 2, "ghost: unlit tiles", TAG)
            shown.append(str(h) + (" ghost" if ghost else ""))
            y += r + 1
        # Then every character it has, two rows high, as much as fits:
        # each group broken into lines as wide as the wall allows.
        got = 0
        for group, hue in zip(TILE_SET, (AMBER, CYAN, ROSE, MINT)):
            for part in self.lines_of(group, cols - 13, 2):
                c, r = letters.size(part, letters.TILE, height=2, wall=self.w)
                if y + r > rows:
                    break
                letters.draw(grid, part, 11, y, hue, font=letters.TILE,
                             height=2, wall=self.w)
                y += r + 1
                got += len(part.replace(" ", ""))
        total = sum(len(g.replace(" ", "")) for g in TILE_SET)
        return (f'TILE, a kitty picture over its cells, "{line}": height '
                f"{', '.join(shown) or 'none fits'}; "
                f"{got} of its {total} characters at height 2")

    def faces(self, grid, cols, rows):
        """The outline fonts, each a heading over ordinary text and then its
        character set, as big as lets all three fit the wall: the heading
        and set heights are tried from the largest down."""
        if not letters.usable(letters.VT323, self.w):
            why = ("no Pillow here" if not letters._pillow()
                   else "the wall does not offer kitty")
            return f"the faces are not drawable here: {why}"
        faces = (
            (letters.WHITE_RABBIT, MINT, "Wake up, Neo.",
             ("White Rabbit, Matthew Welch 1999, MIT",
              "the Matrix's terminal, cleaned up")),
            (letters.BYTES256, AMBER, "256 Bytes 18:07",
             ("256 Bytes, Ray Larabie 2024, CC0",
              "the Altair 8800 and MICR, condensed")),
            (letters.VT323, CYAN, "VT323 login: pi",
             ("VT323, Peter Hull 2011, OFL",
              "the DEC VT320's own type")))

        def layout(head_h, set_h):
            """Each face's (heading rows, its set's lines), or None where a
            heading is too wide or the whole is too tall."""
            out, total = [], 0
            for font, _hue, head, lines in faces:
                c, r = letters.size(head, font, height=head_h, wall=self.w)
                if 2 + c > cols:
                    return None
                parts = [part for group in FACE_SET
                         for part in self.lines_of(group, cols - 4, set_h, font)]
                out.append((r, parts))
                total += r + len(lines) + len(parts) * set_h + 1
            return out if total <= rows + 1 else None

        sizes = ((3, 2), (2, 2), (3, 1), (2, 1), (1, 1))
        for head_h, set_h in sizes:
            plan = layout(head_h, set_h)
            if plan:
                break
        else:
            # Nothing fits whole: the smallest, as much as there is room for.
            head_h, set_h = sizes[-1]
            plan = [(letters.size(head, font, height=head_h, wall=self.w)[1],
                     [p for g in FACE_SET
                      for p in self.lines_of(g, cols - 4, set_h, font)])
                    for font, _h, head, _l in faces]
        y, shown = 0, []
        for (font, hue, head, lines), (r, parts) in zip(faces, plan):
            if y + r > rows:
                break
            letters.draw(grid, head, 2, y, hue, font=font, height=head_h,
                         wall=self.w)
            for i, line in enumerate(lines):
                put(grid, 2, y + r + i, line, "d0d6de" if not i else TAG)
            y += r + len(lines)
            drawn = 0
            for part in parts:
                if y + set_h > rows:
                    break
                letters.draw(grid, part, 2, y, hue, font=font, height=set_h,
                             wall=self.w)
                y += set_h
                drawn += 1
            shown.append(NAMES[font] + ("" if drawn == len(parts) else
                                        f" ({drawn}/{len(parts)} set lines)"))
            y += 1
        return (f"outline fonts, heading {head_h} rows and set {set_h}: "
                + (", ".join(shown) or "none fits"))

    def lines_of(self, text, cols, height, font=letters.TILE):
        """`text` broken between characters into lines of `font` no wider
        than `cols` at `height`."""
        out, line = [], ""
        for ch in text:
            trial = line + ch
            if line and letters.size(trial, font, height=height,
                                     wall=self.w)[0] > cols:
                out.append(line.strip())
                line = ch.lstrip()
            else:
                line = trial
        if line.strip():
            out.append(line.strip())
        return out

    def families(self, grid, cols, rows):
        """The same number on a fourteen-segment display and on a seven."""
        half = (rows - 2) // 2
        said = []
        for i, font in enumerate((letters.SEGMENT, letters.SEGMENT7)):
            if not self.can[font]:
                continue
            top = i * (half + 2)
            used = self.tag(grid, 2, top, f"{NAMES[font]} ghost={GHOST}")
            got = letters.fit(TEST[font], cols - 4, half - used,
                              fonts=(font,), wall=self.w, ghost=GHOST)
            if got:
                self.middle(grid, got, cols, half - used, (AMBER, MINT)[i],
                            top=top + used)
                said.append(NAMES[font])
        return (" against ".join(said) + ": fourteen behind, or seven; "
                + self.ghost_said())

    def resolutions(self, grid, cols, rows):
        """The same asks at a fine resolution and a coarse one."""
        wide = (cols - 3) // 2
        for j, px in enumerate(((14, 34), (5, 9))):
            left = 1 + j * (wide + 1)
            used = self.tag(grid, left + 1, 0, f"px={px[0]}x{px[1]}")
            each = (rows - used - 2) // 3
            asks = ((TEST[letters.SEGMENT], (letters.SEGMENT,),
                     {"smooth": True, "ghost": GHOST}),
                    (TEST[letters.DOTS], (letters.DOTS, letters.HALF), {}),
                    (TEST[letters.BLOCK], (letters.BLOCK,), {}))
            # The font's name above each, and the text in the rows below
            # it, so the text never covers its name.
            named = (letters.size("BLOCK", letters.DOTS)[1]
                     if self.can[letters.DOTS] else 1)
            for i, (text, fonts, opts) in enumerate(asks):
                top = used + 1 + i * each
                got = letters.fit(text, wide - 4, each - 1 - named,
                                  fonts=fonts, px=px, **opts)
                if got:
                    self.tag(grid, left + 1, top, NAMES[got["font"]])
                    self.middle(grid, got, wide, each - 1 - named, HUES[i],
                                top=top + named, left=left)
        return "px fine against coarse: smoothing, dots, cell shape"

    def beside(self, grid, cols, rows):
        """A header like sysinfo's: the machine's name bigger than the rest."""
        host = socket.gethostname().split(".")[0] or "bitwall"
        use = self.font(letters.DOTS)
        c, r = letters.size(host, use)
        top = 1
        for y in range(top, min(rows, top + r)):
            for x in range(cols):
                grid[y][x] = cell(" ", TAG, SHADE)
        letters.draw(grid, host, 2, top, "ffffff", font=use)
        put(grid, c + 5, top, "Mac mini · 8 cores", "d0d6de")
        put(grid, c + 5, top + 1, "up 3 days, 4 hours", TAG)
        clock = time.strftime("%H:%M:%S")
        put(grid, cols - len(clock) - 2, top, clock, "d0d6de")
        lines = ["cpu     12%   ▁▂▂▃▂▁▁▂▃▅▃▂", "load    0.42 0.51 0.60",
                 "memory  6.1 of 16 GiB", "walls   demo"]
        for i, line in enumerate(lines):
            put(grid, 2, top + r + 1 + i, line, "d0d6de" if i else CYAN)
        return (f"{NAMES[use]} name beside small text"
                + self.missing(letters.DOTS))

    def clock(self, grid, cols, rows):
        """The time, and a counter whose digits never move sideways."""
        now = time.strftime("%H:%M:%S")
        top_rows = max(3, rows * 2 // 3)
        got = letters.fit(now, cols - 4, top_rows - 1,
                          fonts=(letters.SEGMENT7, letters.HALF, letters.BLOCK),
                          wall=self.w, smooth=True, ghost=GHOST,
                          spacing="proportional")
        if got:
            self.middle(grid, got, cols, top_rows, AMBER)
        # Sized for the widest the counter will be and anchored at its
        # right, so as it counts nothing moves but the digits that change.
        low = letters.fit("00000.0", cols - 4, rows - top_rows,
                          fonts=(self.font(letters.DOTS), letters.BLOCK),
                          wall=self.w)
        self.counting = None
        if low:
            c, r = letters.size(**low)
            self.counting = (low, (cols + c) // 2,
                             top_rows + (rows - top_rows - r) // 2)
        return "digits hold still as they change"

    def count(self, grid):
        """The clock scene's counter, the one part of it that changes ten
        times a second, drawn over a copy of the rest."""
        if not self.counting:
            return
        low, right, y = self.counting
        low = dict(low, text="%.1f" % (time.monotonic() - self.start))
        c, _r = letters.size(**low)
        letters.draw(grid, x=right - c, y=y, fg=CYAN, **low)

    def wrapped(self, grid, cols, rows):
        """A long sentence, broken between words to be as big as it can."""
        got = letters.fit(LONG, cols - 4, rows - 1,
                          fonts=(letters.HALF, letters.DOTS, letters.BLOCK),
                          wall=self.w, wrap=True, align="centre")
        if not got:
            return "too long to fit"
        self.middle(grid, got, cols, rows, LILAC)
        return f"fit wrap: {NAMES[got['font']]} height={got['height']}"

    def lower(self, grid, cols, rows):
        """DOTS' whole alphabet, lower case and marks included."""
        use = self.font(letters.DOTS)
        got = letters.fit(PANGRAM, cols - 4, rows - 1, fonts=(use,),
                          wall=self.w, wrap=True, align="left")
        if got:
            self.middle(grid, got, cols, rows, MINT)
        return "DOTS lower case and marks" + self.missing(letters.DOTS)


def scene_of(text):
    """`--scene` as the index of the scene to start at, or None for all:
    a short name from SCENES, or a scene's number counting from 1."""
    if text in (None, "", "all"):
        return None
    if text.isdigit() and 1 <= int(text) <= len(SCENES):
        return int(text) - 1
    if text in SCENES:
        return SCENES.index(text)
    raise argparse.ArgumentTypeError(
        f"no scene {text!r}: all, 1..{len(SCENES)}, or one of "
        + ", ".join(SCENES))


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", type=int, default=DEFAULT_FONT,
                    help="receiver font size (default %(default)s)")
    ap.add_argument("--secs", type=int, choices=(5, 8, 10, 20),
                    default=DEFAULT_SECS,
                    help="seconds a scene is held (default %(default)s)")
    ap.add_argument("--scene", type=scene_of, default=None,
                    help="start at this scene, by name or number; all rolls "
                         "through every one from the first (default)")
    ap.add_argument("--hold", action="store_true",
                    help="stay on the scene rather than rolling on")
    ap.add_argument("host", help="the wall, host or host:wall")
    return ap.parse_args(argv)


def main():
    a = parse(sys.argv[1:])

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    # Ten frames a second for the counter's tenths; a scene standing still
    # costs nothing, since an unchanged cell is not sent.
    w = bitwall.attach(a.host, min_frame=0.1)
    demo = Demo(w, a.secs, a.font, first=a.scene or 0, hold=a.hold)
    with w.transition():
        w.set_font_size(a.font)
        w.set_source(demo)
    w.meta(name="letters",
           description="bitwall's big text: every font, size and option, "
                       "a scene at a time")
    cols, rows = w.area()
    fonts = " ".join(NAMES[f] for f in letters.FONTS if demo.can[f])
    say(f"letters -> {a.host}  {cols}x{rows} at font {a.font}  "
        f"{len(demo.scenes)} scenes of {a.secs}s  "
        f"{'holding ' if a.hold else 'from '}{SCENES[a.scene or 0]}  "
        f"depth {w.depth}  "
        f"fonts {fonts}", flush=True)

    area = (cols, rows)
    while not stop.wait(1.0):
        now = w.area()
        if now != area:
            say(f"resized {area[0]}x{area[1]} -> {now[0]}x{now[1]}",
                flush=True)
            area = now
    say("stopping", flush=True)
    w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
