"""The 3D Pipes screensaver, drawn on a character grid.

The core owns the room, the pipes that grow through it and the drawing. It
hands back a grid of `Glyph` -- a character with truecolor `'rrggbb'`
colours -- and knows nothing about bitwall and nothing about terminals, so
the same renderer drives a wall through `main.py` and a terminal through
`pane-pipes`.

**Two pixels per cell.** A cell is about twice as tall as it is wide, so the
upper half block `U+2580` with a foreground and a background colour is two
square pixels stacked in one cell, and a 132x38 grid is a 132x76
framebuffer. Half blocks rather than quadrants or braille because a shaded
cylinder is a smooth colour ramp across its width: quadrants would make four
pixels share two colours and braille one, and either turns the shading into
a dither pattern. Full colour per pixel is what makes a pipe look round.

**Every pixel is a ray.** A pipe is a cylinder and a joint is a sphere, and
both have a closed-form ray intersection, so there are no triangles, no
tessellation and no seams: for each pixel in a primitive's screen bounding
box, solve the quadratic, keep the near root, and the surface normal falls
out of the hit point. The room is a pinhole camera at the origin looking
along +z with the image plane at `f`, which is what makes a pipe running
away from the viewer narrow as it goes.

**A z-buffer is what makes it three-dimensional.** Pipes cross in front of
and behind each other, and the depth test decides that per pixel. Because
nothing in the room ever moves, the colour and depth buffers are never
cleared: a frame draws only the growing tips into buffers that already hold
everything drawn so far, and the depth test occludes a new pipe against old
ones for free.

**Redrawing has to be idempotent, because the diff is the bandwidth.** A
growing pipe redraws its whole segment every frame, and the strict `t <
depth` test means every pixel already on the surface keeps the value it
had. Only the few pixels the tip has newly reached change, so a screensaver
that fills the wall costs a couple of KiB a second on the wire. That is
also why silhouettes are hard-edged rather than antialiased: partial
coverage would have to blend against what is behind it every time the tip
passed, and a pixel that changes value on every frame is a cell on the
wire. The rim darkening below buys most of what the smoothing would --
edge-on the surface turns away from the light and goes dark, which is what
reads as round.

**The room fills, then it is wiped.** Pipes walk a lattice and never enter
an occupied node, so the room does eventually clog; at a fraction of the
nodes taken it dissolves on a ragged diagonal and starts again. A dissolve
rather than a fade because a fade changes every cell on every frame of it,
where a dissolve changes each cell once.
"""
import math
import random
import time
from collections import namedtuple

import numpy as np

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

UPPER = "▀"          # fg is the top pixel, bg the bottom
LOWER = "▄"          # a bottom pixel on its own, over the receiver's own bg

#: Pixels wanted per lattice unit. It sets everything else about the scale:
#: the room is as many nodes across as fit at this spacing, so a pipe is
#: about eight pixels thick whatever grid the receiver hands over.
UNIT_PX = 15.0

RADIUS = 0.33        # pipe radius, in lattice units
JOINT = 0.46         # ball at a turn, fat enough to swallow both pipe ends

FPS = 20
STEP_FRAMES = 8      # frames to cross one lattice unit: 2.5 nodes a second
FILL = 0.55          # share of the room's nodes taken before it is wiped
WIPE_FRAMES = 16
RESPAWN_FRAMES = 10
#: Frames with nothing growing before the room is wiped anyway. A room
#: whose free nodes are all walled in has no move left, and a wall showing
#: a still picture forever looks exactly like a sender that has died.
STALL_FRAMES = FPS * 8

# A pipe runs this many nodes before it stops and a new one starts somewhere
# else, so the room gets several colours rather than one long snake.
RUN = (18, 46)

#: Direction to the light: over the viewer's left shoulder and above. y is
#: down the screen, z away from the camera.
LX, LY, LZ = -0.449, -0.748, -0.489

AMBIENT = 0.16
DIFFUSE = 0.92
SPECULAR = 0.55
RIM = 0.50           # how dark the surface goes where it turns edge-on
FOG = 0.55           # how much of its light the back of the room loses

DIRS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

#: Shiny plastic, which is what the original's pipes are made of.
PALETTE = (
    (236, 62, 62), (64, 148, 255), (78, 222, 128), (255, 190, 64),
    (204, 96, 236), (72, 226, 226), (255, 124, 48), (232, 236, 244),
)

INF = np.float32(np.inf)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class Pipe:
    """One growing run: where its tip is, where it is going, and its colour.

    `step` counts frames into the move from `node` to `node + dir`, so the
    tip advances a fraction of a lattice unit per frame and the segment is
    redrawn a little longer each time.
    """

    __slots__ = ("node", "dir", "colour", "left", "step", "alive", "wait")

    def __init__(self, node, direction, colour, left):
        self.node = node
        self.dir = direction
        self.colour = colour
        self.left = left
        self.step = 0
        self.alive = True
        self.wait = 0


class Game:
    """Pipes growing through a room, composed for whatever area it is given.

    `frame(cols, rows)` is the whole public surface: an area in, a grid of
    Glyph out. Frames are paced to `FPS`; a call before the next one is due
    gets the last grid back, unchanged, which on a wall costs nothing at all.
    """

    FPS = FPS

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.cols = self.rows = 0
        self.next_frame = 0.0
        self.grid = []
        self.dirty = {}
        self._glyphs = {}
        self.prims = []
        self.pipes = []
        self.wipe = None
        self.wipe_at = 0
        self.frames = 0
        self.idle = 0
        self.hue = 0

    # -------------------------------------------------------------- setup

    def _layout(self, cols, rows):
        """Room, camera and buffers for an area, and the scene put back.

        The lattice is sized in pixels rather than fixed, so a preview box
        and a 2K wall both get pipes of the same apparent thickness -- the
        big one simply gets more room to fill. A resize that lands on the
        same lattice keeps the pipes and redraws them through the new
        camera; one that changes it has nowhere to put them and starts over.
        """
        self.cols, self.rows = cols, rows
        self.W, self.H = cols, rows * 2
        self.cx0, self.cy0 = self.W / 2.0, self.H / 2.0

        nx = int(_clamp(round(self.W / UNIT_PX), 4, 24))
        ny = int(_clamp(round(self.H / UNIT_PX), 3, 16))
        nz = int(_clamp(nx, 4, 16))
        same = (nx, ny, nz) == getattr(self, "dims", None)
        self.dims = (nx, ny, nz)
        self.ox, self.oy = (nx - 1) / 2.0, (ny - 1) / 2.0

        # The room sits far enough away to be seen whole and near enough to
        # have perspective: the back plane is about half again the distance
        # of the front, so a pipe at the back is visibly smaller.
        dz = nz - 1
        self.zfront = 1.5 * dz + 4.0
        sx = self.ox + RADIUS + 0.3
        sy = self.oy + RADIUS + 0.3
        self.f = 0.98 * min(self.cx0 * self.zfront / sx,
                            self.cy0 * self.zfront / sy)
        self.zfog = self.zfront - 0.5
        self.fogk = 1.0 / (dz + 1.0)

        self.fb = np.zeros((self.H, self.W, 3), np.uint8)
        self.z = np.full((self.H, self.W), INF, np.float32)
        self.grid = [[BLANK] * cols for _ in range(rows)]
        self.dirty = {}
        self.wipe = None

        if same:
            for p in self.prims:
                self._draw(p)
            for pipe in self.pipes:
                if pipe.alive and pipe.step:
                    self._grow(pipe)
        else:
            self.occupied = np.zeros(self.dims, bool)
            self.taken = 0
            self.idle = 0
            nodes = nx * ny * nz
            self.limit = max(12, int(FILL * nodes))
            self.prims = []
            self.pipes = []
            # Enough pipes that the room fills in a minute or two, which is
            # a scene rather than a wait. A preview box is a small room and
            # one pipe is all it has space for.
            n = int(_clamp(round(nodes / 150.0), 1, 8))
            for i in range(n):
                pipe = self._spawn()
                if pipe:
                    pipe.wait = i * RESPAWN_FRAMES      # stagger the starts
                    self.pipes.append(pipe)

    def _reset(self):
        """Empty the room and start again, keeping the camera."""
        self.occupied[:] = False
        self.taken = 0
        self.idle = 0
        self.prims = []
        for i, pipe in enumerate(self.pipes):
            fresh = self._spawn()
            if fresh:
                fresh.wait = i * RESPAWN_FRAMES
                self.pipes[i] = fresh

    # -------------------------------------------------------- the lattice

    def _world(self, node):
        """Lattice node to camera space."""
        i, j, k = node
        return (i - self.ox, j - self.oy, self.zfront + k)

    def _free(self, node):
        i, j, k = node
        nx, ny, nz = self.dims
        return (0 <= i < nx and 0 <= j < ny and 0 <= k < nz
                and not self.occupied[i, j, k])

    def _choose(self, node, heading):
        """Where a pipe goes next: straight on if it can, else a turn.

        Straight is weighted rather than forced, because a pipe that only
        turns when it is cornered draws long dull runs, and one that turns
        freely never crosses the room.
        """
        options = [d for d in DIRS
                   if d != tuple(-c for c in heading)
                   and self._free(tuple(n + c for n, c in zip(node, d)))]
        if not options:
            return None
        if heading in options and self.rng.random() < 0.62:
            return heading
        turns = [d for d in options if d != heading]
        return self.rng.choice(turns or options)

    def _spawn(self):
        """A new pipe at a free node with somewhere to go."""
        nx, ny, nz = self.dims
        for _ in range(60):
            node = (self.rng.randrange(nx), self.rng.randrange(ny),
                    self.rng.randrange(nz))
            if not self._free(node):
                continue
            heading = self._choose(node, self.rng.choice(DIRS))
            if heading is None:
                continue
            self.occupied[node] = True
            self.taken += 1
            colour = PALETTE[self.hue % len(PALETTE)]
            self.hue += self.rng.choice((1, 3, 5))   # consecutive differ
            pipe = Pipe(node, heading, colour, self.rng.randint(*RUN))
            self._emit(("s", self._world(node), JOINT, colour))
            return pipe
        return None

    # ------------------------------------------------------------ the sim

    def _advance(self):
        """One frame of growth for every pipe."""
        grew = False
        for i, pipe in enumerate(self.pipes):
            if pipe.wait:
                pipe.wait -= 1
                continue
            if not pipe.alive:
                fresh = self._spawn()
                if fresh:
                    self.pipes[i] = fresh
                else:
                    pipe.wait = RESPAWN_FRAMES
                continue
            pipe.step += 1
            self._grow(pipe)
            grew = True
            if pipe.step >= STEP_FRAMES:
                self._arrive(pipe)
        self.idle = 0 if grew else self.idle + 1

    def _grow(self, pipe):
        """Draw the segment as far as the tip has got."""
        u = min(1.0, pipe.step / float(STEP_FRAMES))
        a = self._world(pipe.node)
        b = tuple(c + d * u for c, d in zip(a, pipe.dir))
        self._cylinder(a, b, RADIUS, pipe.colour)

    def _arrive(self, pipe):
        """The tip reaches the next node: take it, and pick the way on."""
        node = tuple(n + d for n, d in zip(pipe.node, pipe.dir))
        a, b = self._world(pipe.node), self._world(node)
        self._emit(("c", a, b, RADIUS, pipe.colour))
        self.occupied[node] = True
        self.taken += 1
        pipe.node = node
        pipe.step = 0
        pipe.left -= 1
        heading = None if pipe.left <= 0 else self._choose(node, pipe.dir)
        if heading is None:
            self._emit(("s", b, JOINT, pipe.colour))     # a capped end
            pipe.alive = False
            pipe.wait = RESPAWN_FRAMES
            return
        if heading != pipe.dir:
            self._emit(("s", b, JOINT, pipe.colour))     # the elbow
        pipe.dir = heading

    def _emit(self, prim):
        """Draw a finished primitive and keep it, so a resize can replay it."""
        self.prims.append(prim)
        self._draw(prim)

    def _draw(self, prim):
        if prim[0] == "c":
            self._cylinder(prim[1], prim[2], prim[3], prim[4])
        else:
            self._sphere(prim[1], prim[2], prim[3])

    # -------------------------------------------------------- the raster

    def _rays(self, centre, radius):
        """Pixel box a bounding sphere covers, and the rays through it.

        A ray is `(dx, dy, 1)` unnormalised, so the parameter that solves an
        intersection is the hit point's z and the depth buffer is the same
        thing a z-buffer always was.
        """
        cx, cy, cz = centre
        if cz <= radius + 0.05:
            return None
        rad = self.f * radius / math.sqrt(cz * cz - radius * radius)
        rad = rad * 1.1 + 1.5
        px, py = self.cx0 + self.f * cx / cz, self.cy0 + self.f * cy / cz
        x0 = max(0, int(px - rad))
        x1 = min(self.W, int(px + rad) + 2)
        y0 = max(0, int(py - rad))
        y1 = min(self.H, int(py + rad) + 2)
        if x0 >= x1 or y0 >= y1:
            return None
        dx = (np.arange(x0, x1, dtype=np.float32) + 0.5 - self.cx0) / self.f
        dy = (np.arange(y0, y1, dtype=np.float32) + 0.5 - self.cy0) / self.f
        return x0, x1, y0, y1, dx[None, :], dy[:, None]

    def _sphere(self, centre, radius, colour):
        box = self._rays(centre, radius)
        if box is None:
            return
        x0, x1, y0, y1, dx, dy = box
        cx, cy, cz = centre
        qa = dx * dx + dy * dy + 1.0
        qb = -2.0 * (dx * cx + dy * cy + cz)
        qc = cx * cx + cy * cy + cz * cz - radius * radius
        disc = qb * qb - 4.0 * qa * qc
        hit = disc > 0.0
        t = (-qb - np.sqrt(np.where(hit, disc, 0.0))) / (2.0 * qa)
        ok = hit & (t > 0.05)
        if not ok.any():
            return
        inv = 1.0 / radius
        nx = (t * dx - cx) * inv
        ny = (t * dy - cy) * inv
        nz = (t - cz) * inv
        self._paint(ok, t, nx, ny, nz, dx, dy, colour, x0, x1, y0, y1)

    def _cylinder(self, a, b, radius, colour):
        ax, ay, az = a
        ux, uy, uz = (b[0] - ax, b[1] - ay, b[2] - az)
        length = math.sqrt(ux * ux + uy * uy + uz * uz)
        if length < 1e-4:
            return
        ux, uy, uz = ux / length, uy / length, uz / length
        half = length / 2
        mid = (ax + ux * half, ay + uy * half, az + uz * half)
        box = self._rays(mid, half + radius)
        if box is None:
            return
        x0, x1, y0, y1, dx, dy = box

        # The quadratic is the ray against an infinite cylinder, taken in the
        # plane across its axis: strip the along-axis part off both the ray
        # direction and the offset to its axis, and it is a circle again.
        dpar = dx * ux + dy * uy + uz
        ex = dx - dpar * ux
        ey = dy - dpar * uy
        ez = 1.0 - dpar * uz
        wpar = -(ax * ux + ay * uy + az * uz)
        fx = -ax - wpar * ux
        fy = -ay - wpar * uy
        fz = -az - wpar * uz
        qa = ex * ex + ey * ey + ez * ez
        qb = 2.0 * (ex * fx + ey * fy + ez * fz)
        qc = fx * fx + fy * fy + fz * fz - radius * radius
        disc = qb * qb - 4.0 * qa * qc
        hit = disc > 0.0
        t = (-qb - np.sqrt(np.where(hit, disc, 0.0))) / (2.0 * qa)
        h = wpar + t * dpar
        ok = hit & (t > 0.05) & (h >= 0.0) & (h <= length)
        if not ok.any():
            return
        inv = 1.0 / radius
        nx = (t * dx - (ax + h * ux)) * inv
        ny = (t * dy - (ay + h * uy)) * inv
        nz = (t - (az + h * uz)) * inv
        self._paint(ok, t, nx, ny, nz, dx, dy, colour, x0, x1, y0, y1)

    def _paint(self, ok, t, nx, ny, nz, dx, dy, colour, x0, x1, y0, y1):
        """Depth-test the hits, shade the survivors, and mark their cells."""
        zs = self.z[y0:y1, x0:x1]
        keep = ok & (t < zs)
        if not keep.any():
            return
        rgb = self._shade(nx, ny, nz, dx, dy, t, colour)
        zs[keep] = t[keep]
        self.fb[y0:y1, x0:x1][keep] = rgb[keep]
        for row in range(y0 // 2, (y1 - 1) // 2 + 1):
            span = self.dirty.get(row)
            if span is None:
                self.dirty[row] = [x0, x1]
            else:
                if x0 < span[0]:
                    span[0] = x0
                if x1 > span[1]:
                    span[1] = x1

    def _shade(self, nx, ny, nz, dx, dy, t, colour):
        """Lit plastic: a diffuse term, a highlight, a dark rim, and fog.

        The rim is what a cylinder needs most at this size. Lambert alone
        leaves the lit side flat and the silhouette as bright as the middle,
        and eight pixels across is not enough gradient to read as round
        without it. Fog puts the back of the room behind the front.
        """
        inv = 1.0 / np.sqrt(dx * dx + dy * dy + 1.0)
        vx, vy, vz = -dx * inv, -dy * inv, -inv
        ndl = np.clip(nx * LX + ny * LY + nz * LZ, 0.0, 1.0)

        hx, hy, hz = LX + vx, LY + vy, LZ + vz
        hn = 1.0 / np.sqrt(hx * hx + hy * hy + hz * hz)
        spec = np.clip((nx * hx + ny * hy + nz * hz) * hn, 0.0, 1.0)
        spec *= spec
        spec *= spec
        spec *= spec
        spec *= spec                                  # cos^16, no pow call

        ndv = np.clip(nx * vx + ny * vy + nz * vz, 0.0, 1.0)
        rim = 1.0 - ndv
        rim *= rim * rim

        far = np.clip((t - self.zfog) * self.fogk, 0.0, 1.0)
        lit = (1.0 - FOG * far) * (1.0 - RIM * rim)
        gain = (AMBIENT + DIFFUSE * ndl) * lit
        out = np.empty(gain.shape + (3,), np.float32)
        white = 255.0 * SPECULAR * spec * lit
        for c in range(3):
            out[..., c] = colour[c] * gain + white
        return np.clip(out, 0.0, 255.0).astype(np.uint8)

    # --------------------------------------------------------- the wipe

    def _start_wipe(self):
        """Order the cells for a ragged diagonal sweep across the room."""
        n = self.rows * self.cols
        idx = np.arange(n)
        xs = (idx % self.cols) / float(self.cols)
        ys = (idx // self.cols) / float(self.rows)
        key = xs + 0.55 * ys + np.random.random(n) * 0.5
        self.wipe = np.array_split(np.argsort(key), WIPE_FRAMES)
        self.wipe_at = 0

    def _wipe_step(self):
        chunk = self.wipe[self.wipe_at]
        self.wipe_at += 1
        ys, xs = chunk // self.cols, chunk % self.cols
        py = np.concatenate((ys * 2, ys * 2 + 1))
        px = np.concatenate((xs, xs))
        self.z[py, px] = INF
        self.fb[py, px] = 0
        lo, hi = int(xs.min()), int(xs.max()) + 1
        for row in np.unique(ys).tolist():
            span = self.dirty.get(row)
            if span is None:
                self.dirty[row] = [lo, hi]
            else:
                span[0] = min(span[0], lo)
                span[1] = max(span[1], hi)
        if self.wipe_at >= len(self.wipe):
            self.wipe = None
            self._reset()

    # --------------------------------------------------------- the cells

    def _glyph(self, top, bottom):
        """A cell for a pixel pair, from a cache, so unchanged cells are
        the same object frame after frame and a front end's own conversion
        can be a lookup."""
        key = (top, bottom)
        g = self._glyphs.get(key)
        if g is None:
            if len(self._glyphs) > 300000:
                self._glyphs.clear()
            if top is None:
                g = BLANK if bottom is None else Glyph(LOWER, bottom,
                                                       None, False)
            else:
                g = Glyph(UPPER, top, bottom, False)
            self._glyphs[key] = g
        return g

    def _sync(self):
        """Rebuild the cells over every pixel that was drawn into."""
        if not self.dirty:
            return
        fb, z = self.fb, self.z
        for row, (x0, x1) in self.dirty.items():
            ty, by = row * 2, row * 2 + 1
            tcode = ((fb[ty, x0:x1, 0].astype(np.int32) << 16)
                     | (fb[ty, x0:x1, 1].astype(np.int32) << 8)
                     | fb[ty, x0:x1, 2])
            bcode = ((fb[by, x0:x1, 0].astype(np.int32) << 16)
                     | (fb[by, x0:x1, 1].astype(np.int32) << 8)
                     | fb[by, x0:x1, 2])
            tc = np.where(z[ty, x0:x1] < INF, tcode, -1).tolist()
            bc = np.where(z[by, x0:x1] < INF, bcode, -1).tolist()
            out = list(self.grid[row])       # a new row: the last one is
            glyph = self._glyph               # still somebody's last frame
            for i in range(x1 - x0):
                t, b = tc[i], bc[i]
                out[x0 + i] = glyph("%06x" % t if t >= 0 else None,
                                    "%06x" % b if b >= 0 else None)
            self.grid[row] = out
        self.dirty.clear()

    # ----------------------------------------------------------- the API

    def frame(self, cols, rows):
        """The grid for an area, advanced if this frame is due."""
        if cols < 2 or rows < 2:
            return [[BLANK] * max(1, cols) for _ in range(max(1, rows))]
        if (cols, rows) != (self.cols, self.rows):
            self._layout(cols, rows)
        now = time.monotonic()
        if now >= self.next_frame:
            self.next_frame = max(now, self.next_frame) + 1.0 / FPS
            self.frames += 1
            if self.wipe is not None:
                self._wipe_step()
            elif self.taken >= self.limit or self.idle >= STALL_FRAMES:
                self._start_wipe()
            else:
                self._advance()
            self._sync()
        return list(self.grid)
