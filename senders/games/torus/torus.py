"""A lit solid tumbling on a character grid, computed for the grid it gets.

The core of the torus sender. It hands back a grid of `Glyph` -- a character
with truecolor `'rrggbb'` colours -- and knows nothing about bitwall and
nothing about terminals, so the same scene drives a wall through `main.py`
and a terminal through `pane-torus`.

**The screen is a framebuffer of square pixels, not a grid of letters.** A
cell is about twice as tall as it is wide, and a half block `▀` splits it
into two pixels that are each about square, with a full colour of its own.
So the picture is drawn at `cols` by `rows * 2` pixels and the aspect
correction is in the shape of that buffer rather than in a fudge factor
applied to the projection. Quadrants would buy another doubling across, but
two colours would then have to cover four pixels: a smooth shaded surface
crossing a tile would be forced to two of its many values and the gradient
that carries the form would band. Braille is finer still and worse for the
same reason, one colour to eight pixels. A half block is the only one of
the three that is lossless -- two pixels, two colours -- and shading, not
edge detail, is what this picture is made of.

**Everything is derived from the area.** The bounding sphere of the shape,
the distance the camera sits at and the buffer it must fit inside give the
focal length in closed form, so the solid is as large as it can be at every
orientation and no larger. The sample density comes from the same number:
enough points that adjacent ones land in adjacent pixels, and not one more.
A 200x60 wall and a 40x12 preview are the same code with a different area.
Past about four hundred columns the honest density costs more than a frame
has time for, and the picture is then drawn into a smaller buffer and blown
up rather than sampled too thinly: a surface sampled too thinly shows holes,
where a blown-up one only shows fewer, larger pixels.

**Shading is continuous, because the cells can carry it.** There is no
character ramp. A key light, a bounce behind it, a Blinn-Phong highlight and
a rim make one brightness between nothing and everything, and that
brightness indexes a 256-entry ramp running from a cold near-black shadow
through ember and amber to white hot. One family of hues getting hotter is
what a lit object does, and it reads as a solid from the far end of a room
where a twelve-step ramp of punctuation reads as text. The ramp is quantised
only because 255 levels are more than an eye finds in a gradient this size,
and because it makes a pixel one byte all the way to the cell.

**The z-buffer is a sort.** Points are drawn back to front and the nearest
write wins, which is a z-buffer without the buffer. Quantising depth to 16
bits makes that sort a radix sort rather than a comparison sort, so the
per-frame cost is linear in the samples. Half of them never reach it: a
point whose normal faces away cannot be seen, and the margin on that test
is the exact half-angle of the view cone, so nothing near a silhouette is
thrown away by it.

**One shape or another changes four lines.** A parametric surface is a
(N, 3) of points and a (N, 3) of unit normals; everything downstream of
that -- the rotation, the projection, the sort, the shading, the cells --
is the same arithmetic whatever produced them.
"""
import math
import time
from collections import namedtuple

import numpy as np

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

SHAPES = ("torus", "knot", "sphere", "cube")

#: Pixel rows a half block stacks into one cell, and the cell's own
#: proportions. Together they say a pixel of the framebuffer is
#: CELL_ASPECT / SUBROWS as tall as it is wide, which is 1 -- square.
SUBROWS = 2
CELL_ASPECT = 2.0

FILL = 0.90          # of the short side, at the worst orientation
DIST = 2.4           # camera distance, in bounding radii
STEP = 0.7           # pixels between neighbouring samples on the surface
LOOSE = 0.80         # the widest spacing that still leaves no holes
MAX_SAMPLES = 500000
LEVELS = 255         # brightnesses in the ramp; index 0 is the background
FLAT = 4             # levels apart that two pixels still count as one

#: One key light, one dim bounce opposite it, a highlight and a rim. The
#: bounce is what keeps a face turned away from the key from going to the
#: background colour and taking a whole side of the solid with it; because
#: the ramp is cool at the bottom and warm at the top, a surface lit only by
#: the bounce comes out cold and dark on its own, which is what a shadow
#: side looks like and costs nothing extra to say.
AMBIENT = 0.05
DIFFUSE = 0.58
BOUNCE = 0.19
SPECULAR = 0.50
RIM = 0.20
SHINE = 5            # squarings, so the highlight exponent is 2 ** 5

#: Ramps as a few stops, interpolated to 256 entries. Each runs from a
#: shadow that is dark but not the background -- a silhouette has to read
#: against a black wall -- up through one family of hues to white. One
#: family, because a surface lit by one light gets brighter and whiter, and
#: anything that changes hue as it turns reads as noise rather than form.
PALETTES = {
    "ember": ((0.00, (8, 10, 24)), (0.16, (58, 22, 40)),
              (0.34, (140, 44, 26)), (0.54, (214, 94, 20)),
              (0.74, (246, 164, 54)), (0.90, (252, 220, 142)),
              (1.00, (255, 250, 236))),
    "ice": ((0.00, (6, 12, 28)), (0.20, (18, 48, 92)),
            (0.42, (30, 100, 156)), (0.62, (72, 160, 208)),
            (0.82, (152, 210, 236)), (1.00, (246, 252, 255))),
    "jade": ((0.00, (6, 16, 14)), (0.22, (14, 58, 42)),
             (0.45, (26, 114, 72)), (0.66, (80, 178, 110)),
             (0.84, (168, 222, 162)), (1.00, (244, 255, 240))),
}


def ramp(name):
    """A palette's stops as 256 rgb rows, entry 0 reserved for nothing."""
    stops = PALETTES[name]
    xs = np.array([s[0] for s in stops], np.float32)
    t = np.linspace(0.0, 1.0, LEVELS, dtype=np.float32)
    out = np.zeros((256, 3), np.uint8)
    for c in range(3):
        ys = np.array([s[1][c] for s in stops], np.float32)
        out[1:, c] = np.interp(t, xs, ys).round()
    return out


# ------------------------------------------------------------------ shapes

def _pack(x, y, z):
    """Three broadcast component arrays as one contiguous (N, 3)."""
    return np.stack([np.broadcast_to(a, np.broadcast_shapes(
        x.shape, y.shape, z.shape)).ravel() for a in (x, y, z)],
        axis=1).astype(np.float32)


def _ring(n):
    """`n` angles round a full turn, the last not repeating the first."""
    return np.linspace(0, 2 * np.pi, n, endpoint=False, dtype=np.float32)


def _turn(spu, arc, least):
    """Samples to lay along an arc of world length `arc` at `spu` a unit."""
    return max(least, int(math.ceil(arc * spu)))


def torus(spu, ring=2.0, tube=1.0):
    """The classic donut: a circle of radius `tube` swept round the z axis.

    Each circle round the z axis gets samples for its own length. The inner
    edge is a third the length of the outer one, and giving every circle
    the outer one's count would spend a third of the samples on points that
    land on pixels already taken.
    """
    nu = _turn(spu, 2 * math.pi * tube, 12)
    u = _ring(nu)
    counts = [_turn(spu, 2 * math.pi * (ring + tube * math.cos(a)), 24)
              for a in u.tolist()]
    u = np.repeat(u, counts)
    v = np.concatenate([_ring(n) for n in counts])
    cu, su, cv, sv = np.cos(u), np.sin(u), np.cos(v), np.sin(v)
    nrm = _pack(cu * cv, cu * sv, su)
    pts = _pack((ring + tube * cu) * cv, (ring + tube * cu) * sv, tube * su)
    return pts, nrm


def sphere(spu, r=2.4):
    """A ball. The normal is the point, which is the whole of the maths."""
    nu = _turn(spu, 2 * math.pi * r, 24)
    nv = _turn(spu, math.pi * r, 12)
    u = _ring(nu)[:, None]
    v = np.linspace(0, np.pi, nv, dtype=np.float32)[None, :]
    nrm = _pack(np.sin(v) * np.cos(u), np.sin(v) * np.sin(u),
                np.broadcast_to(np.cos(v), (nu, nv)))
    return nrm * r, nrm


def _transport(c):
    """A frame that slides along a closed curve without spinning in it.

    Crossing the tangent with a fixed axis gives a frame that flips wherever
    the curve happens to run along that axis, so instead each ring's frame is
    the previous one with its tangent component taken out and renormalised.
    The two ends need not agree: they describe the same circle of points and
    differ only in where its zero is, which no surface can show.
    """
    n = len(c)
    out = []
    u = None
    for i in range(n):
        a, b = c[(i + 1) % n], c[i - 1]
        t = [a[j] - b[j] for j in range(3)]
        m = math.sqrt(sum(v * v for v in t)) or 1.0
        t = [v / m for v in t]
        if u is None:
            seed = [0.0, 0.0, 1.0] if abs(t[2]) < 0.9 else [1.0, 0.0, 0.0]
            u = seed
        d = sum(u[j] * t[j] for j in range(3))
        u = [u[j] - d * t[j] for j in range(3)]
        m = math.sqrt(sum(v * v for v in u)) or 1.0
        u = [v / m for v in u]
        w = [t[1] * u[2] - t[2] * u[1],
             t[2] * u[0] - t[0] * u[2],
             t[0] * u[1] - t[1] * u[0]]
        out.append((u, w))
    return out


def knot(spu, p=2, q=3, tube=0.42):
    """A (2, 3) torus knot, as a tube swept round its own centreline."""
    fine = 2048
    t = np.linspace(0, 2 * np.pi, fine, endpoint=False, dtype=np.float64)
    rad = 2.0 + np.cos(q * t)
    line = np.stack([rad * np.cos(p * t), rad * np.sin(p * t),
                     -np.sin(q * t)], axis=1)
    span = float(np.linalg.norm(np.diff(line, axis=0, append=line[:1]),
                                axis=1).sum())
    nv = _turn(spu, span, 64)
    nu = _turn(spu, 2 * math.pi * tube, 10)
    t = np.linspace(0, 2 * np.pi, nv, endpoint=False, dtype=np.float64)
    rad = 2.0 + np.cos(q * t)
    line = np.stack([rad * np.cos(p * t), rad * np.sin(p * t),
                     -np.sin(q * t)], axis=1)
    frame = _transport(line.tolist())
    u = np.array([f[0] for f in frame], np.float32)
    w = np.array([f[1] for f in frame], np.float32)
    a = np.linspace(0, 2 * np.pi, nu, endpoint=False, dtype=np.float32)
    ca, sa = np.cos(a)[:, None, None], np.sin(a)[:, None, None]
    nrm = (ca * u[None] + sa * w[None]).reshape(-1, 3)
    pts = (line[None].astype(np.float32) + tube * (ca * u[None] + sa * w[None])
           ).reshape(-1, 3)
    n = np.linalg.norm(nrm, axis=1, keepdims=True)
    return np.ascontiguousarray(pts), np.ascontiguousarray(nrm / n)


def cube(spu, edge=3.2):
    """Six flat faces, which is what a hard edge and a flat highlight need."""
    k = _turn(spu, edge, 6)
    g = ((np.arange(k, dtype=np.float32) + 0.5) / k - 0.5) * edge
    a, b = np.meshgrid(g, g, indexing="ij")
    flat = np.full(a.shape, edge / 2, np.float32)
    pts, nrm = [], []
    for axis in range(3):
        for sign in (1.0, -1.0):
            face = [a, b]
            face.insert(axis, flat * sign)
            pts.append(_pack(*face))
            one = np.zeros(3, np.float32)
            one[axis] = sign
            nrm.append(np.broadcast_to(one, (a.size, 3)))
    return np.concatenate(pts), np.ascontiguousarray(np.concatenate(nrm))


BUILD = {"torus": torus, "sphere": sphere, "knot": knot, "cube": cube}


# ------------------------------------------------------------------- scene

class Scene:
    """A solid turning in front of a light, drawn on whatever area it gets.

    `frame(cols, rows)` is the whole public surface: an area in, a grid of
    Glyph out. Geometry is rebuilt when the area or the shape changes and
    reused otherwise, so the steady state is a rotation, a sort and a
    lookup.
    """

    #: Turns a second about the two axes. Irrational to each other, so the
    #: tumble never comes back round to where a viewer last looked at it.
    SPIN = (0.61, 0.97)
    #: The key light walks slowly round the view axis. A fixed light leaves
    #: a sphere -- and any other surface of revolution seen down its own
    #: axis -- a still picture, and a wall showing a still picture looks
    #: broken. Slow enough to read as the sun rather than a disco.
    ORBIT = 2 * math.pi / 26.0

    def __init__(self, shape="torus", palette="ember", spin=1.0, dwell=25.0):
        self.shapes = list(SHAPES) if shape == "all" else [shape]
        for s in self.shapes:
            if s not in BUILD:
                raise ValueError("unknown shape %s" % s)
        self.spin = spin
        self.dwell = dwell
        self.rgb = ramp(palette)
        self.hex = ["%02x%02x%02x" % tuple(int(v) for v in row)
                    for row in self.rgb]
        self.t0 = time.monotonic()
        self.samples = 0
        self.frames = 0
        self._cells = {}
        self._built = None
        self._pts = self._nrm = None
        self._focal = self._depth = self._facing = 0.0

    @property
    def shape(self):
        if len(self.shapes) == 1:
            return self.shapes[0]
        i = int((time.monotonic() - self.t0) / self.dwell)
        return self.shapes[i % len(self.shapes)]

    # -------------------------------------------------------------- build

    def _build(self, cols, rows, shape):
        """Fit the shape to this area, then sample it finely enough for it.

        Both numbers come from the same place. A bounding sphere of radius
        `r` seen from `DIST * r` away projects, at its widest, to a disc of
        `focal * r / sqrt(dist^2 - r^2)` pixels across whatever way it is
        turned, because the silhouette is where the view ray grazes the
        sphere. Setting that equal to the short side of the buffer gives
        the focal length, and dividing it by the depth of the nearest point
        gives pixels per world unit where perspective magnifies most, which
        is what the sample spacing is really asking about.
        """
        width, height = cols, rows * SUBROWS
        tall = CELL_ASPECT / SUBROWS          # pixel height, in pixel widths
        # A cheap pass first, to learn how big the shape actually is and
        # what a sample density costs on it. Both scale exactly: a surface
        # sampled `k` times as finely has `k ** 2` the points.
        probe = 8.0
        pts, nrm = BUILD[shape](probe)
        radius = float(np.sqrt((pts * pts).sum(1)).max())
        depth = DIST * radius
        grazed = radius / math.sqrt(depth * depth - radius * radius)
        focal = FILL * min(width, height * tall) / (2 * grazed)
        # Sampled for the nearest point the surface can reach, where
        # perspective magnifies it most. Anywhere further away is
        # over-sampled and costs only points that land on a pixel already
        # taken. Sampled for any point further back, the part of the surface
        # facing the viewer comes out with holes in it.
        scale = focal / (depth - radius)

        def cost(step):
            return len(pts) * (scale / step / probe) ** 2

        # Past a certain area the surface wants more samples than a frame
        # has time for, and there are two things to give up, in this order.
        # Spacing goes first, up to the point where neighbouring samples
        # stop landing in neighbouring pixels and the surface starts
        # showing holes; that costs nothing a viewer can see. Only when
        # that is not enough does the picture itself get coarser, drawn
        # into a smaller buffer and blown up, which is honest about the
        # budget -- and a wall this size could not carry the bytes of a
        # full-resolution frame at any useful rate anyway.
        step = STEP if cost(STEP) <= MAX_SAMPLES else LOOSE
        shrink = max(1, math.ceil(math.sqrt(cost(step) / MAX_SAMPLES)))
        bw = -(-width // shrink)
        bh = -(-height // shrink)
        focal = FILL * min(bw, bh * tall) / (2 * grazed)
        pts, nrm = BUILD[shape](focal / (depth - radius) / step)

        self._pts, self._nrm = pts, nrm
        self._focal, self._depth, self._radius = focal, depth, radius
        self._shrink, self._bw, self._bh = shrink, bw, bh
        # A point is kept when its normal leans towards the camera by more
        # than the half-angle of the view cone. Anything less exact throws
        # away silhouettes, which is the one place the eye is looking.
        self._facing = radius / depth
        self._tall = tall
        self._qscale = 65535.0 / (2.0 * radius)
        self.samples = len(pts)
        self._built = (cols, rows, shape)

    # ------------------------------------------------------------- render

    def _light(self, t):
        """Key, highlight and bounce, as unit column vectors in world space.

        Three columns rather than three vectors because every one of them is
        about to be dotted with the same normals, and three dot products
        against one (3, 3) is one matrix multiply instead of three passes
        over the points.
        """
        a = t * self.ORBIT
        ca, sa = math.cos(a), math.sin(a)
        lx, ly, lz = -0.55, 0.62, -0.56
        key = np.array([lx * ca - ly * sa, lx * sa + ly * ca, lz], np.float32)
        key /= np.linalg.norm(key)
        # Blinn-Phong, with the camera taken as looking straight down the
        # axis. The error against a real per-point view vector is under the
        # width of the highlight itself.
        toward = np.array([0.0, 0.0, -1.0], np.float32)
        half = key + toward
        # The bounce comes from behind the key and pulled towards the camera,
        # so it fills the shadow side without lighting a second silhouette.
        fill = -key + 0.8 * toward
        return np.stack([key, half / np.linalg.norm(half),
                         fill / np.linalg.norm(fill)], axis=1)

    def _rotation(self, t):
        """Tilt about x, then spin about z: the motion the original had."""
        a, b = (t * s * self.spin for s in self.SPIN)
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        rx = np.array([[1, 0, 0], [0, ca, -sa], [0, sa, ca]], np.float32)
        rz = np.array([[cb, -sb, 0], [sb, cb, 0], [0, 0, 1]], np.float32)
        return rz @ rx

    def frame(self, cols, rows):
        if cols < 2 or rows < 2:
            return [[BLANK] * max(0, cols) for _ in range(max(0, rows))]
        shape = self.shape
        if self._built != (cols, rows, shape):
            self._build(cols, rows, shape)
        width, height = cols, rows * SUBROWS
        t = time.monotonic() - self.t0
        rot = self._rotation(t)

        # Rotating the lights backwards into model space costs nine
        # multiplies; rotating every normal forwards costs three per point.
        # The only normal component wanted in world space is z, for the
        # facing test and the rim, and that is one row of the matrix.
        nrm = self._nrm
        nz = nrm @ rot[2]
        keep = np.flatnonzero(nz < self._facing)
        if not len(keep):
            return [[BLANK] * cols for _ in range(rows)]
        nz = nz[keep]
        dots = nrm[keep] @ (rot.T @ self._light(t))
        np.maximum(dots, 0.0, out=dots)
        lam, spc, bnc = dots[:, 0], dots[:, 1], dots[:, 2]
        for _ in range(SHINE):             # x ** 32, as five squarings
            spc *= spc
        # Brightest where the surface turns away from the camera, which is
        # the silhouette: a lit edge is what separates the solid from a
        # black wall at the far end of a room.
        rim = 1.0 - nz * nz
        rim *= rim * rim
        shade = (AMBIENT + DIFFUSE * lam + SPECULAR * spc + BOUNCE * bnc
                 + RIM * rim)
        np.clip(shade, 0.0, 1.0, out=shade)

        bw, bh = self._bw, self._bh
        pts = self._pts[keep] @ rot.T
        zc = pts[:, 2] + self._depth
        inv = np.reciprocal(zc)
        sx = bw * 0.5 + (self._focal * inv) * pts[:, 0]
        sy = bh * 0.5 - (self._focal / self._tall * inv) * pts[:, 1]
        ix = sx.astype(np.int32)
        iy = sy.astype(np.int32)
        on = ((ix >= 0) & (ix < bw) & (iy >= 0) & (iy < bh))
        if not on.all():
            ix, iy, zc, shade = ix[on], iy[on], zc[on], shade[on]
            if not len(ix):
                return [[BLANK] * cols for _ in range(rows)]

        # Depth as a 16-bit integer that grows towards the camera, so a
        # stable sort of it is a radix sort and painting in that order
        # leaves the nearest point in every pixel.
        key = ((self._depth + self._radius - zc) * self._qscale)
        np.clip(key, 0, 65535, out=key)
        order = np.argsort(key.astype(np.uint16), kind="stable")
        buf = np.zeros(bh * bw, np.uint8)
        level = (shade * (LEVELS - 1)).astype(np.uint8) + 1
        buf[(iy * bw + ix)[order]] = level[order]

        pix = buf.reshape(bh, bw)
        if self._shrink > 1:
            s = self._shrink
            pix = np.repeat(np.repeat(pix, s, 0), s, 1)[:height, :width]
        top = pix[0::2].astype(np.int16)
        bot = pix[1::2].astype(np.int16)
        # A cell whose two pixels are within a level or two of each other is
        # sent as a space on one background instead of a half block on
        # itself: the same picture, one colour on the wire instead of two,
        # and most of a smoothly shaded interior qualifies. The pixels have
        # to be lit for it -- merging a lit pixel with an unlit one would
        # paint over the wall's own background.
        close = (abs(top - bot) <= FLAT) & (top > 0) & (bot > 0)
        mid = (top + bot) >> 1
        top = np.where(close, mid, top)
        bot = np.where(close, mid, bot)
        self.frames += 1
        return self._assemble((top.astype(np.int32) << 8) | bot)

    # -------------------------------------------------------------- cells

    def _glyph(self, key):
        """The cell for one pair of stacked pixels, made once and kept.

        Only the levels that a ramp and a light actually produce are ever
        asked for, and there are at most 65536 pairs of them, so this fills
        in over the first few seconds and never grows again. Sharing the
        instances also makes a front end's frame diff an identity compare.
        """
        top, bot = key >> 8, key & 255
        if top == bot:
            g = BLANK if top == 0 else Glyph(" ", None, self.hex[top], False)
        elif top == 0:
            # Nothing above: draw the lower pixel as ink and leave the
            # receiver's own background alone, which is both cheaper on the
            # wire and correct on a wall of any colour.
            g = Glyph("▄", self.hex[bot], None, False)
        elif bot == 0:
            g = Glyph("▀", self.hex[top], None, False)
        else:
            g = Glyph("▀", self.hex[top], self.hex[bot], False)
        self._cells[key] = g
        return g

    def _assemble(self, key):
        """A (rows, cols) array of pixel pairs to the grid of Glyph."""
        get = self._cells.get
        miss = self._glyph
        out = []
        for krow in key.tolist():
            row = []
            add = row.append
            for k in krow:
                g = get(k)
                add(g if g is not None else miss(k))
            out.append(row)
        return out
