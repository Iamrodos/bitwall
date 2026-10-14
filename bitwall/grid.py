"""The grid a stream makes, held by a receiver that has no screen of its own.

The web receiver hands a sender's bytes to a tab and paints nothing, so
without this only the tab would know what its wall shows. A
controller asking for a `picture` or a `mirror` should not have to go
through a tab to get it, and there may be no tab open at all. So the
server reads the stream into a grid of its own, exactly as the page's
parser reads it -- `bitwall-screen.js`, `Parser` -- and says the grid back
as the painter does.

A bitwall stream is three things: a reset, a cursor move, and colour runs
(README.md in receivers/web). That, not a terminal, is all this parses,
which is what keeps it affordable: the receiver this one replaced ran a
terminal emulator per wall and managed a tenth of the frame rate.
"""
import re

#: Attribute bits, as the page has them.
BOLD, ITALIC, UNDER, STRIKE, REVERSE, FAINT = 1, 2, 4, 8, 16, 32
#: A colour the sender left as the terminal's own.
DEFAULT = -1
#: What the page paints a default in: `INK` and `PAPER` in bitwall-screen.js.
INK, PAPER = 0xd0d0d0, 0x000000
#: The trailing half of a wide character, which the character before it
#: covers. Never written by a sender; a picture skips it.
TAIL = ""


def _palette():
    p = [0x000000, 0xcd0000, 0x00cd00, 0xcdcd00, 0x0000ee, 0xcd00cd,
         0x00cdcd, 0xe5e5e5, 0x7f7f7f, 0xff0000, 0x00ff00, 0xffff00,
         0x5c5cff, 0xff00ff, 0x00ffff, 0xffffff]
    steps = [0, 95, 135, 175, 215, 255]
    p += [(r << 16) | (g << 8) | b for r in steps for g in steps for b in steps]
    p += [(v << 16) | (v << 8) | v for v in (8 + i * 10 for i in range(24))]
    return p


PALETTE = _palette()


def wide(cp):
    """Double width: the sender writes one cell and skips the next. The
    page's ranges, so both step the cursor alike."""
    return (0x1100 <= cp <= 0x115f or (0x2e80 <= cp <= 0xa4cf and cp != 0x303f)
            or 0xac00 <= cp <= 0xd7a3 or 0xf900 <= cp <= 0xfaff
            or 0xfe30 <= cp <= 0xfe6f or 0xff00 <= cp <= 0xff60
            or 0xffe0 <= cp <= 0xffe6 or 0x1f300 <= cp <= 0x1f64f
            or 0x1f900 <= cp <= 0x1f9ff)


#: A stream as tokens: a CSI with its parameters and final byte, a run of
#: printable text, or anything else, which is dropped as the page drops it.
_TOKEN = re.compile(r"\x1b\[([0-9;:<=>?]*)[ -/]*([@-~])|([^\x00-\x1f\x7f]+)|\x1b[^\[]?|[\x00-\x1f\x7f]")
#: An escape that has all arrived: a whole CSI, or ESC and one more.
_WHOLE = re.compile(r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]|[^\[])")
_WIDE_ANY = re.compile("[ᄀ-ᅟ⺀-꓏가-힣豈-﫿"
                       "︰-﹯＀-｠￠-￦\U0001f300-\U0001f64f"
                       "\U0001f900-\U0001f9ff]")


class Grid:
    """Cells as rows of parallel lists -- character, ink, ground and
    attributes -- so a run of text lands as one slice and an unchanged row
    is told from a changed one by one comparison."""

    def __init__(self, cols, rows):
        self.resize(cols, rows)
        self.rest = ""
        self.x = self.y = 0
        self.fg = self.bg = DEFAULT
        self.at = 0

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.ch = [[" "] * cols for _ in range(rows)]
        self.fgs = [[DEFAULT] * cols for _ in range(rows)]
        self.bgs = [[DEFAULT] * cols for _ in range(rows)]
        self.ats = [[0] * cols for _ in range(rows)]

    def feed(self, text):
        """Read decoded text into the grid. A sequence cut off at the end is
        kept for the next piece, as the page keeps it."""
        text = self.rest + text
        self.rest = ""
        cut = text.rfind("\x1b", max(0, len(text) - 64))
        if cut >= 0 and not _WHOLE.match(text, cut):
            self.rest, text = text[cut:], text[:cut]
        cols, rows = self.cols, self.rows
        for m in _TOKEN.finditer(text):
            run = m.group(3)
            if run is not None:
                y, x = self.y, self.x
                if not 0 <= y < rows or x >= cols:
                    continue
                if run.isascii():
                    n = min(len(run), cols - x)
                    self.ch[y][x:x + n] = run[:n]
                    self.fgs[y][x:x + n] = [self.fg] * n
                    self.bgs[y][x:x + n] = [self.bg] * n
                    self.ats[y][x:x + n] = [self.at] * n
                    self.x = x + len(run)
                    continue
                chr_, fgr, bgr, atr = self.ch[y], self.fgs[y], self.bgs[y], self.ats[y]
                fg, bg, at = self.fg, self.bg, self.at
                if not _WIDE_ANY.search(run):
                    n = min(len(run), cols - x)
                    chr_[x:x + n] = run[:n]
                    fgr[x:x + n] = [fg] * n
                    bgr[x:x + n] = [bg] * n
                    atr[x:x + n] = [at] * n
                    self.x = x + len(run)
                    continue
                for c in run:
                    if x >= cols:
                        break
                    chr_[x], fgr[x], bgr[x], atr[x] = c, fg, bg, at
                    if wide(ord(c)):
                        if x + 1 < cols:
                            chr_[x + 1] = TAIL
                        x += 2
                    else:
                        x += 1
                self.x = x
                continue
            final = m.group(2)
            if final == "m":
                self._sgr(m.group(1))
            elif final == "J":
                self.wipe()
            elif final == "H":
                a = m.group(1).split(";")
                self.y = (int(a[0]) if a[0].isdigit() and int(a[0]) else 1) - 1
                self.x = (int(a[1]) if len(a) > 1 and a[1].isdigit() and int(a[1]) else 1) - 1

    def _sgr(self, params):
        ps = params.split(";") if params else ["0"]
        i, n = 0, len(ps)
        fg, bg, at = self.fg, self.bg, self.at
        while i < n:
            p = ps[i]
            v = int(p) if p.isdigit() else 0
            if v == 38 or v == 48:
                mode = ps[i + 1] if i + 1 < n else ""
                col = DEFAULT
                if mode == "2" and i + 4 < n:
                    col = ((_int(ps[i + 2]) & 255) << 16) | ((_int(ps[i + 3]) & 255) << 8) \
                        | (_int(ps[i + 4]) & 255)
                    i += 4
                elif mode == "5" and i + 2 < n:
                    col = PALETTE[_int(ps[i + 2]) & 255]
                    i += 2
                elif mode == "6" and i + 5 < n:
                    # A colour with an alpha, as a cells layer above the
                    # base may send it: held with the alpha above the
                    # colour's bits, and opaque as the colour alone.
                    col = ((_int(ps[i + 2]) & 255) << 16) | ((_int(ps[i + 3]) & 255) << 8) \
                        | (_int(ps[i + 4]) & 255)
                    a = _int(ps[i + 5]) & 255
                    if a != 255:
                        col |= (a + 1) << 24
                    i += 5
                else:
                    i += 1
                if v == 38:
                    fg = col
                else:
                    bg = col
            elif v == 0:
                fg = bg = DEFAULT
                at = 0
            elif 30 <= v <= 37:
                fg = PALETTE[v - 30]
            elif 40 <= v <= 47:
                bg = PALETTE[v - 40]
            elif 90 <= v <= 97:
                fg = PALETTE[v - 82]
            elif 100 <= v <= 107:
                bg = PALETTE[v - 92]
            elif v == 39:
                fg = DEFAULT
            elif v == 49:
                bg = DEFAULT
            elif v in _SET:
                at |= _SET[v]
            elif v in _CLEAR:
                at &= ~_CLEAR[v]
            i += 1
        self.fg, self.bg, self.at = fg, bg, at

    # ------------------------------------------------------------ saying

    def wipe(self):
        """`ESC[2J`: what the page ignores on the base, which keeps its
        grid; a layer's grid is taken back to nothing drawn."""

    def looks(self, y):
        """Row `y` as what the glass shows: each cell's character, ink and
        ground, with reverse and defaults resolved as the page resolves
        them, and whether it is braille showing its unlit dots."""
        out = []
        for c, f, b, a in zip(self.ch[y], self.fgs[y], self.bgs[y], self.ats[y]):
            ink = INK if f == DEFAULT else f & 0xffffff
            ground = PAPER if b == DEFAULT else b & 0xffffff
            if a & REVERSE:
                ink, ground = ground, ink
            out.append((c, ink, ground,
                        bool(a & FAINT) and c != TAIL and 0x2800 <= ord(c) <= 0x28FF))
        return out

    def picture(self, was=None):
        """The grid as a sender would write it, as the painter's
        grid_picture() does: each row placed with a cursor move, colour set
        as truecolour only where it changes, ending with the bare reset.
        With `was` -- rows as looks() gave them last time -- only the cells
        that differ, each placed where it does not follow the last. Returns
        (bytes, rows now), and bytes is empty when nothing changed."""
        parts = []
        ink = ground = None
        dim = False
        now = []
        for y in range(self.rows):
            row = self.looks(y)
            now.append(row)
            before = was[y] if was is not None and y < len(was) else None
            if before == row:
                continue
            at = -1
            for x, (c, fi, gr, fa) in enumerate(row):
                if c == TAIL or (before is not None and before[x] == (c, fi, gr, fa)):
                    continue
                if x != at:
                    parts.append(f"\x1b[{y + 1};{x + 1}H")
                if fa != dim:
                    parts.append("\x1b[2m" if fa else "\x1b[22m")
                    dim = fa
                if fi != ink:
                    parts.append(f"\x1b[38;2;{fi >> 16};{(fi >> 8) & 255};{fi & 255}m")
                    ink = fi
                if gr != ground:
                    parts.append(f"\x1b[48;2;{gr >> 16};{(gr >> 8) & 255};{gr & 255}m")
                    ground = gr
                parts.append(c)
                at = x + (2 if x + 1 < self.cols and row[x + 1][0] == TAIL else 1)
        if not parts:
            return b"", now
        parts.append("\x1b[0m")
        return "".join(parts).encode(), now


def _colour(v, base):
    """One colour of a layer's cell as a sender writes it: truecolour where
    opaque, with its alpha (`38;6`, `48;6`) where not."""
    rgb = v & 0xffffff
    r, g, b = rgb >> 16, (rgb >> 8) & 255, rgb & 255
    if v >> 24:
        return f"\x1b[{base + 8};6;{r};{g};{b};{(v >> 24) - 1}m"
    return f"\x1b[{base + 8};2;{r};{g};{b}m"


class LayerGrid(Grid):
    """A cells layer's grid above the base: every cell it has not drawn is
    transparent, a colour may carry an alpha, and a ground left the default
    is none. Said back as the painter's lgrid_picture() says one: each
    drawn cell placed and coloured, alpha as `38;6`/`48;6`, no ground as
    `49`, and every cell not drawn left out."""

    def resize(self, cols, rows):
        super().resize(cols, rows)
        self.ch = [[None] * cols for _ in range(rows)]

    def wipe(self):
        self.ch = [[None] * self.cols for _ in range(self.rows)]

    def picture(self, was=None):
        parts = []
        ink = ground = None
        dim = False
        for y in range(self.rows):
            at = -1
            for x in range(self.cols):
                c = self.ch[y][x]
                if c is None or c == TAIL:
                    continue
                f, b, a = self.fgs[y][x], self.bgs[y][x], self.ats[y][x]
                ink_v, ground_v = INK if f == DEFAULT else f, None if b == DEFAULT else b
                if a & REVERSE:
                    # Reversed, the ink is the ground, which may be none.
                    ink_v, ground_v = ground_v if ground_v is not None else 1 << 24, ink_v
                fi = _colour(ink_v, 30)
                gr = "\x1b[49m" if ground_v is None else _colour(ground_v, 40)
                if x != at:
                    parts.append(f"\x1b[{y + 1};{x + 1}H")
                if fi != ink:
                    parts.append(fi)
                    ink = fi
                if gr != ground:
                    parts.append(gr)
                    ground = gr
                fa = bool(a & FAINT) and 0x2800 <= ord(c) <= 0x28FF
                if fa != dim:
                    parts.append("\x1b[2m" if fa else "\x1b[22m")
                    dim = fa
                parts.append(c)
                at = x + (2 if x + 1 < self.cols and self.ch[y][x + 1] == TAIL else 1)
        parts.append("\x1b[0m")
        return "".join(parts).encode(), None


_SET = {1: BOLD, 2: FAINT, 3: ITALIC, 4: UNDER, 7: REVERSE, 9: STRIKE}
_CLEAR = {22: BOLD | FAINT, 23: ITALIC, 24: UNDER, 27: REVERSE, 29: STRIKE}


def _int(p):
    return int(p) if p.isdigit() else 0
