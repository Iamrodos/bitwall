#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# ///
"""A maze that draws itself, then is solved several ways over.

    senders/maze/main.py localhost
    senders/maze/main.py --carve wilson --solve astar,bfs localhost
    senders/maze/main.py --speed 2 --font 12 pi.local
    senders/maze/main.py --list

A maze is a grid and its walls are what box-drawing characters are for,
so nothing here stands in for anything: a passage is a run of cells and a
wall is a line between two of them, drawn where the receiver joins lines
at cell edges. A maze cell is three characters across and one down, with
a character of wall on each side, which is square on a screen whose cells
are twice as tall as they are wide.

The piece is a comparison. One algorithm carves the maze, then each solver
in turn is given the same maze from the top-left corner to the bottom-right,
the answer is held on screen for a moment, and then the next carver begins.
A status line along the top says which algorithm is at work, counts the
moves it makes as it makes them, and keeps a tally of how many cells each
solver had to look at, so the maze is partly an explanation of itself.

Carvers -- each makes a perfect maze, one path between any two cells, and
each looks nothing like the others doing it:

  backtracker  one long corridor that snakes until it is stuck, then
               retreats along itself to the last fork
  prim         grows from a seed at every point of its edge at once, so
               it spreads like frost
  kruskal      opens walls anywhere at random and the fragments abruptly
               join; the maze exists everywhere before it exists anywhere
  wilson       a random walk from an unvisited cell that erases its own
               loops until it reaches the maze, then joins in one burst;
               slow to start and quick to finish, and the only one that
               picks uniformly among every possible maze
  division     begins with one open room and draws walls across it, each
               with a gap, cutting rooms into smaller rooms

Solvers:

  bfs          breadth-first, a wave that floods every corridor fairly
  dfs          depth-first, one corridor at a time, wrong until it is not
  astar        A*, which keeps pulling towards the exit and looks like
               it knows something
  deadend      dead-end filling, which never searches at all: it blacks
               out every dead end from its tip inward until only the
               answer is left

Four states, ranked by brightness while an algorithm works: carved floor is
barely lit, ground that was searched and given up is slate, the path is
amber, and the frontier -- wherever the algorithm is working this instant
-- is nearly white. While a finished answer is held the same four are
dressed for reading rather than for watching: floor and searched ground are
both a pale slate, the walls are a light one that carries the shape of the
whole maze, and the path is black, a dark channel cut through a lit maze
that can be followed from the doorway. Only cells that change are
sent, and an algorithm changes a handful a step, so a frame is a few
hundred bytes and a full wall runs at three or four kilobytes a second
before the transport compresses it.

Arguments are never fatal. A wall is a screen in another room with nobody
at the keyboard, so a value out of range is clamped, one that is not a
number falls back to its default, a name that is not an algorithm is
dropped, and what was done is said on stderr.
"""
import argparse
import collections
import heapq
import os
import random
import signal
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SENDERS = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_SENDERS))

import bitwall                                                  # noqa: E402
from bitwall import cell                                        # noqa: E402

DEFAULT_FONT = 14
DEFAULT_FPS = 10.0
DEFAULT_HOLD = 6.0

#: The grid the piece is composed for. It builds itself for any area, but
#: a page showing this wall small sizes its box from here, and this is the
#: proportion of a screen in cells twice as tall as they are wide.
NOMINAL = (160, 45)

#: A maze cell on screen: three characters of floor and one of wall across,
#: one row of floor and one of wall down. Square, on cells that are twice
#: as tall as they are wide.
PERIOD = (4, 2)

#: How long the finished maze is held before the first solver starts. Long
#: enough to see it whole, short enough that nothing seems to have stalled.
HOLD_CARVED = 3.0

#: How long a cell glows after it is carved or filled, and how long it then
#: stays warm. Short: the point is where the work is now, not everywhere
#: it has ever been.
GLOW = (0.5, 1.8)

#: How often the counter in the status line is brought up to date. Ten
#: times a second is fast enough that the number reads as running, and
#: composing the line on every step of an algorithm that takes forty of
#: them a second would be more work than the steps themselves.
COUNT_EVERY = 0.1


# ----------------------------------------------------------------- colour

#: The palette an algorithm works in is one ordered list and a cell's state
#: is an index into it, so brightness is rank: the floor of a passage is
#: barely lifted from the wall's background, searched-and-abandoned ground
#: is slate above that, the found path is amber, and the frontier is nearly
#: white. A cooling cell passes through WARM on its way back down. An
#: opening between two cells takes the dimmer of the two, so a corridor is
#: lit only as far as it has been reached.
BG, FLOOR, DEAD, WARM, PATH, HOT = range(6)
PALETTE = ("0a0b0e", "1a1e26", "313a4d", "7a5a30", "cf8d2e", "ffe9b8")
WALL = "4b515e"          # the lines
INK = "5d6472"           # the status line
TITLE = "9aa3b5"         # the algorithm's name in the status line

#: The same six states, worn while a finished answer is held. Floor and
#: searched ground are both a pale slate, so that the path, drawn black,
#: reads as a dark channel cut through them, and the walls are light enough
#: to carry the shape of the whole maze at once. A cell left cooling by the
#: solver keeps the warm colours it is cooling towards. The background is
#: what the walls stand on and the status line is written on, so it is the
#: one colour both schemes share.
SOLVED = ("0a0b0e", "78859f", "646f89", "7a5a30", "000000", "ffe9b8")
SOLVED_WALL = "a9b8d4"

#: The ink for the exit diamond, the dark one first and the light one
#: second: it is drawn in whichever end of its scheme its own cell is
#: furthest from, so it is seen both on the floor it waits on and on the
#: path that finally arrives at it.
EXIT_INK = (PALETTE[BG], PALETTE[PATH])
SOLVED_EXIT_INK = (SOLVED[BG], SOLVED[HOT])

#: Above this a colour is light enough to take dark ink.
LIT = 0.45


def lightness(rgb):
    """How light a palette entry looks, 0 to 1, weighting red, green and
    blue the way an eye weights them."""
    r, g, b = (int(rgb[k:k + 2], 16) for k in (0, 2, 4))
    return (299 * r + 587 * g + 114 * b) / 255000


# ----------------------------------------------------------------- canvas

class Canvas:
    """The grid the piece paints on, kept between frames.

    bitwall keeps the last grid it was handed and diffs the next against it
    by value, so a row changed in place would compare equal to itself and
    nothing would be sent. A row is therefore copied the first time it is
    touched after a frame goes out, and a frame is a fresh outer list over
    rows that are shared wherever nothing changed.
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

    def frame(self):
        self._fresh.clear()
        return list(self._rows)


class Clock:
    """Steps owed since the last tick, at a rate, without a burst.

    Nobody watching stops render() being called at all, and an algorithm
    that then caught up on a minute of steps in one frame would jump.
    Anything over `cap` is forgiven.
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

#: A wall junction by which of its four arms -- up, right, down, left --
#: carry a wall. A junction with one arm is a wall that ends in the open,
#: which a maze is full of, and the half-line glyphs draw it stopping at
#: the centre of the cell rather than running on.
JUNCTION = {
    (1, 0, 1, 0): "│", (0, 1, 0, 1): "─",
    (0, 1, 1, 0): "┌", (0, 0, 1, 1): "┐", (1, 1, 0, 0): "└", (1, 0, 0, 1): "┘",
    (1, 1, 1, 0): "├", (1, 0, 1, 1): "┤", (0, 1, 1, 1): "┬", (1, 1, 0, 1): "┴",
    (1, 1, 1, 1): "┼",
    (1, 0, 0, 0): "╵", (0, 1, 0, 0): "╶", (0, 0, 1, 0): "╷", (0, 0, 0, 1): "╴",
}


class Look:
    """What this screen can draw, asked once."""

    def __init__(self, wall):
        full = "".join(ch for arms, ch in JUNCTION.items() if sum(arms) > 1)
        stubs = "".join(ch for arms, ch in JUNCTION.items() if sum(arms) == 1)
        self.box = wall.can(full)
        self.stubs = self.box and wall.can(stubs)
        self.goal = "◆" if wall.can("◆") else "*"
        self.vertical = "│" if self.box else "|"
        self.horizontal = "─" if self.box else "-"

    def junction(self, arms):
        if not any(arms):
            return " "
        up, right, down, left = arms
        if self.box:
            if sum(arms) == 1 and not self.stubs:
                return "│" if up or down else "─"
            return JUNCTION[arms]
        if not right and not left:
            return "|"
        if not up and not down:
            return "-"
        return "+"

    def said(self):
        notes = []
        if not self.box:
            notes.append("no box-drawing, walls in ascii")
        elif not self.stubs:
            notes.append("no half-line glyphs, wall ends run to the edge")
        if self.goal != "◆":
            notes.append("no diamond, marking the exit with *")
        return notes


# ----------------------------------------------------------------- the maze

class Maze:
    """The grid, its walls, the state of every cell, and the picture.

    Cells are (i, j) with (0, 0) top-left. `open_r[j][i]` is whether the
    wall between (i, j) and (i + 1, j) is down; `open_d[j][i]` the same
    for (i, j) and (i, j + 1). Every other wall is the border, which is
    closed except for the way in beside the start and the way out beside
    the goal.

    Painting is by cell: repainting a cell repaints its floor, the four
    wall segments around it and the four junctions at its corners, which
    is everything its state or its walls can have changed. The canvas
    drops whatever came out the same, so this costs comparisons and
    sends nothing extra.
    """

    def __init__(self, canvas, look, mw, mh, x0, y0):
        self.canvas, self.look = canvas, look
        self.mw, self.mh, self.x0, self.y0 = mw, mh, x0, y0
        self.start, self.goal = (0, 0), (mw - 1, mh - 1)
        self.now = 0.0
        self.palette, self.wall, self.exit_ink = PALETTE, WALL, EXIT_INK
        self.reset(open_all=False)

    def dress(self, solved):
        """Wear the colours an algorithm works in, or the ones a finished
        answer is held in."""
        self.palette = SOLVED if solved else PALETTE
        self.wall = SOLVED_WALL if solved else WALL
        self.exit_ink = SOLVED_EXIT_INK if solved else EXIT_INK
        self.paint_all()

    def reset(self, open_all):
        """Bare ground: every wall up, or, for a carver that cuts rooms
        rather than digging passages, every wall down."""
        mw, mh = self.mw, self.mh
        self.open_r = [[open_all] * (mw - 1) for _ in range(mh)]
        self.open_d = [[open_all] * mw for _ in range(mh - 1)]
        self.state = [[FLOOR if open_all else BG] * mw for _ in range(mh)]
        self.heat = [[0] * mw for _ in range(mh)]
        self.gen = [[0] * mw for _ in range(mh)]
        self.hot_walls = {}
        self.wall_gen = 0
        self.aging = {WARM: collections.deque(), 0: collections.deque()}
        self.paint_all()

    def clear_marks(self):
        """Every carved cell back to plain floor, ready for a solver."""
        for j in range(self.mh):
            self.state[j] = [FLOOR] * self.mw
            self.heat[j] = [0] * self.mw
        self.hot_walls = {}
        for q in self.aging.values():
            q.clear()
        self.paint_all()

    # -- the graph

    def cells(self):
        return [(i, j) for j in range(self.mh) for i in range(self.mw)]

    def neighbours(self, i, j):
        out = []
        if j > 0:
            out.append((i, j - 1))
        if i + 1 < self.mw:
            out.append((i + 1, j))
        if j + 1 < self.mh:
            out.append((i, j + 1))
        if i > 0:
            out.append((i - 1, j))
        return out

    def is_open(self, a, b):
        (ai, aj), (bi, bj) = a, b
        if aj == bj:
            return self.open_r[aj][min(ai, bi)]
        return self.open_d[min(aj, bj)][ai]

    def passages(self, i, j):
        return [n for n in self.neighbours(i, j) if self.is_open((i, j), n)]

    def state_at(self, c):
        return self.state[c[1]][c[0]]

    def set_open(self, a, b, yes):
        (ai, aj), (bi, bj) = a, b
        if aj == bj:
            self.open_r[aj][min(ai, bi)] = yes
        else:
            self.open_d[min(aj, bj)][ai] = yes
        self.paint_cell(*a)
        self.paint_cell(*b)

    def link(self, a, b):
        self.set_open(a, b, True)

    def wall_between(self, a, b):
        """Put a wall up between two cells, and let it glow as it is laid."""
        (ai, aj), (bi, bj) = a, b
        key = ("v", max(ai, bi), aj) if aj == bj else ("h", ai, max(aj, bj))
        self.wall_gen += 1
        self.hot_walls[key] = [HOT, self.wall_gen]
        self.aging[WARM].append((self.now + GLOW[0], key, self.wall_gen))
        self.aging[0].append((self.now + GLOW[1], key, self.wall_gen))
        self.set_open(a, b, False)

    # -- the states

    def set_state(self, i, j, s):
        if self.state[j][i] != s:
            self.state[j][i] = s
            self.paint_cell(i, j)

    def glow(self, i, j):
        """Light a cell as the frontier, to cool back to its state."""
        self.heat[j][i] = HOT
        self.gen[j][i] += 1
        g = self.gen[j][i]
        self.aging[WARM].append((self.now + GLOW[0], (i, j), g))
        self.aging[0].append((self.now + GLOW[1], (i, j), g))
        self.paint_cell(i, j)

    def carve(self, i, j):
        self.state[j][i] = FLOOR
        self.glow(i, j)

    def cool(self):
        """Let everything that has glowed long enough settle a level."""
        for level, q in self.aging.items():
            while q and q[0][0] <= self.now:
                _, key, g = q.popleft()
                if len(key) == 3:
                    hot = self.hot_walls.get(key)
                    if hot and hot[1] == g:
                        if level:
                            hot[0] = level
                        else:
                            del self.hot_walls[key]
                        kind, i, j = key
                        self.paint_cell(i - (kind == "v"), j - (kind == "h"))
                        self.paint_cell(i, j)
                    continue
                i, j = key
                if self.gen[j][i] == g:
                    self.heat[j][i] = level
                    self.paint_cell(i, j)

    def show_path(self, path):
        """The answer, and only the answer, lit."""
        for j in range(self.mh):
            for i in range(self.mw):
                if self.state[j][i] == HOT:
                    self.set_state(i, j, DEAD)
        for i, j in path:
            self.set_state(i, j, PATH)

    # -- painting

    def colour(self, i, j):
        return self.heat[j][i] or self.state[j][i]

    def dimmer(self, cells):
        """The colour of an opening: the dimmest cell it joins."""
        return self.palette[min(self.colour(i, j) for i, j in cells)]

    def v_closed(self, i, j):
        """Whether there is wall to the left of cell (i, j); i may be mw."""
        if i == 0:
            return (i, j) != self.start
        if i == self.mw:
            return (i - 1, j) != self.goal
        return not self.open_r[j][i - 1]

    def h_closed(self, i, j):
        if j == 0 or j == self.mh:
            return True
        return not self.open_d[j - 1][i]

    def wall_ink(self, *keys):
        hot = max((self.hot_walls[k][0] for k in keys if k in self.hot_walls),
                  default=0)
        return self.palette[hot] if hot else self.wall

    def paint_v(self, i, j):
        x, y = self.x0 + PERIOD[0] * i, self.y0 + PERIOD[1] * j + 1
        if self.v_closed(i, j):
            c = cell(self.look.vertical, self.wall_ink(("v", i, j)),
                     self.palette[BG])
        else:
            beside = [(k, j) for k in (i - 1, i) if 0 <= k < self.mw]
            c = cell(" ", "default", self.dimmer(beside))
        self.canvas.put(x, y, c)

    def paint_h(self, i, j):
        x, y = self.x0 + PERIOD[0] * i + 1, self.y0 + PERIOD[1] * j
        if self.h_closed(i, j):
            c = cell(self.look.horizontal, self.wall_ink(("h", i, j)),
                     self.palette[BG])
        else:
            c = cell(" ", "default", self.dimmer([(i, j - 1), (i, j)]))
        for k in range(PERIOD[0] - 1):
            self.canvas.put(x + k, y, c)

    def paint_junction(self, i, j):
        x, y = self.x0 + PERIOD[0] * i, self.y0 + PERIOD[1] * j
        arms = (int(j > 0 and self.v_closed(i, j - 1)),
                int(i < self.mw and self.h_closed(i, j)),
                int(j < self.mh and self.v_closed(i, j)),
                int(i > 0 and self.h_closed(i - 1, j)))
        if any(arms):
            ink = self.wall_ink(("v", i, j - 1), ("h", i, j), ("v", i, j),
                                ("h", i - 1, j))
            self.canvas.put(x, y, cell(self.look.junction(arms), ink,
                                       self.palette[BG]))
            return
        # No wall meets here, which only happens inside an open room:
        # the corner takes the floor around it.
        around = [(a, b) for a in (i - 1, i) for b in (j - 1, j)
                  if 0 <= a < self.mw and 0 <= b < self.mh]
        self.canvas.put(x, y, cell(" ", "default", self.dimmer(around)))

    def paint_cell(self, i, j):
        col = self.colour(i, j)
        bg = self.palette[col]
        x, y = self.x0 + PERIOD[0] * i + 1, self.y0 + PERIOD[1] * j + 1
        for k in range(PERIOD[0] - 1):
            self.canvas.put(x + k, y, cell(" ", "default", bg))
        if (i, j) == self.goal:
            ink = self.exit_ink[lightness(bg) <= LIT]
            self.canvas.put(x + 1, y, cell(self.look.goal, ink, bg))
        self.paint_v(i, j)
        self.paint_v(i + 1, j)
        self.paint_h(i, j)
        self.paint_h(i, j + 1)
        for a in (i, i + 1):
            for b in (j, j + 1):
                self.paint_junction(a, b)

    def paint_all(self):
        for j in range(self.mh):
            for i in range(self.mw):
                self.paint_cell(i, j)


# ----------------------------------------------------------------- carvers

class Carver:
    """Makes a perfect maze, a step at a time.

    `run` is a generator that yields once per visible step; the show pulls
    on it at `RATE` steps a second. `open_all` says whether it starts from
    an open room rather than from solid ground.
    """

    name = title = ""
    RATE = 40.0
    open_all = False

    def run(self, m, rng):
        raise NotImplementedError


class Backtracker(Carver):
    """One corridor that snakes until it is stuck, then retreats to a fork.

    The retreat is walked rather than jumped, one cell a step, so the
    head is seen coming back along its own corridor; that is what makes
    the algorithm legible as the one that backtracks.
    """

    name, title = "backtracker", "recursive backtracker"

    def run(self, m, rng):
        at = (rng.randrange(m.mw), rng.randrange(m.mh))
        m.carve(*at)
        stack = [at]
        yield
        while stack:
            here = stack[-1]
            free = [n for n in m.neighbours(*here) if m.state_at(n) == BG]
            if free:
                nxt = rng.choice(free)
                m.link(here, nxt)
                m.carve(*nxt)
                stack.append(nxt)
            else:
                stack.pop()
                if stack:
                    m.glow(*stack[-1])
            yield


class Prim(Carver):
    """Grows from a seed at every point of its edge at once.

    The frontier is the uncarved cells beside the maze; one is taken at
    random and joined to a random carved neighbour. Random rather than
    weighted, which is the simplified Prim's and the one that looks like
    frost -- the true weighted one is Kruskal's in a different order.
    """

    name, title = "prim", "Prim's"

    def run(self, m, rng):
        at = (rng.randrange(m.mw), rng.randrange(m.mh))
        m.carve(*at)
        edge = list(m.neighbours(*at))
        on_edge = set(edge)
        yield
        while edge:
            k = rng.randrange(len(edge))
            edge[k], edge[-1] = edge[-1], edge[k]
            c = edge.pop()
            on_edge.discard(c)
            into = [n for n in m.neighbours(*c) if m.state_at(n) != BG]
            m.link(c, rng.choice(into))
            m.carve(*c)
            for n in m.neighbours(*c):
                if m.state_at(n) == BG and n not in on_edge:
                    on_edge.add(n)
                    edge.append(n)
            yield


class Kruskal(Carver):
    """Opens walls anywhere at random, joining fragments that never loop.

    Every wall in a random order; a wall comes down if the cells either
    side are not yet joined. The maze exists everywhere before it exists
    anywhere, and the last few walls to fall join regions the size of
    the screen.
    """

    name, title = "kruskal", "Kruskal's"
    RATE = 30.0

    def run(self, m, rng):
        edges = [((i, j), n) for (i, j) in m.cells()
                 for n in m.neighbours(i, j) if n > (i, j)]
        rng.shuffle(edges)
        parent = {c: c for c in m.cells()}

        def find(c):
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        yield
        for a, b in edges:
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            parent[ra] = rb
            m.link(a, b)
            m.carve(*a)
            m.carve(*b)
            yield


class Wilson(Carver):
    """Loop-erased random walks, joined to the maze one whole walk at a time.

    From an unvisited cell, walk at random; on stepping onto the walk's
    own trail, erase the loop back to that point; on stepping onto the
    maze, carve the whole walk in one go. Hopeless early, when the maze
    is one cell and a walk can wander for a long time before finding it,
    then faster and faster. The walk is shown in the path colour with its
    walls still up, because it is not yet part of the maze.

    The one carver that chooses uniformly among every spanning tree of the
    grid, which is why its mazes have no character of their own.
    """

    name, title = "wilson", "Wilson's"
    #: Quick, because a walk step is one cell and most of them are erased:
    #: the walk on a full wall is thousands of steps for eight hundred cells.
    RATE = 110.0

    def run(self, m, rng):
        cells = m.cells()
        rng.shuffle(cells)
        m.carve(*cells.pop())
        yield
        for c in cells:
            if m.state_at(c) != BG:
                continue
            walk = [c]
            index = {c: 0}
            m.set_state(*c, PATH)
            m.glow(*c)
            yield
            while True:
                nxt = rng.choice(m.neighbours(*walk[-1]))
                if nxt in index:
                    k = index[nxt]
                    for dropped in walk[k + 1:]:
                        del index[dropped]
                        m.set_state(*dropped, BG)
                    del walk[k + 1:]
                    m.glow(*nxt)
                elif m.state_at(nxt) == BG:
                    index[nxt] = len(walk)
                    walk.append(nxt)
                    m.set_state(*nxt, PATH)
                    m.glow(*nxt)
                else:
                    for a, b in zip(walk, walk[1:] + [nxt]):
                        m.link(a, b)
                        m.carve(*a)
                    yield
                    break
                yield


class Division(Carver):
    """Starts with one open room and draws walls across it, each with a gap.

    A room is cut across its longer side into two, and each half is cut
    again, largest rooms first, until every room is one cell wide or tall.
    A wall is drawn one segment a step, so it is seen being laid, and the
    gap left in it is the only way through. The only carver that builds
    walls rather than passages, and the one whose mazes have long straight
    corridors, which a walker across the room can read from the doorway.
    """

    name, title = "division", "recursive division"
    open_all = True
    RATE = 30.0

    def run(self, m, rng):
        rooms = collections.deque([(0, 0, m.mw, m.mh)])
        yield
        while rooms:
            x, y, w, h = rooms.popleft()
            if w < 2 or h < 2:
                continue
            across = h > w or (h == w and rng.random() < 0.5)
            if across:
                k = rng.randrange(1, h)
                gap = rng.randrange(x, x + w)
                for i in range(x, x + w):
                    if i != gap:
                        m.wall_between((i, y + k - 1), (i, y + k))
                        yield
                rooms.append((x, y, w, k))
                rooms.append((x, y + k, w, h - k))
            else:
                k = rng.randrange(1, w)
                gap = rng.randrange(y, y + h)
                for j in range(y, y + h):
                    if j != gap:
                        m.wall_between((x + k - 1, j), (x + k, j))
                        yield
                rooms.append((x, y, k, h))
                rooms.append((x + k, y, w - k, h))


# ----------------------------------------------------------------- solvers

def trace(parent, goal):
    path = [goal]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    path.reverse()
    return path


class Solver:
    """Finds the path from start to goal, a step at a time.

    `run` yields once per cell looked at and returns the path, a list of
    cells from start to goal, or None if there is none. Cells it is about
    to look at are HOT, cells it has looked at and moved on from are DEAD,
    and a solver that carries a current path shows it in PATH.
    """

    name = title = short = ""
    RATE = 30.0

    def run(self, m, rng):
        raise NotImplementedError


class Breadth(Solver):
    """Breadth-first: a wave that floods every corridor at the same pace."""

    name, title, short = "bfs", "breadth-first", "bfs"

    def run(self, m, rng):
        parent = {m.start: None}
        queue = collections.deque([m.start])
        m.set_state(*m.start, HOT)
        while queue:
            here = queue.popleft()
            m.set_state(*here, DEAD)
            if here == m.goal:
                return trace(parent, m.goal)
            for n in m.passages(*here):
                if n not in parent:
                    parent[n] = here
                    queue.append(n)
                    m.set_state(*n, HOT)
            yield
        return None


class Depth(Solver):
    """Depth-first: commits to one corridor at a time and is wrong until
    it is not. Its stack is a path, and is shown as one."""

    name, title, short = "dfs", "depth-first", "dfs"

    def run(self, m, rng):
        stack = [m.start]
        seen = {m.start}
        m.set_state(*m.start, HOT)
        while stack:
            here = stack[-1]
            if here == m.goal:
                return list(stack)
            free = [n for n in m.passages(*here) if n not in seen]
            if free:
                nxt = rng.choice(free)
                seen.add(nxt)
                m.set_state(*here, PATH)
                m.set_state(*nxt, HOT)
                stack.append(nxt)
            else:
                stack.pop()
                m.set_state(*here, DEAD)
                if stack:
                    m.set_state(*stack[-1], HOT)
            yield
        return None


class AStar(Solver):
    """A* with the walking distance to the exit as its guess.

    In a maze the guess is often wrong -- the corridor that heads for the
    exit is the one that dead-ends beside it -- but it is right often
    enough that this one visibly pulls towards the corner. Ties go to the
    deeper cell, so it commits rather than dithers.
    """

    name, title, short = "astar", "A*", "a*"

    def run(self, m, rng):
        gi, gj = m.goal

        def guess(c):
            return abs(c[0] - gi) + abs(c[1] - gj)

        cost = {m.start: 0}
        parent = {m.start: None}
        heap = [(guess(m.start), 0, 0, m.start)]
        done = set()
        tie = 0
        m.set_state(*m.start, HOT)
        while heap:
            _, _, _, here = heapq.heappop(heap)
            if here in done:
                continue
            done.add(here)
            m.set_state(*here, DEAD)
            if here == m.goal:
                return trace(parent, m.goal)
            for n in m.passages(*here):
                g = cost[here] + 1
                if g < cost.get(n, g + 1):
                    cost[n] = g
                    parent[n] = here
                    tie += 1
                    heapq.heappush(heap, (g + guess(n), -g, tie, n))
                    m.set_state(*n, HOT)
            yield
        return None


class DeadEnd(Solver):
    """Dead-end filling: never searches, just blacks out every dead end
    from its tip inward until only the answer is left.

    A dead end is a cell with one way out; filling it may leave the cell
    behind it with one way out, and so on up the corridor. Every dead end
    retreats at once, so the maze eats itself from its extremities. Steps
    counted are cells filled, which is everything but the path.
    """

    name, title, short = "deadend", "dead-end filling", "fill"
    RATE = 45.0

    def run(self, m, rng):
        alive = set(m.cells())
        ends = (m.start, m.goal)

        def ways(c):
            return sum(1 for n in m.passages(*c) if n in alive)

        queue = collections.deque(c for c in m.cells()
                                  if c not in ends and ways(c) <= 1)
        while queue:
            c = queue.popleft()
            if c not in alive or c in ends or ways(c) > 1:
                continue
            alive.discard(c)
            m.set_state(*c, DEAD)
            m.glow(*c)
            for n in m.passages(*c):
                if n in alive and n not in ends and ways(n) <= 1:
                    queue.append(n)
            yield
        path, prev = [m.start], None
        while path[-1] != m.goal:
            on = [n for n in m.passages(*path[-1]) if n in alive and n != prev]
            if len(on) != 1:
                return None
            prev = path[-1]
            path.append(on[0])
        return path


CARVERS = {c.name: c for c in (Backtracker, Prim, Kruskal, Wilson, Division)}
SOLVERS = {s.name: s for s in (Breadth, Depth, AStar, DeadEnd)}


# ----------------------------------------------------------------- the show

class Show:
    """One maze after another: carved by the next carver, solved by every
    solver in turn, each answer held, then bare ground again.

    Built for an area and thrown away when the area changes; the rotation
    through the carvers lives in `turn`, which is shared across rebuilds
    so a resize does not start the cycle over.
    """

    def __init__(self, cols, rows, rng, look, opts, turn):
        self.cols, self.rows = cols, rows
        self.rng, self.look, self.opts, self.turn = rng, look, opts, turn
        self.canvas = Canvas(cols, rows, PALETTE[BG])
        self.carvers = [CARVERS[n]() for n in opts["carve"]]
        self.solvers = [SOLVERS[n]() for n in opts["solve"]]
        pw, ph = PERIOD
        # The first row belongs to the status line, so the maze is sized to
        # the rows under it and centred among them.
        mw, mh = (cols - 1) // pw, (rows - 2) // ph
        self.maze = None
        if mw >= 2 and mh >= 2:
            x0 = (cols - (pw * mw + 1)) // 2
            y0 = 1 + (rows - 1 - (ph * mh + 1)) // 2
            self.maze = Maze(self.canvas, look, mw, mh, x0, y0)
        self.phase = None
        self.caption_text = None
        self.tally = []
        self.begin_carve()

    # -- phases

    def begin_carve(self):
        self.carver = self.carvers[self.turn["n"] % len(self.carvers)]
        self.turn["n"] += 1
        self.tally = []
        self.k = 0
        self.steps = 0
        self.counted = 0.0
        if self.maze:
            self.maze.dress(solved=False)
            self.maze.reset(self.carver.open_all)
            self.gen = self.carver.run(self.maze, self.rng)
        self.clock = Clock(self.carver.RATE * self.opts["speed"])
        self.phase = "carve"
        self.working()

    def begin_solve(self):
        self.solver = self.solvers[self.k]
        self.steps = 0
        self.counted = 0.0
        self.maze.dress(solved=False)
        self.maze.clear_marks()
        self.gen = self.solver.run(self.maze, self.rng)
        self.clock = Clock(self.solver.RATE * self.opts["speed"])
        self.phase = "solve"
        self.working()

    def step(self, now):
        """Advance whatever is running by the steps it is owed, and bring
        its count in the status line up to date when that is due."""
        m = self.maze
        m.now = now
        cap = max(1, int(self.clock.rate / 2))
        for _ in range(self.clock.owed(now, cap)):
            try:
                next(self.gen)
            except StopIteration as done:
                if self.phase == "carve":
                    self.phase = "carved"
                    self.until = now + HOLD_CARVED
                    self.caption("maze generated by", self.carver.title,
                                 f"{m.mw * m.mh} cells")
                else:
                    path = done.value
                    self.phase = "solved"
                    self.until = now + self.opts["hold"]
                    m.dress(solved=True)
                    if path:
                        m.show_path(path)
                        self.tally.append(f"{self.solver.short} {self.steps}")
                        self.caption("solved with", self.solver.title,
                                     f"{self.steps} moves · "
                                     f"path {len(path)}")
                    else:
                        self.tally.append(f"{self.solver.short} lost")
                        self.caption("no path found by", self.solver.title)
                return
            self.steps += 1
        if now >= self.counted:
            self.counted = now + COUNT_EVERY
            self.working()

    def tick(self, now):
        if self.maze is None:
            self.caption("no room in this area for a", "maze")
            return
        if self.phase in ("carve", "solve"):
            self.step(now)
        elif now >= self.until:
            if self.phase == "carved":
                self.begin_solve()
            elif self.k + 1 < len(self.solvers):
                self.k += 1
                self.begin_solve()
            else:
                self.begin_carve()
        self.maze.cool()

    # -- the status line

    def working(self):
        """What the running algorithm has to say for itself, with the count
        of the steps it has taken so far."""
        if self.phase == "carve":
            self.caption("generating maze with", self.carver.title,
                         f"{self.steps} steps")
        else:
            self.caption("solving with", self.solver.title,
                         f"{self.steps} moves")

    def caption(self, doing, title, rest=""):
        """One line along the top: what is being done, the algorithm doing
        it, whatever that algorithm has to add, and the tally of this maze's
        solvers so far, if there is room."""
        left = f"{doing} {title}"
        at = len(doing) + 1
        if len(left) > self.cols:
            # On a wall too narrow for the sentence, the name of the
            # algorithm is the half worth keeping.
            left, at = title, 0
        if rest:
            left += f" · {rest}"
        right = "   ".join(self.tally)
        if len(left) + 4 + len(right) > self.cols:
            right = ""
        text = left + " " * (self.cols - len(left) - len(right)) + right
        if text == self.caption_text:
            return
        self.caption_text = text
        for x, ch in enumerate(text[:self.cols]):
            ink = TITLE if at <= x < at + len(title) else INK
            self.canvas.put(x, 0, cell(ch, ink, PALETTE[BG]))

    def frame(self):
        return self.canvas.frame()


# ----------------------------------------------------------------- front end

def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except ValueError:
            say(f"maze: {text!r} is not a number, using {default}",
                file=sys.stderr, flush=True)
            return default
    return convert


def clamp(name, value, lo, hi):
    got = min(hi, max(lo, value))
    if got != value:
        say(f"maze: {name} {value:g} is outside {lo:g}..{hi:g}, using {got:g}",
            file=sys.stderr, flush=True)
    return got


def chosen(what, text, table):
    """The names asked for that exist, in the order asked; all if none do."""
    if text == "all":
        return list(table)
    names, dropped = [], []
    for n in text.split(","):
        (names if n in table else dropped).append(n)
    if dropped:
        say(f"maze: no {what} called {', '.join(repr(d) for d in dropped)}"
            f"; I have {' '.join(table)}", file=sys.stderr, flush=True)
    if not names:
        say(f"maze: using every {what}", file=sys.stderr, flush=True)
        return list(table)
    return names


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="maze", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bad values are clamped or defaulted, never fatal: the wall "
               "keeps drawing and stderr says what changed.")
    ap.add_argument("host", nargs="?", help="receiver to attach to")
    ap.add_argument("--carve", default="all",
                    help="one carver, several comma-separated, or all "
                         "(default); --list names them")
    ap.add_argument("--solve", default="all",
                    help="one solver, several comma-separated, or all "
                         "(default); each maze is solved by every one")
    ap.add_argument("--list", action="store_true",
                    help="name the carvers and solvers and exit")
    ap.add_argument("--speed", type=number(1.0), default=1.0,
                    help="multiplier on every algorithm's pace (default 1, "
                         "0.1..10)")
    ap.add_argument("--hold", type=number(DEFAULT_HOLD), default=DEFAULT_HOLD,
                    help=f"seconds a solved maze is shown before the next "
                         f"solver (default {DEFAULT_HOLD:g}, 0..120)")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=number(DEFAULT_FPS), default=DEFAULT_FPS,
                    help=f"frames a second at most (default {DEFAULT_FPS:g})")
    ap.add_argument("--seed", type=number(None), default=None,
                    help="random seed, for a run that repeats")
    args, unknown = ap.parse_known_intermixed_args(argv)
    for u in unknown:
        say(f"maze: ignoring unknown argument {u!r}", file=sys.stderr,
            flush=True)
    if args.list:
        print("carve: " + " ".join(CARVERS))
        print("solve: " + " ".join(SOLVERS))
        sys.exit(0)
    if not args.host:
        ap.error("no host given")
    return args


def main():
    args = parse(sys.argv[1:])
    opts = {"carve": chosen("carver", args.carve, CARVERS),
            "solve": chosen("solver", args.solve, SOLVERS),
            "speed": clamp("speed", args.speed, 0.1, 10),
            "hold": clamp("hold", args.hold, 0, 120)}
    font = int(clamp("font", args.font, 4, 400))
    fps = clamp("fps", args.fps, 1, 60)
    rng = random.Random(None if args.seed is None else int(args.seed))

    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    look = Look(wall)
    for note in look.said():
        say(f"maze: {note}", file=sys.stderr, flush=True)

    turn = {"n": 0}
    showing = {"show": None}

    def render(cols, rows):
        show = showing["show"]
        if show is None or (show.cols, show.rows) != (cols, rows):
            show = showing["show"] = Show(cols, rows, rng, look, opts, turn)
        show.tick(time.monotonic())
        return show.frame()

    with wall.transition():
        wall.set_font_size(font)
        wall.set_source(render)
    wall.meta(name="maze")
    cols, rows = wall.area()
    say(f"maze -> {args.host}  {cols}x{rows} at font {font}  "
        f"carve {'+'.join(opts['carve'])}  solve {'+'.join(opts['solve'])}  "
        f"{fps:g}fps  (ctrl-c to stop)", flush=True)

    try:
        while not stop.wait(0.5):
            pass
    finally:
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
