"""Video files drawn on a character grid, several pixels to a cell.

The core of the video sender. It hands back a grid of `Glyph` -- a character
with truecolor `'rrggbb'` colours -- and knows nothing about bitwall and
nothing about terminals, so the same player drives a wall through `main.py`
and a terminal through `pane-video`.

**A cell is a small tile of pixels, and a mode decides how small.** A wall is
about 174x42 cells, so the picture that reaches it is a tenth of an iPod
screen and every pixel is dear. Each mode trades colour against resolution:

    half     1x2 per cell, two colours     the default
    quad     2x2 per cell, two colours     detail, for high-contrast material
    braille  2x4 per cell, two colours     the finest grid there is
    ascii    1x1 per cell, one colour      a look rather than a picture
    fine     2x4 per cell, two colours     the same grid, searched properly

**How two colours are chosen.** Given a tile and a decision about which of
its pixels are foreground, the least-squares answer is the mean of each
group, so the only real question is where the split falls. `quad` and
`braille` split at the tile's mean brightness, which is one pass and right
often enough. `fine` sorts the tile by brightness and scores every split by
the squared error it leaves against the source, which is what `chafa` does;
prefix sums make each candidate a handful of multiplies, so the search is
seven of those rather than eight passes over the tile. Whichever split wins,
the coverage mask picks the character: a block or quadrant glyph where one
covers the mask exactly, and the braille cell for that mask otherwise,
because braille spans all 256 masks a 2x4 tile can produce.

**The pixels are numpy's, and so is the frame.** A frame is a hundred
thousand samples and every mode's arithmetic -- luminance, the split, the
group means, the mask -- is the same operation over all of them, which is
one array expression each. The modes all end at the same three arrays -- a
character index, a foreground and a background -- packed into one. `arrays`
hands those over as they are, for a front end that can take a frame
whole; `frame` assembles them into a grid of Glyph, one cached lookup per
cell, for one that wants the grid.

**ffmpeg does the pixels.** Decoding, sharpening, scaling, the
aspect-preserving pad, the contrast lift and the frame-rate conversion are
all one `-vf` chain away, and what comes back on stdout is exactly the
framebuffer the mode asked for. At this size the filter chain matters more
than anything downstream of it: lanczos rather than the default scaler, a
touch of unsharp before the scale so edges survive it, and a gamma, contrast
and saturation lift afterwards because detail is what the downscale takes
and punch is what is left to give.

**A frame ahead of time.** ffmpeg decodes far faster than a film plays, so a
reader thread keeps a few frames queued and `frame()` never waits on a
decoder. A resize restarts ffmpeg seeked to where the film had got to, so
the picture resumes rather than starting over.

**Truecolor, not the 256 palette.** The cell format takes a colour as a hex
string and both receivers handle 24-bit, so colours go through unquantised.
Nothing here dithers, bands or approximates.
"""
import os
import queue
import random
import shutil
import subprocess
import threading
import time
from collections import namedtuple

import numpy as np

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)

MODES = ("half", "quad", "braille", "ascii", "fine")

#: pixels per cell, (across, down), for each mode
TILE = {"half": (1, 2), "quad": (2, 2), "braille": (2, 4),
        "ascii": (1, 1), "fine": (2, 4)}

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm",
             ".mpg", ".mpeg", ".wmv", ".flv", ".ts")

ASCII_RAMP = " .:-=+*#%@"

# Rec.601 luma in eighths of a byte, so brightness is integer arithmetic.
LUMA = np.array([77, 151, 28], np.int32)


def find_ffmpeg():
    """ffmpeg wherever this machine keeps it."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
              "/usr/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    raise SystemExit("ffmpeg not found on PATH")


def playlist(paths, shuffle=False):
    """Files to play, with any directory expanded into the videos in it."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for n in sorted(names):
                    if n.lower().endswith(VIDEO_EXT) and not n.startswith("."):
                        out.append(os.path.join(root, n))
        elif os.path.exists(p):
            out.append(p)
        else:
            raise SystemExit("no such file: %s" % p)
    if not out:
        raise SystemExit("no video files in %s" % ", ".join(paths))
    if shuffle:
        random.shuffle(out)
    return out


# ------------------------------------------------------------------ glyphs

# Every character any mode can draw, gathered into one table so a mode's
# output is an index into it and the assembly pass below is mode-agnostic.
GLYPHS = [" "]
_INDEX = {" ": 0}


def _g(ch):
    i = _INDEX.get(ch)
    if i is None:
        i = _INDEX[ch] = len(GLYPHS)
        GLYPHS.append(ch)
    return i


# Quadrant glyphs over a 2x2 tile, indexed by a mask of (TL, TR, BL, BR).
QUAD_GLYPH = (" ", "▘", "▝", "▀", "▖", "▌", "▞", "▛",
              "▗", "▚", "▐", "▜", "▄", "▙", "▟", "█")

# Bit b of a 2x4 tile mask is the pixel at row b >> 1, column b & 1, which is
# the order the tiles come out of the reshape below.
_QUAD_BITS = tuple(
    (1 << (top * 2 + col)) | (1 << ((top + 1) * 2 + col))
    for col, top in ((0, 0), (1, 0), (0, 2), (1, 2)))     # TL, TR, BL, BR


def _quad_at_2x4(m):
    out = 0
    for q in range(4):
        if m & (1 << q):
            out |= _QUAD_BITS[q]
    return out


_BLOCK_FOR_MASK = {_quad_at_2x4(m): QUAD_GLYPH[m] for m in range(16)}

# Braille spans every 2x4 mask, which is what makes the fine mode's search
# unconstrained: whatever split wins, some character carries it.
_DOT = (0, 3, 1, 4, 2, 5, 6, 7)     # tile bit -> braille dot bit


def _braille(mask):
    out = 0
    for b in range(8):
        if mask & (1 << b):
            out |= 1 << _DOT[b]
    return chr(0x2800 + out)


BRAILLE = tuple(_braille(m) for m in range(256))

HALF_IX = _g("▀")
QUAD_IX = np.array([_g(c) for c in QUAD_GLYPH], np.int64)
BRAILLE_IX = np.array([_g(c) for c in BRAILLE], np.int64)
# Solid where a solid glyph fits the mask exactly, dots everywhere else.
FINE_IX = np.array([_g(_BLOCK_FOR_MASK.get(m, BRAILLE[m]))
                    for m in range(256)], np.int64)
ASCII_IX = np.array([_g(c) for c in ASCII_RAMP], np.int64)
#: Each character index as its code point, for handing a frame over whole.
POINTS = np.array([ord(c) for c in GLYPHS], np.int32)

# A cell is packed into one integer -- character, foreground, background --
# so the assembly pass is a single dict lookup per cell. Colour 0 is the
# terminal's own, and a real colour is its 24-bit value plus one.
_CH = 50
_FG = 25
_MASK25 = (1 << 25) - 1


class _Cells(dict):
    """Glyphs by packed key, made once and shared.

    Sharing the instances also makes the front ends' frame diff an identity
    comparison. The cap is there because a long film does eventually walk a
    large part of the cube.
    """

    def __missing__(self, key):
        if len(self) > 400000:
            self.clear()
        fg = (key >> _FG) & _MASK25
        bg = key & _MASK25
        g = Glyph(GLYPHS[key >> _CH],
                  None if fg == 0 else "%06x" % (fg - 1),
                  None if bg == 0 else "%06x" % (bg - 1),
                  False)
        self[key] = g
        return g


CELLS = _Cells()


def _assemble(key):
    """A (rows, cols) array of packed cells to the grid of Glyph."""
    cells = CELLS
    get = cells.get
    miss = cells.__missing__
    out = []
    for krow in key.tolist():
        row = []
        add = row.append
        for k in krow:
            g = get(k)
            add(g if g is not None else miss(k))
        out.append(row)
    return out


def _unpack(key):
    """Packed cells as (code point, foreground, background) arrays, a
    colour being 0xRRGGBB and -1 for the terminal's own."""
    ch = POINTS[key >> _CH]
    fg = (((key >> _FG) & _MASK25) - 1).astype(np.int32)
    bg = ((key & _MASK25) - 1).astype(np.int32)
    return ch, fg, bg


def _code(rgb):
    """(..., 3) of uint8 to the packed colour the key wants, plus one."""
    r = rgb[..., 0].astype(np.int64)
    return (r << 16) + (rgb[..., 1].astype(np.int64) << 8) + rgb[..., 2] + 1


# ------------------------------------------------------------------ player

class Player:
    """A playlist of video files, looping, painted onto whatever area it gets.

    `frame(cols, rows)` is the whole public surface: an area in, a grid of
    Glyph out. `arrays(cols, rows)` is the same picture as arrays. Before
    the first frame, and when something has gone wrong, what there is to
    show is a sentence rather than a picture, and both give that as a grid
    of Glyph. Frames are paced to `fps`; a call before the next one is due
    gets the last one back, because a receiver asking faster than the film
    plays would only re-send a picture that had not changed.
    """

    QUEUE = 6           # frames read ahead, so a decode hiccup is invisible
    FLAT = 10           # brightness spread below which a tile is one colour

    def __init__(self, paths, fps=20, mode="half", punch=1.0, shuffle=False,
                 start=0.0, cell_ratio=2.0):
        if mode not in MODES:
            raise SystemExit("unknown mode %s (%s)" % (mode, "|".join(MODES)))
        self.files = playlist(paths, shuffle)
        self.fps = fps
        self.mode = mode
        self.punch = punch
        self.ffmpeg = find_ffmpeg()
        self.pack = getattr(self, "_pack_" + mode)
        self.tile = TILE[mode]
        #: How much taller than wide a cell is on the glass. A mode's
        #: tile is square only while the cell it fills has the same
        #: proportions: `half` is 1x2 and `fine` 2x4, and both want a
        #: 2:1 cell. A receiver rounds its cell to whole pixels, so a
        #: small font drifts off that -- 6x15 rather than 6x12 -- and a
        #: picture drawn as though the framebuffer pixels were square
        #: comes out stretched by the difference.
        self.cell_ratio = cell_ratio
        #: What the bars either side of the picture are filled with. A
        #: work keeps its own proportions, so unless it matches the wall
        #: exactly there is padding, and padding a different black from
        #: the wall around it draws a rectangle nobody asked for.
        self.ground = "black"

        self.index = 0
        #: Where each film is entered. Openings are logos and dark credits,
        #: which is the worst case for a small framebuffer.
        #: Where to come in. A number, or something to call for one --
        #: which is how a rotation gets a different point in every film
        #: rather than the same one each time.
        self.start = start
        self._entry = self._pick_start()
        self.seek = 0.0          # where in the file this ffmpeg was started
        self.played = 0          # frames out of this ffmpeg
        self.loops = 0
        self.frames = 0
        self.cols = self.rows = 0
        self.nbytes = 0
        self.proc = None
        self.q = None
        self.gen = 0             # bumped to retire a reader thread
        self.error = None        # ffmpeg's last complaint, if any
        #: The last frame, as packed cells or, when it is a sentence, as a
        #: grid of Glyph; and the same frame in the other two forms, made
        #: when first asked for.
        self._grid = None
        self._glyphs = self._unpacked = None
        self.next_frame = 0.0

    @property
    def path(self):
        return self.files[self.index]

    @property
    def position(self):
        """Seconds into the current file."""
        return self.seek + self.played / float(self.fps)

    # ------------------------------------------------------------ ffmpeg

    def _filters(self, w, h):
        """Sharpen and lift, then scale and pad, and hand back rgb24.

        Everything that shapes the picture happens before the scale, because
        it is the scale that throws detail away: an edge has to still be
        there when the scaler averages over it, and a shadow lifted first
        keeps some of itself through the average that would otherwise take
        it. Gamma is in with contrast because a dark scene shown this small
        goes to mud, and contrast on its own only makes it darker. `punch`
        drives all three, and zero leaves the picture alone but for the
        scale.

        The tail pins the format. `eq` works in planar YUV, so leaving it
        after the pad has ffmpeg insert a converter there, and chroma
        subsampling rounds an odd width up -- at which point the pad is
        narrower than its own input and the graph fails to rebuild. An odd
        number of columns is an ordinary thing for a receiver to report.
        """
        p = self.punch
        chain = []
        if abs(self.squash - 1.0) > 0.01:
            # Before everything else: pre-distorting by the inverse of
            # the framebuffer's pixel shape means the aspect-preserving
            # fit below lands the picture on the glass in its own
            # proportions rather than the cell's.
            chain.append("scale=iw:ih*%.5f" % self.squash)
        if p > 0:
            chain.append("unsharp=5:5:%.2f:3:3:0.0" % (0.9 * p))
            chain.append("eq=contrast=%.3f:saturation=%.3f:gamma=%.3f"
                         % (1.0 + 0.14 * p, 1.0 + 0.30 * p, 1.0 + 0.10 * p))
        # Snapped to whole cells as well as fitted. Rounding the pad
        # offset is only half of it: a picture an odd number of pixels
        # wide has one aligned edge and one that still falls inside a
        # cell, and that cell is drawn as a blend of picture and bar.
        # `force_divisible_by` makes the scaler choose a size that
        # cannot do that, at the cost of up to a tile of the picture.
        tw, th = self.tile
        chain.append("scale=%d:%d:flags=lanczos:"
                     "force_original_aspect_ratio=decrease:"
                     "force_divisible_by=%d" % (w, h, max(tw, th)))
        # Centred, but only ever by a whole cell. A tile is two pixels
        # across and four down, so an offset of `(ow-iw)/2` can land the
        # picture's edge in the middle of a cell -- and that cell is then
        # drawn as a blend of picture and padding, which reads as a
        # coloured line down the outside of the frame. Rounding the
        # offset down to a whole tile costs at most a pixel of centring
        # and puts every edge on a boundary.
        chain.append("pad=%d:%d:trunc((ow-iw)/2/%d)*%d:"
                     "trunc((oh-ih)/2/%d)*%d:color=%s"
                     % (w, h, tw, tw, th, th, self.ground))
        chain.append("format=rgb24")
        return ",".join(chain)

    @property
    def squash(self):
        """What to multiply the source height by before fitting it."""
        tw, th = self.tile
        return th / (tw * self.cell_ratio)

    def _start(self, cols, rows, seek=0.0):
        self._stop()
        self.cols, self.rows = cols, rows
        tw, th = self.tile
        w, h = cols * tw, rows * th
        self.seek = seek
        self.played = 0
        self.error = None
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if seek > 0.5:
            cmd += ["-ss", "%.3f" % seek]
        # Decode at the film's own speed. Left to run flat out it fills the
        # queue with frames from the future, and a reader taking the newest
        # one races through them -- the film fast-forwards. Paced, the queue
        # only ever holds what has actually happened, so taking the newest
        # drops what we were too slow to show and keeps the clock honest.
        cmd += ["-re"]
        cmd += ["-i", self.path, "-vf", self._filters(w, h),
                "-r", str(self.fps), "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        self.nbytes = w * h * 3
        self.q = queue.Queue(maxsize=self.QUEUE)
        self.gen += 1
        threading.Thread(target=self._read_ahead, daemon=True,
                         args=(self.proc, self.q, self.nbytes,
                               self.gen)).start()
        threading.Thread(target=self._read_errors, daemon=True,
                         args=(self.proc, self.gen)).start()

    def _read_errors(self, proc, gen):
        """ffmpeg's complaints, kept for whoever asks why there is no picture.

        A filter graph that will not build says so once on stderr and then
        exits, and a decoder whose stderr nobody reads is a decoder that
        fails invisibly. This is the difference between a black rectangle
        and a sentence naming what went wrong.
        """
        try:
            for line in proc.stderr:
                if gen != self.gen:
                    return
                line = line.decode("utf-8", "replace").strip()
                if line:
                    self.error = line
        except (OSError, ValueError):
            return          # the pipe was closed under us by _stop

    def _stop(self):
        self.gen += 1                      # tells the reader it is finished
        p, self.proc = self.proc, None
        self.q = None
        if p is None:
            return
        # SIGKILL rather than SIGTERM. ffmpeg is almost always blocked
        # writing to a pipe that nobody is draining any longer, and a polite
        # signal leaves it sitting there until the wait gives up -- which on
        # a resize is two seconds of frozen picture. There is nothing in a
        # decoder that wants an orderly shutdown.
        p.kill()
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass
        p.wait()

    def _read_ahead(self, proc, q, nbytes, gen):
        """Frames off the pipe into the queue, until this reader is retired.

        A pipe read is free to come back short, so a frame is read to length
        rather than in one call. The put has a timeout so that retiring the
        thread does not depend on anyone draining the queue, and `None` is
        how the end of a file reaches the other side.
        """
        out = proc.stdout
        while gen == self.gen:
            buf = b""
            while len(buf) < nbytes:
                try:
                    more = out.read(nbytes - len(buf))
                except (OSError, ValueError):
                    more = None
                if not more:
                    buf = None
                    break
                buf += more
            while gen == self.gen:
                try:
                    q.put(buf, timeout=0.2)
                    break
                except queue.Full:
                    continue
            if buf is None:
                return

    def close(self):
        self._stop()

    # ------------------------------------------------------------- frame

    @staticmethod
    def _blank(cols, rows):
        return [[BLANK] * max(0, cols) for _ in range(max(0, rows))]

    def _note(self, cols, rows, text):
        """One line of text on an empty grid.

        Anything that stops the picture arriving -- a decoder still opening
        a file, a filter graph that would not build, a playlist of things
        that turned out not to be video -- reaches the screen as a sentence.
        A black rectangle is the one answer that cannot be acted on.
        """
        g = self._blank(cols, rows)
        if cols > 0 and rows > 0:
            row = g[rows // 2]
            for i, ch in enumerate(text[:cols]):
                row[i] = Glyph(ch, "ff9060", None, True)
        return g

    def status(self):
        """Why there is no picture, in one line."""
        if self.error:
            return "video: ffmpeg: %s" % self.error
        return "video: opening %s" % os.path.basename(self.path)

    def frame(self, cols, rows):
        """The picture as a grid of Glyph."""
        got = self._advance(cols, rows)
        if isinstance(got, list):
            return got
        if self._glyphs is None or self._glyphs[0] is not got:
            self._glyphs = (got, _assemble(got))
        return self._glyphs[1]

    def arrays(self, cols, rows):
        """The picture as (code point, foreground, background) arrays, or
        a grid of Glyph when what there is to show is a sentence."""
        got = self._advance(cols, rows)
        if isinstance(got, list):
            return got
        if self._unpacked is None or self._unpacked[0] is not got:
            self._unpacked = (got, _unpack(got))
        return self._unpacked[1]

    def _advance(self, cols, rows):
        """The frame due now: packed cells, or a grid of Glyph."""
        now = time.monotonic()
        if self._grid is not None and (cols, rows) == (self.cols, self.rows) \
                and now < self.next_frame:
            return self._grid
        # The next deadline comes off the last one, not off now: a sleep
        # that overshoots by a millisecond would otherwise add a
        # millisecond to every frame and the film would run slow. A caller
        # more than a frame late gets a fresh clock instead of a burst.
        period = 1.0 / self.fps
        self.next_frame = (now + period if now > self.next_frame + period
                           else self.next_frame + period)

        if cols < 2 or rows < 2:
            self._grid = self._blank(cols, rows)
            return self._grid

        if self.proc is None:
            self._start(cols, rows, seek=self._entry)
        elif (cols, rows) != (self.cols, self.rows):
            # Resume where the film had got to; a resize is not a rewind.
            self._start(cols, rows, seek=self.position)

        for _ in range(len(self.files) + 1):
            try:
                data = self.q.get(timeout=0.05)
            except queue.Empty:
                # Nothing ready yet. A caller asks every frame interval from
                # the moment it attaches, which is well before a decoder has
                # opened a file, so this is the normal first second.
                return self._grid if self._grid is not None \
                    else self._note(cols, rows, self.status())
            if data is not None:
                self.played += 1
                self.frames += 1
                # Catch up to the clock. A caller slower than the frame
                # rate -- a receiver that cannot paint this fast, a wall
                # composing for something else -- would otherwise advance
                # the film one frame per call, and the film would play
                # slowly rather than drop what it could not show. Take the
                # newest frame waiting and count the rest as played.
                while True:
                    try:
                        nxt = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:            # end of this file
                        self.q.put(None)
                        break
                    data = nxt
                    self.played += 1
                    self.frames += 1
                    self.dropped = getattr(self, "dropped", 0) + 1
                self._grid = self.pack(data, cols, rows)
                return self._grid
            if self.played == 0 and self.error is None:
                self.error = "%s produced no frames" % \
                    os.path.basename(self.path)
            self._next_file()
            self._start(cols, rows, seek=self._entry)
        return self._note(cols, rows, self.status())

    def _pick_start(self):
        return float(self.start() if callable(self.start) else (self.start or 0))

    def _next_file(self):
        self.index += 1
        if self.index >= len(self.files):
            self.index = 0
            self.loops += 1
        self._entry = self._pick_start()      # a fresh way into this one

    # ------------------------------------------------------------ paint

    def _tiles(self, data, cols, rows):
        """The frame as (rows, cols, pixels-per-cell, 3).

        One reshape and one transpose gather each cell's own pixels into a
        contiguous run, which is what lets every mode below be an expression
        over whole frames rather than a loop over cells.
        """
        tw, th = self.tile
        a = np.frombuffer(data, np.uint8).reshape(rows * th, cols * tw, 3)
        return (a.reshape(rows, th, cols, tw, 3)
                 .transpose(0, 2, 1, 3, 4)
                 .reshape(rows, cols, th * tw, 3))

    @staticmethod
    def _luma(t):
        return (t.astype(np.int32) * LUMA).sum(-1) >> 8

    # Every mode packs its cells, (rows, cols) of int64, and has a painting
    # beside it that assembles those into Glyph for whoever wants a grid.

    def _paint_half(self, data, cols, rows):
        return _assemble(self._pack_half(data, cols, rows))

    def _paint_quad(self, data, cols, rows):
        return _assemble(self._pack_quad(data, cols, rows))

    def _paint_braille(self, data, cols, rows):
        return _assemble(self._pack_braille(data, cols, rows))

    def _paint_ascii(self, data, cols, rows):
        return _assemble(self._pack_ascii(data, cols, rows))

    def _paint_fine(self, data, cols, rows):
        return _assemble(self._pack_fine(data, cols, rows))

    def _pack_half(self, data, cols, rows):
        """Two stacked pixels per cell: foreground on top, background below.

        A cell whose pixels match is a space on that background instead of a
        half block on itself. It is identical on screen and about a third
        fewer bytes, and a film is full of flat sky, black bars and shadow.
        """
        a = np.frombuffer(data, np.uint8).reshape(rows * 2, cols, 3)
        top, bot = a[0::2], a[1::2]
        fg = _code(top)
        bg = _code(bot)
        same = (top == bot).all(-1)
        ch = np.where(same, 0, HALF_IX)
        fg = np.where(same, 0, fg)
        return (ch << _CH) | (fg << _FG) | bg

    def _pack_ascii(self, data, cols, rows):
        """One pixel a cell, its brightness picking a character off a ramp."""
        a = np.frombuffer(data, np.uint8).reshape(rows, cols, 3)
        lum = self._luma(a)
        ch = ASCII_IX[(lum * (len(ASCII_RAMP) - 1)) // 255]
        # An explicit black background rather than the receiver's own: the
        # ramp is ink, and ink needs paper it can be read against wherever
        # this lands.
        return (ch << _CH) | (_code(a) << _FG) | 1

    def _split(self, t, lum, sel):
        """Group means and a coverage mask for a chosen foreground set.

        The mean of each group is the least-squares colour for it once the
        split is fixed, so every mode ends here: the two colours, the mask
        that says which pixels took which, and the flat cells -- a tile with
        nothing to separate, or one where the split took everything -- sent
        out as a single background so they cost one colour instead of two.
        """
        n = t.shape[2]
        tot = t.sum(2, dtype=np.int64)
        nf = sel.sum(2, dtype=np.int64)
        fsum = (t * sel[..., None]).sum(2, dtype=np.int64)
        nb = n - nf
        flat = ((lum.max(2) - lum.min(2)) < self.FLAT) | (nf == 0) | (nb == 0)

        nfc = np.maximum(nf, 1)[..., None]
        nbc = np.maximum(nb, 1)[..., None]
        fg = _code((fsum // nfc).astype(np.uint8))
        bg = _code(((tot - fsum) // nbc).astype(np.uint8))
        mean = _code((tot // n).astype(np.uint8))

        bits = (1 << np.arange(n, dtype=np.int64))
        mask = (sel * bits).sum(2)
        return flat, mask, fg, bg, mean

    def _finish(self, flat, mask, fg, bg, mean, table):
        """Masks to characters, with the flat cells collapsed to a space.

        A cell that came out wholly foreground is a space on the foreground
        colour, which looks the same as a full block and sends less.
        """
        full = mask == (table.shape[0] - 1)
        ch = np.where(flat | full, 0, table[mask])
        fgc = np.where(flat | full, 0, fg)
        bgc = np.where(flat, mean, np.where(full, fg, bg))
        return (ch << _CH) | (fgc << _FG) | bgc

    def _pack_quad(self, data, cols, rows):
        """Four pixels per cell, split at their mean brightness."""
        t = self._tiles(data, cols, rows)
        lum = self._luma(t)
        sel = lum > (lum.sum(2) // 4)[..., None]
        flat, mask, fg, bg, mean = self._split(t, lum, sel)
        return self._finish(flat, mask, fg, bg, mean, QUAD_IX)

    def _pack_braille(self, data, cols, rows):
        """Eight pixels per cell as dots, split at their mean brightness."""
        t = self._tiles(data, cols, rows)
        lum = self._luma(t)
        sel = lum > (lum.sum(2) // 8)[..., None]
        flat, mask, fg, bg, mean = self._split(t, lum, sel)
        return self._finish(flat, mask, fg, bg, mean, BRAILLE_IX)

    def _pack_fine(self, data, cols, rows):
        """Eight pixels per cell, and the split that really fits them.

        Sorting a tile by brightness reduces the search from all 256 subsets
        to the seven that a threshold can produce, and the best of those is
        the best of all of them: any subset that is not brightness-ordered
        can be improved by swapping a dark foreground pixel for a bright
        background one. Prefix sums over the sorted tile turn each candidate
        into a handful of multiplies.

        Squared error is minimised where the between-group sum of squares is
        largest, which is the score below: the tile's own total is a
        constant across the seven, so it never has to be computed.
        """
        t = self._tiles(data, cols, rows)
        lum = self._luma(t)

        order = np.argsort(lum, axis=2)
        ts = np.take_along_axis(t, order[..., None], axis=2).astype(np.int64)
        cum = ts.cumsum(2)
        tot = cum[:, :, 7, :]

        dark = cum[:, :, :7, :]                  # the n darkest, n = 1..7
        light = tot[:, :, None, :] - dark
        nd = np.arange(1, 8, dtype=np.int64)
        score = ((dark * dark).sum(3) / nd + (light * light).sum(3) / (8 - nd))
        bn = score.argmax(2) + 1                 # pixels below the split

        # Back from sorted order to tile order: a pixel is foreground when
        # its rank puts it above the split.
        rank = np.empty_like(order)
        np.put_along_axis(rank, order, np.arange(8, dtype=order.dtype), axis=2)
        sel = rank >= bn[..., None]

        flat, mask, fg, bg, mean = self._split(t, lum, sel)
        return self._finish(flat, mask, fg, bg, mean, FINE_IX)


# -------------------------------------------------------------- arguments

OPTIONS = """  --mode M       half|quad|braille|ascii|fine (default half)
  --fps N        frames a second (default 20)
  --punch F      picture lift before the downscale, 0 off (default 1.0)
  --cell R       how many times taller than wide a receiver's cell is
                 (default 2); a small font rounds away from that and
                 the picture stretches by the difference
  --shuffle      play the files in a random order"""


def parse_args(args, usage):
    """The options both front ends share, and whatever positionals are left."""
    opts = {"fps": 20, "mode": "half", "punch": 1.0, "shuffle": False,
            "cell_ratio": 2.0}
    args = list(args)
    rest = []
    while args:
        a = args.pop(0)
        if a == "--fps" and args:
            opts["fps"] = int(args.pop(0))
        elif a == "--mode" and args:
            opts["mode"] = args.pop(0)
        elif a == "--punch" and args:
            opts["punch"] = float(args.pop(0))
        elif a == "--cell" and args:
            opts["cell_ratio"] = max(0.5, min(8.0, float(args.pop(0))))
        elif a == "--shuffle":
            opts["shuffle"] = True
        elif a.startswith("--"):
            raise SystemExit("unknown option %s\n%s" % (a, usage))
        else:
            rest.append(a)
    if not rest:
        raise SystemExit(usage)
    if opts["mode"] not in MODES:
        raise SystemExit("unknown mode %s (%s)" % (opts["mode"],
                                                   "|".join(MODES)))
    return rest, opts
