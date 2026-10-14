"""A wall painted straight onto the display, in readable form.

The design `bitwall-paint.c` implements, written first in Python so the
geometry, the palette and the grammar could be argued about in something
legible. The C is what runs: this reaches about five frames a second on a
Pi 3 where the C reaches seventy, because two thousand runs a frame is two
thousand trips through the interpreter. Kept because the tables and the
rules are easier to read here, and because a small screen -- a handful of
runs a frame rather than thousands -- could be driven by it directly.

It takes the bytes a sender writes and puts them on the glass: nothing
here talks to a sender, owns a fifo or knows what bitwall is for, so it
can be driven from a test with a string and a fake clock.

**A wall is not a terminal.** What arrives is already cells -- absolutely
positioned runs, a colour, and characters -- because every sender composes
them, and even a live tmux session is decoded into cells by `TmuxSource`
before it leaves the sending machine. So the grammar to read is three
things: move the cursor, set a colour, draw a run. No scrollback, no
reflow, no selection, no wrapping, no mouse, no OSC. What a terminal
emulator spends its time on is precisely what a wall never asks for.

**Most cells are rectangles, not glyphs.** Block and half-block code
points are geometry, and `SHAPES` says where in the cell the ink goes as
fractions of it. Drawing those as fills means the font is never consulted
for the characters walls use most, and it means two receivers agree about
what a block looks like rather than deferring to whatever face each one
happened to load. The same table drives the web receiver.

**Colour is per run, so painting is per run.** A frame is a few thousand
runs rather than ten thousand cells, and a run of one colour is one fill.
That ratio is what makes this fast enough in Python: the work per frame is
proportional to how much of the picture changed shape, not to its area.

**Glyphs are cached by what they look like.** A cell that is a real
character is rendered once for each (character, ink, ground) it appears in
and kept, because walls draw the same few hundred combinations over and
over -- a dashboard has one palette and a film has almost no text at all.
"""
import re
import unicodedata

#: Where the ink goes inside a cell, as fractions of it: (x, y, w, h).
#: A character here is never asked of the font.
SHAPES = {
    0x2588: [(0, 0, 1, 1)],                      # full block
    0x2580: [(0, 0, 1, 0.5)],                    # upper half
    0x2584: [(0, 0.5, 1, 0.5)],                  # lower half
    0x258c: [(0, 0, 0.5, 1)],                    # left half
    0x2590: [(0.5, 0, 0.5, 1)],                  # right half
    0x2596: [(0, 0.5, 0.5, 0.5)],                # quadrants
    0x2597: [(0.5, 0.5, 0.5, 0.5)],
    0x2598: [(0, 0, 0.5, 0.5)],
    0x2599: [(0, 0, 0.5, 1), (0.5, 0.5, 0.5, 0.5)],
    0x259a: [(0, 0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)],
    0x259b: [(0, 0, 1, 0.5), (0, 0.5, 0.5, 0.5)],
    0x259c: [(0, 0, 1, 0.5), (0.5, 0.5, 0.5, 0.5)],
    0x259d: [(0.5, 0, 0.5, 0.5)],
    0x259e: [(0.5, 0, 0.5, 0.5), (0, 0.5, 0.5, 0.5)],
    0x259f: [(0.5, 0, 0.5, 1), (0, 0.5, 0.5, 0.5)],
    0x25aa: [(0.1, 0.3, 0.8, 0.4)],              # the leds emitter
}
for _i in range(1, 8):                           # eighths, up and across
    SHAPES[0x2580 + _i] = [(0, 1 - _i / 8, 1, _i / 8)]
    SHAPES[0x2588 + _i] = [(0, 0, (8 - _i) / 8, 1)]

#: The shapes that may be drawn as one wide fill across a run, because
#: every rectangle in them spans the cell from edge to edge. A shape that
#: does not -- a left half, a quadrant -- has to be drawn per cell or it
#: becomes one long bar instead of several short ones.
TILES = frozenset(code for code, rects in SHAPES.items()
                  if all(fx == 0 and fw == 1 for fx, _fy, fw, _fh in rects))

#: Shading is a blend rather than a shape: the ink at that strength over
#: the ground, which is what the character means and what a font would
#: draw as a dot pattern too fine to survive a small cell.
SHADES = {0x2591: 0.25, 0x2592: 0.5, 0x2593: 0.75}

#: The default colours, and what `default` in a cell resolves to.
INK = (204, 204, 204)
GROUND = (0, 0, 0)

#: The xterm 256-colour cube, built rather than tabulated.
def _palette():
    out = [(0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0),
           (0, 0, 170), (170, 0, 170), (0, 170, 170), (170, 170, 170),
           (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85),
           (85, 85, 255), (255, 85, 255), (85, 255, 255), (255, 255, 255)]
    steps = (0, 95, 135, 175, 215, 255)
    for r in steps:
        for g in steps:
            for b in steps:
                out.append((r, g, b))
    for i in range(24):
        v = 8 + i * 10
        out.append((v, v, v))
    return out


PALETTE = _palette()

#: One escape sequence: the parameters, then the letter that says what it
#: is. Everything a wall sends is CUP or SGR; anything else is skipped
#: rather than refused, because a sender is allowed to be tidier than we
#: are and a wall should not go dark over a cursor-hide.
TOKEN = re.compile(rb"\x1b\[([0-9;?]*)([A-Za-z])")

#: The colour pair a wall sends more than any other, matched whole.
PAIR = re.compile(rb"^38;5;(\d+);48;5;(\d+)$")

#: How many rendered cells to keep. A dashboard uses a few hundred
#: combinations and a film almost none, so this is generous; it exists so
#: that a wall which cycles through every colour cannot grow without end.
CACHE = 4096


class Pen:
    """The colours a run is drawn in, as the last SGR left them."""

    __slots__ = ("fg", "bg", "bold", "reverse", "ink")

    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = None          # None is the screen's own default
        self.bg = None
        self.bold = False
        self.reverse = False
        self.settle()

    def settle(self):
        """Work out the pair this pen paints, once per colour change.

        A run asks for its colours and there are far more runs than
        there are changes of colour, so the resolving of defaults and
        of reverse happens here rather than there.
        """
        fg = self.fg if self.fg is not None else INK
        bg = self.bg if self.bg is not None else GROUND
        self.ink = (bg, fg) if self.reverse else (fg, bg)

    def colours(self):
        """The (ink, ground) this pen paints, defaults resolved."""
        return self.ink

    def take(self, params):
        """Apply one SGR's parameters.

        Only what a wall sends: reset, bold, reverse, the 16 named
        colours, and the 256 and truecolor forms. A parameter that is
        not one of those leaves the pen alone, so an unknown attribute
        costs a colour rather than a frame.
        """
        n = 0
        while n < len(params):
            p = params[n]
            if p in (0, None):
                self.reset()
            elif p == 1:
                self.bold = True
            elif p == 7:
                self.reverse = True
            elif p == 22:
                self.bold = False
            elif p == 27:
                self.reverse = False
            elif p == 39:
                self.fg = None
            elif p == 49:
                self.bg = None
            elif 30 <= p <= 37:
                self.fg = PALETTE[p - 30 + (8 if self.bold else 0)]
            elif 40 <= p <= 47:
                self.bg = PALETTE[p - 40]
            elif 90 <= p <= 97:
                self.fg = PALETTE[p - 90 + 8]
            elif 100 <= p <= 107:
                self.bg = PALETTE[p - 100 + 8]
            elif p in (38, 48) and n + 1 < len(params):
                kind = params[n + 1]
                if kind == 5 and n + 2 < len(params):
                    c = PALETTE[params[n + 2] % 256]
                    n += 2
                elif kind == 2 and n + 4 < len(params):
                    c = (params[n + 2] % 256, params[n + 3] % 256,
                         params[n + 4] % 256)
                    n += 4
                else:
                    n += 1
                    continue
                if p == 38:
                    self.fg = c
                else:
                    self.bg = c
            n += 1
        self.settle()


def _params(raw):
    """An escape sequence's parameters as numbers, empty meaning zero."""
    out = []
    for bit in raw.split(b";"):
        if not bit:
            out.append(0)
        else:
            try:
                out.append(int(bit))
            except ValueError:
                out.append(0)
    return out


class Screen:
    """The display, and what is currently on it.

    `write(data)` is the whole surface a caller needs, plus `set_font` to
    change the grid and `blank` to take the picture away. Nothing here
    blocks on a sender or knows one exists.

    **A bare reset ends a frame.** `ESC[0m` with no parameters is the one
    mark a sender guarantees, and a frame larger than a pipe arrives in
    pieces -- so presenting on every chunk would show a band sweeping
    down the wall. Presenting on the delimiter shows whole frames.
    """

    def __init__(self, font_path, size=14, shape=None, driver="kmsdrm"):
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", driver)
        import pygame
        self.pygame = pygame
        pygame.init()
        pygame.font.init()
        pygame.mouse.set_visible(False)
        flags = pygame.FULLSCREEN if shape is None else 0
        self.surface = pygame.display.set_mode(shape or (0, 0), flags)
        self.width, self.height = self.surface.get_size()
        self.font_path = font_path
        self.pen = Pen()
        self.glyphs = {}
        self.dirty = []
        self.drew = False
        self.blanked = False
        self.x = self.y = 0
        self.frames = 0
        self.set_font(size)

    # -- the grid

    def set_font(self, size):
        """Take a new font size and report the grid it produces.

        The cell is the font's own advance and line height rather than
        anything chosen here, so the grid matches what the glyphs
        actually need and a caller is told the truth rather than a
        request.
        """
        self.size = max(4, min(400, int(size)))
        self.font = self.pygame.font.Font(self.font_path, self.size)
        self.cw = max(1, self.font.size("M")[0])
        self.ch = max(1, self.font.get_linesize())
        self.cols = max(1, self.width // self.cw)
        self.rows = max(1, self.height // self.ch)
        self.glyphs.clear()
        self.surface.fill(GROUND)
        self.dirty = [self.surface.get_rect()]
        self.drew = True
        return self.cols, self.rows

    @property
    def area(self):
        return self.cols, self.rows

    def blank(self, on):
        """Take the picture away, or put the last one back.

        Blanking paints black rather than stopping: a wall that simply
        stopped would hold whatever was last on it, which is the one
        thing a blanked screen must not do.
        """
        if on == self.blanked:
            return
        self.blanked = on
        if on:
            self.surface.fill(GROUND)
            self.dirty = [self.surface.get_rect()]
            self.present()

    # -- painting

    def _glyph(self, ch, fg, bg):
        """One cell's worth of character, rendered once and kept."""
        key = (ch, fg, bg)
        got = self.glyphs.get(key)
        if got is None:
            if len(self.glyphs) >= CACHE:
                self.glyphs.clear()
            got = self.font.render(ch, True, fg, bg)
            self.glyphs[key] = got
        return got

    def _shape(self, code, fg, x, y, cells=1):
        """A block character, as the rectangles it is made of.

        `cells` draws it that many times across in one fill each, which
        is only sound where the shape spans the whole cell width -- a
        full block or a horizontal half tiles into a longer bar, where a
        left half would become one wide bar instead of several narrow
        ones. `TILES` is the set that may be widened.
        """
        for fx, fy, fw, fh in SHAPES[code]:
            n = cells if code in TILES else 1
            self.surface.fill(fg, (x + int(fx * self.cw),
                                   y + int(fy * self.ch),
                                   max(1, int(fw * self.cw) * n),
                                   max(1, int(fh * self.ch))))
            if n == 1 and cells > 1:
                for k in range(1, cells):
                    self.surface.fill(fg, (x + k * self.cw + int(fx * self.cw),
                                           y + int(fy * self.ch),
                                           max(1, int(fw * self.cw)),
                                           max(1, int(fh * self.ch))))

    def _shade(self, code, fg, bg, x, y):
        """A shaded character, as ink mixed into ground at its strength."""
        t = SHADES[code]
        mix = tuple(int(b + (f - b) * t) for f, b in zip(fg, bg))
        self.surface.fill(mix, (x, y, self.cw, self.ch))

    def run(self, text):
        """Draw `text` at the cursor, and move the cursor past it."""
        if self.blanked or not text:
            self.x += len(text)
            return
        fg, bg = self.pen.colours()
        x0 = self.x * self.cw
        y0 = self.y * self.ch
        if self.y >= self.rows:
            self.x += len(text)
            return
        room = max(0, self.cols - self.x)
        text = text[:room]
        if not text:
            return
        span = (x0, y0, len(text) * self.cw, self.ch)
        self.surface.fill(bg, span)          # the run's ground, in one fill
        one = text[0]
        if len(text) > 1 and text.count(one) == len(text) and ord(one) in SHAPES:
            # A run of one block character is the same rectangles drawn
            # the width of the run rather than once per cell, which is
            # the difference between a fill a cell and a fill a run.
            self._shape(ord(one), fg, x0, y0, len(text))
        else:
            for n, ch in enumerate(text):
                code = ord(ch)
                if ch == " ":
                    continue                  # the ground is already there
                x = x0 + n * self.cw
                if code in SHAPES:
                    self._shape(code, fg, x, y0)
                elif code in SHADES:
                    self._shade(code, fg, bg, x, y0)
                else:
                    self.surface.blit(self._glyph(ch, fg, bg), (x, y0))
        self.dirty.append(span)
        self.drew = True
        self.x += len(text)

    def present(self):
        """Put what has been drawn on the glass."""
        if not self.dirty:
            return
        self.pygame.display.update(self.dirty)
        self.dirty = []
        self.frames += 1

    # -- reading

    def write(self, data):
        """Paint everything in `data`, presenting at each frame's end."""
        at = 0
        for m in TOKEN.finditer(data):
            if m.start() > at:
                self.run(data[at:m.start()].decode("utf-8", "replace"))
            at = m.end()
            raw, kind = m.group(1), m.group(2)
            if kind == b"m":
                quick = PAIR.match(raw)
                if quick is not None:
                    # `38;5;N;48;5;M`, which is almost every colour a
                    # wall sends: read straight rather than split into a
                    # list and walked.
                    pen = self.pen
                    pen.fg = PALETTE[int(quick.group(1)) % 256]
                    pen.bg = PALETTE[int(quick.group(2)) % 256]
                    pen.settle()
                elif not raw:
                    # The frame delimiter, and a reset of the pen.
                    self.pen.reset()
                    if self.drew:
                        self.present()
                        self.drew = False
                else:
                    self.pen.take(_params(raw))
            elif kind == b"H" or kind == b"f":
                p = _params(raw) if raw else [1, 1]
                while len(p) < 2:
                    p.append(1)
                self.y = max(0, p[0] - 1)
                self.x = max(0, p[1] - 1)
            elif kind == b"J":
                self.surface.fill(GROUND)
                self.dirty = [self.surface.get_rect()]
                self.drew = True
            # Everything else -- cursor hide, erase-line, private modes --
            # is a tidiness a wall does not need and is stepped over.
        if len(data) > at:
            self.run(data[at:].decode("utf-8", "replace"))

    def close(self):
        self.pygame.quit()
