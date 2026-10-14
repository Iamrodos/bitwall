#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# ///
"""Generative art that is made of cells.

    senders/art/main.py --piece truchet localhost
    senders/art/main.py --piece hilbert,wang --secs 900 pi.local
    senders/art/main.py --piece rule --rule 30 --speed 2 localhost
    senders/art/main.py --list

A character grid is usually standing in for pixels. These pieces are the
other case: the grid is the medium, and each one is built from the fact
that a cell is a tile with four edges that meet its neighbours exactly.

  truchet   Smith's two-tile Truchet, in rounded box-drawing arcs. Tiles
            flip one at a time and the curves are coloured by how long
            they are, so a flip that joins two loops recolours both.
  hilbert   A Hilbert curve traced in box-drawing lines, drawn one order
            at a time from coarse to fine, and erased the same way.
  wang      Wang tiles with three edge states -- no line, light, heavy --
            which is exactly the light-and-heavy box-drawing set. It grows
            from a seed, and once full it weathers: patches erode and heal.
  rule      An elementary cellular automaton, a generation a step, written
            as a strip chart that wraps rather than scrolls.
  ant       Langton's ant on a torus. Chaos, then abruptly a highway, and
            when the highway meets its own past, chaos again.

Everything accretes: a frame costs only the cells that changed, and each
piece changes a handful a second. The frame rate is a ceiling, not a speed;
`--speed` is the speed.

Arguments are never fatal. A wall is a screen in another room with nobody
at the keyboard, so a value out of range is clamped, one that is not a
number falls back to its default, and what was done is said on stderr.
"""
import argparse
import collections
import heapq
import math
import os
import random
import signal
import sys
import threading
import time
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
_SENDERS = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_SENDERS))

import bitwall                                                  # noqa: E402
from bitwall import cell                                        # noqa: E402

DEFAULT_FONT = 14
DEFAULT_FPS = 10.0
DEFAULT_SECS = 600.0

#: The grid the pieces are composed for. None of them needs it -- each
#: builds itself for whatever area it is given -- but a page showing this
#: wall small sizes its box from here, and this is the proportion of a
#: screen in cells that are twice as tall as they are wide.
NOMINAL = (160, 45)


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


# ----------------------------------------------------------------- colour

def rgb(h):
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def hexcol(r, g, b):
    return "%02x%02x%02x" % (max(0, min(255, int(r))),
                             max(0, min(255, int(g))),
                             max(0, min(255, int(b))))


def mix(a, b, t):
    ra, ga, ba = rgb(a)
    rb, gb, bb = rgb(b)
    return hexcol(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def ramp(stops, t):
    """A colour `t` of the way along a list of stops."""
    t = max(0.0, min(1.0, t))
    at = t * (len(stops) - 1)
    i = min(len(stops) - 2, int(at))
    return mix(stops[i], stops[i + 1], at - i)


# ----------------------------------------------------------------- canvas

class Canvas:
    """The grid a piece paints on, kept between frames.

    bitwall keeps the last grid it was handed and diffs the next against it
    by value, so a row that is changed in place would compare equal to
    itself and nothing would be sent. Rows are therefore copied the first
    time they are touched after a frame goes out, and a frame is a fresh
    outer list over rows that are shared wherever nothing changed.
    """

    def __init__(self, cols, rows, bg):
        self.cols, self.rows = cols, rows
        self.bg = bg
        self.blank = cell(" ", "default", bg)
        self._rows = [[self.blank] * cols for _ in range(rows)]
        self._fresh = set()

    def put(self, x, y, c):
        if not (0 <= x < self.cols and 0 <= y < self.rows):
            return
        row = self._rows[y]
        if row[x] == c:
            return
        if y not in self._fresh:
            row = self._rows[y] = list(row)
            self._fresh.add(y)
        row[x] = c

    def clear(self, x, y):
        self.put(x, y, self.blank)

    def frame(self):
        self._fresh.clear()
        return list(self._rows)


class Pixels:
    """A lattice of coloured points on a canvas, two to a cell.

    A cell is twice as tall as it is wide, so a lattice that is one point
    per cell comes out stretched; `▀` with the foreground as the top point
    and the background as the bottom gives two square-ish points a cell.
    Without half blocks the lattice is one point a cell and the picture is
    tall, which is a fair rendering rather than a broken one.
    """

    def __init__(self, canvas, half):
        self.canvas = canvas
        self.half = half
        self.w = canvas.cols
        self.h = canvas.rows * (2 if half else 1)
        self.px = [[canvas.bg] * self.w for _ in range(self.h)]

    def set(self, x, y, colour):
        if self.px[y][x] == colour:
            return
        self.px[y][x] = colour
        if not self.half:
            self.canvas.put(x, y, cell(" ", "default", colour))
            return
        cy = y // 2
        top, bottom = self.px[cy * 2][x], self.px[cy * 2 + 1][x]
        if top == bottom:
            self.canvas.put(x, cy, cell(" ", "default", top))
        else:
            self.canvas.put(x, cy, cell("▀", top, bottom))


class Clock:
    """Steps owed since the last tick, at a rate, without a burst.

    Nobody watching stops render() being called at all, and a piece that
    then caught up on a minute of steps in one frame would jump. Anything
    over `cap` is forgiven.
    """

    def __init__(self, rate):
        self.rate = rate
        self.last = None

    def owed(self, now, cap):
        if self.last is None:
            self.last = now
            return 0
        n = int((now - self.last) * self.rate)
        if n > cap:
            self.last = now
            return cap
        if n:
            self.last += n / self.rate
        return n


# ----------------------------------------------------------------- glyphs

#: Two-way connections through a cell, as the box-drawing glyph that
#: draws them. A key is the set of edges a line leaves through.
ARCS = {frozenset("UD"): "│", frozenset("LR"): "─", frozenset("UL"): "╯",
        frozenset("UR"): "╰", frozenset("DL"): "╮", frozenset("DR"): "╭"}
SQUARE = {frozenset("UD"): "│", frozenset("LR"): "─", frozenset("UL"): "┘",
          frozenset("UR"): "└", frozenset("DL"): "┐", frozenset("DR"): "┌"}
ASCII = {frozenset("UD"): "|", frozenset("LR"): "-", frozenset("UL"): "/",
         frozenset("UR"): "\\", frozenset("DL"): "\\", frozenset("DR"): "/"}


def box_arms():
    """Every light-and-heavy box-drawing glyph, by its arms (U, R, D, L).

    Each arm is 0 for none, 1 for light and 2 for heavy. Read from the
    Unicode names -- `BOX DRAWINGS DOWN LIGHT AND RIGHT HEAVY` -- rather
    than typed out, because there are eighty of them and the names are the
    authority. A clause without a weight of its own takes the one already
    said, which is how `LIGHT DOWN AND RIGHT` reads.
    """
    out = {(0, 0, 0, 0): " "}
    side = {"UP": (0,), "RIGHT": (1,), "DOWN": (2,), "LEFT": (3,),
            "VERTICAL": (0, 2), "HORIZONTAL": (1, 3)}
    weight = {"LIGHT": 1, "HEAVY": 2}
    for cp in list(range(0x2500, 0x254C)) + list(range(0x2574, 0x2580)):
        name = unicodedata.name(chr(cp))
        if "DASH" in name or "DOUBLE" in name:
            continue
        arms = [0, 0, 0, 0]
        seen = 1
        for clause in name.removeprefix("BOX DRAWINGS ").split(" AND "):
            words = clause.split()
            w = [weight[t] for t in words if t in weight]
            seen = w[0] if w else seen
            for t in words:
                for i in side.get(t, ()):
                    arms[i] = seen
        out[tuple(arms)] = chr(cp)
    return out


BOX = box_arms()


class Look:
    """What this screen can draw, asked once, answered for every piece."""

    def __init__(self, wall):
        self.arcs = ARCS if wall.can("".join(ARCS.values())) else \
            SQUARE if wall.can("".join(SQUARE.values())) else ASCII
        self.square = SQUARE if wall.can("".join(SQUARE.values())) else ASCII
        self.half = wall.can("▀")
        heavy = "".join(ch for arms, ch in BOX.items() if 2 in arms)
        light = "".join(ch for arms, ch in BOX.items()
                        if 2 not in arms and any(arms))
        self.weights = (0, 1, 2) if wall.can(heavy + light) else \
            (0, 1) if wall.can(light) else None

    def junction(self, arms):
        """A tile with these arms, in the best glyph set the screen has."""
        if self.weights:
            return BOX[arms]
        n = sum(1 for a in arms if a)
        if n == 0:
            return " "
        if arms == (1, 0, 1, 0) or arms[0] and not arms[1] and not arms[3]:
            return "|"
        if arms == (0, 1, 0, 1) or not arms[0] and not arms[2]:
            return "-"
        return "+"

    def said(self):
        notes = []
        if self.arcs is not ARCS:
            notes.append("no rounded corners, using "
                         + ("square ones" if self.arcs is SQUARE else "ascii"))
        if not self.half:
            notes.append("no half blocks, one point a cell")
        if self.weights != (0, 1, 2):
            notes.append("no heavy box lines, "
                         + ("light only" if self.weights else "ascii"))
        return notes


# ----------------------------------------------------------------- pieces

class Piece:
    """One work: built for an area, advanced to a time, painting as it goes."""

    name = ""

    def __init__(self, cols, rows, rng, look, opts):
        self.cols, self.rows = cols, rows
        self.rng, self.look, self.opts = rng, look, opts
        self.canvas = Canvas(cols, rows, self.BG)

    def tick(self, now):
        raise NotImplementedError

    def frame(self):
        return self.canvas.frame()


class Truchet(Piece):
    """Smith's Truchet tiles, the two-arc kind, in box-drawing arcs.

    Each tile is an odd number of cells wide and tall, so there is a cell
    at the middle of every edge for a curve to leave through. Tile A joins
    top to left and bottom to right; tile B is its mirror. Any tile can be
    swapped for the other and every curve still meets its neighbour at the
    edge midpoints, which is the whole property of the tiling, and the
    piece leans on it: a tile flips every so often and the curves reroute.

    A curve is coloured by its length. Short closed loops are dim, long
    ones bright, and a curve that runs off the wall is cool grey -- so the
    flip that joins two loops into one, or cuts a long one in two, is what
    shows. The tile is 5x3, which is nearly square on a cell twice as
    tall as it is wide.
    """

    name = "truchet"
    BG = "141210"
    TILE = (5, 3)
    LOOP = ("6b5236", "b8863a", "e6c25a", "f5ecd0")
    OPEN = "4c5f6e"
    RATE = 1.0            # flips a second

    OPPOSITE = {"U": "D", "D": "U", "L": "R", "R": "L"}

    def __init__(self, cols, rows, rng, look, opts):
        super().__init__(cols, rows, rng, look, opts)
        self.tw, self.th = self.TILE
        self.nx, self.ny = cols // self.tw, rows // self.th
        self.ox = (cols - self.nx * self.tw) // 2
        self.oy = (rows - self.ny * self.th) // 2
        self.shapes = self.build_shapes()
        self.kind = [[rng.randrange(2) for _ in range(self.nx)]
                     for _ in range(self.ny)]
        self.colour = {}
        self.clock = Clock(self.RATE * opts["speed"])
        self.recolour()
        self.paint_all()

    def staircase(self):
        """The cells of the arc from the top midpoint to the left one.

        A digital line, taking whichever of left and down is furthest
        behind its share, so the arc runs diagonally through the corner
        quadrant rather than hugging the tile's edge -- two arcs that hug
        edges read as a double line where tiles meet.
        """
        cx, cy = self.tw // 2, self.th // 2
        x, y = cx, 0
        moves = []
        left = down = 0
        while left < cx or down < cy:
            take_left = down >= cy or (left < cx and (left + 0.5) / cx
                                       <= (down + 0.5) / cy)
            moves.append("L" if take_left else "D")
            left += take_left
            down += not take_left
        cells = []
        enter = "U"
        for m in moves:
            cells.append((x, y, frozenset((enter, m))))
            x, y = (x - 1, y) if m == "L" else (x, y + 1)
            enter = self.OPPOSITE[m]
        cells.append((x, y, frozenset((enter, "L"))))
        return cells

    def build_shapes(self):
        """Both tiles as [(ports, {cell: connections})] -- two arcs each."""
        tw, th = self.tw, self.th
        base = self.staircase()

        def flip_x(cells):
            swap = {"L": "R", "R": "L"}
            return [(tw - 1 - x, y, frozenset(swap.get(d, d) for d in c))
                    for x, y, c in cells]

        def flip_y(cells):
            swap = {"U": "D", "D": "U"}
            return [(x, th - 1 - y, frozenset(swap.get(d, d) for d in c))
                    for x, y, c in cells]

        tl = base
        br = flip_y(flip_x(base))
        tr = flip_x(base)
        bl = flip_y(base)
        return (((("U", "L"), tl), (("D", "R"), br)),
                ((("U", "R"), tr), (("D", "L"), bl)))

    def port(self, i, j, side):
        """The shared name of a tile edge, so two tiles agree on it."""
        if side == "U":
            return ("h", i, j)
        if side == "D":
            return ("h", i, j + 1)
        if side == "L":
            return ("v", i, j)
        return ("v", i + 1, j)

    def recolour(self):
        """Colour every arc by the curve it belongs to."""
        parent = {}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        at_port = {}
        for j in range(self.ny):
            for i in range(self.nx):
                for n, (ports, _) in enumerate(self.shapes[self.kind[j][i]]):
                    arc = (i, j, n)
                    parent[arc] = arc
                    for side in ports:
                        at_port.setdefault(self.port(i, j, side), []).append(arc)
        open_curves = set()
        for p, arcs in at_port.items():
            if len(arcs) == 2:
                union(arcs[0], arcs[1])
            else:
                open_curves.add(arcs[0])
        size = collections.Counter(find(a) for a in parent)
        leaks = {find(a) for a in open_curves}
        longest = max(math.log(max(size.values(), default=4) / 4), 1e-9)
        colour = {}
        for a in parent:
            root = find(a)
            if root in leaks:
                colour[a] = self.OPEN
            else:
                # Log scale: the eye should separate 4 from 8 from 16, and
                # a hundred-arc loop should already be nearly white.
                t = math.log(size[root] / 4) / max(longest, math.log(32))
                colour[a] = ramp(self.LOOP, t)
        self.colour = colour

    def paint_tile(self, i, j):
        x0, y0 = self.ox + i * self.tw, self.oy + j * self.th
        drawn = set()
        for n, (_, cells) in enumerate(self.shapes[self.kind[j][i]]):
            fg = self.colour[(i, j, n)]
            for x, y, conn in cells:
                self.canvas.put(x0 + x, y0 + y,
                                cell(self.look.arcs[conn], fg, self.BG))
                drawn.add((x, y))
        for y in range(self.th):
            for x in range(self.tw):
                if (x, y) not in drawn:
                    self.canvas.clear(x0 + x, y0 + y)

    def paint_all(self):
        for j in range(self.ny):
            for i in range(self.nx):
                self.paint_tile(i, j)

    def tick(self, now):
        if not self.nx or not self.ny:
            return
        for _ in range(self.clock.owed(now, 3)):
            i, j = self.rng.randrange(self.nx), self.rng.randrange(self.ny)
            self.kind[j][i] ^= 1
            before = self.colour
            self.recolour()
            # Only the curves whose colour moved need repainting, and the
            # flipped tile; the canvas drops any cell that did not change.
            for (ti, tj, n), c in self.colour.items():
                if (ti, tj) == (i, j) or before.get((ti, tj, n)) != c:
                    self.paint_tile(ti, tj)


class Wang(Piece):
    """Wang tiles on a grid of cells, one tile a cell.

    A Wang tile has a colour on each edge and may sit beside a tile whose
    facing edge matches. With three edge colours -- no line, a light line,
    a heavy line -- the eighty-one tiles are exactly the light-and-heavy
    box-drawing set plus the blank, and a matching edge is a line that
    continues. Because every combination of edges is a glyph there is
    always a tile that fits, so the tiling never gets stuck and never
    repeats.

    It grows outward from one seed, each new tile chosen to match what is
    already placed and weighted to run straight, turn sometimes, branch
    rarely and dead-end almost never. Once the wall is full it weathers:
    a small patch erodes to bare wall and heals from its rim in, so the
    picture is never finished. A tile is bright when it is laid and
    settles to its resting colour over half a minute; heavy lines are
    copper and light ones slate.

    Tiles are laid in strict order of distance -- outward from the seed,
    inward from the centre of a patch -- rather than at random along the
    edge of the growth. Two empty cells fenced in with one line running
    into them are a dead end whichever is laid first, and a ragged edge
    makes such pockets constantly; laid in distance order, a cell's placed
    neighbours are always its inner ones and its free ones the outer, so
    a pocket never forms and the only cells that can be forced into a stub
    are the far corners of the wall and the last cell of a patch.
    """

    name = "wang"
    BG = "0e0e10"
    HEAVY = ("f0a060", "c07a40", "8a5a30", "5c3d22")
    LIGHT = ("b8c8d4", "86979f", "5c6a72", "3b454c")
    AGES = (4.0, 12.0, 30.0)
    GROW = 300.0          # tiles a second while the wall fills
    HEAL = 80.0           # tiles a second once it is weathering
    ERODE = 0.5           # patches a second

    def __init__(self, cols, rows, rng, look, opts):
        super().__init__(cols, rows, rng, look, opts)
        self.weights = look.weights or (0, 1)
        self.tiles = [[None] * cols for _ in range(rows)]
        self.gen = [[0] * cols for _ in range(rows)]
        self.queue = []
        self.aging = collections.deque()
        self.full = False
        self.grow = Clock(self.GROW * opts["speed"])
        self.erode = Clock(self.ERODE * opts["speed"])
        sx, sy = rng.randrange(cols), rng.randrange(rows)
        for y in range(rows):
            for x in range(cols):
                self.enqueue(x, y, self.distance(x, y, sx, sy))

    def distance(self, x, y, cx, cy):
        """How far (x, y) is from (cx, cy), in screen terms, roughened.

        Columns count half because a cell is twice as tall as it is wide,
        so the growth looks round rather than tall. The jitter is under a
        quarter so that no two neighbours can swap order -- neighbours
        differ by at least a half before it -- and the front is ragged
        rather than a perfect diamond.
        """
        return abs(x - cx) / 2 + abs(y - cy) + self.rng.random() * 0.24

    def enqueue(self, x, y, key):
        heapq.heappush(self.queue, (key, x, y))

    def next_cell(self):
        """The next unplaced cell in laying order, or None."""
        while self.queue:
            _, x, y = heapq.heappop(self.queue)
            if self.tiles[y][x] is None:
                return x, y
        return None

    def neighbour(self, x, y, side):
        dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[side]
        nx, ny = x + dx, y + dy
        if 0 <= nx < self.cols and 0 <= ny < self.rows:
            return nx, ny
        return None

    @staticmethod
    def prefer(arms, owed):
        """How much this tile is wanted here, before the edges have a say.

        Lines should run, turn now and then, branch rarely and stop almost
        never; heavy lines are the accent, not the fabric. `owed` says, for
        each side, how many lines already run into the unplaced neighbour
        there whose every other side is settled -- or None if it still has
        a side open. This tile is the last word on that neighbour, so if
        what it leaves would be one lone arm, a stub is forced there, and
        that costs nearly everything. Without the look-ahead the edge of
        the growth strands arms all over the wall.
        """
        n = sum(1 for a in arms if a)
        if n == 0:
            w = 2.5
        elif n == 1:
            # Not zero: a wall corner with one line running in has nothing
            # else to offer, and a choice with no weight at all is an error.
            w = 0.001
        elif n == 2:
            w = 3.0 if (arms[0] and arms[2]) or (arms[1] and arms[3]) else 1.2
        elif n == 3:
            w = 0.3
        else:
            w = 0.12
        if 1 in arms and 2 in arms:
            w *= 0.12
        elif 2 in arms:
            w *= 0.6
        for a, into in zip(arms, owed):
            if into is not None and into + (a != 0) == 1:
                w *= 0.02
        return w

    def fixed_arm(self, x, y, side):
        """What the edge of (x, y) on `side` must be, or None if it is free."""
        n = self.neighbour(x, y, side)
        if n is None:
            return 0
        t = self.tiles[n[1]][n[0]]
        return None if t is None else t[(side + 2) % 4]

    def choose(self, x, y):
        fixed, owed = [], []
        for side in range(4):
            arm = self.fixed_arm(x, y, side)
            fixed.append(self.weights if arm is None else (arm,))
            into = None
            n = self.neighbour(x, y, side)
            if arm is None:
                others = [self.fixed_arm(n[0], n[1], s)
                          for s in range(4) if s != (side + 2) % 4]
                if None not in others:
                    into = sum(1 for a in others if a)
            owed.append(into)
        cands, weights = [], []
        for up in fixed[0]:
            for right in fixed[1]:
                for down in fixed[2]:
                    for left in fixed[3]:
                        arms = (up, right, down, left)
                        cands.append(arms)
                        weights.append(self.prefer(arms, owed))
        return self.rng.choices(cands, weights)[0]

    def paint(self, x, y, level):
        arms = self.tiles[y][x]
        if arms is None or not any(arms):
            self.canvas.clear(x, y)
            return
        pal = self.HEAVY if 2 in arms else self.LIGHT
        self.canvas.put(x, y, cell(self.look.junction(arms), pal[level], self.BG))

    def place(self, x, y, now):
        self.tiles[y][x] = self.choose(x, y)
        self.gen[y][x] += 1
        self.paint(x, y, 0)
        for k, age in enumerate(self.AGES):
            self.aging.append((now + age, x, y, self.gen[y][x], k + 1))

    def erode_patch(self):
        """Bare a diamond of wall, queued to heal from its rim inward."""
        cx, cy = self.rng.randrange(self.cols), self.rng.randrange(self.rows)
        r = self.rng.randint(2, 4)
        for y in range(max(0, cy - r), min(self.rows, cy + r + 1)):
            for x in range(max(0, cx - 2 * r), min(self.cols, cx + 2 * r + 1)):
                if abs(x - cx) / (2 * r) + abs(y - cy) / r > 1.0:
                    continue
                if self.tiles[y][x] is not None:
                    self.tiles[y][x] = None
                    self.gen[y][x] += 1
                    self.canvas.clear(x, y)
                    self.enqueue(x, y, -self.distance(x, y, cx, cy))

    def tick(self, now):
        while self.aging and self.aging[0][0] <= now:
            _, x, y, gen, level = self.aging.popleft()
            if self.gen[y][x] == gen:
                self.paint(x, y, level)
        if self.full:
            for _ in range(self.erode.owed(now, 1)):
                self.erode_patch()
        for _ in range(self.grow.owed(now, int(self.grow.rate))):
            cell_at = self.next_cell()
            if cell_at is None:
                if not self.full:
                    self.full = True
                    self.grow = Clock(self.HEAL * self.opts["speed"])
                break
            self.place(*cell_at, now)


class Ant(Piece):
    """Langton's ant, on a lattice that wraps.

    Two rules: on a pale cell turn right, darken it and step; on a dark
    cell turn left, lighten it and step. That is the whole of it, and for
    something like ten thousand steps it makes an apparently structureless
    mess -- then, with nothing changed, it starts building a diagonal
    highway and runs off along it for good. Why it always does this is
    open.

    The trail is amber where it is fresh and cools over half a minute, so
    the highway reads as the new thing crossing old work rather than as
    more of the same. A cell the ant has cleared keeps a trace of having
    been visited, because a lattice that returned exactly to bare would
    lose the record of where the ant has been.

    Points are two to a cell, so the lattice is square rather than
    stretched, and it wraps: a torus means the highway eventually runs
    back into its own past, which is the only way this ends up composed
    rather than merely leaving.
    """

    name = "ant"
    BG = "0b0c0e"

    #: A mark is bright as it is made and then it is simply part of the
    #: trail. A long fade spreads the eye over everything the ant has ever
    #: done, which is the opposite of what is interesting: what matters is
    #: where it is working now against everywhere it has been. So the
    #: settle is quick and lands on one resting colour.
    COOL = 1.2
    STEPS = 2
    HOT = ("ffe6b0", "c9781f", "9a5f22")
    #: Where the ant is, and what a cleared point keeps of having been.
    HERE = "ffffff"
    GHOST = "17191d"
    #: Steps a second. Fast enough that the first ten thousand are a
    #: minute rather than an afternoon, slow enough to watch.
    RATE = 240.0

    def __init__(self, cols, rows, rng, look, opts):
        super().__init__(cols, rows, rng, look, opts)
        self.px = Pixels(self.canvas, look.half)
        self.w, self.h = self.px.w, self.px.h
        self.on = [[False] * self.w for _ in range(self.h)]
        self.at = [[0.0] * self.w for _ in range(self.h)]
        self.x, self.y = self.w // 2, self.h // 2
        self.dir = 0                          # 0 up, 1 right, 2 down, 3 left
        self.clock = Clock(self.RATE * opts["speed"])
        self.cool = Clock(6.0)
        self.started = None

    def shade(self, x, y, now):
        """What a point looks like: its heat, or its trace, or the ant."""
        if x == self.x and y == self.y:
            return self.HERE
        if not self.on[y][x]:
            return self.GHOST if self.at[y][x] else self.canvas.bg
        age = min(1.0, (now - self.at[y][x]) / self.COOL)
        return ramp(self.HOT, round(age * self.STEPS) / self.STEPS)

    def step(self, now):
        was = (self.x, self.y)
        if self.on[self.y][self.x]:
            self.dir = (self.dir - 1) % 4
            self.on[self.y][self.x] = False
        else:
            self.dir = (self.dir + 1) % 4
            self.on[self.y][self.x] = True
        self.at[self.y][self.x] = now
        dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[self.dir]
        self.x = (self.x + dx) % self.w
        self.y = (self.y + dy) % self.h
        self.px.set(was[0], was[1], self.shade(was[0], was[1], now))
        self.px.set(self.x, self.y, self.HERE)

    def tick(self, now):
        if self.started is None:
            self.started = now
        for _ in range(self.clock.owed(now, 400)):
            self.step(now)
        # The cooling is swept rather than tracked, because every marked
        # point is cooling at once and a queue of them would be the whole
        # lattice. A sweep repaints only what has actually changed shade,
        # and the canvas drops the rest.
        for _ in range(self.cool.owed(now, 3)):
            for y in range(self.h):
                row = self.on[y]
                for x in range(self.w):
                    if row[x]:
                        self.px.set(x, y, self.shade(x, y, now))


PIECES = {c.name: c for c in (Truchet, Wang, Ant)}

#: What each piece is, for whoever reads the wall's name off a list.
ABOUT = {
    "truchet": "Truchet tiles in rounded arcs, flipping one at a time",
    "wang": "Wang tiles in light and heavy lines, growing and weathering",
    "ant": "Langton's ant on a torus: chaos, then a highway",
}


# ----------------------------------------------------------------- front end

def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except ValueError:
            say(f"art: {text!r} is not a number, using {default}",
                file=sys.stderr, flush=True)
            return default
    return convert


def clamp(name, value, lo, hi):
    got = min(hi, max(lo, value))
    if got != value:
        say(f"art: {name} {value:g} is outside {lo:g}..{hi:g}, using {got:g}",
            file=sys.stderr, flush=True)
    return got


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="art", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bad values are clamped or defaulted, never fatal: the wall "
               "keeps drawing and stderr says what changed.")
    ap.add_argument("host", nargs="?", help="receiver to attach to")
    ap.add_argument("--piece", default="all",
                    help="one piece, several comma-separated, or all "
                         "(default); --list names them")
    ap.add_argument("--list", action="store_true",
                    help="name the pieces and exit")
    ap.add_argument("--secs", type=number(DEFAULT_SECS), default=DEFAULT_SECS,
                    help=f"seconds a piece holds before the next "
                         f"(default {DEFAULT_SECS:g})")
    ap.add_argument("--speed", type=number(1.0), default=1.0,
                    help="multiplier on every piece's pace (default 1, "
                         "0.1..10)")
    ap.add_argument("--rule", type=number(110), default=110,
                    help="the elementary automaton for `rule` (default 110, "
                         "0..255)")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=number(DEFAULT_FPS), default=DEFAULT_FPS,
                    help=f"frames a second at most (default {DEFAULT_FPS:g})")
    ap.add_argument("--seed", type=number(None), default=None,
                    help="random seed, for a run that repeats")
    args, unknown = ap.parse_known_intermixed_args(argv)
    for u in unknown:
        say(f"art: ignoring unknown argument {u!r}", file=sys.stderr,
            flush=True)
    if args.list:
        print(" ".join(PIECES))
        sys.exit(0)
    if not args.host:
        ap.error("no host given")
    return args


def main():
    args = parse(sys.argv[1:])
    order = list(PIECES) if args.piece == "all" else \
        [n for n in args.piece.split(",") if n in PIECES]
    if not order:
        say(f"art: {args.piece!r} names no piece I have; using all",
            file=sys.stderr, flush=True)
        order = list(PIECES)
    secs = clamp("secs", args.secs, 5, 86400)
    font = int(clamp("font", args.font, 4, 400))
    fps = clamp("fps", args.fps, 1, 60)
    opts = {"speed": clamp("speed", args.speed, 0.1, 10),
            "rule": int(clamp("rule", args.rule, 0, 255))}
    rng = random.Random(None if args.seed is None else int(args.seed))

    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    look = Look(wall)
    for note in look.said():
        say(f"art: {note}", file=sys.stderr, flush=True)

    started = time.monotonic()
    showing = {"piece": None, "name": None}

    def render(cols, rows):
        now = time.monotonic()
        want = order[int((now - started) / secs) % len(order)]
        piece = showing["piece"]
        if piece is None or piece.name != want \
                or (piece.cols, piece.rows) != (cols, rows):
            piece = showing["piece"] = PIECES[want](cols, rows, rng, look, opts)
        piece.tick(now)
        return piece.frame()

    with wall.transition():
        wall.set_font_size(font)
        wall.set_source(render)
    wall.meta(name="art",
              description="Generative art made of cells: " + ", ".join(order))
    cols, rows = wall.area()
    say(f"art -> {args.host}  {cols}x{rows} at font {font}  "
        f"{'+'.join(order)}  {fps:g}fps  (ctrl-c to stop)", flush=True)

    try:
        while not stop.wait(0.5):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"art: resized to {cols}x{rows}", flush=True)
            piece = showing["piece"]
            name = piece.name if piece else None
            if name and name != showing["name"]:
                # The label follows the piece so a page listing walls says
                # which one is up, not just that something is.
                showing["name"] = name
                wall.meta(name=name, description=ABOUT[name])
                say(f"art: {name}", flush=True)
    finally:
        say("art: stopped", flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
