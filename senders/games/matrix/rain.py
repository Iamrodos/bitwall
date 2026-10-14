"""The falling glyph rain, as a grid of coloured characters.

The core owns the rain and hands back a grid of `Glyph` -- a character with
a truecolour foreground. It knows nothing about bitwall and nothing about
terminals, so the same rain drives a wall through `main.py` and a terminal
through `pane-matrix`.

**A trail is a gradient, not three shades.** Every cell carries its own
'rrggbb', so a stream is a white-hot head, a green body and a tail that
dies out over a dozen cells or more. That continuous falloff is the whole
look; a terminal palette cannot do it and that is why cmatrix does not look
like the film.

**A column is a stream, and streams differ.** Speed, trail length, when it
starts and where it stops are all drawn per stream, and a stream rests for a
while before the column is used again. Rain reads as rain because no two
columns are in step; a grid of identical columns reads as a screensaver.

**Glyphs change where they sit.** In the film a character in a trail keeps
mutating after it has fallen past. Each column swaps a character out of the
bright half of its trail a few times a second, which is most of the life in
it, and biased bright because a mutation nobody can see costs the same to
send as one they can.

**What it costs to send.** bitwall ships only cells that changed, so the
bill is set by how many cells a stream disturbs, not by the frame rate. A
cell's colour comes from its *whole-cell* distance behind the head, and
those distances are quantised to `SHADES` steps, so a stream spends nothing
at all on the frames between two rows and at most `SHADES` cells when it
crosses one -- however long its trail is, and whether the wall is running at
ten frames a second or thirty.
"""
import random
import time
from collections import namedtuple

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

#: Halfwidth katakana: the film's alphabet, and one advance width each, so
#: a glyph occupies the one cell the grid has given it. Anything from the
#: fullwidth block would take two and shear the column.
KATAKANA = tuple(chr(c) for c in range(0xFF66, 0xFF9E))
DIGITS = tuple("0123456789")
LATIN = tuple("ZKMVXY=+*<>:")

#: Katakana carries the look; digits and a few Latin shapes are the salt.
#: Weighting is by repetition so a draw is one `choice` on a flat tuple.
GLYPHS = KATAKANA * 6 + DIGITS * 2 + LATIN

#: An ASCII alphabet for a receiver whose font has no katakana in it. The
#: same silhouette -- narrow, angular, no descenders to speak of.
ASCII_GLYPHS = tuple("0123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
                     "<>/\\|=+*-:;!?#$%&@^~") * 1

#: The gradient a trail is cut from, as (brightness, r, g, b) stops.
#: Brightness 1 is the head: white with just enough green in it to belong
#: to the stream. It falls through the phosphor green the body sits at and
#: goes out well before the end of the trail, which is what gives a stream
#: a soft edge rather than a stop.
RAMP = ((1.00, 218, 255, 226),
        (0.86, 130, 255, 168),
        (0.62, 46, 238, 104),
        (0.34, 16, 150, 58),
        (0.14, 6, 74, 28),
        (0.00, 0, 0, 0))

#: Distinct steps a trail is drawn in. The cap on what a stream costs when
#: it crosses a row: a 30-cell trail and a 12-cell trail both disturb at
#: most this many cells, because the far half of a long trail quantises
#: into bands the eye cannot separate anyway.
SHADES = 14

#: How fast the gradient falls away behind the head. Above 1 the bright
#: core is short and the dim tail is long, which is the film's shape.
FALLOFF = 1.25


def _shade(v):
    """The colour at brightness `v`, as 'rrggbb'."""
    for i in range(len(RAMP) - 1):
        hi, hr, hg, hb = RAMP[i]
        lo, lr, lg, lb = RAMP[i + 1]
        if v >= lo:
            f = (v - lo) / (hi - lo)
            return "%02x%02x%02x" % (int(lr + (hr - lr) * f),
                                     int(lg + (hg - lg) * f),
                                     int(lb + (hb - lb) * f))
    return "000000"


#: Level 0 is dark enough to be nothing. A cell there is sent as a blank
#: rather than as a near-black glyph: it is the cheapest cell on the wire
#: and it is what the eye sees anyway.
SHADE_HEX = tuple(_shade(i / SHADES) for i in range(SHADES + 1))

#: The rows a stream's speed is quoted against. A stream crossing a fifty
#: row wall and one crossing a twelve row preview box at the same rows a
#: second are not the same rain: the small one is a blur. Speeds are scaled
#: by the root of the grid's height so a stream takes a comparable time to
#: fall whatever it is falling down, and clamped so a very tall wall does
#: not turn into a firehose.
NOMINAL_ROWS = 34.0
SPEED_SCALE = (0.55, 1.2)

_LEVELS = {}


def _levels(trail):
    """Shade level for each whole-cell distance behind a head.

    Cached per trail length: a stream asks for this on every frame and the
    answer only depends on how long its trail is.
    """
    got = _LEVELS.get(trail)
    if got is None:
        got = tuple(min(SHADES,
                        int(((1.0 - d / (trail + 1.0)) ** FALLOFF) * SHADES))
                    for d in range(trail + 1))
        _LEVELS[trail] = got
    return got


class Stream:
    """One column of rain, and the run it is currently making.

    The head's position is computed from the clock rather than accumulated
    per frame, so a stream falls at the speed it was given whatever rate
    the wall is running at, and a dropped frame costs nothing.
    """

    __slots__ = ("x", "rows", "rng", "cfg", "scale", "speed", "trail",
                 "stop", "t0", "chars", "levels", "at", "flick")

    def __init__(self, x, rows, rng, cfg, scale):
        self.x = x
        self.rows = rows
        self.rng = rng
        self.cfg = cfg
        self.scale = scale
        self.chars = []
        self.start(time.monotonic(), staggered=True)

    def start(self, now, staggered=False):
        """Draw a fresh run: how fast, how long, how far, and when.

        `staggered` starts the run at a random point in its own life, which
        is how the wall comes up already raining instead of with every
        column released from the top at once.
        """
        rng, cfg = self.rng, self.cfg
        rows = self.rows
        self.speed = rng.uniform(*cfg.speed) * self.scale
        lo, hi = cfg.trail
        self.trail = max(3, int(rng.triangular(lo * rows, hi * rows,
                                               (lo + hi) * 0.45 * rows)))
        # Most streams run off the bottom; some give out on the way down,
        # which is what stops the lower half of the wall being a solid
        # band of green.
        self.stop = (rng.randint(rows // 4, rows - 1)
                     if rng.random() < cfg.short else rows + self.trail)
        self.levels = _levels(self.trail)
        self.chars = [rng.choice(cfg.glyphs) for _ in range(rows)]
        # A run lasts this long, so resting for a share of it is how the
        # density knob turns into a number of streams on the wall.
        life = (self.stop + self.trail) / self.speed
        rest = rng.expovariate(1.0) * life * (1.0 - cfg.density) / cfg.density
        self.t0 = now + rest
        if staggered:
            self.t0 = now - rng.uniform(0.0, life + rest)
        self.at = -1
        self.flick = now + (rng.expovariate(cfg.flicker)
                            if cfg.flicker > 0 else 0.0)

    def draw(self, now, grid):
        """Paint this stream's cells into `grid`, or start the next run."""
        head = int((now - self.t0) * self.speed)
        if head < 0:
            return                                  # resting between runs
        top = head - self.trail
        if top > self.stop:
            self.start(now)
            return

        rng, chars = self.rng, self.chars
        # Every row the head has crossed since the last frame takes a new
        # character, so a fast stream does not lay down a repeat.
        if head != self.at:
            for y in range(max(self.at + 1, head - self.trail), head + 1):
                if 0 <= y < self.rows:
                    chars[y] = rng.choice(self.cfg.glyphs)
            self.at = head

        rate = self.cfg.flicker
        if rate > 0:
            # A run that has just come out of a rest is not owed the swaps
            # it slept through, and catching up on a minute of them in one
            # frame would be a burst of cells nobody asked for.
            if self.flick < now - 1.0:
                self.flick = now
            while now >= self.flick:
                # Bright half only: a mutation in the dark tail costs a
                # cell on the wire and shows nobody anything.
                y = head - rng.randrange(0, max(1, self.trail // 2) + 1)
                if 0 <= y < self.rows:
                    chars[y] = rng.choice(self.cfg.glyphs)
                self.flick += rng.expovariate(rate)

        x, levels = self.x, self.levels
        lowest = min(head, self.stop, self.rows - 1)
        for y in range(max(0, top + 1), lowest + 1):
            lvl = levels[head - y]
            if lvl:
                grid[y][x] = Glyph(chars[y], SHADE_HEX[lvl], None,
                                   lvl == SHADES)


class Config:
    """What the rain looks like, as the few numbers that change it."""

    __slots__ = ("speed", "trail", "short", "density", "flicker", "glyphs")

    def __init__(self, speed=(2.5, 9.0), trail=(0.18, 0.62), short=0.2,
                 density=0.6, flicker=5.0, glyphs=GLYPHS):
        #: Rows a second, drawn per stream.
        self.speed = speed
        #: Trail length, as a share of the rows on the wall, so the rain
        #: keeps its proportions on a preview box and on a whole wall.
        self.trail = trail
        #: Chance a stream gives out before the bottom.
        self.short = short
        #: Roughly the share of columns raining at any moment.
        self.density = density
        #: Glyph swaps a second, per stream.
        self.flicker = flicker
        self.glyphs = glyphs


class Rain:
    """Falling glyph rain sized to whatever grid it is handed.

        rain = Rain()
        grid = rain.frame(cols, rows)       # rows of Glyph

    `frame` is the whole interface, and it is the same shape as bitwall's
    source contract. A new area is a new set of streams; nothing is cached
    across one, because trail length is a share of the rows and a stream
    that outlived the grid would be the wrong length in it.
    """

    def __init__(self, cfg=None, seed=None):
        self.cfg = cfg or Config()
        self.rng = random.Random(seed)
        self.area = (0, 0)
        self.streams = []

    def resize(self, cols, rows):
        lo, hi = SPEED_SCALE
        scale = min(hi, max(lo, (rows / NOMINAL_ROWS) ** 0.5))
        self.area = (cols, rows)
        self.streams = [Stream(x, rows, self.rng, self.cfg, scale)
                        for x in range(cols)]

    def frame(self, cols, rows):
        if (cols, rows) != self.area:
            self.resize(cols, rows)
        now = time.monotonic()
        grid = [[BLANK] * cols for _ in range(rows)]
        for s in self.streams:
            s.draw(now, grid)
        return grid
