#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy", "scipy"]
# ///
"""Surf side on: waves running in and bursting against a cliff.

    senders/surf/main.py localhost
    senders/surf/main.py --secs 300 --font 10 pi.local

The water is real, as far as a wall goes: tens of thousands of particles
of it, simulated as a liquid (FLIP, the method films use for splashes) in
a tank seen from the side, with a sea bed shoaling up to a rock cliff. A
wave maker out of sight sends in a wave every few seconds; each steepens
over the shallows, throws its lip forward, and hits the cliff, where the
water has nowhere to go but up. Nothing about a splash is drawn on
purpose. Spray is whatever water the impact threw clear, falling back
under gravity.

Every `--secs` the coast is new: a different cliff, a different palette,
and the sea starting again from flat.

Above the water is black, and the rock and the deep water are near black,
so from across the room the sea is the picture. The water is drawn in
half blocks, two full-colour pixels a cell, whiter where it is moving fast
or full of air; spray clear of the body is braille dots. A liquid is not
cheap: this wants a core of the machine it runs on, so run it on a Mac or a
Pi 5 rather than a Pi 3.

Arguments are never fatal. A wall is a screen in another room with nobody
at the keyboard, so a value out of range is clamped, one that is not a
number falls back to its default, and what was done is said on stderr.
"""
import argparse
import math
import os
import signal
import sys
import threading
import time

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import cg
from scipy.ndimage import minimum_filter1d, uniform_filter1d

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))   # repo root

import bitwall                                                 # noqa: E402

DEFAULT_SECS = 120
DEFAULT_FONT = 10
DEFAULT_FPS = 20

HALF = "▀"
BRAILLE = "⣿"
DOTS = np.array([[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]],
                np.int64)

#: Gravity, in cells of the simulation a second squared.
G = 60.0
#: How much of a particle's velocity is its own rather than the grid's.
#: Near one keeps splashes lively; the rest damps the noise out.
FLIP = 0.96
#: Particles to a cell of water when the sea starts.
PER_CELL = 4
#: How much finer than the water's cells the rock is shaped and met.
FINE = 4
#: Pixels of water, one over another, that make the body of the sea
#: rather than spray over it, for where its depth is counted from.
SOLID = 4
#: A cell may hold this much more than its share before it is made to
#: spread, and how hard it is pushed to, as a fraction of one step.
CROWD_FREE = 1.2
CROWD_PUSH = 0.1
#: How hard the wave maker's stroke is, in gravities.
WAVE = 4.0

#: Palettes: water at the surface, deep, and as foam; rock. The deep water
#: and the rock are all near black, so from across the room the bed and
#: the cliff sink into the dark and the sea looks deep.
PALETTES = {
    "overcast": dict(top="2f7c92", deep="02090e", foam="eef6f8",
                     rock="2a2520"),
    "dusk": dict(top="3d6d96", deep="03060e", foam="ffeedd", rock="2a2520"),
    "day": dict(top="2a9cb8", deep="020b12", foam="ffffff", rock="2a2520"),
    "night": dict(top="1c4466", deep="02070e", foam="c4d6e6", rock="2a2520"),
}


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def rgb(h):
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], float) / 255


def pack(c):
    """(..., 3) floats 0..1 to 0xRRGGBB, rounded to steps of four: fine
    enough that deep water has no bands, coarse enough to save some
    churn."""
    c = (np.clip(c, 0, 1) * 255).astype(np.int64) & 0xFC
    return (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]


# ----------------------------------------------------------------- the grid

def weights(gx, gy, w, h):
    """The four grid points round each (gx, gy) on a w x h lattice, as
    flat indices and bilinear weights, clamped to the lattice."""
    gx = np.clip(gx, 0, w - 1.0001)
    gy = np.clip(gy, 0, h - 1.0001)
    i = gx.astype(np.int64)
    j = gy.astype(np.int64)
    fx = gx - i
    fy = gy - j
    k = j * w + i
    return ((k, (1 - fx) * (1 - fy)), (k + 1, fx * (1 - fy)),
            (k + w, (1 - fx) * fy), (k + w + 1, fx * fy))


def splat(gx, gy, value, shape):
    """Particles' values onto a lattice, averaged by weight; and where any
    particle reached."""
    h, w = shape
    total = np.zeros(h * w)
    weight = np.zeros(h * w)
    for k, a in weights(gx, gy, w, h):
        total += np.bincount(k, a * value, h * w)
        weight += np.bincount(k, a, h * w)
    reached = weight > 1e-6
    grid = np.where(reached, total / np.maximum(weight, 1e-9), 0.0)
    return grid.reshape(h, w), reached.reshape(h, w)


def gather(grid, gx, gy):
    h, w = grid.shape
    flat = grid.ravel()
    return sum(flat[k] * a for k, a in weights(gx, gy, w, h))


def extend(grid, known, passes=2):
    """Carry velocities out from where the water is into the air round it,
    so a particle at the edge of a splash reads a velocity, not a zero."""
    for _ in range(passes):
        total = np.zeros_like(grid)
        count = np.zeros_like(grid)
        for axis, step in ((0, 1), (0, -1), (1, 1), (1, -1)):
            g = np.roll(grid, step, axis)
            m = np.roll(known, step, axis)
            total += np.where(m, g, 0)
            count += m
        fill = ~known & (count > 0)
        grid = np.where(fill, total / np.maximum(count, 1), grid)
        known = known | fill
    return grid


# ----------------------------------------------------------------- the sea

class Surf:
    """A tank, y up, with a sea bed, a cliff, and water in it, bigger than
    the wall shows.

    The wall is a window on it. Out of sight to the left is more sea, with
    the wave maker and the stretch that swallows returning waves; above is
    room for spray to fly up into and fall back out of; to the right the
    cliff carries on. So nothing the wall shows has a tank's wall at it,
    and nothing bounces off one.
    """

    def __init__(self, cols, rows, rng):
        self.cols, self.rows = cols, rows
        self.rng = rng
        VW, VH = max(24, cols // 2), max(16, rows)
        x0 = int(VW * 0.35)
        W = x0 + VW + int(VW * 0.12)
        H = VH + int(VH * 0.6)
        self.W, self.H = W, H
        self.x0, self.VW, self.VH = x0, VW, VH
        xs = np.arange(W) + 0.5

        def wobble(amp, n=4):
            f = rng.uniform(0.02, 0.25, n)
            p = rng.uniform(0, 2 * np.pi, n)
            return amp * sum(np.sin(xs * fi + pi) for fi, pi in zip(f, p)) / n

        # The bed is deep out of sight and shoals across the wall towards
        # the cliff. The cliff is drawn rough first -- a face that leans
        # and bulges as it climbs, outcrops, boulders at its foot, a broken
        # top rising inland so what comes over drains home -- then blurred
        # and cut at half, which rounds every corner the way weather would.
        # It is kept FINE times finer than the water's cells, and both the
        # particles and the picture meet that shape, not the cells.
        F = FINE
        foot = x0 + VW * rng.uniform(0.74, 0.82)
        shoal = np.clip((xs - x0) / (foot - x0), 0, 1)
        bed = VH * (0.04 + 0.1 * shoal ** 2) + wobble(VH * 0.02)
        face = VH * rng.uniform(0.64, 0.72)
        top = VH * rng.uniform(0.72, 0.8)
        run = np.clip((xs - foot) / max(1, W - foot), 0, 1)
        crest = face + (top - face) * run ** 0.5 + wobble(VH * 0.05, 6)

        fx = (np.arange(W * F) + 0.5) / F
        fy = (np.arange(H * F) + 0.5) / F
        X, Y = fx[None, :], fy[:, None]
        # Where the face stands at each height: a lean (back, or a little
        # out over the water), a slow bulge, and outcrops.
        lean = rng.uniform(-0.12, 0.45)
        edge = foot + lean * (Y - bed[int(foot)])
        for _ in range(3):
            edge = edge + rng.uniform(0.6, 2.2) * np.sin(
                Y * rng.uniform(0.08, 0.35) + rng.uniform(0, 2 * np.pi))
        for _ in range(int(rng.integers(1, 4))):
            at = rng.uniform(bed[int(foot)], face)
            edge = edge - rng.uniform(2, 5) * np.exp(
                -((Y - at) / rng.uniform(1.5, 4)) ** 2)
        rough = (Y < np.interp(fx, xs, bed)[None, :]) | \
            ((X > edge) & (Y < np.interp(fx, xs, crest)[None, :]))
        # Boulders fallen at the foot, half buried.
        for _ in range(int(rng.integers(1, 4))):
            bx = foot - rng.uniform(1, 9)
            r = rng.uniform(1.5, 4)
            by = np.interp(bx, xs, bed) + r * rng.uniform(0, 0.6)
            rough |= (X - bx) ** 2 + (Y - by) ** 2 < r * r
        from scipy.ndimage import gaussian_filter
        self.rock = gaussian_filter(rough.astype(float), F * 1.1) > 0.5
        self.fine = F
        # A cell of the water's grid is solid only where it is rock all
        # but through; a cell the rock only cuts is water's to fill up to
        # the rock, which is what keeps the water meeting it with no gap.
        self.solid = self.rock.reshape(H, F, W, F).mean(axis=(1, 3)) > 0.9
        self.foot = foot
        y = np.arange(H)[:, None] + 0.5

        # Still water, packed a few particles a cell with a little jitter.
        self.level = VH * 0.45
        cells = np.argwhere(~self.solid & (y < self.level))
        n = len(cells) * PER_CELL
        j = np.repeat(cells[:, 0], PER_CELL)
        i = np.repeat(cells[:, 1], PER_CELL)
        x = i + rng.uniform(0.05, 0.95, n)
        y = j + rng.uniform(0.05, 0.95, n)
        dry = ~self.in_rock(x, y)
        self.x, self.y = x[dry], y[dry]
        self.u = np.zeros(len(self.x))
        self.v = np.zeros(len(self.x))
        self.p = None
        self.index = None
        # The wave maker, out of sight: a piston stroke every few seconds,
        # each one wave. A stroke rather than a steady to and fro, because
        # one big wave meeting the cliff alone is what bursts; a train of
        # them mostly cancels in the shallows.
        self.maker = (int(x0 * 0.45), int(x0 * 0.95))
        # Behind it, at the far end, a stretch of sea that swallows waves,
        # so what comes back off the cliff dies there rather than piling up
        # against the tank's end and coming in again.
        self.sponge = x0 * 0.4
        self.stroke = 1.4
        self.next = 0.5
        self.at = None
        self.strength = 0.0
        # The rock's look, fixed per coast.
        self.grain = rng.uniform(0.75, 1.15, (rows * 2, cols))

    def in_rock(self, x, y):
        f = self.fine
        return self.rock[np.clip((y * f).astype(np.int64), 0, self.rock.shape[0] - 1),
                         np.clip((x * f).astype(np.int64), 0, self.rock.shape[1] - 1)]

    # --------------------------------------------------------- one step

    def cells(self):
        i = np.clip(self.x.astype(np.int64), 0, self.W - 1)
        j = np.clip(self.y.astype(np.int64), 0, self.H - 1)
        fluid = np.zeros(self.H * self.W, bool)
        fluid[j * self.W + i] = True
        return fluid.reshape(self.H, self.W) & ~self.solid

    def project(self, u, v, fluid, dt):
        H, W, S = self.H, self.W, self.solid
        su = np.ones((H, W + 1), bool)
        su[:, 1:W] = S[:, :-1] | S[:, 1:]
        sv = np.ones((H + 1, W), bool)
        sv[1:H, :] = S[:-1, :] | S[1:, :]
        u[su] = 0
        v[sv] = 0
        div = (u[:, 1:] - u[:, :-1]) + (v[1:, :] - v[:-1, :])

        n = int(fluid.sum())
        if n == 0:
            return u, v
        idx = np.full((H, W), -1, np.int64)
        idx[fluid] = np.arange(n)
        Sp = np.pad(S, 1, constant_values=True)
        Fp = np.pad(fluid, 1, constant_values=False)
        Ip = np.pad(idx, 1, constant_values=-1)
        diag = np.zeros((H, W))
        r, c = [], []
        for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nS = Sp[1 + dj:H + 1 + dj, 1 + di:W + 1 + di]
            nF = Fp[1 + dj:H + 1 + dj, 1 + di:W + 1 + di]
            nI = Ip[1 + dj:H + 1 + dj, 1 + di:W + 1 + di]
            diag += ~nS
            pair = fluid & nF
            r.append(idx[pair])
            c.append(nI[pair])
        r = np.concatenate(r)
        c = np.concatenate(c)
        d = np.maximum(diag[fluid], 1)
        A = csr_matrix((np.concatenate([d, -np.ones(len(r))]),
                        (np.concatenate([np.arange(n), r]),
                         np.concatenate([np.arange(n), c]))), shape=(n, n))
        # Particles pack down under gravity a little every step, and a sea
        # that shrinks is a sea going flat. A cell holding more than its
        # share is asked to spread by that much, which holds the volume.
        count = np.bincount(
            np.clip(self.y.astype(np.int64), 0, H - 1) * W
            + np.clip(self.x.astype(np.int64), 0, W - 1),
            minlength=H * W).reshape(H, W)
        # Both ways inside the water, so the noise in how particles fall
        # into cells nets out; at the surface only outwards, since a
        # surface cell is thin by nature and pulling it in would suck the
        # sea down.
        crowd = count / PER_CELL - 1
        inside = fluid.copy()
        for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            inside &= (Fp | Sp)[1 + dj:H + 1 + dj, 1 + di:W + 1 + di]
        crowd = np.where(inside, np.clip(crowd, -0.5, 1),
                         np.clip(crowd - CROWD_FREE + 1, 0, 1))
        b = -(div - crowd * CROWD_PUSH / dt)[fluid]
        # The last step's pressure, where the same cells are water, is a
        # good first guess and roughly halves the iterations.
        x0 = None
        if self.p is not None:
            x0 = self.p[fluid]
        p, _ = cg(A, b, x0=x0, rtol=1e-3, maxiter=200)
        P = np.zeros((H, W))
        P[fluid] = p
        self.p = P
        gu = P[:, 1:] - P[:, :-1]
        gv = P[1:, :] - P[:-1, :]
        u[:, 1:W] -= np.where(su[:, 1:W], 0, gu)
        v[1:H, :] -= np.where(sv[1:H, :], 0, gv)
        return u, v

    def step(self, t, dt):
        H, W = self.H, self.W
        x, y = self.x, self.y
        u, ku = splat(x, y - 0.5, self.u, (H, W + 1))
        v, kv = splat(x - 0.5, y, self.v, (H + 1, W))
        u0, v0 = u.copy(), v.copy()
        fluid = self.cells()

        v -= G * dt
        if t >= self.next:
            # Sets, near enough: mostly middling waves, now and then a big
            # one, and the gap after a big one longer.
            self.at = t
            self.strength = self.rng.choice([0.7, 0.85, 1.0, 1.3],
                                            p=[0.3, 0.3, 0.25, 0.15])
            self.next = t + self.rng.uniform(4.5, 7.5) * self.strength
        if self.at is not None and t - self.at < self.stroke:
            # Out and back: a whole cycle leaves the sea where it was, so
            # the stroke makes a wave rather than shoving the water along.
            push = math.sin(2 * math.pi * (t - self.at) / self.stroke)
            a, b = self.maker
            u[:, a:b] += G * WAVE * self.strength * push * dt

        u, v = self.project(u, v, fluid, dt)
        u = extend(u, ku)
        v = extend(v, kv)
        u0 = extend(u0, ku)
        v0 = extend(v0, kv)

        nu = gather(u, x, y - 0.5)
        nv = gather(v, x - 0.5, y)
        self.u = FLIP * (self.u + nu - gather(u0, x, y - 0.5)) + (1 - FLIP) * nu
        self.v = FLIP * (self.v + nv - gather(v0, x - 0.5, y)) + (1 - FLIP) * nv
        calm = np.exp(-dt * 5 * np.clip(1 - x / self.sponge, 0, 1))
        self.u *= calm
        self.v *= calm
        # The whole sea drifting one way is the tank sloshing, which the
        # open sea would never do; waves move water back and forth and
        # average to nothing, so taking out the average drift leaves them.
        body = self.y < self.level * 1.3
        if body.any():
            self.u[body] -= self.u[body].mean() * (1 - math.exp(-dt / 1.5))
        cap = G * 1.2
        np.clip(self.u, -cap, cap, out=self.u)
        np.clip(self.v, -cap, cap, out=self.v)

        # Carried by the grid, a half step then a whole one.
        mx = x + gather(u, x, y - 0.5) * dt / 2
        my = y + gather(v, x - 0.5, y) * dt / 2
        nx = x + gather(u, mx, my - 0.5) * dt
        ny = y + gather(v, mx - 0.5, my) * dt
        nx = np.clip(nx, 0.01, W - 0.01)
        ny = np.clip(ny, 0.01, H - 0.01)
        # Into the rock is back where it was, and stopped.
        hit = self.in_rock(nx, ny)
        nx[hit], ny[hit] = x[hit], y[hit]
        self.u[hit] *= -0.1
        self.v[hit] *= -0.1
        self.x, self.y = nx, ny

    def tick(self, t, dt):
        # Steps short enough that nothing crosses more than a cell in one.
        fast = max(1.0, float(np.max(np.abs(self.u))), float(np.max(np.abs(self.v))))
        n = int(min(4, max(1, math.ceil(dt * fast / 0.9))))
        for s in range(n):
            self.step(t + dt * s / n, dt / n)

    # --------------------------------------------------------- the picture

    def frame(self, look, dots):
        cols, rows = self.cols, self.rows
        PW, PH = cols, rows * 2
        sx, sy = PW / self.VW, PH / self.VH
        # The window on the tank, and a margin of water round it, so the
        # blur at the wall's edges sees the sea carrying on past them.
        M = 2
        fx = (self.x - self.x0) * sx + M
        fy = (self.VH - self.y) * sy + M
        seen = (fx >= 0) & (fx < PW + 2 * M) & (fy >= 0) & (fy < PH + 2 * M)
        px = fx[seen].astype(np.int64)
        py = fy[seen].astype(np.int64)
        QW, QH = PW + 2 * M, PH + 2 * M
        k = py * QW + px
        count = np.bincount(k, minlength=QH * QW).reshape(QH, QW).astype(float)
        speed = np.hypot(self.u[seen], self.v[seen])
        fast = np.bincount(k, speed, QH * QW).reshape(QH, QW)

        def blur(a):
            a = a + 0.5 * (np.roll(a, 1, 0) + np.roll(a, -1, 0))
            a = a + 0.5 * (np.roll(a, 1, 1) + np.roll(a, -1, 1))
            return a[M:-M, M:-M]

        # Rock, from the same fine shape the particles meet, over the
        # margin too, so every open pixel is one water can reach.
        gy = self.VH - (np.arange(QH) - M + 0.5) / sy
        gx = self.x0 + (np.arange(QW) - M + 0.5) / sx
        stone_m = self.in_rock(gx[None, :], gy[:, None])
        rock = stone_m[M:-M, M:-M]

        # Particles near a pixel, counted only over the neighbours that
        # could hold water: against the sea bed or the cliff, half the
        # neighbours are rock, and counting them as empty would call the
        # water there thin -- dots of black along every rock edge.
        room = blur((~stone_m).astype(float))
        dens = blur(count) * 4 / np.maximum(room, 1)
        full = PER_CELL / (sx * sy) * 4          # dens in still water
        water = dens > full * 0.3
        mean = blur(fast) / np.maximum(blur(count), 1e-6)

        # Black above the water, so the eye stays on the sea.
        dark = np.zeros((PH, PW, 3))
        # Deeper is darker, and fast water and thin, airy water go white.
        # A pixel of air alone in the water is a gap in the particles, not
        # a pocket, and is drawn as water.
        near = sum(np.roll(np.roll(water, dy, 0), dx, 1)
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        wet = water | (near >= 5)
        # Depth is counted from the top of the body of water in each
        # column: the first place it is wet SOLID pixels deep, which a
        # splash in the air over the column never is. Counting from the
        # nearest air instead would start again under every bubble and
        # draw a bright stripe down from it.
        body_top = wet.copy()
        for k in range(1, SOLID):
            body_top[:-k] &= wet[k:]
            body_top[-k:] = False
        top = np.where(body_top.any(axis=0), body_top.argmax(axis=0), PH)
        # Eased across a few columns, so where the surface steps the shade
        # under it does not step with it.
        top = uniform_filter1d(top.astype(float), 5, mode="nearest")
        down = np.arange(PH)[:, None]
        depth = np.maximum(down - top[None, :], 0)
        # Water with rock over it -- in a notch of the cliff -- would count
        # its depth from the rock and come out as bright as the surface.
        # It takes the depth of the open water beside it on the same row
        # instead, so the sea runs into the notch the same shade.
        under = np.maximum.accumulate(rock, axis=0) & wet
        if under.any():
            beside = np.where(wet & ~under, depth.astype(float), np.inf)
            reach = max(3, int(round(sx * 8)))
            beside = minimum_filter1d(beside, 2 * reach + 1, axis=1)
            depth = np.where(under, np.where(np.isfinite(beside), beside,
                                             PH * 0.3), depth)
        d = 1 - np.exp(-depth / (PH * 0.18))
        body = look["top"] * (1 - d[..., None]) + look["deep"] * d[..., None]
        churn = np.clip((mean - G * 0.25) / (G * 0.5), 0, 1)
        airy = np.clip(1 - dens / full, 0, 1) * 0.8
        white = np.clip(np.maximum(churn, airy) * np.exp(-depth / 10), 0, 1)
        body = body + (look["foam"] - body) * white[..., None]
        stone = look["rock"] * self.grain[..., None]
        pic = np.where(rock[..., None], stone,
                       np.where(wet[..., None], body, dark))
        packed = pack(pic)
        ch = np.full((rows, cols), ord(HALF), np.int64)
        fg = packed[0::2]
        bg = packed[1::2]

        if dots:
            # Spray: particles out in the open, as dots in open cells.
            open_ = ~wet & ~rock
            clear = open_[0::2] & open_[1::2]
            vx, vy = fx[seen] - M, fy[seen] - M
            inside = (vx >= 0) & (vx < PW) & (vy >= 0) & (vy < PH)
            vx, vy = vx[inside], vy[inside]
            out = open_[vy.astype(np.int64), vx.astype(np.int64)]
            if out.any():
                dx = np.clip((vx[out] * 2).astype(np.int64), 0, cols * 2 - 1)
                dy = np.clip((vy[out] * 2).astype(np.int64), 0, rows * 4 - 1)
                grid = np.zeros((rows * 4, cols * 2), np.int64)
                grid[dy, dx] = 1
                bits = (grid.reshape(rows, 4, cols, 2)
                        * DOTS[None, :, None, :]).sum(axis=(1, 3))
                show = clear & (bits > 0)
                tint = pack(look["foam"] * 0.9)
                ch[show] = 0x2800 + bits[show]
                fg[show] = tint
        return ch, fg, bg


def look_of(name):
    return {k: rgb(v) for k, v in PALETTES[name].items()}


# ----------------------------------------------------------------- front end

def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except ValueError:
            say(f"surf: {text!r} is not a number, using {default}",
                file=sys.stderr, flush=True)
            return default
    return convert


def clamp(name, value, lo, hi):
    got = min(hi, max(lo, value))
    if got != value:
        say(f"surf: {name} {value:g} is outside {lo:g}..{hi:g}, using {got:g}",
            file=sys.stderr, flush=True)
    return got


def parse(argv):
    ap = argparse.ArgumentParser(
        prog="surf", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bad values are clamped or defaulted, never fatal: the wall "
               "keeps drawing and stderr says what changed.")
    ap.add_argument("host", nargs="?", help="receiver to attach to")
    ap.add_argument("--palette", default="all",
                    help="overcast, dusk, day, night, several comma-separated, "
                         "or all (default), a different one each coast")
    ap.add_argument("--secs", type=number(DEFAULT_SECS), default=DEFAULT_SECS,
                    help=f"seconds before a new coast (default {DEFAULT_SECS:g})")
    ap.add_argument("--speed", type=number(1.0), default=1.0,
                    help="multiplier on how fast the sea runs (default 1, "
                         "0.1..3)")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"font size to ask for (default {DEFAULT_FONT})")
    ap.add_argument("--fps", type=number(DEFAULT_FPS), default=DEFAULT_FPS,
                    help=f"frames a second at most (default {DEFAULT_FPS:g})")
    ap.add_argument("--seed", type=number(None), default=None,
                    help="random seed, for a run that repeats")
    args, unknown = ap.parse_known_intermixed_args(argv)
    for u in unknown:
        say(f"surf: ignoring unknown argument {u!r}", file=sys.stderr,
            flush=True)
    if not args.host:
        ap.error("no host given")
    return args


def main():
    args = parse(sys.argv[1:])
    palettes = list(PALETTES) if args.palette == "all" else \
        [n for n in args.palette.split(",") if n in PALETTES]
    if not palettes:
        say(f"surf: {args.palette!r} names no palette I have; using all",
            file=sys.stderr, flush=True)
        palettes = list(PALETTES)
    secs = clamp("secs", args.secs, 20, 86400)
    font = int(clamp("font", args.font, 4, 400))
    fps = clamp("fps", args.fps, 1, 60)
    speed = clamp("speed", args.speed, 0.1, 3)
    step = speed / fps
    rng = np.random.default_rng(None if args.seed is None else int(args.seed))

    wall = bitwall.attach(args.host, min_frame=1.0 / fps)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    dots = wall.can(BRAILLE)
    if not dots:
        say("surf: this screen has no braille; no spray in the air",
            file=sys.stderr, flush=True)

    started = time.monotonic()
    state = {"surf": None, "cycle": None, "palette": None, "look": None,
             "last": None, "t": 0.0}

    def render(cols, rows):
        now = time.monotonic()
        cycle = int((now - started) / secs)
        s = state["surf"]
        if s is None or state["cycle"] != cycle or (s.cols, s.rows) != (cols, rows):
            s = state["surf"] = Surf(cols, rows, rng)
            if state["cycle"] != cycle or state["palette"] is None:
                choices = [n for n in palettes if n != state["palette"]] \
                    or palettes
                state["palette"] = choices[rng.integers(len(choices))]
                state["look"] = look_of(state["palette"])
            state["cycle"] = cycle
            state["last"] = None
            state["t"] = 0.0
        # Every frame moves the sea on by the same step, however late it
        # was asked for: a frame held up on its way to the wall then only
        # slows the motion for a moment, where stepping by the time that
        # really passed stalls and then lurches to catch up.
        dt = 0.0 if state["last"] is None else step
        state["last"] = now
        state["t"] += dt
        if dt > 0:
            s.tick(state["t"], dt)
        return s.frame(state["look"], dots)

    with wall.transition():
        wall.set_font_size(font)
        wall.set_source(bitwall.FunctionSource(arrays=render))
    wall.meta(name="surf", description="Waves bursting against a cliff")
    cols, rows = wall.area()
    say(f"surf -> {args.host}  {cols}x{rows} at font {font}  "
        f"palettes {'+'.join(palettes)}  new coast every {secs:g}s  {fps:g}fps  "
        f"(ctrl-c to stop)", flush=True)

    named = None
    try:
        while not stop.wait(0.5):
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"surf: resized to {cols}x{rows}", flush=True)
            if state["cycle"] is not None and state["cycle"] != named:
                named = state["cycle"]
                s = state["surf"]
                say(f"surf: new coast, {state['palette']}, "
                    f"{len(s.x)} particles on {s.W}x{s.H}", flush=True)
    finally:
        say("surf: stopped", flush=True)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
