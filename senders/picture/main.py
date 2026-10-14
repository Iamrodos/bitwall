#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["numpy", "pillow"]
# ///
"""Museum pictures on a wall, one at a time, with the label underneath.

    senders/picture/main.py localhost
    senders/picture/main.py --series hiroshige,posada --hold 40 pi.local
    senders/picture/main.py --category space localhost
    senders/picture/main.py --mode half --punch 0 --font 10 localhost
    senders/picture/main.py --list

A gallery rather than a slideshow: one work is held long enough to look at,
its label sits under it the way a label sits under a picture, and the next
one arrives by dissolving through it. `gallery.py` chooses the work and
fetches it; everything here is about what a picture looks like when it is
made of characters.

**The painting is the video sender's.** `senders/games/cellfilm/player.py`
already turns a framebuffer into two-colour cells by every method worth
using, and a still is a film of one frame, so `Painter` takes those methods
and leaves behind the playlist, the decoder thread and the clock. What is
left is one ffmpeg run per picture per area.

**Square pixels want `fine`.** A cell is twice as tall as it is wide, so a
mode's tile is square only when it is twice as tall as it is wide too:
`half` at 1x2 and `fine` at 2x4 both are, and `fine` has twice the linear
resolution, which on a 160x45 wall is 320x180. That is the default here.
`half` is the honest alternative, fewer colours but each one truer, and
worth trying on a poster.

**A picture is still, so nothing is sent.** Between dissolves every cell
comes out identical to the last frame and the receiver's damage tracking
drops the lot, so a held picture costs nothing on the wire however fast the
wall asks for it. The frame rate exists for the dissolve alone.

**A wall that draws pictures gets the picture, in the best way it has.**
Where the wall gives a video layer whose `images=` says JPEG or PNG -- the
web receiver, the Pi painter -- the work goes on a layer of the sender's
own over its cells, as the file it came as wherever that suits (a JPEG
not much bigger than its box), and otherwise made the box's size by
ffmpeg; the wall scales it into the box the cells would have filled, with
its shape kept, and holds it until the next. Where it does not but its
`caps` says `kitty` -- a shell receiver in Ghostty, a pane -- the work is
a PNG the size of that box, passed through bitwall untouched with
`passthrough()` and placed in those cells for the terminal to draw. Either
way the cells under it are left the colour of the wall, the label stays
cells, and the next work replaces it at once, without a dissolve. Only a
wall that draws neither gets the picture in cells.

**The label is what makes it a gallery.** Title, then artist and date, then
the series a print came from. A museum print's title is often `Place, from
the series "..."`, which is two facts wearing one hat, so they are split and
the series is dimmed: the picture is of the place.

**The lettering suits the picture.** A museum label is set in the bold
block letters; a photograph from space or a still from a film is labelled
in the braille dot-matrix instead, which is finer and reads as a caption
on a screen rather than a card on a wall. A wall that cannot draw braille
gets the block letters for everything.

Nothing here is fatal. A museum that cannot be reached leaves the picture
already up and says so on the label, an unknown series name is dropped, and
a number out of range is clamped.
"""
import argparse
import base64
import itertools
import os
import random
import signal
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SENDERS = os.path.dirname(_HERE)
# The repo is on the path for `bitwall`, the video directory for `player`.
sys.path.insert(0, os.path.dirname(_SENDERS))
sys.path.insert(0, os.path.join(_SENDERS, "games", "cellfilm"))

import bitwall                                                  # noqa: E402
import bitwall.video                                            # noqa: E402
import gallery as art                                           # noqa: E402
import player                                                   # noqa: E402
from bitwall import cell, letters                               # noqa: E402

DEFAULT_FONT = 10
DEFAULT_HOLD = 30.0      # seconds a picture stays up
DEFAULT_MODE = "fine"
DEFAULT_PUNCH = 0.35     # gentler than a film: a print is already graded
FPS = 20.0               # the dissolve's frame rate, and nothing else's

#: The grid this is composed for, which a page uses to shape a small box.
#: Wide, because a picture wants width and the label wants a line under it.
NOMINAL = (160, 48)

#: How long one picture takes to become the next. Cells change in a random
#: order, so the old picture erodes rather than wipes, which reads as a
#: dissolve on a grid far too coarse for a real one.
DISSOLVE = 1.4

#: The label: three rows of text and a blank one above them, so the picture
#: has an edge rather than running into its own caption.
LABEL = 4

#: The same label drawn from blocks instead: a blank row, five for the
#: title, a gap, five for who and when. Taller, and legible.
LABEL_BIG = 13

#: How wide a side bar has to be before the label goes in it rather than
#: under the picture. Four cells to a block letter, so this is room for
#: about seven letters a line, which is a short word and a margin.
BAR = 32

#: One line of drawn text and a blank row above it: the gallery card,
#: artist at one end and the work at the other.
CARD = 7

#: The least a label beside the picture (columns) or under it (rows) is
#: given in the default arrangement, so a picture that could fill the wall
#: still leaves its label room to be read.
FILL_SIDE = 24
FILL_UNDER = 7

#: The most lines a title is broken onto, and the share of the wall's
#: depth a line of it may be at most: four lines, each up to a fifth.
TITLE_LINES = 4
TITLE_LINE = 5
#: The letters a painting's label is set in, in order: solid, a whole
#: cell or half of one to each of the bitmap's pixels. TILE's tiles have
#: gaps between them, which at a label's size read as a sign of lamps.
SOLID = [letters.BLOCK, letters.HALF]

WALL = "000000"          # the wall: black, the same black the bars are
TITLE = "e4e1d8"         # the work
INK = "9a958a"           # artist and date
DIM = "5c5952"           # the series a print came from, and any complaint

#: The least of a picture that has to be lit for it to be worth hanging:
#: the share of it brighter than a deep shadow. A photograph of the night
#: side of the Earth or a single star field is a black wall with a few
#: sparks in it, which from across a room is a wall that has gone off.
#: Paintings and film stills are nowhere near this; it is space that is.
LIT = 0.2
#: The kitty images a picture is sent as: this id and the next, in turn,
#: so the next picture is up before the last is taken down.
KITTY_ID = 7001
#: The most of a file that goes to a layer as it came; a bigger one is made
#: the box's size first.
IMAGE_MOST = 4 << 20
#: Pixels across a cell's width in the PNG sent to a kitty terminal, which
#: scales it to the cells; enough to look sharp, capped so a wide wall's
#: picture stays under 1920 across.
KITTY_CELL = 12
#: The most a kitty picture may be. It goes to a terminal as base64, a
#: third bigger, and a terminal over ssh to an iPad froze for a while on a
#: 3.8 MB PNG; so a picture past this is made again with fewer pixels a
#: cell, and as 256 colours where that is still not enough.
KITTY_MOST = 400 << 10

#: The series labelled in braille dots rather than block letters: the
#: photographs and the film stills, everything that is not the museum's.
DOTTED = set(art.CATEGORIES["space"][0]) | set(art.CATEGORIES["anime"][0])


class Painter(player.Player):
    """The video player's painting, without its playlist or its decoder.

    Every `_paint_*` method there is a pure function of a framebuffer and an
    area, which is exactly what a still needs, while everything around them
    is about a file playing in time, which is exactly what a still does not
    have. So this takes the first and defines the second away.
    """

    def __init__(self, mode, punch, cell_ratio=2.0):
        self.mode = mode
        self.punch = punch
        self.tile = player.TILE[mode]
        self.paint = getattr(self, "_paint_" + mode)
        self.ffmpeg = player.find_ffmpeg()
        # The bars a work leaves are the wall showing through, so they
        # are painted in the same colour it is.
        self.ground = "0x" + WALL
        #: How much taller than wide a cell is on the glass. Everything
        #: here treats a framebuffer pixel as square, which holds only
        #: while a mode's tile has the same proportions as the cell it
        #: fills: `fine` is 2x4 and wants a 2:1 cell. A receiver rounds
        #: its cell to whole pixels, so a small font drifts off that --
        #: 3x8 rather than 3x6 -- and a picture drawn as though the
        #: pixels were square comes out stretched.
        self.cell_ratio = cell_ratio

    def grid(self, path, cols, rows):
        """One picture at one size, or None if ffmpeg would not read it.

        The filter chain is the player's: sharpen and lift before the
        scale, because the scale is what throws detail away, then pad to
        the area so the picture keeps its own proportions and the bars are
        the wall showing through.
        """
        tw, th = self.tile
        w, h = cols * tw, rows * th
        if w < 2 or h < 2:
            return None
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-i", path, "-vf", self._filters(w, h), "-frames:v", "1",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out.stdout) != w * h * 3:
            return None
        return self.paint(out.stdout, cols, rows)

    def lit(self, path):
        """How much of a picture is lit, 0 to 1, or 1 if ffmpeg cannot say.

        Measured on a thumbnail in grey, which is all a share of bright
        pixels needs, and says nothing is wrong when it cannot measure:
        the full paint that follows will find out for itself.
        """
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-i", path, "-vf", "scale=64:36", "-frames:v", "1",
               "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return 1.0
        if not out.stdout:
            return 1.0
        return sum(1 for b in out.stdout if b > 48) / len(out.stdout)


def plain(text):
    """`text` as characters the block letters have: capitals, accents
    folded, anything unknown a space."""
    return letters.plain(text, letters.BLOCK)


def sized(font, scale):
    """The size of `font` a label at `scale` is set in.

    The block letters are five rows to a scale. The braille dots are
    given the same height, so every layout here, which counts in block
    letters' rows, holds for either.
    """
    if font == letters.DOTS:
        return {"height": letter_height(scale)}
    return {"scale": scale}


def ink(text, cell_ratio, scale=1, turn=None, font=letters.BLOCK):
    """`text` in drawn letters, as rows in which a cell is ink or None.

    Set mono, the way a label is: every letter the same width, so
    `fits_in` can count them. Widened by the cell's own shape, since a
    3x5 bitmap drawn a cell a pixel on a cell far taller than wide is a
    thin scratch rather than a letter. Turned "up", a word runs up a
    column reading from the bottom.
    """
    got = letters.rows(text, font, TITLE, spacing="mono", cell=cell_ratio,
                       turn=turn, edges="cut", **sized(font, scale))
    return got or [()] * letter_height(scale)


def inked(ch, colour, under):
    """A cell of drawn text in `colour` over the cell it covers: solid,
    or a cut corner's diagonal half, or a cell of braille dots, with what
    was there behind it."""
    if ch[0] == " ":
        return cell(" ", "default", colour)
    return cell(ch[0], colour, under[2])


def upended_depth(text, scale=1):
    """The rows a word on its side is given: its letters and a letter's
    gap below them."""
    tall = letters.size(text, letters.BLOCK, spacing="mono", turn="up",
                        scale=scale)[1]
    return tall + scale if tall else 0


def upended_width(cell_ratio, scale=1):
    return letters.size("A", letters.BLOCK, turn="up", cell=cell_ratio,
                        scale=scale)[0]


#: The most words a title can have and still be set on its side. Turned
#: text is read a letter at a time, so a line of it is slow, and three
#: columns of small words reads as a stack rather than as a title.
UPENDED_WORDS = 2


def letter_height(scale=1):
    return letters.size("A", letters.BLOCK, scale=scale)[1]


def fits_in(width, cell_ratio, scale=1, font=letters.BLOCK):
    """How many characters of drawn text fit across `width` cells.

    Measured over ten letters rather than two: the braille letters sit on
    a grid of dots, not of cells, so a letter's step is a fraction of a
    cell and a line of them is wider than twice one letter suggests.
    """
    one = letters.size("A", font, spacing="mono", cell=cell_ratio,
                       **sized(font, scale))[0]
    ten = letters.size("A" * 10, font, spacing="mono", cell=cell_ratio,
                       **sized(font, scale))[0]
    step = (ten - one) / 9
    return max(1, int((width - one) // step) + 1)


def wrap(text, width):
    """`text` broken into lines of at most `width` characters.

    Words are kept whole where they fit and broken where one is longer
    than the line, because a title with a long word in it is still worth
    showing and the alternative is a blank bar.
    """
    lines, line = [], ""
    for word in (text or "").split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def breaks(words, lines):
    """The ways of setting `words` on exactly that many lines, as text,
    the evenest first, so a tie in size goes to it: every one where that
    is few, the evenest for each line length where it is not."""
    if lines <= 1:
        return [" ".join(words)]
    if lines == 2 or len(words) <= 9:
        out = []
        for cut_ in itertools.combinations(range(1, len(words)), lines - 1):
            ends = (0,) + cut_ + (len(words),)
            out.append("\n".join(" ".join(words[a:b]) for a, b in zip(ends, ends[1:])))
        return sorted(out, key=lambda t: max(map(len, t.split("\n"))))
    out = []
    for width in range(max(len(w) for w in words), len(" ".join(words)) + 1):
        got = wrap(" ".join(words), width)
        if len(got) == lines:
            out.append("\n".join(got))
        elif len(got) < lines:
            break
    return out


def fit(text, width):
    """A line cut to the width, with an ellipsis where it was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[:width - 1].rstrip() + "…" if width > 1 else text[:1]



def credit(artist, date, room):
    """Who made it and when, as the texts to set: one, `artist - date`,
    where that fits a line of `room`, else the artist and then the date on
    a line of its own, so a wrap never leaves the dash hanging."""
    if not date:
        return [artist]
    one = f"{artist} - {date}"
    return [one] if len(one) <= room else [artist, date]

def cut(text, width):
    """The same, for drawn text, which has no ellipsis in its alphabet.

    The block font holds a full stop and no single-glyph ellipsis, so a
    cut is marked with three of them. Marking it matters more here than
    in typed text: a drawn title that simply stops looks like a title
    that is that length.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 4:
        return text[:width]
    return text[:width - 3].rstrip() + "..."


class Show:
    """One picture at a time, dissolving into the next.

    `frame(cols, rows)` is the whole surface. The gallery is asked for the
    next work on a thread, because a museum is a network away and a wall
    must not stop painting to wait for one; when the answer arrives the
    dissolve starts, and until it does the picture already up stays up.
    """

    def __init__(self, gallery, painter, hold, rng, want="auto"):
        self.gallery, self.painter = gallery, painter
        self.hold, self.rng = hold, rng
        #: One arrangement rather than the rotation, for looking at a
        #: particular one. Still only used where it suits the work:
        #: forcing a layout a title cannot fit would be forcing a bad
        #: frame, so an unsuitable one falls back to the rotation.
        self.want = want
        self.cols = self.rows = 0
        self.wall = None             # set once attached: its cells' size
        #: Whether the cell's shape was given on the command line rather
        #: than taken from what the wall says of its cells.
        self.cell_said = False
        self.work = None             # what is on the wall
        self.art = None              # its cells, picture area only
        self.coming = None           # the next work, once it has arrived
        self.old = None              # the picture being dissolved through
        self.order = ()              # the order its cells give way
        self.plan = None             # where the picture and its label go
        self.until = 0.0             # when the current picture has had long
        self.started = 0.0           # when this dissolve began
        self.shown = 0
        #: How the picture goes -- `image` on the layer, `kitty`, or `cells`
        #: -- and where the kitty one on the wall was placed, (x, wide, tall).
        self.how = "cells"
        self.placed = None
        #: The video layer the picture goes on, once the wall has given
        #: one; what is up on it, as (path, at, size); and whether the
        #: work is still to go up there, since a full pipe drops a frame.
        self.layer = None
        self.imaged = None
        self.image_due = False
        #: Set as the sender goes, so taking its pictures down never puts
        #: one up the other way.
        self.leaving = False
        self.ident = KITTY_ID
        self.asking = False
        self.note = "fetching"
        self._ask()

    # -- getting the next picture

    def _ask(self):
        """Send for the next work, unless one is already on its way."""
        if self.asking or self.coming is not None:
            return
        self.asking = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            work = self.gallery.next()
        finally:
            self.asking = False
        if work is None:
            # Nothing to show and nothing to be done about it here. The
            # label says so and the next hold tries again, which is what a
            # wall wants from a museum that is briefly unreachable.
            self.note = f"{self.gallery.asked} is not answering"
            return
        self.note = ""
        self.coming = work

    def _take(self, now, cols, rows):
        """Put the work that has arrived up, dissolving through what is up."""
        work, self.coming = self.coming, None
        if self.painter.lit(work.path) < LIT:
            say(f"skipped {work.title[:40]!r}: too dark to hang", flush=True)
            self._ask()
            return
        # How it goes is settled before the work is taken on, so a change of
        # how puts up what was there, not this one twice.
        how = self._mode()
        # The layout is the arriving work's, not the one on screen: a
        # portrait following a landscape moves the label as it lands.
        self.work = work
        self.plan = self._plan(cols, rows)
        if how != "cells":
            if not self._put(work.path):
                self.note = f"ffmpeg could not read {os.path.basename(work.path)}"
                self._ask()
                return
            self.art = self.old = None
            self.order = ()
            self.shown += 1
            self.started = now
            self.until = now + self.hold
            return
        art = self.painter.grid(work.path, self.plan["wide"],
                                self.plan["tall"])
        if art is None:
            self.note = f"ffmpeg could not read {os.path.basename(work.path)}"
            self._ask()
            return
        self.old = self.art if self.art is not None else None
        self.art = art
        self.shown += 1
        self.started = now
        self.until = now + self.hold
        if self.old is not None and len(self.old) == len(art) \
                and len(self.old[0]) == len(art[0]):
            n = len(art) * len(art[0])
            self.order = self.rng.sample(range(n), n)
        else:
            self.order = ()

    # -- how the picture goes

    def _mode(self):
        """How pictures go now: `image` where the layer is open and its
        wall draws JPEG or PNG and says its glass and its cells; `kitty`
        where the wall says its terminal draws kitty graphics; else
        `cells`. Asked each time, since a wall says its caps once it has
        opened, a layer can come and go, and a wall given back says them
        again."""
        if self.leaving:
            return self.how
        layer = self.layer
        if (layer is not None and layer.open_now and self.wall.box((0, 0, 1, 1))
                and (layer.takes("jpeg") or layer.takes("png"))):
            now = "image"
        elif self.wall is not None and self.wall.offers("kitty"):
            now = "kitty"
        else:
            now = "cells"
        if now != self.how:
            say(f"pictures as {'images on a layer' if now == 'image' else now}",
                flush=True)
            if self.how == "kitty":
                self.unplace()
            elif self.how == "image":
                self.unimage()
            self.how = now
            if now != "cells" and self.work is not None:
                self.art, self.old, self.order = None, None, ()
                self._put(self.work.path)
        return now

    def _put(self, path):
        """The work up as a picture, the way `_mode` says."""
        return self._image(path) if self.how == "image" else self._place(path)

    # -- a picture on a video layer

    def _image(self, path):
        """The work on the layer, scaled by the wall into its box with its
        shape kept and held until the next. The file goes as it came where
        the wall takes its codec and it is not much bigger than the box;
        anything else is made the box's size by ffmpeg, as JPEG where the
        wall takes it. False where neither could be done."""
        box = self.wall.box((self.plan["x"], 0, self.plan["wide"], self.plan["tall"]))
        if box is None:
            return False
        bx, by, bw, bh = box
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return False
        codec = bitwall.protocol.image_of(data)
        size = bitwall.protocol.image_size(codec, data) if codec else None
        if not (size and self.layer.takes(codec) and len(data) <= IMAGE_MOST
                and size[0] <= 2 * bw and size[1] <= 2 * bh):
            data = self._made(path, min(bw, 4096), min(bh, 4096))
            codec = bitwall.protocol.image_of(data) if data else None
            size = bitwall.protocol.image_size(codec, data) if codec else None
            if not size:
                return False
        k = min(bw / size[0], bh / size[1])
        dw, dh = max(1, round(size[0] * k)), max(1, round(size[1] * k))
        at = (bx + (bw - dw) // 2, by + (bh - dh) // 2)
        self.image_due = not self.layer.frame(data, at=at, size=(dw, dh), hold="still")
        self.imaged = (path, at, (dw, dh))
        return True

    def _made(self, path, w, h):
        """The work fitted inside w by h by ffmpeg: JPEG where the layer
        takes it, else PNG. None where ffmpeg would not."""
        jpeg = self.layer.takes("jpeg")
        try:
            out = subprocess.run(
                [self.painter.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-i", path, "-vf",
                 f"scale={w}:{h}:force_original_aspect_ratio=decrease,format="
                 + ("yuvj444p" if jpeg else "rgb24"),
                 "-frames:v", "1", "-f", "image2pipe",
                 "-vcodec", "mjpeg" if jpeg else "png"]
                + (["-q:v", "2"] if jpeg else []) + ["-"],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout or None

    def unimage(self):
        """Take the picture off the layer, if one is up."""
        if self.imaged is not None and self.layer is not None:
            self.layer.clear()
        self.imaged = None
        self.image_due = False

    # -- a picture as a kitty image

    def _place(self, path):
        """The work as a PNG the size of its box, placed in its cells.
        False where ffmpeg would not make one."""
        plan = self.plan
        wide, tall = plan["wide"], plan["tall"]
        most = max(2, min(KITTY_CELL, 1920 // max(1, wide)))
        tries = [(a, False) for a in sorted({most, most * 3 // 4, most // 2, most // 3, 2},
                                            reverse=True) if 2 <= a <= most] + [(2, True)]
        png = None
        for across, few in tries:
            png = self._png(path, wide * across,
                            round(tall * across * self.painter.cell_ratio), few)
            if png is None:
                return False
            if len(png) <= KITTY_MOST:
                break
        say(f"kitty picture {len(png) // 1024} KiB at {across} px a cell"
            + (", 256 colours" if few else ""), flush=True)
        # Sent under the other of two ids and placed at the picture's top
        # left cell, stretched over its cells, and only then the last one
        # deleted: the stream is in order, so on a slow link the picture
        # going stays up until the one coming has all arrived, rather than
        # the wall standing blank meanwhile. No answer asked for (q=2),
        # since one would arrive at the terminal as typing.
        old = self.ident if self.placed is not None else None
        self.ident = KITTY_ID + 1 if self.ident == KITTY_ID else KITTY_ID
        payload = base64.b64encode(png).decode()
        parts = [payload[i:i + 4096] for i in range(0, len(payload), 4096)]
        keys = f"a=T,f=100,i={self.ident},c={wide},r={tall},C=1,q=2"
        esc = [f"\x1b[1;{plan['x'] + 1}H"]
        for n, part in enumerate(parts):
            more = 1 if n < len(parts) - 1 else 0
            head = f"{keys},m={more}" if n == 0 else f"m={more}"
            esc.append(f"\x1b_G{head};{part}\x1b\\")
        if old is not None:
            esc.append(f"\x1b_Ga=d,d=I,i={old},q=2\x1b\\")
        self.wall.passthrough("".join(esc).encode())
        self.placed = (plan["x"], wide, tall)
        return True

    def _png(self, path, w, h, few):
        """The work fitted into w by h on the wall's black, as a PNG, in
        256 colours where `few`; None where ffmpeg would not make one."""
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x{WALL}")
        vf += (",split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse"
               if few else ",format=rgb24")
        try:
            out = subprocess.run(
                [self.painter.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-i", path, "-filter_complex" if few else "-vf", vf, "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.stdout.startswith(b"\x89PNG") else None

    def unplace(self):
        """Take the kitty image off the terminal, if one is up."""
        if self.placed is not None and self.wall is not None:
            self.wall.passthrough(f"\x1b_Ga=d,d=I,i={self.ident},q=2\x1b\\".encode())
        self.placed = None

    # -- composing

    def _bars(self, cols, rows):
        """Where the picture lands, and what black is left around it.

        A work keeps its own proportions, so unless it happens to match
        the wall there is bar either side of it or above and below. This
        works out which and how much, from the work's shape and the
        squash the painter applies, without rendering anything.
        """
        if self.work is None:
            return cols, rows, 0
        shape = max(0.05, self.work.aspect / max(0.05, self.painter.squash))
        tw, th = self.painter.tile
        box_w, box_h = cols * tw, rows * th
        if shape > box_w / max(1, box_h):
            fit_w, fit_h = box_w, box_w / shape
        else:
            fit_h, fit_w = box_h, box_h * shape
        pic_c = max(1, int(fit_w // tw))
        pic_r = max(1, int(fit_h // th))
        return pic_c, pic_r, (cols - pic_c) // 2

    def _fill(self, cols, rows):
        """The picture as large as the wall allows, against an edge, and
        the label filling what is left: the default arrangement.

        Two ways are tried. The picture full height with the label in a
        column beside it, and full width with the label in rows under it,
        each leaving the label at least the room a readable one needs, and
        each offered only where its label can be set whole in its block;
        the one with the larger picture is used. The picture's box hugs the
        picture, so none of the black is spent as bars round it: all of
        it is the label's. None where neither leaves a label room.
        """
        if self.work is None:
            return None
        shape = max(0.05, self.work.aspect / max(0.05, self.painter.squash))
        tw, th = self.painter.tile

        def fit(box_c, box_r):
            if box_c < 1 or box_r < 1:
                return 0, 0
            w, h = box_c * tw, box_r * th
            if shape > w / h:
                return box_c, max(1, int(w / shape // th))
            return max(1, int(h * shape // tw)), box_r

        gap = 2
        side_min = max(FILL_SIDE, cols // 5)
        under_min = max(FILL_UNDER, rows // 6)
        options = []
        sc, sr = fit(cols - side_min - gap, rows)
        if sc and cols - sc - gap >= side_min \
                and self._fill_text((sc + gap, 0, cols - sc - gap, rows), rows):
            options.append((sc * sr, {"kind": "fill", "x": 0, "wide": sc, "tall": sr,
                                      "at": None, "label": (sc + gap, 0, cols - sc - gap, rows)}))
        uc, ur = fit(cols, rows - under_min - gap)
        if ur and rows - ur - gap >= under_min \
                and self._fill_text((0, ur + gap, cols, rows - ur - gap), rows):
            options.append((uc * ur, {"kind": "fill", "x": (cols - uc) // 2, "wide": uc,
                                      "tall": ur, "at": None,
                                      "label": (0, ur + gap, cols, rows - ur - gap)}))
        if not options:
            return None
        return max(options, key=lambda o: o[0])[1]

    def _plan(self, cols, rows):
        """Where the picture goes and where the label goes with it.

        A work keeps its own proportions, so the black it leaves is the
        material a layout is made of: a portrait on a wide wall leaves
        columns wide enough to hang a card in, a landscape leaves almost
        nothing and the label has to take rows instead. So the layouts
        are tried in order and the first that suits the shape of this
        particular work is the one used.

        Among those that suit, it rotates -- `shown` advances the pick --
        so a wall of portraits does not settle into one arrangement for
        an hour. A layout that no longer suits is simply not offered,
        which is why rotating cannot produce a bad frame.

        A plan is where the picture is drawn (`x`, `wide`, `tall`) and
        what the label does (`kind`, and the region it has).
        """
        if self.want in ("auto", "fill") and self._big(rows):
            filled = self._fill(cols, rows)
            if filled is not None:
                return filled
        pic_c, _pic_r, left = self._bars(cols, rows)
        big = self._big(rows)
        r = self.painter.cell_ratio
        face = self.face
        name, _series = self.work.series_of if self.work else ("", "")
        who = self.work.artist if self.work else ""

        def holds(width, lines=1):
            """Whether this work's text fits that width, unbroken.

            Two separate failures, and the second is the nastier: a
            title may need more lines than there is room for, or one
            word in it may be longer than the line, which `wrap` can
            only deal with by breaking the word. A broken word in a
            drawn label reads as a mistake, so a layout that would
            cause one is not offered for this work at all.
            """
            room = fits_in(width - 4, r, font=face)
            for text in (name, who):
                if any(len(word) > room for word in text.split()):
                    return False
                if len(wrap(text, room)) > lines:
                    return False
            return True

        fits = []
        if big and left >= BAR and holds(left, lines=4):
            # Centred, and the label hung in the column it already left.
            fits.append({"kind": "bar", "x": 0, "wide": cols, "tall": rows,
                         "at": (0, left)})
        gap = min(cols - 8, left * 2)
        if big and left >= BAR and holds(gap, lines=3):
            # All the black gathered to one side instead, so the column
            # is twice as wide and the picture sits against an edge.
            fits.append({"kind": "bar", "x": gap, "wide": cols - gap,
                         "tall": rows, "at": (0, gap)})
        # Turned on its side, a word to a column. Worth doing only when
        # there are columns to make a pattern of: one word alone in a
        # bar reads as a mistake rather than a choice, and a word deeper
        # than the wall cannot be set this way at all.
        # It wants the gathered bar rather than the centred one: a
        # turned word needs a column of its own, and a title of more
        # than two words is a stack rather than a title. Block letters
        # only: braille on its side is a column of dots, not a word.
        words = name.split()[:UPENDED_WORDS]
        span = upended_width(r)
        step = span + max(2, span // 5)
        # The name at its foot is one line, and the spine is offered only
        # where the whole of it fits: "FRA BA..." under a title the height
        # of the wall reads as a mistake, and a credit the licence asks for
        # cannot be cut at all.
        credit = f"{who} - {self.work.date}" if self.work and self.work.date else who
        whole = len(credit) <= fits_in(gap - 4, r, font=face)
        if (big and face == letters.BLOCK and whole
                and 0 < len(name.split()) <= UPENDED_WORDS and left >= BAR
                and len(words) * step <= gap - 8
                and all(upended_depth(w) <= rows - CARD - 4 for w in words)):
            fits.append({"kind": "spine", "x": gap, "wide": cols - gap,
                         "tall": rows, "at": (0, gap)})

        # The card puts two texts on one line, so each has half the wall
        # and a long title rules the layout out rather than being chopped.
        if big and rows >= CARD + 20 and holds(cols // 2):
            fits.append({"kind": "card", "x": 0, "wide": cols,
                         "tall": rows - CARD, "at": None})
        if big and rows >= LABEL_BIG + 20:
            lines = self._strip_lines(cols)
            if lines is None:
                fits.append({"kind": "strip", "x": 0, "wide": cols,
                             "tall": rows - LABEL_BIG, "at": None})
            elif lines:
                deep = len(lines) * (letter_height() + 1) + 1
                if rows - deep >= 20:
                    fits.append({"kind": "strip", "x": 0, "wide": cols,
                                 "tall": rows - deep, "at": None,
                                 "lines": lines})
        if not fits:
            n = self._label(rows)
            fits.append({"kind": "typed" if n else "none", "x": 0,
                         "wide": cols, "tall": rows - n, "at": None})
        if self.want != "auto":
            wanted = [f for f in fits if f["kind"] == self.want]
            if wanted:
                return wanted[self.shown % len(wanted)]
        return fits[self.shown % len(fits)]

    @property
    def face(self):
        """The letters the drawn label is set in: braille dots for the
        photographs and film stills, where the wall can draw them, and
        the block letters for everything else."""
        series = self.work.series if self.work else self.gallery.names[0]
        if series in DOTTED and letters.usable(letters.DOTS, self.wall):
            return letters.DOTS
        return letters.BLOCK

    def _big(self, rows):
        """Whether the label is drawn rather than typed: wherever the wall
        has the rows for a drawn label and a picture above it. Drawn, it is
        sized to the room the layout leaves, which on a wall with space is
        far larger than a row of the receiver's own glyphs; typed is for a
        wall too short to give it that room."""
        return rows >= LABEL_BIG + 20

    def _label(self, rows):
        """How many rows the label takes, which on a short wall is none."""
        if self._big(rows):
            return LABEL_BIG if rows >= LABEL_BIG + 20 else LABEL
        return LABEL if rows >= LABEL + 6 else 0

    def _cell(self):
        """How many times taller than wide a cell is on the glass: as the
        wall says of its cells where it does and nobody said otherwise,
        else as the painter has it. Every box here is counted in cells and
        the picture on a layer or in kitty keeps its own shape in pixels,
        so a cell taken for 2:1 on a 6x16 glass gives the picture a box
        a third too narrow, and a label beside it that the picture covers."""
        if not self.cell_said and self.wall is not None:
            got = self.wall.screen().get("cell")
            if got and got[0] > 0:
                return max(0.5, min(8.0, got[1] / got[0]))
        return self.painter.cell_ratio

    def frame(self, cols, rows):
        now = time.monotonic()
        ratio = self._cell()
        if (cols, rows) != (self.cols, self.rows) \
                or abs(ratio - self.painter.cell_ratio) > 0.01:
            if abs(ratio - self.painter.cell_ratio) > 0.01:
                say(f"cell {ratio:.2f}:1, as the wall says", flush=True)
                self.painter.cell_ratio = ratio
            self.cols, self.rows = cols, rows
            # A resize repaints from the file; a dissolve part way
            # through cannot survive it, because the picture it was
            # dissolving from was composed for the old area.
            self.old, self.order = None, ()
            if self.work is not None:
                self.plan = self._plan(cols, rows)
                if self._mode() != "cells":
                    self.art = None
                    self._put(self.work.path)
                else:
                    self.art = self.painter.grid(
                        self.work.path, self.plan["wide"], self.plan["tall"])
        if self.work is not None and self._mode() == "image" and self.layer is not None \
                and (self.image_due or self.layer.repaint_asked()):
            # A frame the pipe had no room for, or a glass that changed
            # under the layer and took it down: the box is worked out again.
            self._image(self.work.path)
        if self.coming is not None:
            self._take(now, cols, rows)
        elif self.work is not None and now >= self.until:
            self._ask()
            self.until = now + 5.0      # ask again in a while, not every frame
        elif self.work is None:
            self._ask()

        blank = cell(" ", "default", WALL)
        rows_out = [[blank] * cols for _ in range(rows)]
        plan = self.plan or {"kind": "none", "x": 0, "wide": cols,
                             "tall": rows, "at": None}
        if self.art is not None:
            self._picture(rows_out, plan, now)
        if self.work is not None or self.note:
            if plan["kind"] == "fill":
                self._fill_label(rows_out, plan["label"])
            elif plan["kind"] == "bar":
                self._beside(rows_out, rows, plan["at"])
            elif plan["kind"] == "spine":
                self._spine(rows_out, rows, plan["at"])
            elif plan["kind"] == "card":
                self._card(rows_out, plan["tall"], cols)
            elif plan["kind"] == "strip":
                self._drawn(rows_out, plan["tall"], cols)
            elif plan["kind"] == "typed":
                self._caption(rows_out, plan["tall"], cols)
        return rows_out

    def _spine(self, out, rows, at):
        """The title on its side and large, the artist set the usual way.

        Two things read at different speeds. Turned text is read a
        letter at a time, so it is worth spending on the two words a
        title can spare and nothing else; the name and date are read at
        a glance and belong the way round everything else is. Setting
        one against the other is the whole of the arrangement, and it
        only works while the turned part stays short.

        The columns stand just clear of the picture rather than centred
        in the black, so the title reads as belonging to the edge of the
        image and the rest of the bar is air.
        """
        x0, width = at
        r = self.painter.cell_ratio
        name, _series = self.work.series_of if self.work else ("", "")
        who = self.work.artist if self.work else ""
        if self.work and self.work.date:
            who = f"{who} - {self.work.date}"
        if self.note:
            name = self.note
        words = plain(name).split()[:UPENDED_WORDS]
        if not words:
            return

        # As large as the column and the wall will take. The title is
        # the element here, so it is worth the whole of the search.
        scale = 1
        for size in (4, 3, 2):
            span = upended_width(r, size)
            step = span + max(2, span // 5)
            if (len(words) * step <= width - 8
                    and max(upended_depth(w, size) for w in words)
                    <= rows - CARD - 4):
                scale = size
                break
        span = upended_width(r, scale)
        step = span + max(2, span // 5)
        block = len(words) * step - (step - span)
        x = x0 + max(2, width - block - 6)      # clear of the picture

        for word in words:
            deep = upended_depth(word, scale)
            y = max(1, (rows - CARD - deep) // 2)
            for n, bits in enumerate(ink(word, r, scale, turn="up")):
                if y + n >= len(out):
                    break
                row = out[y + n]
                for i, ch in enumerate(bits):
                    if ch is not None and x + i < len(row):
                        row[x + i] = inked(ch, TITLE, row[x + i])
            x += step

        # The name along the foot of the column, upright and smaller.
        room = fits_in(width - 4, r)
        top = rows - letter_height() - 2
        for n, bits in enumerate(ink(cut(who, room), r)):
            if top + n >= len(out):
                break
            row = out[top + n]
            for i, ch in enumerate(bits):
                if ch is not None and x0 + 2 + i < len(row):
                    row[x0 + 2 + i] = inked(ch, INK, row[x0 + 2 + i])

    @property
    def blocks_only(self):
        """Whether the label stays in block letters: for the paintings and
        prints, whose labels look made of the grid that way, rather than
        stepping down to the finer half blocks and braille, which read as
        an LED sign beside a painting."""
        series = self.work.series if self.work else self.gallery.names[0]
        return series not in DOTTED

    def _strip_lines(self, cols):
        """The strip's label in block letters, each text wrapped whole to
        the strip's width: [(line, colour)], the strip as deep as they
        need; [] where a single word is wider than the strip, which is a
        strip not to offer; None where the strip is not set this way."""
        if not self.blocks_only:
            return None
        if letters.usable(letters.TILE, self.wall):
            name, _s = self.work.series_of if self.work else ("", "")
            who = self.note or (f"{self.work.artist} - {self.work.date}"
                                if self.work and self.work.date
                                else (self.work.artist if self.work else ""))
            chosen = self._choose((name, who), cols - 4, letter_height())
            if chosen is not None and chosen["font"] == letters.TILE:
                return None
        room = fits_in(cols - 4, self.painter.cell_ratio, font=letters.BLOCK)
        name, _series = self.work.series_of if self.work else ("", "")
        if self.note:
            whos = [self.note]
        else:
            whos = credit(self.work.artist if self.work else "",
                          self.work.date if self.work else "", room)
        texts = [(name, TITLE)] + [(w, INK) for w in whos]
        if any(len(w) > room for t, _c in texts for w in plain(t).split()):
            return []
        return [(line, c) for t, c in texts if t for line in wrap(plain(t), room)]

    def _fill_text(self, region, rows):
        """The label set in its block, or None where it cannot be: what to
        draw, [(text keywords, x, y, colour)]. The title as large as the
        block takes (`_title`), and who made it and when under it, smaller,
        both in the same letters; beside the picture at the block's middle
        height, under it at its top; inset two cells all round."""
        x0, y0, w, h = region
        x0, y0, w, h = x0 + 2, y0 + 1, w - 4, h - 2
        if w < 4 or h < 2 or (self.work is None and not self.note):
            return None
        name, _series = self.work.series_of if self.work else ("", "")
        if self.note:
            name, whos = self.note, []
        elif self.work.date:
            # One line where it fits, the date under the artist where not:
            # never a dash left hanging at a line's end.
            whos = [f"{self.work.artist} - {self.work.date}",
                    f"{self.work.artist}\n{self.work.date}"] \
                + [f"{t}\n{self.work.date}" for t in breaks(self.work.artist.split(), 2)]
        else:
            whos = [self.work.artist] + breaks(self.work.artist.split(), 2)
        steps = SOLID if self.blocks_only else \
            [self.face] + [f for f in (letters.HALF, letters.DOTS) if f != self.face]
        beside = y0 <= 1
        # The title takes up to two thirds of the depth, and never more
        # than a third of the wall: a title the height of the wall is a
        # poster, not a label.
        t_h = max(2, min(h * 2 // 3, max(2, rows // 3)))
        for font in steps:
            # A title smaller, a row at a time, where the one that fits
            # leaves its credit too little room to be set at all.
            for t_lim in range(t_h, 2, -1):
                got = self._credited(name, whos, font, w, h, t_lim, rows)
                if got is None:
                    continue
                if got is False:
                    break
                title, credit = got
                tr = letters.size(**title, wall=self.wall)[1]
                cr = letters.size(**credit, wall=self.wall)[1] if credit else 0
                block = tr + (2 + cr if credit else 0)
                y = y0 + max(0, (h - block) // 2) if beside else y0
                out = [(title, x0, y, TITLE)]
                if credit:
                    out.append((credit, x0, y + tr + 2, INK))
                return out
        return None

    def _credited(self, name, whos, font, w, h, t_lim, rows):
        """The title no taller than `t_lim` and the credit under it, in
        `font` in a block w by h: (title, credit); None where the credit
        has no room under that title; False where the title cannot be set
        in this font at all."""
        title = self._title(name, font, w, t_lim, max(2, rows // TITLE_LINE))
        if title is None:
            return False
        tr = letters.size(**title, wall=self.wall)[1]
        # Who made it is smaller than a line of what it is: two thirds
        # of a title line's height, whatever lines either takes.
        line = tr // (title["text"].count("\n") + 1)
        room = h - tr - 2
        c_h = min(room, max(3, line * 2 // 3))
        # On one line, or broken where that sets it a good deal larger,
        # as the title is.
        credit = None
        for t in whos if c_h >= 2 else ():
            n = t.count("\n") + 1
            got = letters.fit(t, w, min(room, c_h * n), fonts=(font,),
                              wall=self.wall, edges="cut")
            if got and (credit is None or got["height"] * 4
                        >= credit["height"] * 5 * n):
                credit = got
        if whos and credit is None:
            return None
        return title, credit

    def _fill_label(self, out, region):
        """The label in its block, as `_fill_text` sets it, and nothing
        outside the block where it cannot be set."""
        for got, x, y, colour in self._fill_text(region, len(out)) or ():
            letters.draw(out, x=x, y=y, fg=colour, wall=self.wall, **got)

    def _title(self, name, font, w, h, line_most):
        """The title in `font` at the largest that fits w by h, a line of
        it never taller than `line_most` rows: on one line, or broken
        between words onto as many as `TITLE_LINES` where each more is a
        good deal larger -- a quarter taller at least. A long title on one
        line is a thread across the black, and a short one as tall as it
        can be outweighs the picture, so the depth goes to lines of a size
        a label is. Of the places to break it, the one that sets it
        largest, the evener at a tie."""
        def fit(text, lines):
            return letters.fit(text, w, min(h, lines * line_most), fonts=(font,),
                               wall=self.wall, edges="cut")
        def each(got):
            return got["height"] / (got["text"].count("\n") + 1)
        words = name.split()
        best = None
        for lines in range(1, min(TITLE_LINES, len(words)) + 1):
            got = None
            for text in breaks(words, lines):
                one = fit(text, lines)
                if one is not None and (got is None or one["height"] > got["height"]):
                    got = one
            if got is not None and (best is None or each(got) * 4 >= each(best) * 5):
                best = got
        return best

    def _choose(self, texts, width, height):
        """One set of letters for every line of a label: the largest that
        holds each of `texts` whole in width by height, trying the label's
        own face first, then the half-block letters, then braille dots,
        each finer than the last and so holding more, as `letters.fit`
        judges them for this wall. A label in two faces reads as two
        labels, so the lines share the face and the height, the smallest
        any of them needs. None where no face holds them all."""
        texts = [t for t in texts if t]
        if not texts or width <= 0 or height <= 0:
            return None
        # The paintings and prints keep solid letters, the block cells and
        # then the same letters in half cells.
        steps = SOLID if self.blocks_only else \
            [self.face] + [f for f in (letters.HALF, letters.DOTS) if f != self.face]
        for font in steps:
            got = [letters.fit(t, width, height, fonts=(font,), wall=self.wall,
                               edges="cut") for t in texts]
            if all(got):
                least = min(g["height"] for g in got)
                return {k: v for k, v in got[0].items() if k != "text"} \
                    | {"height": least, "wall": self.wall}
        return None

    def _set(self, out, text, x, y, width, height, colour, chosen, right=False):
        """`text` in the label's chosen letters, or cut in the face where
        none were chosen; against the box's right edge where asked."""
        if chosen is None:
            line = ink(cut(text, fits_in(width, self.painter.cell_ratio,
                                         font=self.face)),
                       self.painter.cell_ratio, font=self.face)
            wide = len(line[0]) if line and line[0] else 0
            at = x + width - wide if right else x
            for n, row in enumerate(line):
                if y + n >= len(out):
                    break
                for dx, ch in enumerate(row):
                    if ch is not None and 0 <= at + dx < len(out[y + n]):
                        out[y + n][at + dx] = inked(ch, colour, out[y + n][at + dx])
            return
        wide, tall_ = letters.size(text, **chosen)
        at = x + width - wide if right else x
        # Set down to sit on the line where it is shorter than the box.
        letters.draw(out, text, x=at, y=y + (height - tall_), fg=colour, **chosen)

    def _card(self, out, tall, cols):
        """One drawn line: who made it at one end, what it is at the other.

        The gallery card, and the layout that costs the picture least --
        six rows rather than a column. Where the two would collide the
        title gives way, because the wall has just shown the picture and
        the name under it is the part that is not obvious.
        """
        pad = 2
        name = (self.work.series_of[0] if self.work else "")
        who = self.work.artist if self.work else ""
        if self.note:
            name = self.note
        half = (cols - pad * 2) // 2 - 3
        height = letter_height()
        chosen = self._choose((who, name), half, height)
        self._set(out, who, pad, tall + 1, half, height, INK, chosen)
        self._set(out, name, cols - pad - half, tall + 1, half, height, TITLE,
                  chosen, right=True)

    def _picture(self, out, plan, now):
        """The picture, or as much of it as the dissolve has revealed."""
        art, old = self.art, self.old
        part = 1.0
        if old is not None and self.order:
            part = (now - self.started) / DISSOLVE
            if part >= 1.0:
                self.old, self.order, old = None, (), None
        at, tall = plan["x"], plan["tall"]
        wide = len(art[0]) if art else 0
        for y in range(min(tall, len(art))):
            row = out[y]
            src = art[y]
            for x in range(min(len(row) - at, wide)):
                g = src[x]
                row[at + x] = cell(g.ch,
                                   "default" if g.fg is None else g.fg,
                                   "default" if g.bg is None else g.bg)
        if old is None or not self.order:
            return
        # Whatever the dissolve has not reached yet is still the old
        # picture. Walking the cells that have NOT turned is the smaller
        # half for most of the dissolve and the same work for all of it.
        keep = self.order[int(len(self.order) * min(1.0, max(0.0, part))):]
        for n in keep:
            y, x = divmod(n, wide)
            if y >= len(old) or x >= len(old[y]) or y >= tall:
                continue
            if not 0 <= at + x < len(out[y]):
                continue
            g = old[y][x]
            out[y][at + x] = cell(g.ch,
                                  "default" if g.fg is None else g.fg,
                                  "default" if g.bg is None else g.bg)

    def _beside(self, out, rows, at):
        """The label in the black beside the picture, wrapped to fit.

        The bar a portrait work leaves is far wider than a caption needs
        and costs the picture nothing, so the label goes there whole:
        title, then who and when, then the series a print came from if
        there is still room. Set vertically about a third of the way
        down, where a gallery hangs its card rather than centred, which
        reads as an accident.
        """
        x0, width = at
        pad = 2
        r = self.painter.cell_ratio
        face = self.face
        name, series = self.work.series_of if self.work else ("", "")
        who = self.work.artist if self.work else ""
        if self.work and self.work.date:
            who = f"{who} - {self.work.date}"
        if self.note:
            who = self.note

        def plan(scale):
            """The blocks at this size, the rows they take, and whether
            every word survived whole.

            A bigger letter is fewer letters to the line, so a word that
            fits the column at one size is broken across two lines at
            the next one up. The size is chosen here rather than where
            the layout was, so the check has to be made here too.
            """
            room = fits_in(width - pad * 2, r, scale, face)
            if self.note or not (self.work and self.work.date):
                whos = [who]
            else:
                small = fits_in(width - pad * 2, r, max(1, scale - 1), face)
                whos = credit(self.work.artist, self.work.date, small)
            texts = [(name, TITLE, scale)] + [(w, INK, max(1, scale - 1)) for w in whos]
            if series:
                texts.append((series, DIM, 1))
            whole = all(len(word) <= room
                        for text, _i, _s in texts
                        for word in text.split())
            out_ = [(wrap(text, room), colour, sc)
                    for text, colour, sc in texts]
            deep = sum(len(b) * (letter_height(sc) + sc) + 2
                       for b, _i, sc in out_)
            return out_, deep, whole

        # The biggest size that still leaves the column some air and
        # breaks no word. Trying each is cheaper than solving for it and
        # never off by one.
        blocks, deep, _whole = plan(1)
        for scale in (3, 2):
            try_blocks, try_deep, whole = plan(scale)
            if whole and try_deep <= rows - 4 and all(
                    len(b) <= 4 for b, _i, _s in try_blocks):
                blocks, deep = try_blocks, try_deep
                break
        while deep > rows - 4 and len(blocks) > 1:
            blocks.pop()                           # the series goes first
            deep = sum(len(b) * (letter_height(sc) + sc) + 2
                       for b, _i, sc in blocks)
        y = max(1, (rows - deep) // 3)
        for lines, colour, scale in blocks:
            for text in lines:
                for n, bits in enumerate(ink(text, r, scale, font=face)):
                    if y + n >= len(out):
                        return
                    row = out[y + n]
                    for x, ch in enumerate(bits):
                        if ch is not None and x0 + pad + x < len(row):
                            at = x0 + pad + x
                            row[at] = inked(ch, colour, row[at])
                y += letter_height(scale) + scale
            y += 2

    def _drawn(self, out, tall, cols):
        """The label drawn under the picture, a line each for the work and
        who made it, each in letters they share, the largest that hold
        both whole (`_choose`). Not wrapped:
        a title is a label, and a label that runs to two lines has stopped
        being one.
        """
        pad = 2
        name, _series = self.work.series_of if self.work else ("", "")
        who = self.work.artist if self.work else ""
        if self.work and self.work.date:
            who = f"{who} - {self.work.date}"
        if self.note:
            who = self.note
        # One line each here: the strip is as tall as it is and a title
        # that wrapped would push the second line off the wall. Both are in
        # one face and size, the largest that holds them whole.
        lines = (self.plan or {}).get("lines")
        if lines:
            # Block letters, wrapped whole, a line under the last.
            y = tall + 1
            for line, colour in lines:
                for n, bits in enumerate(ink(line, self.painter.cell_ratio)):
                    if y + n >= len(out):
                        break
                    for x, ch in enumerate(bits[:cols - pad]):
                        if ch is not None:
                            out[y + n][pad + x] = inked(ch, colour, out[y + n][pad + x])
                y += letter_height() + 1
            return
        height = letter_height()
        chosen = self._choose((name, who), cols - pad * 2, height)
        self._set(out, name, pad, tall + 1, cols - pad * 2, height, TITLE, chosen)
        self._set(out, who, pad, tall + 7, cols - pad * 2, height, INK, chosen)

    def _caption(self, out, tall, cols):
        """The label: what it is, who made it, and what it came out of."""
        if self._big(self.rows) and self._label(self.rows) == LABEL_BIG:
            return self._drawn(out, tall, cols)
        pad = 2 if cols > 8 else 0
        width = cols - pad * 2
        lines = []
        if self.work is not None:
            name, series = self.work.series_of
            lines.append((fit(name, width), TITLE))
            who = self.work.artist
            if self.work.date:
                who = f"{who}  ·  {self.work.date}"
            lines.append((fit(who, width), INK))
            lines.append((fit(series, width), DIM))
        if self.note:
            lines = (lines or [("", TITLE)] * 2)[:2] + [(fit(self.note,
                                                             width), DIM)]
        for n, (text, ink) in enumerate(lines[:LABEL - 1]):
            y = tall + 1 + n
            if y >= len(out):
                break
            for x, ch in enumerate(text):
                out[y][pad + x] = cell(ch, ink, WALL)


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def number(default):
    """An argparse type that never refuses: not a number means default."""
    def convert(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            say(f"picture: {text!r} is not a number, using {default:g}",
                file=sys.stderr, flush=True)
            return default
    return convert


def clamp(name, value, lo, hi):
    if value < lo or value > hi:
        say(f"picture: {name} {value:g} is outside {lo:g}..{hi:g}, "
            f"using {min(hi, max(lo, value)):g}", file=sys.stderr, flush=True)
    return min(hi, max(lo, value))


def parse(argv):
    ap = argparse.ArgumentParser(
        add_help=True, description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", help="the receiver to draw on")
    ap.add_argument("--category", default="art",
                    help=f"what kind of picture "
                         f"({'|'.join(art.CATEGORIES)}, default art)")
    ap.add_argument("--series", default="",
                    help="comma separated, instead of a category; "
                         "--list shows them")
    ap.add_argument("--list", action="store_true",
                    help="what there is to show, and why")
    ap.add_argument("--hold", type=number(DEFAULT_HOLD), default=DEFAULT_HOLD,
                    help=f"seconds a picture stays up "
                         f"(default {DEFAULT_HOLD:g}, 3..600)")
    ap.add_argument("--mode", default=DEFAULT_MODE,
                    help=f"how a cell is made of pixels "
                         f"({'|'.join(player.MODES)}, default {DEFAULT_MODE})")
    ap.add_argument("--punch", type=number(DEFAULT_PUNCH),
                    default=DEFAULT_PUNCH,
                    help=f"lift before the downscale, 0 leaves it alone "
                         f"(default {DEFAULT_PUNCH:g}, 0..3)")
    ap.add_argument("--font", type=number(DEFAULT_FONT), default=DEFAULT_FONT,
                    help=f"receiver font size (default {DEFAULT_FONT:g})")
    ap.add_argument("--layout", default="auto",
                    help="pin one arrangement (fill|bar|spine|card|strip) "
                         "instead of rotating through those that suit")
    ap.add_argument("--cell", type=number(2.0), default=None,
                    help="how many times taller than wide a cell is on "
                         "the receiver (0.5..8; default what the wall says "
                         "of its cells, else 2)")
    ap.add_argument("--seed", default=None, help="fix which works come up")
    a = ap.parse_args(argv)

    a.hold = clamp("--hold", a.hold, 3.0, 600.0)
    a.punch = clamp("--punch", a.punch, 0.0, 3.0)
    a.font = int(clamp("--font", a.font, 4, 64))
    if a.cell is not None:
        a.cell = clamp("--cell", a.cell, 0.5, 8.0)
    if a.layout not in ("auto", "fill", "bar", "spine", "card", "strip"):
        say(f"picture: no layout {a.layout!r}, rotating instead",
            file=sys.stderr, flush=True)
        a.layout = "auto"
    if a.mode not in player.MODES:
        say(f"picture: no mode {a.mode!r}, using {DEFAULT_MODE}",
            file=sys.stderr, flush=True)
        a.mode = DEFAULT_MODE
    named = [n.strip() for n in a.series.split(",") if n.strip()]
    for n in named:
        if n not in art.SERIES:
            say(f"picture: no series {n!r}, dropped", file=sys.stderr,
                flush=True)
    if a.category not in art.CATEGORIES:
        say(f"picture: no category {a.category!r}, showing the art",
            file=sys.stderr, flush=True)
        a.category = "art"
    a.names = [n for n in named if n in art.SERIES] \
        or list(art.CATEGORIES[a.category][0])
    return a


def take_layer(wall, show):
    """A video layer above the base for the pictures, if the wall gives
    one: the Wall's own, so it says the sender's instance. A wall that
    takes no video, or has no layer free, leaves the pictures to kitty or
    cells, and says so once."""
    until = time.monotonic() + 30
    while not wall.talking and time.monotonic() < until:
        time.sleep(0.2)
    try:
        layer = wall.layer(place="above")
    except Exception as e:                                  # noqa: BLE001
        say(f"no layer for the pictures: {e}", flush=True)
        return
    if not layer.open_now:
        say("the wall gave no layer for the pictures", flush=True)
        layer.close()
        return
    layer.meta(name="picture")
    while layer.caps() is None and layer.open_now and time.monotonic() < until:
        time.sleep(0.1)
    say(f"layer {layer.z} for the pictures; the wall draws "
        + ", ".join(c for c in ("jpeg", "png", "qoi") if layer.takes(c)), flush=True)
    show.layer = layer


def main():
    a = parse(sys.argv[1:])
    if a.list:
        print(art.listing())
        return 0
    if not a.host:
        print("usage: senders/picture/main.py [options] HOST", file=sys.stderr)
        return 2

    rng = random.Random(a.seed)
    gallery = art.Gallery(a.names, seed=a.seed)
    show = Show(gallery, Painter(a.mode, a.punch, a.cell or 2.0), a.hold, rng,
                a.layout)
    show.cell_said = a.cell is not None

    wall = bitwall.attach(a.host, min_frame=1.0 / FPS)
    with wall.transition():
        wall.set_font_size(a.font)
        cols, rows = wall.area()
        show.wall = wall
        wall.set_source(show.frame)
    wall.meta(name="picture")
    threading.Thread(target=take_layer, args=(wall, show), daemon=True).start()
    tw, th = show.painter.tile
    say(f"picture -> {a.host}  {cols}x{rows} at font {a.font}  "
        f"framebuffer {cols * tw}x{(rows - show._label(rows)) * th}  "
        f"mode {a.mode}  cell {show.painter.cell_ratio:.2f}:1  layout {a.layout}  "
        f"{'+'.join(a.names)}  "
        f"hold {a.hold:g}s  "
        f"(ctrl-c to stop)", flush=True)

    stopping = {"now": False}

    def halt(*_):
        stopping["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halt)
    try:
        while not stopping["now"]:
            time.sleep(0.5)
    finally:
        print()
        say(f"{show.shown} pictures, {gallery.fetched} fetched, "
            f"{wall.bytes_sent / 1024:.0f} KiB sent", flush=True)
        # A kitty image is the terminal's, not the cells': it stays after
        # the sender unless taken down, so it goes before the wall does.
        show.leaving = True
        show.unplace()
        if show.layer is not None:
            show.layer.close()
        time.sleep(0.3)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
