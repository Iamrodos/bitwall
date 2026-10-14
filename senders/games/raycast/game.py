"""A self-playing Doom-style raycaster drawn on a character grid.

The core owns the world, the bot that walks it, and the drawing. It hands
back a grid of `Glyph` -- a character with 256-palette colours -- and knows
nothing about bitwall and nothing about terminals, so the same engine drives
a wall through `main.py` and a terminal through `pane-raycast`.

**Two pixels per cell.** A terminal cell is about twice as tall as it is
wide, so the upper half block `U+2580` with a foreground and a background
colour is two independently coloured square pixels stacked in one cell. A
95x23 pane is therefore a 95x46 framebuffer and a 194x53 wall is 194x106.
`Framebuffer` is that surface; it is the only thing that knows about the
half block, and the raycaster below only ever writes pixels.

**One ray per column.** A character grid is made of vertical slabs, which is
exactly what a raycaster produces: cast a ray per screen column, walk the
grid with DDA until it meets a wall, and the perpendicular distance to that
wall gives the height of the slab to draw. Perpendicular rather than
euclidean is what removes the fisheye, and the camera-plane formulation
below gives it for free.

**Cost lives in the tables, not the loop.** Wall colour is a function of
material, face, distance and texel, and all four are known in advance, so
`WALLTEX` is baked at import into a strip of finished palette indices per
material, face, shade and texture column. Drawing a slab is then a lookup
per pixel and one strided slice assignment per column, which is the whole
reason a 194x106 framebuffer holds 12fps in pure Python.

**The bot.** It is watched, not played, so smooth motion beats tactics: a
breadth-first path to a goal, a carrot point a little way along it, a turn
rate cap, and forward speed scaled by how squarely it is facing the way it
wants to go. It hunts and shoots the things that move, and a progress
watchdog gives it a fresh goal if it stops covering ground.
"""
import math
import os
import random
import time
from collections import deque, namedtuple

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

UPPER = "▀"          # the half block that makes a cell two pixels

TEXSZ = 32                # wall texture is TEXSZ x TEXSZ texels
TEXMASK = TEXSZ - 1
# Quantised: neighbouring columns land on the same colour, which is what
# makes a slab of wall one long unchanged run for the stream to encode.
SHADES = 12
FOG = 11.0                # distance at which a wall reaches the darkest shade
FOV = 64.0                # horizontal field of view, degrees

# A head bob is a footfall, and at twelve frames a second it has to be
# paced like one: any faster and the horizon lands somewhere different
# every frame, which reads as the picture shaking rather than as walking.
# At full speed this turns the sine over in about a second, or two steps.
BOB_RATE = 2.6
BOB_PIXELS = 1.0


# --------------------------------------------------------------- colours

CUBE = (0, 95, 135, 175, 215, 255)


def _level(v):
    """The cube level, 0..5, nearest an 8-bit channel value."""
    best, bi = 1e9, 0
    for i, c in enumerate(CUBE):
        d = abs(c - v)
        if d < best:
            best, bi = d, i
    return bi


def _grey(v):
    """The 24-step grey nearest a 0..1 intensity."""
    gi = int(round((v * 255.0 - 8) / 10.0))
    return 16 if gi < 0 else (231 if gi > 23 else 232 + gi)


def spec(rgb):
    """How a colour wants to be shaded: along the greys, or along the cube.

    A neutral keeps its real intensity, because the 24-step grey ramp can
    render it far more finely than the cube's six levels; anything with a
    hue keeps its cube level triple.
    """
    lv = tuple(_level(c) for c in rgb)
    if max(lv) == min(lv):
        return (0, sum(rgb) / 765.0)
    return (1, lv)


def shade(sp, t):
    """One palette index: a colour dimmed to t of itself.

    Dimming in RGB and then quantising is what wrecks a dark ramp. The cube
    has six levels per channel and nothing between 0 and 95, so a slate
    heading for black loses red and green before blue and turns navy for a
    few rows, and a dark brick lands on a grey because grey is numerically
    nearer than any red the palette can offer.

    Dimming in the cube's own level space instead keeps the hue by
    construction: the brightest channel sets the step, the others follow in
    proportion, so a red stays red all the way down and only ever runs out
    of steps.
    """
    kind, v = sp
    if kind == 0:
        return _grey(v * t)
    m = max(v)
    step = int(m * t + 0.5)
    if step <= 0:
        return 16
    r, g, b = (int(c * step / m + 0.5) for c in v)
    return 16 + 36 * r + 6 * g + b


def ramp(rgb, n=SHADES, lo=0.22, hi=1.0):
    """n palette indices from a dim version of a colour up to the colour."""
    sp = spec(rgb)
    return tuple(shade(sp, lo + (hi - lo) * (i / (n - 1.0))) for i in range(n))


# Distance to shade, indexed by int(distance * 8). One table read replaces a
# divide and a clamp for every column drawn.
DIST_SHADE = tuple(
    max(0, min(SHADES - 1, int((SHADES - 1) * (1.0 - (i / 8.0) / FOG) + 0.5)))
    for i in range(256))

# Bands rather than a fade to black: a corridor that ends in darkness hides
# the thing the renderer is for, which is the perspective. The ceiling is
# neutral so it takes the fine grey ramp and gradates smoothly; the floor is
# warm, so the cube gives it three or four broad bands, which is what a flat
# floor wants anyway.
CEIL_RAMP = ramp((150, 150, 150), lo=0.16, hi=0.62)
FLOOR_RAMP = ramp((255, 175, 95), lo=0.22, hi=0.70)

HUD_BG = 234
HUD_FG = 250
HUD_DIM = 240
HUD_HOT = 203
HUD_GOLD = 220
HUD_GREEN = 77
MAP_BG = 235
MAP_WALL = 244
MAP_SEEN = 238
MAP_PLAYER = 231
MAP_FOE = 196


# -------------------------------------------------------------- textures

def _tex_brick():
    t = [[0] * TEXSZ for _ in range(TEXSZ)]
    for y in range(TEXSZ):
        off = ((y // 8) % 2) * 8
        joint_y = (y % 8) == 0
        for x in range(TEXSZ):
            if joint_y or ((x + off) % 16) == 0:
                t[y][x] = -4
            elif (y % 8) == 1:
                t[y][x] = 1
            else:
                t[y][x] = -1 if ((x * 7 + y * 13) % 5) == 0 else 0
    return t


def _tex_panel():
    t = [[0] * TEXSZ for _ in range(TEXSZ)]
    for y in range(TEXSZ):
        for x in range(TEXSZ):
            mx, my = x % 16, y % 16
            if mx == 0 or my == 0:
                v = -4
            elif mx == 1 or my == 1:
                v = 1
            elif mx in (4, 11) and my in (4, 11):
                v = 2
            else:
                v = -1 if (my > 12) else 0
            t[y][x] = v
    return t


def _tex_stone():
    rng = random.Random(20260915)
    t = [[0] * TEXSZ for _ in range(TEXSZ)]
    for y in range(TEXSZ):
        for x in range(TEXSZ):
            v = rng.choice((-2, -1, -1, 0, 0, 0, 1))
            if (y % 11) == 0:
                v -= 2
            t[y][x] = v
    return t


def _tex_metal():
    t = [[0] * TEXSZ for _ in range(TEXSZ)]
    for y in range(TEXSZ):
        for x in range(TEXSZ):
            v = ((x * 5) % 7) - 3
            v = 1 if v > 0 else (-3 if v < -1 else 0)
            if y in (0, 1, 30, 31):
                v = -4
            elif y in (15, 16):
                v = -2
            t[y][x] = v
    return t


# material -> (base rgb, texel offsets). Index 0 is unused: the map stores
# 0 for open floor, so a material id is always truthy. The bases run to the
# top of a cube channel so the level ladder has its full five steps; a
# mid-bright base would have three, and three is not a gradient.
MATERIALS = (
    None,
    ((225, 225, 225), _tex_stone),     # 1 grey stone
    ((255, 95, 95), _tex_brick),       # 2 brick
    ((95, 255, 255), _tex_panel),      # 3 panel
    ((255, 135, 0), _tex_metal),       # 4 rusted metal
)

RAMPN = SHADES + 3       # headroom so the two faces can sit apart on one ramp
SIDE_LIFT = 3            # north/south faces are this much brighter than east/west


def _bake_walls():
    """WALLTEX[material][face][shade][texel_x] -> a column of palette indices.

    Baking the texel offset into the shade here is what keeps the inner
    loop to a lookup: the drawing code never clamps, never multiplies and
    never touches RGB.

    A face is a step along the same ramp rather than a separately dimmed
    colour. Dimming the RGB and re-quantising drops a dark brick's side onto
    a grey, because that is the nearest thing the palette has; moving along
    the ladder the material already owns cannot change its hue.
    """
    out = [None]
    for rgb, texfn in MATERIALS[1:]:
        tex = texfn()
        pal = ramp(rgb, n=RAMPN, lo=0.16, hi=0.88)
        faces = []
        for face in (0, 1):
            lift = 0 if face else SIDE_LIFT
            shades = []
            for s in range(SHADES):
                cols = []
                for x in range(TEXSZ):
                    cols.append(tuple(
                        pal[max(0, min(RAMPN - 1, s + lift + tex[y][x]))]
                        for y in range(TEXSZ)))
                shades.append(tuple(cols))
            faces.append(tuple(shades))
        out.append(tuple(faces))
    return tuple(out)


WALLTEX = _bake_walls()


# --------------------------------------------------------------- sprites

# Art colours are named at full brightness and land on a cube corner, so
# every one of them has a whole ladder to be shaded down. A colour picked
# for how it reads on paper -- a dark brown, a deep red -- has no dark
# neighbours in a 240-colour palette and bakes out as black.
SPRITE_KEY = {
    ".": None,
    "k": (48, 48, 48), "D": (95, 95, 95), "d": (135, 135, 95),
    "g": (95, 175, 95), "G": (135, 255, 135),
    "r": (175, 0, 0), "R": (255, 95, 95), "p": (255, 175, 95),
    "y": (255, 215, 0), "Y": (255, 255, 175),
    "w": (255, 255, 255), "s": (175, 175, 175), "S": (215, 215, 215),
    "b": (95, 95, 175), "o": (255, 135, 0), "m": (175, 0, 175),
    "e": (255, 255, 215), "c": (175, 215, 255),
}

ART_BARREL = (
    "...dddddd...",
    "..dGGGGGGd..",
    ".dGggggggGd.",
    ".dGggggggGd.",
    ".dDkkkkkkDd.",
    ".dDkyyyykDd.",
    ".dDkyYYykDd.",
    ".dDkyyyykDd.",
    ".dDkkkkkkDd.",
    ".dGggggggGd.",
    ".dGggggggGd.",
    ".dGggggggGd.",
    ".dGggggggGd.",
    ".dGggggggGd.",
    "..dGGGGGGd..",
    "...dddddd...",
)

ART_PILLAR = (
    ".SSSSSSSS.",
    "SSSSSSSSSS",
    "SssssssssS",
    ".ssssssss.",
    "..sSSSSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSssSs..",
    "..sSSSSs..",
    ".ssssssss.",
    "SssssssssS",
    "SSSSSSSSSS",
    ".SSSSSSSS.",
    "..SSSSSS..",
)

ART_IMP = (
    ".....rrrr.....",
    "...rrRRRRrr...",
    "..rRRRRRRRRr..",
    "..rRRRRRRRRr..",
    "..rRyyRRyyRr..",
    "..rRRRRRRRRr..",
    "..rRRkkkkRRr..",
    "...rRRRRRRr...",
    "....rrrrrr....",
    "..rrRRRRRRrr..",
    ".rRRRRRRRRRRr.",
    "rRRr.RRRR.rRRr",
    "rRr..RRRR..rRr",
    "rr...RRRR...rr",
    ".....rrrr.....",
    "....rRRRRr....",
    "...rRRr.rRRr..",
    "...rrr...rrr..",
)

ART_CORPSE = (
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "..............",
    "...rr....rr...",
    "..rRRr..rRRr..",
    ".rRRRRrrRRRRr.",
    "rRRRRRRRRRRRRr",
    "rRkkRRRRRRkkRr",
    "rrrrrrrrrrrrrr",
)

ART_LAMP = (
    "..YYYY..",
    ".YeeeeY.",
    "Yeeeeeey",
    "YeeYYeey",
    ".yyYYyy.",
    "..oooo..",
    "..s..s..",
    "..sSSs..",
    "..sSSs..",
    "..sSSs..",
    "..sSSs..",
    ".ssSSss.",
    ".SSSSSS.",
    "SSSSSSSS",
)

ART_GUN = (
    ".............kssk.............",
    ".............kssk.............",
    ".............ksSk.............",
    ".............ksSk.............",
    ".............ksSk.............",
    ".............ksSk.............",
    ".............ksSk.............",
    "...........kkssSSkk...........",
    "...........ksSSSSsk...........",
    "..........kdDDDDDDdk..........",
    "..........kdDDDDDDdk..........",
    "........kdDDDDDDDDDDdk........",
    ".......kDDDDDDDDDDDDDDk.......",
    "......kDDDDDDDDDDDDDDDDk......",
    ".....kDDDDDDDDDDDDDDDDDDk.....",
    "....kDDDDDDDDDDDDDDDDDDDDk....",
)

ART_FLASH = (
    "......yy......",
    "....y.yy.y....",
    "...yyeeeeyy...",
    "..y.eeYYee.y..",
    ".yyeeYYYYeeyy.",
    "yy.eYYYYYYe.yy",
    ".yyeeYYYYeeyy.",
    "..y.eeYYee.y..",
    "...yyeeeeyy...",
    "....y.yy.y....",
    "......yy......",
)


def _bake_sprite(art, lo=0.26):
    """Art -> per-shade columns of palette index or None.

    Columns rather than rows because a billboard is drawn as vertical
    stripes, one per screen column, the same shape as a wall slab.
    """
    h = len(art)
    w = len(art[0])
    shades = []
    for s in range(SHADES):
        t = lo + (1.0 - lo) * (s / (SHADES - 1.0))
        cols = []
        for x in range(w):
            col = []
            for y in range(h):
                sp = SPRITE_SPEC.get(art[y][x])
                col.append(None if sp is None else shade(sp, t))
            cols.append(tuple(col))
        shades.append(tuple(cols))
    return tuple(shades)


SPRITE_SPEC = {k: (None if v is None else spec(v))
               for k, v in SPRITE_KEY.items()}

SPRITES = {
    "barrel": _bake_sprite(ART_BARREL),
    "pillar": _bake_sprite(ART_PILLAR),
    "imp": _bake_sprite(ART_IMP),
    "corpse": _bake_sprite(ART_CORPSE),
    "lamp": _bake_sprite(ART_LAMP, lo=0.62),   # it glows; it does not fade
}
GUN = _bake_sprite(ART_GUN, lo=0.6)[SHADES - 3]
FLASH = _bake_sprite(ART_FLASH, lo=1.0)[SHADES - 1]

# How tall a sprite stands relative to a wall of the same distance, and how
# far its feet sit above the floor line.
SPRITE_SCALE = {"barrel": 0.55, "pillar": 1.0, "imp": 0.80,
                "corpse": 0.28, "lamp": 0.62}
SPRITE_LIFT = {"barrel": 0.0, "pillar": 0.0, "imp": 0.0,
               "corpse": 0.0, "lamp": 0.34}


# ----------------------------------------------------------- framebuffer

class Framebuffer:
    """A w x 2h surface of palette indices, painted as half-block glyphs.

    Every cell becomes one `U+2580` with the top pixel as its foreground
    and the bottom pixel as its background, so the character grid carries
    twice its own height in square pixels.

    The buffer is flat and row-major, which makes a horizontal span a plain
    slice and a vertical span a strided slice. Both assign at C speed, and a
    raycaster writes almost nothing else.
    """

    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.ph = h * 2
        self.buf = [0] * (w * self.ph)
        self._solid = {}
        self._glyphs = _GlyphCache()

    def solid(self, colour):
        """A full-width row of one colour, kept so fills are one assignment."""
        row = self._solid.get(colour)
        if row is None:
            row = self._solid[colour] = [colour] * self.w
        return row

    def set_pixel(self, x, y, colour):
        if 0 <= x < self.w and 0 <= y < self.ph:
            self.buf[y * self.w + x] = colour

    def fill_rows(self, y0, y1, colour):
        w, buf, row = self.w, self.buf, self.solid(colour)
        for y in range(max(0, y0), min(self.ph, y1)):
            buf[y * w:y * w + w] = row

    def fill_rect(self, x, y, rw, rh, colour):
        x0, x1 = max(0, x), min(self.w, x + rw)
        if x1 <= x0:
            return
        span = [colour] * (x1 - x0)
        w, buf = self.w, self.buf
        for yy in range(max(0, y), min(self.ph, y + rh)):
            buf[yy * w + x0:yy * w + x1] = span

    def column(self, x, y0, y1, colours):
        """Write a vertical run in one strided assignment."""
        w = self.w
        self.buf[y0 * w + x:y1 * w + x:w] = colours

    def glyphs(self):
        """The surface as rows of Glyph, two pixels to a cell."""
        buf, w, h = self.buf, self.w, self.h
        get = self._glyphs.__getitem__
        out = []
        for y in range(h):
            o = 2 * y * w
            out.append(list(map(get, zip(buf[o:o + w], buf[o + w:o + 2 * w]))))
        return out


class _GlyphCache(dict):
    """Glyphs keyed by colour pair.

    Every cell of every frame changes in a raycaster, so building ten
    thousand namedtuples a frame is real work; there are only ever a few
    hundred distinct colour pairs on screen, so they are made once. Shared
    instances also make the front ends' frame diff an identity comparison.

    A cell whose two pixels are the same colour is a space on that
    background rather than a half block on itself. It looks identical and
    costs a third of the bytes, and the flat parts of a scene -- ceiling,
    floor, a wall face seen square on -- are most of the frame.
    """

    def __missing__(self, key):
        if len(self) > 40000:
            self.clear()
        top, bot = key
        g = Glyph(" ", None, bot, False) if top == bot \
            else Glyph(UPPER, top, bot, False)
        self[key] = g
        return g


# ------------------------------------------------------------------- map

class World:
    """A grid of rooms and corridors, and the things standing in it.

    Solid cells hold a material id and open cells hold 0, so the DDA loop
    tests one truthy lookup per step. The outer ring is always solid, which
    is what lets that loop run without a bounds check.
    """

    def __init__(self, w, h, rng):
        self.w, self.h = w, h
        self.rng = rng
        self.grid = [[1] * w for _ in range(h)]
        self.rooms = []
        self._carve()
        self._materials()
        self.open_cells = [(x, y)
                           for y in range(h) for x in range(w)
                           if not self.grid[y][x]]

    def _carve(self):
        rng, w, h = self.rng, self.w, self.h
        tries = 0
        while len(self.rooms) < 9 and tries < 400:
            tries += 1
            rw, rh = rng.randint(3, 7), rng.randint(3, 7)
            x, y = rng.randint(1, w - rw - 2), rng.randint(1, h - rh - 2)
            if any(x - 1 < r[0] + r[2] + 1 and r[0] - 1 < x + rw + 1 and
                   y - 1 < r[1] + r[3] + 1 and r[1] - 1 < y + rh + 1
                   for r in self.rooms):
                continue
            for yy in range(y, y + rh):
                for xx in range(x, x + rw):
                    self.grid[yy][xx] = 0
            self.rooms.append((x, y, rw, rh))
        for i in range(1, len(self.rooms)):
            ax, ay = self._centre(self.rooms[i - 1])
            bx, by = self._centre(self.rooms[i])
            self._corridor(ax, ay, bx, by)
        # One extra link so the map has a loop in it and the bot is not
        # forever walking back down the corridor it came in by.
        if len(self.rooms) > 3:
            ax, ay = self._centre(self.rooms[0])
            bx, by = self._centre(self.rooms[-1])
            self._corridor(ax, ay, bx, by)

    @staticmethod
    def _centre(r):
        return r[0] + r[2] // 2, r[1] + r[3] // 2

    def _corridor(self, ax, ay, bx, by):
        g = self.grid
        for x in range(min(ax, bx), max(ax, bx) + 1):
            g[ay][x] = 0
            if ay + 1 < self.h - 1:
                g[ay + 1][x] = 0
        for y in range(min(ay, by), max(ay, by) + 1):
            g[y][bx] = 0
            if bx + 1 < self.w - 1:
                g[y][bx + 1] = 0

    def _materials(self):
        """Give each wall the material of the room it stands nearest.

        Regions of one material read as places, which is what makes the map
        legible while walking it.
        """
        if not self.rooms:
            return
        mats = [self.rng.choice((1, 2, 3, 4)) for _ in self.rooms]
        cents = [self._centre(r) for r in self.rooms]
        for y in range(self.h):
            row = self.grid[y]
            for x in range(self.w):
                if not row[x]:
                    continue
                best, bi = 1e9, 0
                for i, (cx, cy) in enumerate(cents):
                    d = (cx - x) ** 2 + (cy - y) ** 2
                    if d < best:
                        best, bi = d, i
                row[x] = mats[bi]

    def solid(self, x, y):
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return 1
        return self.grid[int(y)][int(x)]

    def free(self, x, y, r=0.28):
        """Is a disc of radius r at (x, y) clear of walls?"""
        return not (self.solid(x - r, y - r) or self.solid(x + r, y - r) or
                    self.solid(x - r, y + r) or self.solid(x + r, y + r))

    def path(self, start, goal):
        """Breadth-first cells from start to goal, or None."""
        if start == goal:
            return [start]
        g, w, h = self.grid, self.w, self.h
        prev = {start: None}
        q = deque((start,))
        while q:
            cur = q.popleft()
            cx, cy = cur
            for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                           (cx, cy + 1), (cx, cy - 1)):
                if nx < 0 or ny < 0 or nx >= w or ny >= h:
                    continue
                if g[ny][nx] or (nx, ny) in prev:
                    continue
                prev[(nx, ny)] = cur
                if (nx, ny) == goal:
                    out = [goal]
                    while out[-1] != start:
                        out.append(prev[out[-1]])
                    out.reverse()
                    return out
                q.append((nx, ny))
        return None

    def visible(self, ax, ay, bx, by):
        """Line of sight, sampled along the segment."""
        dx, dy = bx - ax, by - ay
        n = int(max(abs(dx), abs(dy)) * 3) + 1
        sx, sy = dx / n, dy / n
        x, y = ax, ay
        for _ in range(n):
            x += sx
            y += sy
            if self.solid(x, y):
                return False
        return True


class Thing:
    """A billboard standing in the world."""

    __slots__ = ("kind", "x", "y", "hp", "hurt_at", "dead_at", "respawn_at",
                 "dist", "wobble")

    def __init__(self, kind, x, y, hp=0):
        self.kind = kind
        self.x, self.y = x, y
        self.hp = hp
        self.hurt_at = -9.0
        self.dead_at = 0.0
        self.respawn_at = 0.0
        self.dist = 0.0
        self.wobble = random.random() * 6.283


# ------------------------------------------------------------------ game

class Game:
    """One endless walk through a generated level.

    `frame(cols, rows)` is the whole public surface: an area in, a grid of
    Glyph out. The area is the front end's to report and may change between
    calls, so the framebuffer and every table sized by it are rebuilt when
    it does; the world and the walker survive, because they belong to the
    session rather than to the screen.
    """

    # Every cell changes every frame, so the receiver's damage tracking
    # cannot help and this is real bandwidth. Never faster than this.
    #: Every cell changes every frame, so the frame rate is the whole cost
    #: at both ends. Overridable to find a receiver's ceiling.
    FPS = int(os.environ.get("RAYCAST_FPS", "12"))
    MAP_W = MAP_H = 26

    SECTOR_KILLS = 22         # a fresh level, so an hour of this is not one room
    MOVE = 2.4                # cells a second at full tilt
    TURN = 2.6                # radians a second
    RADIUS = 0.27
    RELOAD = 0.42
    RANGE = 13.0

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.cols = self.rows = 0
        self.fb = None
        self.hud_rows = 0
        self.view_rows = 0
        self.view_ph = 0
        self._camx = ()
        self._bands = {}
        self._trows = {}
        self._grid = None
        self.frames = 0
        self.now = time.monotonic()
        self._last = self.now
        self.next_frame = 0.0    # when a front end may ask for the next one
        self.plane_k = math.tan(math.radians(FOV) * 0.5)
        self._new_level()

    # ------------------------------------------------------------ level

    def _new_level(self):
        self.world = World(self.MAP_W, self.MAP_H, self.rng)
        self.seen = [[False] * self.MAP_W for _ in range(self.MAP_H)]
        cx, cy = self.rng.choice(self.world.open_cells)
        self.px, self.py = cx + 0.5, cy + 0.5
        self.ang = self.rng.uniform(0, 6.283)
        self.things = []
        self._populate()
        self.health = 100.0
        self.ammo = 50
        self.kills = 0
        self.level = getattr(self, "level", 0) + 1
        self.path = []
        self.leg = 0
        self.goal = None
        self.mode = "explore"
        self.target = None
        self.fired_at = -9.0
        self.hit_at = -9.0
        self.hurt_at = -9.0
        self.died_at = 0.0
        self.dead = False
        self.bob = 0.0
        self.recoil = 0.0
        self.msg = "SECTOR %d" % self.level
        self.msg_at = self.now
        self.warp_at = self.now
        self._progress_pos = (self.px, self.py)
        self._progress_at = self.now
        self._repath(force=True)

    def _populate(self):
        rng = self.world.rng
        spots = [c for c in self.world.open_cells
                 if (c[0] + 0.5 - self.px) ** 2 + (c[1] + 0.5 - self.py) ** 2 > 9]
        rng.shuffle(spots)
        plan = (["imp"] * 5 + ["barrel"] * 6 + ["pillar"] * 4 + ["lamp"] * 5)
        for kind in plan:
            if not spots:
                break
            x, y = spots.pop()
            hp = 3 if kind == "imp" else (1 if kind == "barrel" else 0)
            self.things.append(Thing(kind, x + 0.5, y + 0.5, hp))

    # ----------------------------------------------------------- geometry

    def _fit(self, cols, rows):
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        # A thin status bar, and none at all on a short area: the view is
        # the thing worth looking at, so it is the last thing to give ground.
        self.hud_rows = max(3, min(6, rows // 8)) if rows >= 14 else 0
        self.view_rows = rows - self.hud_rows
        self.view_ph = self.view_rows * 2
        self.fb = Framebuffer(cols, rows) if cols > 0 and rows > 0 else None
        self._bands = {}
        self._trows = {}
        self._grid = None
        if cols > 0:
            k = self.plane_k
            self._camx = tuple(k * (2.0 * (x + 0.5) / cols - 1.0)
                               for x in range(cols))
            self._zbuf = [1e9] * cols

    def playable(self):
        return self.cols >= 24 and self.view_rows >= 6

    # --------------------------------------------------------------- bot

    def _repath(self, force=False):
        """Choose somewhere to be and a way to get there.

        Goals are biased towards cells the walker has not stood in, so the
        level opens up rather than the bot pacing one corridor.
        """
        w = self.world
        here = (int(self.px), int(self.py))
        pool = w.open_cells
        best = None
        for _ in range(24):
            c = self.rng.choice(pool)
            d = abs(c[0] - here[0]) + abs(c[1] - here[1])
            if d < 5:
                continue
            score = d + (0 if self.seen[c[1]][c[0]] else 14)
            if best is None or score > best[0]:
                best = (score, c)
        goal = best[1] if best else self.rng.choice(pool)
        p = w.path(here, goal)
        if p:
            self.goal, self.path, self.leg = goal, p, 0
        elif force:
            self.goal, self.path, self.leg = here, [here], 0

    def _pick_target(self):
        """The nearest imp that can be seen, if any."""
        best, bd = None, 1e9
        for t in self.things:
            if t.kind != "imp" or t.hp <= 0:
                continue
            d = math.hypot(t.x - self.px, t.y - self.py)
            if d < self.RANGE and d < bd and \
                    self.world.visible(self.px, self.py, t.x, t.y):
                best, bd = t, d
        return best

    def _carrot(self):
        """A point a little way along the path, which is what smooths corners.

        Aiming at the next cell centre makes the walk a sequence of turns
        taken on the spot. Aiming at a blend of the next two makes it a
        curve, and the difference is the whole feel of the thing.
        """
        p = self.path
        if not p:
            return None
        while self.leg < len(p) - 1:
            cx, cy = p[self.leg]
            if (cx + 0.5 - self.px) ** 2 + (cy + 0.5 - self.py) ** 2 < 0.55:
                self.leg += 1
            else:
                break
        i = min(self.leg, len(p) - 1)
        ax, ay = p[i][0] + 0.5, p[i][1] + 0.5
        if i + 1 < len(p):
            bx, by = p[i + 1][0] + 0.5, p[i + 1][1] + 0.5
            return ax * 0.45 + bx * 0.55, ay * 0.45 + by * 0.55
        return ax, ay

    def _think(self, dt):
        now = self.now
        self.seen[int(self.py)][int(self.px)] = True

        if self.dead:
            if now - self.died_at > 2.6:
                self._respawn()
            return

        foe = self._pick_target()
        if foe is not None and self.ammo > 0:
            self.mode = "hunt"
            self.target = foe
            aim = (foe.x, foe.y)
            want = math.hypot(foe.x - self.px, foe.y - self.py) > 2.6
        else:
            if self.mode == "hunt":
                self.mode = "explore"
                self._repath(force=True)
            self.target = None
            aim = self._carrot()
            want = True
            if self.goal is None or self.leg >= len(self.path) - 1:
                if aim is None or \
                        (aim[0] - self.px) ** 2 + (aim[1] - self.py) ** 2 < 0.4:
                    self._repath(force=True)
                    aim = self._carrot()

        if aim is None:
            return

        # Turn towards the aim point at a capped rate: a camera that snaps is
        # the one thing that makes this unwatchable.
        want_ang = math.atan2(aim[1] - self.py, aim[0] - self.px)
        err = (want_ang - self.ang + math.pi) % (2 * math.pi) - math.pi
        cap = self.TURN * dt
        self.ang += cap if err > cap else (-cap if err < -cap else err)
        self.ang %= 2 * math.pi

        # Forward speed falls away as the turn opens up, so a corner is taken
        # slowly and a straight is taken fast.
        align = math.cos(err)
        speed = self.MOVE * dt * (align if align > 0 else 0.0) ** 1.5
        if not want:
            speed *= 0.15
        if speed > 0:
            self._walk(math.cos(self.ang) * speed, math.sin(self.ang) * speed)
            self.bob += speed * BOB_RATE

        if self.mode == "hunt" and abs(err) < 0.14 and self.ammo > 0 and \
                now - self.fired_at > self.RELOAD:
            self._fire()

        # A watchdog on ground covered, not on velocity: a bot rubbing along
        # a wall is moving and still going nowhere. It answers with a new
        # goal and nothing else -- spinning the camera to break a wall hug
        # is a jump cut, and the capped turn reaches the new heading on its
        # own within half a second.
        if (self.px - self._progress_pos[0]) ** 2 + \
                (self.py - self._progress_pos[1]) ** 2 > 1.2:
            self._progress_pos = (self.px, self.py)
            self._progress_at = now
        elif now - self._progress_at > 2.2:
            self._progress_pos = (self.px, self.py)
            self._progress_at = now
            self.mode = "explore"
            self._repath(force=True)

        self._things_think(dt)
        if self.health < 100.0:
            self.health = min(100.0, self.health + 3.0 * dt)
        if self.ammo < 12 and now - self.hurt_at > 3.0:
            self.ammo = min(60, self.ammo + 1)
        if self.kills >= self.SECTOR_KILLS:
            self._new_level()

    def _walk(self, dx, dy):
        """Move, sliding along whatever it cannot pass through."""
        w, r = self.world, self.RADIUS
        if w.free(self.px + dx, self.py, r):
            self.px += dx
        if w.free(self.px, self.py + dy, r):
            self.py += dy

    def _fire(self):
        self.fired_at = self.now
        self.ammo -= 1
        self.recoil = 1.0
        t = self.target
        if t is None or t.hp <= 0:
            return
        t.hp -= 1
        t.hurt_at = self.now
        self.hit_at = self.now
        if t.hp <= 0:
            t.dead_at = self.now
            t.respawn_at = self.now + self.rng.uniform(7.0, 14.0)
            if t.kind == "imp":
                self.kills += 1
                self.ammo = min(60, self.ammo + 6)
                self._say("KILL  +6 AMMO")
            self.target = None

    def _things_think(self, dt):
        now = self.now
        for t in self.things:
            if t.hp <= 0 and t.respawn_at and now > t.respawn_at:
                spots = [c for c in self.world.open_cells
                         if (c[0] + .5 - self.px) ** 2 +
                            (c[1] + .5 - self.py) ** 2 > 36]
                if spots:
                    c = self.rng.choice(spots)
                    t.x, t.y = c[0] + 0.5, c[1] + 0.5
                t.hp = 3 if t.kind == "imp" else 1
                t.respawn_at = 0.0
                continue
            if t.kind != "imp" or t.hp <= 0:
                continue
            d = math.hypot(t.x - self.px, t.y - self.py)
            if d < 9.0 and self.world.visible(t.x, t.y, self.px, self.py):
                s = 1.0 * dt
                a = math.atan2(self.py - t.y, self.px - t.x)
                a += math.sin(now * 1.7 + t.wobble) * 0.6
                nx, ny = t.x + math.cos(a) * s, t.y + math.sin(a) * s
                if self.world.free(nx, t.y, 0.3):
                    t.x = nx
                if self.world.free(t.x, ny, 0.3):
                    t.y = ny
                if d < 2.2 and now - self.hurt_at > 0.7 and not self.dead:
                    self.hurt_at = now
                    self.health -= self.rng.uniform(4.0, 11.0)
                    if self.health <= 0:
                        self._die()

    def _die(self):
        self.health = 0.0
        self.dead = True
        self.died_at = self.now
        self._say("YOU DIED")

    def _respawn(self):
        spots = [c for c in self.world.open_cells
                 if not any(t.kind == "imp" and t.hp > 0 and
                            (t.x - c[0]) ** 2 + (t.y - c[1]) ** 2 < 25
                            for t in self.things)]
        c = self.rng.choice(spots or self.world.open_cells)
        self.px, self.py = c[0] + 0.5, c[1] + 0.5
        self.health = 100.0
        self.ammo = max(self.ammo, 25)
        self.dead = False
        self.mode = "explore"
        self._say("RESPAWN")
        self._repath(force=True)

    def _say(self, text):
        self.msg = text
        self.msg_at = self.now

    # ------------------------------------------------------------- draw

    def _band_rows(self, horizon):
        """Ceiling and floor shading for every pixel row, cached per horizon.

        Bands depend only on how far a row is from the horizon, and the
        horizon only moves with the head bob, so this is a handful of small
        lists reused for the life of the run.
        """
        rows = self._bands.get(horizon)
        if rows is None:
            ph = self.view_ph
            up = float(max(1, horizon))
            down = float(max(1, ph - horizon))
            rows = []
            for y in range(ph):
                d = y - horizon
                if d < 0:
                    t = min(1.0, -d / up)
                    rows.append(CEIL_RAMP[int(t * (SHADES - 1))])
                else:
                    t = min(1.0, d / down)
                    rows.append(FLOOR_RAMP[int(t * (SHADES - 1))])
            if len(self._bands) > 64:
                self._bands.clear()
            self._bands[horizon] = rows
        return rows

    def _tex_rows(self, line_h):
        """Texel row for every pixel of a slab of this height."""
        tr = self._trows.get(line_h)
        if tr is None:
            s = float(TEXSZ) / line_h
            tr = tuple(int(i * s) & TEXMASK for i in range(line_h))
            if len(self._trows) > 3000:
                self._trows.clear()
            self._trows[line_h] = tr
        return tr

    def _cast(self, fb, horizon):
        """One ray per column, DDA to the first solid cell, draw the slab."""
        px, py = self.px, self.py
        dirx, diry = math.cos(self.ang), math.sin(self.ang)
        planex, planey = -diry, dirx      # the camera plane, |plane| = tan(fov/2)
        grid = self.world.grid
        ph = self.view_ph
        w, column = fb.w, fb.column
        camx = self._camx
        zbuf = self._zbuf
        tex_rows = self._tex_rows
        walltex = WALLTEX
        dist_shade = DIST_SHADE
        ipx, ipy = int(px), int(py)

        for x in range(w):
            cx = camx[x]
            rdx = dirx + planex * cx
            rdy = diry + planey * cx
            mapx, mapy = ipx, ipy
            ddx = 1e30 if rdx == 0.0 else (1.0 / rdx if rdx > 0 else -1.0 / rdx)
            ddy = 1e30 if rdy == 0.0 else (1.0 / rdy if rdy > 0 else -1.0 / rdy)
            if rdx < 0.0:
                stepx = -1
                sdx = (px - mapx) * ddx
            else:
                stepx = 1
                sdx = (mapx + 1.0 - px) * ddx
            if rdy < 0.0:
                stepy = -1
                sdy = (py - mapy) * ddy
            else:
                stepy = 1
                sdy = (mapy + 1.0 - py) * ddy

            while True:
                if sdx < sdy:
                    sdx += ddx
                    mapx += stepx
                    side = 0
                else:
                    sdy += ddy
                    mapy += stepy
                    side = 1
                mat = grid[mapy][mapx]      # the border ring is always solid
                if mat:
                    break

            if side == 0:
                perp = sdx - ddx
                if perp < 0.02:
                    perp = 0.02
                wallx = py + perp * rdy
            else:
                perp = sdy - ddy
                if perp < 0.02:
                    perp = 0.02
                wallx = px + perp * rdx
            zbuf[x] = perp

            line_h = int(ph / perp)
            if line_h < 1:
                line_h = 1
            y0 = horizon - (line_h >> 1)
            dy0 = 0 if y0 < 0 else y0
            dy1 = y0 + line_h
            if dy1 > ph:
                dy1 = ph
            if dy1 <= dy0:
                continue

            wallx -= int(wallx)
            tx = int(wallx * TEXSZ)
            if (side == 0 and rdx > 0.0) or (side == 1 and rdy < 0.0):
                tx = TEXMASK - tx
            shade = dist_shade[255 if perp > 31.8 else int(perp * 8.0)]
            strip = walltex[mat][side][shade][tx]
            rows = tex_rows(line_h)[dy0 - y0:dy1 - y0]
            column(x, dy0, dy1, [strip[t] for t in rows])

    def _draw_things(self, fb, horizon):
        """Billboards, furthest first, clipped per column against the zbuffer."""
        px, py = self.px, self.py
        dirx, diry = math.cos(self.ang), math.sin(self.ang)
        planex, planey = -diry, dirx
        det = planex * diry - dirx * planey
        if det == 0.0:
            return
        inv = 1.0 / det
        w, ph, buf = fb.w, self.view_ph, fb.buf
        zbuf = self._zbuf

        vis = []
        for t in self.things:
            if t.hp <= 0 and t.kind != "imp":
                continue
            sx, sy = t.x - px, t.y - py
            depth = inv * (-planey * sx + planex * sy)
            if depth < 0.35 or depth > 26.0:
                continue
            lat = inv * (diry * sx - dirx * sy)
            vis.append((depth, lat, t))
        vis.sort(key=lambda v: -v[0])

        for depth, lat, t in vis:
            kind = "corpse" if (t.kind == "imp" and t.hp <= 0) else t.kind
            if t.hp <= 0 and kind != "corpse":
                continue
            tex = SPRITES[kind]
            shade = DIST_SHADE[255 if depth > 31.8 else int(depth * 8.0)]
            if t.hurt_at and self.now - t.hurt_at < 0.14:
                shade = SHADES - 1
            cols = tex[shade]
            tw, th = len(cols), len(cols[0])

            scale = SPRITE_SCALE[kind]
            h = int(ph / depth * scale)
            if h < 2:
                continue
            wid = max(1, int(h * tw / float(th)))
            screen_x = int((w * 0.5) * (1.0 + lat / (depth * self.plane_k)))
            x0 = screen_x - (wid >> 1)
            # Feet on the floor: a billboard hangs from the horizon by half a
            # wall, so its base is where a full-height wall's base would be.
            base = horizon + int(ph / depth * 0.5)
            y1 = base - int(ph / depth * SPRITE_LIFT[kind])
            y0 = y1 - h

            dx0 = 0 if x0 < 0 else x0
            dx1 = min(w, x0 + wid)
            dy0 = 0 if y0 < 0 else y0
            dy1 = min(ph, y1)
            if dx1 <= dx0 or dy1 <= dy0:
                continue
            colstep = tw / float(wid)
            rowstep = th / float(h)
            rows = [int((y - y0) * rowstep) for y in range(dy0, dy1)]
            for sxp in range(dx0, dx1):
                if zbuf[sxp] <= depth:
                    continue
                col = cols[int((sxp - x0) * colstep)]
                o = dy0 * w + sxp
                for r in rows:
                    c = col[r]
                    if c is not None:
                        buf[o] = c
                    o += w

    def _draw_gun(self, fb, horizon):
        """The weapon, and the flash that says it went off."""
        w, ph, buf = fb.w, self.view_ph, fb.buf
        tw, th = len(GUN), len(GUN[0])
        gw = max(10, min(56, w // 4))
        gh = max(4, int(gw * th / float(tw)))
        kick = int(self.recoil * gh * 0.30)
        x0 = (w - gw) // 2
        y0 = ph - gh + kick
        colstep = tw / float(gw)
        rowstep = th / float(gh)
        for sxp in range(max(0, x0), min(w, x0 + gw)):
            col = GUN[int((sxp - x0) * colstep)]
            for y in range(max(0, y0), ph):
                c = col[min(th - 1, int((y - y0) * rowstep))]
                if c is not None:
                    buf[y * w + sxp] = c

        since = self.now - self.fired_at
        if since < 0.10:
            ftw, fth = len(FLASH), len(FLASH[0])
            fw = max(6, gw // 2)
            fh = max(4, int(fw * fth / float(ftw)))
            fx = (w - fw) // 2
            fy = y0 - fh // 2
            cs = ftw / float(fw)
            rs = fth / float(fh)
            for sxp in range(max(0, fx), min(w, fx + fw)):
                col = FLASH[int((sxp - fx) * cs)]
                for y in range(max(0, fy), min(ph, fy + fh)):
                    c = col[int((y - fy) * rs)]
                    if c is not None:
                        buf[y * w + sxp] = c

    def _tint(self, fb, colour, rows):
        """Wash the top of the view, for a hit taken or a shot landed."""
        w, buf = fb.w, fb.buf
        for y in range(min(rows, self.view_ph)):
            o = y * w
            buf[o:o + w] = fb.solid(colour)

    # -------------------------------------------------------------- hud

    def _draw_hud(self, fb, text):
        """A thin status bar: condition, ammo, and the map in the corner.

        Bars and the map are pixels so the bar carries the same resolution
        as the scene; the labels are glyphs laid over the top, because a
        character is the one thing the half-block trick cannot draw. A bar
        is exactly two pixel rows, which is one cell row, so a label sits
        beside it without fighting it for the cell.
        """
        w, ph = fb.w, fb.ph
        p0 = self.view_ph
        if ph <= p0:
            return
        c0 = self.view_rows
        fb.fill_rows(p0, ph, HUD_BG)
        fb.fill_rows(p0, p0 + 1, 238)          # the line under the view

        # --- the map, tucked into the right-hand end of the bar
        by = p0 + 2
        bh = min(self.world.h, ph - by)
        bw = min(self.world.w, max(0, w // 3))
        bx = w - bw - 1
        if bw >= 8 and bh >= 4 and bx > 26:
            self._draw_map(fb, bx, by, bw, bh)
        else:
            bx = w

        # --- bars and readouts on the left
        bar_x = 4
        bar_w = max(6, min(30, bx - bar_x - 8))
        hp = max(0.0, min(1.0, self.health / 100.0))
        am = max(0.0, min(1.0, self.ammo / 60.0))
        fill = HUD_GREEN if hp > 0.6 else (HUD_GOLD if hp > 0.3 else HUD_HOT)
        rows_left = self.rows - (c0 + 1)
        if rows_left >= 1:
            self._bar(fb, bar_x, (c0 + 1) * 2, bar_w, hp, fill)
            text.append((0, c0 + 1, "HP", HUD_DIM))
            text.append((bar_x + bar_w + 1, c0 + 1, "%3d" % int(self.health),
                         HUD_HOT if self.health < 34 else HUD_FG))
        if rows_left >= 2:
            self._bar(fb, bar_x, (c0 + 2) * 2, bar_w, am, 38)
            text.append((0, c0 + 2, "AM", HUD_DIM))
            text.append((bar_x + bar_w + 1, c0 + 2, "%3d" % self.ammo, HUD_FG))
        if rows_left >= 3:
            state = "DEAD" if self.dead else self.mode
            live = self.now - self.msg_at < 2.6
            line = "s%d  k%-3d %s" % (self.level, self.kills, state)
            if live:
                line = "%-18s%s" % (line, self.msg)
            text.append((0, c0 + 3, line[:max(0, bx - 1)],
                         HUD_GOLD if live else HUD_DIM))

    def _bar(self, fb, x, py, w, frac, colour):
        n = int(w * frac + 0.5)
        fb.fill_rect(x, py, w, 2, 236)
        if n > 0:
            fb.fill_rect(x, py, n, 2, colour)

    def _draw_map(self, fb, bx, by, bw, bh):
        """One map cell per pixel, windowed on the walker and clamped.

        A scaled-to-fit map turns to mush in a bar this thin, so the window
        slides instead: whatever of the level fits is shown at full
        resolution, and a box large enough holds all of it.
        """
        mw, mh = self.world.w, self.world.h
        grid, seen = self.world.grid, self.seen
        ox = max(0, min(mw - bw, int(self.px) - bw // 2))
        oy = max(0, min(mh - bh, int(self.py) - bh // 2))
        buf, fbw = fb.buf, fb.w
        for j in range(bh):
            grow, srow = grid[oy + j], seen[oy + j]
            o = (by + j) * fbw + bx
            for i in range(bw):
                mx = ox + i
                if grow[mx]:
                    c = MAP_WALL if srow[mx] else 238
                else:
                    c = MAP_SEEN if srow[mx] else MAP_BG
                buf[o + i] = c

        def plot(mx, my, colour):
            i, j = int(mx) - ox, int(my) - oy
            if 0 <= i < bw and 0 <= j < bh:
                fb.set_pixel(bx + i, by + j, colour)

        for t in self.things:
            if t.kind == "imp" and t.hp > 0:
                plot(t.x, t.y, MAP_FOE)
        for k in (2.6, 1.8, 1.0):
            plot(self.px + math.cos(self.ang) * k,
                 self.py + math.sin(self.ang) * k, HUD_GOLD)
        plot(self.px, self.py, MAP_PLAYER)

    # ------------------------------------------------------------- frame

    def frame(self, cols, rows):
        """An area in, a grid of Glyph out. The whole of the public surface.

        Frames are capped rather than produced on demand: every cell of a
        raycast frame differs from the last one, so a front end asking for
        more of them costs real bandwidth and buys nothing the eye can use.
        A call before the next frame is due gets the last grid back.
        """
        now = time.monotonic()
        if self._grid is not None and (cols, rows) == (self.cols, self.rows) \
                and now < self.next_frame:
            return self._grid
        self.next_frame = now + 1.0 / self.FPS

        self._fit(cols, rows)
        dt = now - self._last
        if dt > 0.5:
            dt = 1.0 / self.FPS      # a stall is not a teleport
        self._last = now
        self.now = now

        if not self.playable():
            g = [[BLANK] * max(0, cols) for _ in range(max(0, rows))]
            if rows and cols:
                msg = "raycast: area too small"[:cols]
                for i, ch in enumerate(msg):
                    g[rows // 2][i] = Glyph(ch, HUD_HOT, None, True)
            self._grid = g
            return g

        self._think(dt)
        self.recoil = max(0.0, self.recoil - dt * 4.5)

        fb = self.fb
        horizon = self.view_ph // 2 + int(math.sin(self.bob) * BOB_PIXELS)
        horizon = max(2, min(self.view_ph - 3, horizon))

        rows_band = self._band_rows(horizon)
        buf, w = fb.buf, fb.w
        solid = fb.solid
        for y in range(self.view_ph):
            o = y * w
            buf[o:o + w] = solid(rows_band[y])

        self._cast(fb, horizon)
        self._draw_things(fb, horizon)
        self._draw_gun(fb, horizon)

        since_hurt = now - self.hurt_at
        warp = now - self.warp_at
        if warp < 0.5:
            # A new level is a cut, and a cut wants a wipe over it or it
            # reads as the stream having glitched.
            self._tint(fb, 16, self.view_ph if warp < 0.25 else
                       int(self.view_ph * (0.5 - warp) * 4))
        elif self.dead:
            t = min(1.0, (now - self.died_at) / 0.5)
            self._tint(fb, 52 if t > 0.5 else 88, self.view_ph)
        elif since_hurt < 0.25:
            self._tint(fb, 88, max(1, int(self.view_ph * 0.12)))

        text = []
        self._draw_hud(fb, text)
        grid = fb.glyphs()

        for x, y, s, fg in text:
            if not (0 <= y < rows):
                continue
            row = grid[y]
            for i, ch in enumerate(s):
                if 0 <= x + i < cols:
                    row[x + i] = Glyph(ch, fg, HUD_BG, False)

        self.frames += 1
        self._grid = grid
        return grid
