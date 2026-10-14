"""A population of soft organisms, drawn as metaballs on a pixel grid.

The core of the cells sender. `World.frame(cols, rows)` hands back a grid of
`Glyph` -- a character with truecolour `'rrggbb'` colours -- and knows
nothing about bitwall and nothing about terminals, so the same population
drives a wall through `main.py` and a terminal through `pane-cells`.

**An organism is a field, not a shape.** Every organism is a handful of
nodes, and each node radiates a field that falls off with distance. A pixel
is inside a body where the sum of every node's field crosses a threshold.
That one rule does most of the work: two bodies that approach grow a neck
between them before they touch, a body pulled in two thins at the waist and
lets go, and a dying body loses its field and breaks into motes before it
is gone. Nothing is cut and nothing pops; every transition is the field
changing continuously.

**Mass is area and it is conserved.** A node of radius r carries mass r^2,
an organism's mass is the sum over its nodes, and the kernel is chosen so
that nodes stacked on one spot look like one node of the summed mass. A
split hands each child the nodes on its side of a line, so the two children
weigh exactly what the parent did; a merge is the union of two node sets.
Nodes ease toward the places their body plan wants them, so a child settles
into its own shape over a couple of seconds and a swallowed body flows into
the one that took it.

**Three kinds, so the wall reads as a population.** A drifter is large and
slow, a core with satellites that wander around it and give it the
pseudopod wobble of an amoeba. A swimmer is a chain of nodes following the
path its head has swum, sinuous because the head wiggles. A mote is small
and quick, gliding and darting. Size, speed, colour and lifespan are drawn
per organism inside the kind's range, so no two are quite alike.

**Life has a rhythm.** An organism coalesces out of a scatter of dim nodes,
grows while it is young, splits when it is large, may merge with what it
meets, ages in colour, and dies over several seconds -- shrinking, slowing,
dimming and losing its cohesion. Growth slows and births stop as the wall
fills, and old age carries on regardless, so the count settles around what
it was asked for and keeps turning over rather than being capped. Splitting
costs the children some of their life and merging gives some back.

**The pixels are numpy's and the glyphs are python's.** Each node adds its
field to the pixels within its reach, one small array operation per node,
and colour is accumulated the same way so that where two bodies meet their
colours mix in the neck. Shading is a function of field strength: dim at
the rim, full colour in the body, and a pale core where the field is
strongest. Every pixel colour is rounded to a coarse step before it is sent,
because a body whose interior shimmers by one part in 256 costs the wire a
full repaint every frame for nothing anyone can see.
"""
import colorsys
import math
import random
import time
from collections import deque, namedtuple

import numpy as np

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

#: How far a node's field reaches, in radii, and how soft its centre is.
#: Both fix the threshold: a lone node's surface sits at exactly one radius.
SUPPORT = 2.5
CORE = 0.35
THRESHOLD = 1.0 / (1.0 + CORE ** 2) - 1.0 / (SUPPORT ** 2 + CORE ** 2)

#: Bytes on the wire: colours are rounded to this step so that a slow
#: shimmer inside a body does not re-send every cell of it each frame.
STEP = 12

#: The grid this is composed for. Sizes and speeds are fractions of the
#: grid, so a thumbnail gets small organisms rather than one filling it.
#: Past the nominal grid a body grows more slowly than the grid does and
#: the count makes up the difference, so a wall with four times the cells
#: shows more bodies rather than the same few, twice the size; the two
#: exponents are chosen so the share of the wall covered stays the same.
NOMINAL = (150, 45)
NOMINAL_SCALE = math.sqrt(NOMINAL[0] * NOMINAL[1] * 2)
SIZE_EXP = 0.6
COUNT_EXP = 2.0 - 2.0 * SIZE_EXP

BIRTH_SECS = 3.5
DEATH_SECS = 5.0
#: How long after a split the halves refuse to rejoin, and after a merge
#: a body holds together before it may split again.
SPLIT_REFRACTORY = 25.0
MERGE_REFRACTORY = 25.0
#: How long a body goes without appetite after it is born, joins, or
#: touches something it does not join, drawn afresh each time so that the
#: population does not fall into step. A touch refused reads as a bounce.
APPETITE = (12.0, 40.0)
#: What a split costs each child and a merge gives back, as a share of
#: lifespan.
SPLIT_COST = 0.10
MERGE_GAIN = 0.12
#: Trail points are this far apart, and this many are kept, so a chain has
#: a path to follow even when a merge has made it long.
TRAIL_STEP = 0.3
TRAIL_LEN = 600


class Kind:
    """One kind of organism and the ranges it is drawn from.

    `size` is the radius of a whole-body equivalent circle as a fraction of
    the grid's geometric mean side; `speed` is in those same units a
    second. `hues` is the palette slice the kind is drawn from, in degrees.
    `wander` is how restlessly the heading drifts.
    """

    def __init__(self, name, size, speed, lifespan, hues, nodes, weight,
                 wander, splits=True, wiggle=0.0, dash=0.0, cohesion=4.0,
                 momentum=3.5):
        self.name = name
        self.size = size
        self.speed = speed
        self.lifespan = lifespan
        self.hues = hues
        self.nodes = nodes
        self.weight = weight
        self.wander = wander
        self.splits = splits
        self.wiggle = wiggle
        self.dash = dash
        self.cohesion = cohesion
        self.momentum = momentum


KINDS = (
    Kind("drifter", (0.060, 0.085), (0.014, 0.028), (200, 360), (150, 300),
         nodes=6, weight=0.34, wander=0.5, cohesion=2.5, momentum=1.8),
    Kind("swimmer", (0.055, 0.075), (0.075, 0.125), (140, 240), (320, 420),
         nodes=18, weight=0.38, wander=0.6, wiggle=0.55, cohesion=7.0),
    Kind("mote", (0.018, 0.026), (0.10, 0.18), (60, 120), (60, 150),
         nodes=3, weight=0.28, wander=2.2, splits=False, dash=2.0,
         cohesion=8.0),
)
KIND = {k.name: k for k in KINDS}


class Config:
    """Everything an operator can turn, with the defaults.

    A front end fills this from its arguments. Values are clamped in
    `clamp()` rather than refused, because a wall has nobody at the
    keyboard to read an error, and a slightly wrong animation beats none.
    """

    def __init__(self):
        self.population = 8        # organisms aimed for at the nominal grid
        self.split = True
        self.merge = True
        self.birth = True
        self.death = True
        self.growth = True
        self.aura = True
        self.speed = 1.0           # multiplier on every kind's speed
        self.size = 1.0            # multiplier on every kind's size
        self.lifespan = 1.0        # multiplier on every kind's lifespan
        self.hue = None            # a base hue in degrees, or the palettes
        self.spread = 40.0         # hue spread either side of `hue`
        self.kinds = tuple(k.name for k in KINDS)
        self.seed = None

    def clamp(self):
        """Pull every value into its working range; says what it changed."""
        notes = []
        fresh = Config()

        def rng(name, lo, hi):
            v = getattr(self, name)
            try:
                v = float(v)
                if v != v:
                    raise ValueError
            except (TypeError, ValueError):
                notes.append(f"{name}: not a number, using default")
                setattr(self, name, getattr(fresh, name))
                return
            c = min(hi, max(lo, v))
            if c != v:
                notes.append(f"{name}: {v:g} is outside {lo:g}..{hi:g}, "
                             f"using {c:g}")
            setattr(self, name, c)

        rng("population", 1, 60)
        self.population = int(round(self.population))
        rng("speed", 0.1, 5.0)
        rng("size", 0.3, 3.0)
        rng("lifespan", 0.1, 10.0)
        rng("spread", 0.0, 180.0)
        if self.hue is not None:
            try:
                self.hue = float(self.hue) % 360.0
            except (TypeError, ValueError):
                notes.append("hue: not a number, using the palettes")
                self.hue = None
        kinds = tuple(k for k in self.kinds if k in KIND)
        if not kinds:
            notes.append("kinds: none of those are kinds, using all")
            kinds = fresh.kinds
        elif len(kinds) != len(self.kinds):
            notes.append("kinds: ignoring the ones that are not kinds")
        self.kinds = kinds
        return notes


# ------------------------------------------------------------- organism

class Organism:
    """One body: a head that steers, and nodes that carry its field.

    The head is the point that moves; nodes are absolute positions with
    radii, and each frame they ease toward where the body plan puts them.
    `extra` marks nodes the plan has no slot for -- inherited from a merge
    or a split -- which shrink to nothing while the planned ones grow.
    """

    def __init__(self, world, kind, x, y, mass, hue, age=0.0,
                 lifespan=None, born=True):
        self.world = world
        self.kind = kind
        self.x, self.y = x, y
        self.heading = random.uniform(0, 2 * math.pi)
        self.turn = 0.0
        self.vx = self.vy = 0.0
        self.fx = self.fy = 0.0       # what the neighbours are doing to it
        self.partner = None           # a body it is drawn toward, if any
        self.soft = 0.0               # seconds left of ignoring the crowd
        self.hue = hue
        self.age = age
        self.lifespan = lifespan if lifespan is not None else \
            random.uniform(*kind.lifespan) * world.cfg.lifespan
        self.speed = random.uniform(*kind.speed)
        self.size = random.uniform(*kind.size)
        self.phase = random.uniform(0, 2 * math.pi)
        self.dash = 0.0           # motes: seconds of burst left
        self.glide = random.uniform(1.0, 3.0)
        self.state = "born" if born else "alive"
        self.since = 0.0          # seconds in this state
        self.no_merge = random.uniform(*APPETITE) if born else 0.0
        self.no_split = BIRTH_SECS if born else 0.0
        self.trail = deque(maxlen=TRAIL_LEN)
        self.trail.append((x, y))
        n = kind.nodes
        self.ph = np.random.uniform(0, 2 * math.pi, n)
        self.fq = np.random.uniform(0.25, 0.6, n)
        self.ang = np.linspace(0, 2 * math.pi, n, endpoint=False) \
            + np.random.uniform(-0.4, 0.4, n)
        #: Hue offset per node, and the offsets the plan wants. A chain
        #: shades from head to tail and a drifter's satellites each lean a
        #: little off its core, so a body is a gradient and not a flat
        #: fill. A node that arrives from another body keeps that body's
        #: hue and drifts to the plan's, so a merge blends and a split
        #: parts in the colours they began with.
        if kind.name == "swimmer":
            self.plan_tint = np.linspace(12.0, -28.0, n)
        else:
            self.plan_tint = np.random.uniform(-10.0, 10.0, n)
            self.plan_tint[0] = 0.0
        self.tint = self.plan_tint.copy()
        self.nx = np.full(n, x, float)
        self.ny = np.full(n, y, float)
        self.nr = np.zeros(n)
        self.extra = np.zeros(n, bool)
        #: What a newborn is gathering toward, and what a dying body had
        #: when it began to go.
        self.target_mass = mass
        self.mass_at_death = mass
        self.mass = mass
        if born:
            # Scattered and faint, so the body draws itself together.
            self.mass = mass * 0.03
            s = math.sqrt(mass) * 2.5
            self.nx = np.clip(x + np.random.uniform(-s, s, n), 1, world.W - 2)
            self.ny = np.clip(y + np.random.uniform(-s, s, n), 1, world.H - 2)
        self._settle_radii()

    # -------------------------------------------------------- measures

    def radius(self):
        """Radius of the one circle that would carry this body's mass."""
        return math.sqrt(max(self.mass, 1e-6))

    def nominal_mass(self):
        return (self.size * self.world.scale * self.world.cfg.size) ** 2

    def life(self):
        return min(1.0, self.age / self.lifespan)

    def alive(self):
        return self.state == "alive"

    def colours(self):
        """Each node's colour now, as an (m, 3) array in 0..1.

        Youth is saturated and bright; age drains both. A newborn arrives
        pale and takes on its colour as it gathers; a dying body goes dark.
        """
        a = self.life()
        s = 0.92 - 0.4 * a
        v = 1.0 - 0.25 * a
        if self.state == "born":
            u = min(1.0, self.since / BIRTH_SECS)
            s *= 0.25 + 0.75 * u
            v *= 0.7 + 0.3 * u
        elif self.state == "dying":
            u = min(1.0, self.since / DEATH_SECS)
            s *= 1.0 - 0.6 * u
            v *= (1.0 - u) ** 1.3
        m = len(self.nr)
        out = np.empty((m, 3))
        for k in range(m):
            h = self.hue + self.tint[k]
            out[k] = colorsys.hsv_to_rgb((h % 360.0) / 360.0, s, v)
        return out

    # -------------------------------------------------------- body plan

    def _plan(self, t, spread):
        """Where the plan wants each node, and how big, unnormalised."""
        R = self.radius()
        n = self.kind.nodes
        if self.kind.name == "swimmer":
            return self._plan_chain(n, R, spread)
        if self.kind.name == "mote":
            if abs(self.vx) + abs(self.vy) > 1e-6:
                a = math.atan2(self.vy, self.vx)
            else:
                a = self.heading
            d = R * 0.8 * spread
            back = np.arange(n) * d
            tx = self.x - math.cos(a) * back
            ty = self.y - math.sin(a) * back
            return tx, ty, 1.0 - 0.5 * np.arange(n) / max(1, n - 1)
        # A drifter: a core, and satellites breathing in and out around it.
        rho = R * (0.55 + 0.35 * np.sin(self.fq * t * 2 * math.pi + self.ph))
        rho *= spread
        ang = self.ang + 0.35 * np.sin(0.7 * self.fq * t * 2 * math.pi
                                       + 2.1 * self.ph)
        tx = self.x + rho * np.cos(ang)
        ty = self.y + rho * np.sin(ang)
        tr = 0.5 + 0.12 * np.sin(1.3 * self.fq * t * 2 * math.pi + 3 * self.ph)
        tx[0], ty[0], tr[0] = self.x, self.y, 1.0
        return tx, ty, tr

    def _plan_chain(self, n, R, spread):
        """Nodes strung back along the trail at a steady spacing.

        The spacing is half a head radius, close enough that the summed
        field is a smooth tube and not a string of beads.
        """
        taper = 1.0 - 0.5 * (np.arange(n) / max(1, n - 1)) ** 1.4
        head = R / math.sqrt(float((taper ** 2).sum()))
        gap = head * 0.55 * spread
        pts = self.trail
        tx = np.empty(n)
        ty = np.empty(n)
        px, py = pts[-1]
        tx[0], ty[0] = px, py
        k = 1
        acc = 0.0
        for i in range(len(pts) - 2, -1, -1):
            if k >= n:
                break
            qx, qy = pts[i]
            seg = math.hypot(qx - px, qy - py)
            while k < n and acc + seg >= gap * k:
                f = (gap * k - acc) / seg if seg > 0 else 0.0
                tx[k] = px + (qx - px) * f
                ty[k] = py + (qy - py) * f
                k += 1
            acc += seg
            px, py = qx, qy
        if k < n:
            # A trail shorter than the body: lay the rest out straight
            # behind the last point there is, so a newborn is a body and
            # not a heap.
            hx, hy = math.cos(self.heading), math.sin(self.heading)
            for j in range(k, n):
                tx[j] = px - hx * gap * (j - k + 1)
                ty[j] = py - hy * gap * (j - k + 1)
        return tx, ty, taper

    def _settle_radii(self):
        """Scale every node so the body weighs exactly its mass."""
        tot = float((self.nr ** 2).sum())
        if tot <= 1e-12:
            n = len(self.nr)
            self.nr = np.full(n, math.sqrt(max(self.mass, 1e-9) / n))
            return
        self.nr *= math.sqrt(self.mass / tot)

    def adopt(self, nx, ny, nr, hue, order=False):
        """Take these nodes as the body, whatever the plan wanted.

        The largest nodes fill the plan's slots (or the first ones, when
        `order` matters, as it does for a chain); the rest are extras that
        will be absorbed. The body then weighs what the nodes weigh. `hue`
        is the absolute hue each node arrives with, which it keeps for now.
        """
        n = self.kind.nodes
        nx, ny, nr, hue = (np.asarray(a, float).copy()
                           for a in (nx, ny, nr, hue))
        if not order:
            idx = np.argsort(-nr)
            nx, ny, nr, hue = nx[idx], ny[idx], nr[idx], hue[idx]
        m = len(nr)
        if m < n:
            pad = n - m
            nx = np.concatenate([nx, np.full(pad, self.x)])
            ny = np.concatenate([ny, np.full(pad, self.y)])
            nr = np.concatenate([nr, np.zeros(pad)])
            hue = np.concatenate([hue, np.full(pad, self.hue)])
            m = n
        self.nx, self.ny, self.nr = nx, ny, nr
        self.tint = (hue - self.hue + 180.0) % 360.0 - 180.0
        self.extra = np.arange(m) >= n
        self.mass = float((nr ** 2).sum())
        self.target_mass = self.mass

    # -------------------------------------------------------- stepping

    def step(self, dt, t):
        w = self.world
        cfg = w.cfg
        # A crowd ages a body: when there are far more than the wall wants,
        # as after a resize down, old age comes sooner and the count
        # settles in a minute rather than an hour.
        crowd = 1.0 + 2.0 * max(0.0, len(w.orgs) / w.target() - 1.2)
        self.age += dt * crowd
        self.since += dt
        self.no_merge = max(0.0, self.no_merge - dt)
        self.no_split = max(0.0, self.no_split - dt)
        self.soft = max(0.0, self.soft - dt)
        if self.state == "born" and self.since >= BIRTH_SECS:
            self.state, self.since = "alive", 0.0
        if self.state == "alive" and cfg.death and self.age >= self.lifespan:
            self.state, self.since = "dying", 0.0
            self.mass_at_death = self.mass

        # Mass: a newborn gathers it, the living grow toward the kind's
        # ceiling while there is room on the wall, the dying let it go.
        if self.state == "born":
            u = min(1.0, self.since / BIRTH_SECS)
            self.mass = self.target_mass * (0.03 + 0.97 * u * u)
        elif self.state == "dying":
            u = min(1.0, self.since / DEATH_SECS)
            self.mass = self.mass_at_death * (1.0 - u) ** 2.2
        elif cfg.growth:
            cap = self.nominal_mass() * 1.6
            self.mass += 0.035 * self.mass * (1.0 - self.mass / cap) \
                * w.room() * dt

        self._move(dt)

        spread = 1.0
        cohesion = self.kind.cohesion
        if self.state == "born":
            u = min(1.0, self.since / BIRTH_SECS)
            spread = 1.0 + 1.6 * (1.0 - u) ** 2
            cohesion *= 0.6 + 0.4 * u
        elif self.state == "dying":
            u = min(1.0, self.since / DEATH_SECS)
            spread = 1.0 + 2.2 * u
            cohesion *= max(0.08, 1.0 - u) * 0.5
        tx, ty, tr = self._plan(t, spread)
        # Extras have no slot: they sink into the head and give up their
        # radius to the planned nodes through the mass settlement.
        n = self.kind.nodes
        m = len(self.nr)
        gx = np.full(m, self.x)
        gy = np.full(m, self.y)
        gr = np.zeros(m)
        gx[:n], gy[:n] = tx, ty
        gr[:n] = tr * math.sqrt(self.mass / max(1e-9, float((tr ** 2).sum())))
        k = 1.0 - math.exp(-cohesion * dt)
        self.nx += (gx - self.nx) * k
        self.ny += (gy - self.ny) * k
        self.nr += (gr - self.nr) * k
        gt = np.zeros(m)
        gt[:n] = self.plan_tint
        self.tint += (gt - self.tint) * (1.0 - math.exp(-0.6 * dt))
        self._settle_radii()
        if m > n:
            keep = ~(self.extra & (self.nr < 0.12 * self.radius()))
            if not keep.all():
                self.nx, self.ny = self.nx[keep], self.ny[keep]
                self.nr, self.extra = self.nr[keep], self.extra[keep]
                self.tint = self.tint[keep]
                self._settle_radii()

    def _move(self, dt):
        w = self.world
        kind = self.kind
        cfg = w.cfg
        # Steering wanders as a noisy turn rate that keeps relaxing to
        # straight, which is what gives a path curves rather than kinks.
        self.turn += (-self.turn / 1.5) * dt \
            + kind.wander * math.sqrt(dt) * random.gauss(0, 1)
        lim = 1.4 if kind.name == "swimmer" else 2.5
        self.turn = max(-lim, min(lim, self.turn))
        self.heading += self.turn * dt

        # Motes glide, then dash.
        speed = self.speed * w.scale * cfg.speed
        if kind.dash:
            if self.dash > 0:
                self.dash -= dt
                speed *= kind.dash
            else:
                self.glide -= dt
                if self.glide <= 0:
                    self.dash = random.uniform(0.4, 0.9)
                    self.glide = random.uniform(1.5, 4.0)
                    self.turn += random.uniform(-2.0, 2.0)
        if self.state == "dying":
            u = min(1.0, self.since / DEATH_SECS)
            speed *= (1.0 - u) ** 2
        elif self.state == "born":
            speed *= 0.4
        # Age slows a body a little, and a heavy one is slower than a light
        # one of its kind: the merged giant lumbers.
        speed *= 1.0 - 0.3 * self.life()
        nominal = self.nominal_mass()
        speed *= (nominal / max(self.mass, nominal * 0.25)) ** 0.25

        # Steer back from the edges long before reaching them.
        R = self.radius()
        margin = 0.06 * min(w.W, w.H) + R
        cx, cy = w.W / 2, w.H / 2
        push = 0.0
        for d in (self.x, w.W - self.x, self.y, w.H - self.y):
            if d < margin:
                push = max(push, (margin - d) / margin)
        if push > 0:
            want = math.atan2(cy - self.y, cx - self.x)
            diff = (want - self.heading + math.pi) % (2 * math.pi) - math.pi
            self.heading += diff * min(1.0, 3.0 * push * dt)

        if self.partner is not None:
            # Something it may join is close: turn toward it.
            p = self.partner
            want = math.atan2(p.y - self.y, p.x - self.x)
            diff = (want - self.heading + math.pi) % (2 * math.pi) - math.pi
            self.heading += diff * min(1.0, 1.5 * dt)
        aim = self.heading
        if kind.wiggle:
            aim += kind.wiggle * math.sin(self.since * 2 * math.pi * 0.7
                                          + self.phase)
        ax = math.cos(aim) * speed
        ay = math.sin(aim) * speed
        # Momentum: velocity chases the aim rather than being it.
        k = 1.0 - math.exp(-dt * kind.momentum)
        self.vx += (ax + self.fx - self.vx) * k
        self.vy += (ay + self.fy - self.vy) * k
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.x = min(w.W - 1.0 - R * 0.5, max(1.0 + R * 0.5, self.x))
        self.y = min(w.H - 1.0 - R * 0.5, max(1.0 + R * 0.5, self.y))
        lx, ly = self.trail[-1]
        if math.hypot(self.x - lx, self.y - ly) >= TRAIL_STEP:
            self.trail.append((self.x, self.y))

    def rescale(self, fx, fy, fm):
        """Follow the grid to a new size."""
        self.x *= fx
        self.y *= fy
        self.vx *= fx
        self.vy *= fy
        self.nx *= fx
        self.ny *= fy
        self.nr *= fm
        self.mass *= fm * fm
        self.target_mass *= fm * fm
        self.mass_at_death *= fm * fm
        self.trail = deque(((px * fx, py * fy) for px, py in self.trail),
                           maxlen=TRAIL_LEN)
        self._settle_radii()


# ------------------------------------------------------------- world

class World:
    """The population, and the grid it is painted onto.

    `frame(cols, rows)` is the whole public surface: an area in, a grid of
    Glyph out. The clock is read on each call, so the frame rate is a cap
    and never a speed. `advance(dt)` steps the simulation without painting,
    which is what a test wanting an hour of population uses.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        if self.cfg.seed is not None:
            random.seed(self.cfg.seed)
            np.random.seed(self.cfg.seed % (2 ** 32))
        self.W = self.H = 0
        self.scale = 1.0
        self.orgs = []
        self.t = 0.0
        self.last = None
        self.births = self.deaths = self.splits = self.merges = 0
        self._cells = {}
        self._hex = {}
        self._xs = self._ys = None
        self._spawn_due = 0.0

    # -------------------------------------------------------- population

    def target(self):
        """Organisms wanted on this grid: the asked-for count at the
        nominal grid, scaled for this one, and never fewer than two."""
        ratio = math.sqrt(self.W * self.H) / NOMINAL_SCALE
        return max(2.0, self.cfg.population * ratio ** COUNT_EXP)

    def room(self):
        """How much growing room the wall has left, 1 empty to 0 full."""
        return max(0.0, 1.0 - len(self.orgs) / (1.5 * self.target()))

    def _new_hue(self, kind):
        cfg = self.cfg
        if cfg.hue is not None:
            return cfg.hue + random.uniform(-cfg.spread, cfg.spread)
        return random.uniform(*kind.hues) % 360.0

    def _pick_kind(self):
        kinds = [KIND[n] for n in self.cfg.kinds]
        r = random.random() * sum(k.weight for k in kinds)
        for k in kinds:
            r -= k.weight
            if r <= 0:
                return k
        return kinds[-1]

    def spawn(self, kind=None, at=None, born=True):
        kind = kind or self._pick_kind()
        size = random.uniform(*kind.size)
        R = size * self.scale * self.cfg.size
        if at is None:
            # The emptiest of a few tries, so a birth is not on top of
            # someone.
            best, score = None, -1.0
            for _ in range(6):
                x = random.uniform(R * 2 + 1, self.W - R * 2 - 1) \
                    if self.W > R * 4 + 2 else self.W / 2
                y = random.uniform(R * 2 + 1, self.H - R * 2 - 1) \
                    if self.H > R * 4 + 2 else self.H / 2
                d = min((math.hypot(o.x - x, o.y - y) for o in self.orgs),
                        default=1e9)
                if d > score:
                    best, score = (x, y), d
            at = best
        mass = R * R * (1.0 if born else random.uniform(0.6, 1.1))
        o = Organism(self, kind, at[0], at[1], mass, self._new_hue(kind),
                     born=born)
        o.size = size
        if not born:
            o.age = random.uniform(0, 0.5) * o.lifespan
        self.orgs.append(o)
        self.births += 1
        return o

    def _split(self, o):
        """Halve a body along a line through it; the halves push apart."""
        kind = o.kind
        nx, ny, nr = o.nx, o.ny, o.nr
        nh = o.hue + o.tint
        ang = o.heading + math.pi / 2 + random.uniform(-0.5, 0.5)
        if kind.name == "swimmer":
            n = len(nr)
            cut = max(1, n // 2)
            a = (nx[:cut], ny[:cut], nr[:cut], nh[:cut])
            b = (nx[cut:], ny[cut:], nr[cut:], nh[cut:])
            order = True
        else:
            # The core cannot be shared, so it becomes two half-cores a
            # little apart along the cut, and the satellites go with
            # whichever side they are on.
            ux, uy = math.cos(ang), math.sin(ang)
            big = int(np.argmax(nr))
            r2 = nr[big] / math.sqrt(2)
            off = nr[big] * 0.35
            px = np.concatenate([nx, [nx[big] + ux * off, nx[big] - ux * off]])
            py = np.concatenate([ny, [ny[big] + uy * off, ny[big] - uy * off]])
            pr = np.concatenate([nr, [r2, r2]])
            ph = np.concatenate([nh, [nh[big], nh[big]]])
            pr[big] = 0.0
            side = (px - o.x) * ux + (py - o.y) * uy >= 0
            side[-2], side[-1] = True, False
            sa, sb = side & (pr > 0), ~side & (pr > 0)
            a = (px[sa], py[sa], pr[sa], ph[sa])
            b = (px[sb], py[sb], pr[sb], ph[sb])
            order = False
        if len(a[2]) == 0 or len(b[2]) == 0:
            return None
        age = o.age + SPLIT_COST * o.lifespan
        kids = []
        for nodes in (a, b):
            w2 = nodes[2] ** 2
            cx = float((nodes[0] * w2).sum() / w2.sum())
            cy = float((nodes[1] * w2).sum() / w2.sum())
            hx, hy = (nodes[0][0], nodes[1][0]) if order else (cx, cy)
            c = Organism(self, kind, float(hx), float(hy), 1e-3,
                         o.hue + random.uniform(-14, 14), age=age,
                         lifespan=o.lifespan, born=False)
            c.adopt(*nodes, order=order)
            c.size = o.size
            c.speed = o.speed * random.uniform(0.85, 1.15)
            c.heading = o.heading
            c.vx, c.vy = o.vx, o.vy
            c.no_merge = SPLIT_REFRACTORY
            c.no_split = SPLIT_REFRACTORY
            # Halves part under their own momentum, not the crowd's
            # shove, so the neck between them has time to be seen.
            c.soft = 4.0
            kids.append(c)
        front, back = kids
        if order:
            front.trail = deque(o.trail, maxlen=TRAIL_LEN)
            # The back half's nodes already lie along the path it will
            # follow; it swims on and then veers off.
            back.trail = deque(zip(b[0].tolist()[::-1], b[1].tolist()[::-1]),
                               maxlen=TRAIL_LEN)
            back.turn += random.choice((-1, 1)) * random.uniform(1.5, 2.5)
        else:
            ux, uy = math.cos(ang), math.sin(ang)
            kick = 0.025 * self.scale * self.cfg.speed
            front.vx += ux * kick
            front.vy += uy * kick
            back.vx -= ux * kick
            back.vy -= uy * kick
            front.heading = ang
            back.heading = ang + math.pi
        self.orgs.remove(o)
        self.orgs.extend(kids)
        self.splits += 1
        return kids

    def _merge(self, a, b):
        """The lighter body flows into the heavier one."""
        if b.mass > a.mass:
            a, b = b, a
        tot = a.mass + b.mass
        wa, wb = a.mass / tot, b.mass / tot
        ha_nodes, hb_nodes = a.hue + a.tint, b.hue + b.tint
        # Hue mixed on the circle, weighted by mass.
        ha, hb = math.radians(a.hue), math.radians(b.hue)
        mx = wa * math.cos(ha) + wb * math.cos(hb)
        my = wa * math.sin(ha) + wb * math.sin(hb)
        a.hue = math.degrees(math.atan2(my, mx)) % 360.0
        a.age = max(0.0, wa * a.age + wb * b.age - MERGE_GAIN * a.lifespan)
        a.lifespan = wa * a.lifespan + wb * b.lifespan
        a.vx = wa * a.vx + wb * b.vx
        a.vy = wa * a.vy + wb * b.vy
        a.adopt(np.concatenate([a.nx, b.nx]), np.concatenate([a.ny, b.ny]),
                np.concatenate([a.nr, b.nr]),
                np.concatenate([ha_nodes, hb_nodes]),
                order=a.kind.name == "swimmer")
        a.no_split = MERGE_REFRACTORY
        a.no_merge = random.uniform(*APPETITE)
        self.orgs.remove(b)
        self.merges += 1
        return a

    def _interact(self):
        """Pairs: join what may merge and is touching, draw together what
        may merge and is near, and push apart what may not."""
        cfg = self.cfg
        orgs = list(self.orgs)
        for o in orgs:
            o.fx = o.fy = 0.0
            o.partner = None
        gone = set()
        biggest = max(k.size[1] for k in KINDS) * self.scale * cfg.size
        cap = 2.0 * biggest ** 2
        for i, a in enumerate(orgs):
            if id(a) in gone:
                continue
            for b in orgs[i + 1:]:
                if id(b) in gone:
                    continue
                dx, dy = b.x - a.x, b.y - a.y
                d = math.hypot(dx, dy) + 1e-6
                ra, rb = a.radius(), b.radius()
                reach = ra + rb
                if d > 2.2 * reach:
                    continue
                can = (cfg.merge and a.alive() and b.alive()
                       and a.no_merge <= 0 and b.no_merge <= 0
                       and a.mass + b.mass <= cap)
                if can and d < 0.6 * reach:
                    kept = self._merge(a, b)
                    gone.add(id(a) if kept is b else id(b))
                    if kept is b:
                        break
                    continue
                if can:
                    if a.partner is None:
                        a.partner = b
                    if b.partner is None:
                        b.partner = a
                    continue
                if d < 1.5 * reach and a.soft <= 0 and b.soft <= 0:
                    # Bodies that will not join should not look as if
                    # they had: keep their fields apart, and let neither
                    # try again for a while.
                    ux, uy = dx / d, dy / d
                    g = 0.12 * self.scale * cfg.speed * (1.5 * reach - d) \
                        / (1.5 * reach)
                    a.fx -= ux * g * (rb / reach)
                    a.fy -= uy * g * (rb / reach)
                    b.fx += ux * g * (ra / reach)
                    b.fy += uy * g * (ra / reach)
                    if d < reach:
                        for o in (a, b):
                            if o.no_merge <= 0:
                                o.no_merge = random.uniform(*APPETITE)

    def advance(self, dt):
        """Step the population by `dt` seconds, in small substeps."""
        while dt > 0:
            h = min(dt, 1.0 / 30)
            dt -= h
            self._step(h)

    def _step(self, dt):
        cfg = self.cfg
        self.t += dt
        self._interact()
        for o in list(self.orgs):
            o.step(dt, self.t)
            if o.state == "dying" and o.since >= DEATH_SECS:
                self.orgs.remove(o)
                self.deaths += 1
        # Splits: a body over its size, settled, and not too old.
        if cfg.split:
            for o in list(self.orgs):
                if (o.kind.splits and o.alive() and o.no_split <= 0
                        and o.mass >= 1.3 * o.nominal_mass()
                        and o.life() < 0.85):
                    rate = 1.0 / 30.0 * (o.mass / o.nominal_mass()) ** 2
                    if random.random() < rate * dt:
                        self._split(o)
        # Births: often while the wall is empty, never once it is full.
        if cfg.birth:
            want = self.target()
            n = len(self.orgs)
            rate = 0.4 * max(0.0, 1.0 - n / want)
            if n < 2:
                rate = max(rate, 0.5)
            self._spawn_due -= dt
            if rate <= 0:
                self._spawn_due = 0.0
            elif self._spawn_due <= 0:
                self.spawn()
                self._spawn_due = random.expovariate(rate)

    # -------------------------------------------------------- the grid

    def _resize(self, W, H):
        if (W, H) == (self.W, self.H):
            return
        scale = NOMINAL_SCALE * (math.sqrt(W * H) / NOMINAL_SCALE) ** SIZE_EXP
        if self.W and self.H:
            fx, fy = W / self.W, H / self.H
            for o in self.orgs:
                o.rescale(fx, fy, scale / self.scale)
        self.W, self.H = W, H
        self.scale = scale
        self._ys = np.arange(H, dtype=float)[:, None]
        self._xs = np.arange(W, dtype=float)[None, :]
        if not self.orgs:
            # A wall should show a population from its first frame, not a
            # slow fill from nothing.
            for _ in range(int(round(self.target()))):
                self.spawn(born=False)
            self.births = 0

    def frame(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        self._resize(cols, rows * 2)
        now = time.monotonic()
        if self.last is None:
            self.last = now
        dt = min(0.25, now - self.last)
        self.last = now
        if dt > 0:
            self.advance(dt)
        return self.paint(cols, rows)

    def field(self):
        """The summed field and the mass-weighted colour, per pixel."""
        W, H = self.W, self.H
        F = np.zeros((H, W))
        A = np.zeros((H, W, 3))
        xs, ys = self._xs, self._ys
        inv_far = 1.0 / (SUPPORT ** 2 + CORE ** 2)
        for o in self.orgs:
            cols = o.colours()
            for k in range(len(o.nr)):
                r = float(o.nr[k])
                if r < 0.15:
                    continue
                cx, cy = float(o.nx[k]), float(o.ny[k])
                reach = SUPPORT * r
                x0, y0 = max(0, int(cx - reach)), max(0, int(cy - reach))
                x1 = min(W, int(cx + reach) + 2)
                y1 = min(H, int(cy + reach) + 2)
                if x0 >= x1 or y0 >= y1:
                    continue
                dx = (xs[:, x0:x1] - cx) / r
                dy = (ys[y0:y1, :] - cy) / r
                f = 1.0 / (dx * dx + dy * dy + CORE ** 2) - inv_far
                np.maximum(f, 0.0, out=f)
                F[y0:y1, x0:x1] += f
                A[y0:y1, x0:x1] += f[:, :, None] * cols[k]
        return F, A

    def pixels(self):
        """Every pixel's colour as (H, W, 3) uint8; black is empty."""
        F, A = self.field()
        u = F / THRESHOLD
        body = u >= 1.0
        # Rim to core: dim at the surface, full a little way in, and a
        # pale highlight where the field is strongest.
        b = np.where(body, 0.45 + 0.55 * np.clip((u - 1.0) / 1.6, 0, 1), 0.0)
        if self.cfg.aura:
            halo = (u >= 0.55) & ~body
            b = np.where(halo, 0.22 * ((u - 0.55) / 0.45) ** 2, b)
        rgb = A / np.maximum(F, 1e-9)[:, :, None]
        rgb *= b[:, :, None]
        w = np.clip((u - 3.0) / 5.0, 0.0, 0.5)[:, :, None]
        rgb = rgb * (1.0 - w) + w
        q = (np.clip(rgb, 0, 1) * 255.0 / STEP).astype(np.int64) * STEP
        return q.astype(np.uint8)

    def paint(self, cols, rows):
        px = self.pixels()
        top = px[0::2].astype(np.int64)
        bot = px[1::2].astype(np.int64)
        tc = (top[..., 0] << 16) | (top[..., 1] << 8) | top[..., 2]
        bc = (bot[..., 0] << 16) | (bot[..., 1] << 8) | bot[..., 2]
        key = (tc << 24) | bc
        cells = self._cells
        get = cells.get
        miss = self._miss
        out = []
        for krow in key.tolist():
            row = []
            add = row.append
            for k in krow:
                g = get(k)
                add(g if g is not None else miss(k))
            out.append(row)
        return out

    def arrays(self, cols, rows):
        """The frame `frame` would give as (ch, fg, bg) arrays of code
        points and 0xRRGGBB colours, -1 for none, the same glyphs chosen
        the same way by whole-array masks rather than a lookup a cell."""
        cols, rows = max(1, cols), max(1, rows)
        self._resize(cols, rows * 2)
        now = time.monotonic()
        if self.last is None:
            self.last = now
        dt = min(0.25, now - self.last)
        self.last = now
        if dt > 0:
            self.advance(dt)
        px = self.pixels()
        top = px[0::2].astype(np.int64)
        bot = px[1::2].astype(np.int64)
        t = (top[..., 0] << 16) | (top[..., 1] << 8) | top[..., 2]
        b = (bot[..., 0] << 16) | (bot[..., 1] << 8) | bot[..., 2]
        same, tb, bb = t == b, t == 0, b == 0
        ch = np.where(same, 0x20, np.where(tb, 0x2584, 0x2580)).astype(np.int32)
        fg = np.where(same, -1, np.where(tb, b, t))
        bg = np.where(same, np.where(tb, -1, b), np.where(bb | tb, -1, b))
        return ch, fg, bg

    def _hexof(self, c):
        s = self._hex.get(c)
        if s is None:
            s = self._hex[c] = "%06x" % c
        return s

    def _miss(self, key):
        """The glyph for a top and bottom pixel, made once and kept.

        Black is the receiver's own background and costs the least; a cell
        with one black half is a half block in one colour, and matching
        halves are a space on that colour.
        """
        if len(self._cells) > 300000:
            self._cells.clear()
        t, b = key >> 24, key & 0xFFFFFF
        if t == 0 and b == 0:
            g = BLANK
        elif t == b:
            g = Glyph(" ", None, self._hexof(b), False)
        elif b == 0:
            g = Glyph("▀", self._hexof(t), None, False)
        elif t == 0:
            g = Glyph("▄", self._hexof(b), None, False)
        else:
            g = Glyph("▀", self._hexof(t), self._hexof(b), False)
        self._cells[key] = g
        return g

    def total_mass(self):
        return sum(o.mass for o in self.orgs)
