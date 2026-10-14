"""A frame held as arrays, for a source whose picture is already numpy's.

A source that draws pixels holds its frame as arrays long before it holds
cells: a film is a decoded framebuffer, an LED panel is a strip of RGB.
Turning that into a grid of cell tuples costs a python object per cell, and
diffing and serialising the grid costs a python step per cell again, which
at 384x98 is 37,000 of each every frame -- the whole of the frame budget.

So a source may offer `arrays(cols, rows)` beside `cells(cols, rows)`, and
the wall asks for that instead. It returns

    (ch, fg, bg)  or  (ch, fg, bg, attrs)

each a (rows, cols) array:

    ch      the character as a code point, 0 for the second half of a
            double-width glyph; or a numpy unicode array of single
            characters, which is the same thing viewed differently
    fg, bg  a truecolour 0xRRGGBB, or -1 for the terminal's own colour
    attrs   optional, a bitmask of BOLD, ITALICS, UNDERSCORE, STRIKE,
            REVERSE and FAINT

or None when nothing has changed, exactly as `cells` may. It may also
return a grid of cells, which then goes the way `cells` would have: a
source that is mostly pixels can still put up a sentence without building
it as arrays. Palette colours and names are what the cell format is for,
so they are not spelled here.

What leaves is the same bytes the grid would have produced. The diff is
one comparison over the whole frame, the runs are found from where the
changed cells are, and the escapes are assembled from tables by index, so
nothing here visits a cell from python.

numpy is imported by this module alone, and this module only when a
source offers arrays, so everything else in bitwall runs without it.
"""
import numpy as np

from . import stream
from . import MAX_COLS, MAX_ROWS
from .cells import BLANK

BOLD, ITALICS, UNDERSCORE, STRIKE, REVERSE, FAINT = 1, 2, 4, 8, 16, 32
DEFAULT = -1

#: The attribute bits in the order `stream._style` writes their SGR
#: parameters, which is what makes the two paths the same bytes.
_ATTR_PARAMS = ((BOLD, "1"), (FAINT, "2"), (ITALICS, "3"), (UNDERSCORE, "4"),
                (STRIKE, "9"), (REVERSE, "7"))


def _attr_text(bits):
    return "".join(";" + p for bit, p in _ATTR_PARAMS if bits & bit)


def _table(strings):
    a = np.empty(len(strings), object)
    a[:] = strings
    return a


# Every piece a truecolour escape is assembled from. A look is six of
# these, and an index into one is the whole cost of a piece. Each table
# ends in an empty string, which is what a colour that is the terminal's
# own indexes instead of its numbers.
_N = [str(v) for v in range(256)]
_DEC_SEMI = _table([n + ";" for n in _N] + [""])
_DEC_M = _table([n + "m" for n in _N] + [""])
#: The opening of a look, by attribute bits and whether the foreground is
#: the terminal's own: `ESC[0;1;38;2;` or `ESC[0;1;39;`.
_OPEN = _table([("\033[0" + _attr_text(bits)
                 + (";39;" if default else ";38;2;"))
                for bits in range(64) for default in (False, True)])
_BG_OPEN = _table(["48;2;", "49m"])
_NONE = 1 << 16
_PAIRS = None


def _pairs():
    """`r;g;` for every red and green, made once: two numbers in one
    piece, and the last entry empty for the terminal's own colour."""
    global _PAIRS
    if _PAIRS is None:
        _PAIRS = _table([a + b for a in _DEC_SEMI[:256] for b in
                         _DEC_SEMI[:256]] + [""])
    return _PAIRS


#: The cursor move that opens a run, in two halves so neither needs
#: formatting: `ESC[row;` and `colH`.
_ROW = _table(["\033[%d;" % (y + 1) for y in range(MAX_ROWS)])
_COL = _table(["%dH" % (x + 1) for x in range(MAX_COLS)])

_CHARS = None


def _chars():
    """The basic plane as strings, indexed by code point, made once."""
    global _CHARS
    if _CHARS is None:
        _CHARS = _table([""] + [chr(c) for c in range(1, 0x10000)])
    return _CHARS


class Frame:
    """One frame's arrays, placed in the area it is being drawn in.

    Compared like a grid, so the render loop's `grid != prev` asks the
    same question of either, and it answers once for the whole frame.
    """

    __slots__ = ("ch", "fg", "bg", "attrs", "cols", "rows")

    def __init__(self, ch, fg, bg, attrs, cols, rows):
        self.ch, self.fg, self.bg, self.attrs = ch, fg, bg, attrs
        self.cols, self.rows = cols, rows

    @classmethod
    def fit(cls, got, cols, rows):
        """A source's arrays, clipped or padded to cols x rows.

        Always a copy. A source is free to keep drawing into the arrays it
        handed over, and this frame is what the next one is diffed
        against, so it cannot share them.
        """
        ch, fg, bg = got[0], got[1], got[2]
        attrs = got[3] if len(got) > 3 else None
        ch = np.asarray(ch)
        if ch.dtype.kind == "U":
            ch = ch.astype("<U1").view(np.int32).reshape(ch.shape)
        out_ch = np.full((rows, cols), ord(BLANK[0]), np.int32)
        out_fg = np.full((rows, cols), DEFAULT, np.int32)
        out_bg = np.full((rows, cols), DEFAULT, np.int32)
        out_at = np.zeros((rows, cols), np.uint8)
        h, w = min(rows, ch.shape[0]), min(cols, ch.shape[1])
        out_ch[:h, :w] = ch[:h, :w]
        out_fg[:h, :w] = np.asarray(fg)[:h, :w]
        out_bg[:h, :w] = np.asarray(bg)[:h, :w]
        if attrs is not None:
            out_at[:h, :w] = np.asarray(attrs)[:h, :w]
        return cls(out_ch, out_fg, out_bg, out_at, cols, rows)

    def __eq__(self, other):
        if not isinstance(other, Frame):
            return NotImplemented
        return (self.ch.shape == other.ch.shape
                and np.array_equal(self.ch, other.ch)
                and np.array_equal(self.fg, other.fg)
                and np.array_equal(self.bg, other.bg)
                and np.array_equal(self.attrs, other.attrs))

    __hash__ = None

    def cells(self):
        """The same frame as a grid of cells.

        For the rare frame that has to meet a grid -- a source that
        answered with cells last time -- and for holding the two paths to
        each other.
        """
        def colour(v):
            return "default" if v < 0 else "%06x" % v
        out = []
        for chs, fgs, bgs, ats in zip(self.ch.tolist(), self.fg.tolist(),
                                      self.bg.tolist(), self.attrs.tolist()):
            out.append([(chr(c) if c else "", colour(f), colour(b),
                         bool(a & BOLD), bool(a & ITALICS),
                         bool(a & UNDERSCORE), bool(a & STRIKE),
                         bool(a & REVERSE)) + ((True,) if a & FAINT else ())
                        for c, f, b, a in zip(chs, fgs, bgs, ats)])
        return out

    def serialise(self, prev, full, depth="true"):
        """What `stream.serialise` would write for this frame, in bulk."""
        if prev is not None and not isinstance(prev, Frame):
            return stream.serialise(self.cells(), prev, full, self.cols,
                                    self.rows, depth)
        cols = self.cols
        ch, fg = self.ch.ravel(), self.fg.ravel()
        bg, at = self.bg.ravel(), self.attrs.ravel()
        head = "\033[0m" if full else ""
        if full or prev is None or prev.ch.shape != self.ch.shape:
            idx = np.arange(ch.size)
        else:
            idx = np.flatnonzero((ch != prev.ch.ravel())
                                 | (fg != prev.fg.ravel())
                                 | (bg != prev.bg.ravel())
                                 | (at != prev.attrs.ravel()))
        if not idx.size:
            return head + head

        # Runs. Changed cells join one run when no more than GAP unchanged
        # cells lie between them on the same row, which is the rule the
        # grid path applies probing forward from each changed cell.
        y = idx // cols
        opens = np.ones(idx.size, bool)
        opens[1:] = (np.diff(idx) > stream.GAP + 1) | (y[1:] != y[:-1])
        first = np.flatnonzero(opens)
        last = np.append(first[1:] - 1, idx.size - 1)
        start, length = idx[first], idx[last] + 1 - idx[first]
        lead = np.cumsum(length) - length          # each run's first cell
        pos = np.repeat(start - lead, length) + np.arange(length.sum())
        runs = np.zeros(pos.size, bool)
        runs[lead] = True

        # A cell drawn is one with a character; the second half of a wide
        # glyph is covered by the first and is skipped, as a grid's is.
        c = ch[pos]
        drawn = c != 0
        look = ((at[pos].astype(np.int64) << 50)
                | ((fg[pos].astype(np.int64) + 1) << 25)
                | (bg[pos].astype(np.int64) + 1))
        # The look carries on across runs, so an escape is needed where a
        # drawn cell differs from the last drawn cell before it.
        dl = look[drawn]
        new = np.ones(dl.size, bool)
        new[1:] = dl[1:] != dl[:-1]
        sgr = np.zeros(pos.size, bool)
        sgr[np.flatnonzero(drawn)[new]] = True

        pieces = self._looks(pos[sgr], fg, bg, at, depth)
        k = pieces.shape[1]

        count = 2 * runs + k * sgr + drawn
        off = np.cumsum(count) - count
        out = np.empty(int(count.sum()), object)
        ry, rx = divmod(pos[runs], cols)
        out[off[runs]] = _ROW[ry]
        out[off[runs] + 1] = _COL[rx]
        so = off[sgr] + 2 * runs[sgr]
        for j in range(k):
            out[so + j] = pieces[:, j]
        co = (off + 2 * runs + k * sgr)[drawn]
        c = c[drawn]
        wide = c >= 0x10000
        if wide.any():
            text = np.empty(c.size, object)
            text[~wide] = _chars()[c[~wide]]
            text[wide] = [chr(v) for v in c[wide].tolist()]
            out[co] = text
        else:
            out[co] = _chars()[c]
        return head + "".join(out.tolist()) + "\033[0m"

    @staticmethod
    def _looks(at_cells, fg, bg, at, depth):
        """The escape for each cell that needs one, as rows of pieces."""
        f, b, a = fg[at_cells], bg[at_cells], at[at_cells].astype(np.int64)
        if depth == "true" or depth not in stream.DEPTHS:
            p = np.empty((f.size, 6), object)
            fd, bd = f < 0, b < 0
            pairs = _pairs()
            p[:, 0] = _OPEN[a * 2 + fd]
            p[:, 1] = pairs[np.where(fd, _NONE, f >> 8)]
            p[:, 2] = _DEC_SEMI[np.where(fd, 256, f & 255)]
            p[:, 3] = _BG_OPEN[bd.astype(np.intp)]
            p[:, 4] = pairs[np.where(bd, _NONE, b >> 8)]
            p[:, 5] = _DEC_M[np.where(bd, 256, b & 255)]
            return p
        # A reduced screen: each distinct colour is reduced once, through
        # the same function the grid path uses, and indexed back out.
        p = np.empty((f.size, 3), object)
        p[:, 0] = _table(["\033[0" + _attr_text(bits) + ";"
                          for bits in range(64)])[a]
        for col, values, base, end in ((1, f, 30, ";"), (2, b, 40, "m")):
            uniq, inv = np.unique(values, return_inverse=True)
            p[:, col] = _table([
                stream._colour_text("default" if v < 0 else "%06x" % v,
                                    base, depth) + end
                for v in uniq.tolist()])[inv.ravel()]
        return p
