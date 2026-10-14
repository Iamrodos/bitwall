"""Reading ANSI art, which is not the same as emulating a terminal.

An `.ans` file is escape sequences and CP437 bytes, and a terminal emulator
will read most of one correctly -- but not all of one, and the differences
are exactly where art lives. A terminal defers its wrap: write in the last
column and the cursor stays there until the next character arrives, so a
newline after a full line costs one row. DOS wrapped immediately, so the
same file costs two. Terminals scroll, clear, and answer queries; art does
none of those and wants none of them.

So this reads the art dialect rather than borrowing an emulator: the
sequences a piece actually uses, the wrap rule it was drawn against, and
nothing else. What comes out is a grid of cells.

Checked against the renders at 16colo.rs, which are the reference the
scene itself uses -- `verify.py` compares a parse against those pixel by
pixel, so a change here is answerable rather than a matter of opinion.
"""

#: The sixteen colours of a DOS text mode, in hardware order. Not an
#: approximation of the art's palette: it is the palette, because a piece
#: was drawn by choosing from exactly these on exactly this hardware.
VGA = ("000000", "aa0000", "00aa00", "aa5500", "0000aa", "aa00aa",
       "00aaaa", "aaaaaa", "555555", "ff5555", "55ff55", "ffff55",
       "5555ff", "ff55ff", "55ffff", "ffffff")

#: CP437's first thirty-two code points are graphics on a PC and controls
#: everywhere else. A file saying 0x03 means a heart, not end-of-text.
#: The five kept as controls are the ones an ANSI stream really uses.
LOW = "".join(chr(c) for c in (
    0x0000, 0x263a, 0x263b, 0x2665, 0x2666, 0x2663, 0x2660, 0x2022,
    0x25d8, 0x25cb, 0x25d9, 0x2642, 0x2640, 0x266a, 0x266b, 0x263c,
    0x25ba, 0x25c4, 0x2195, 0x203c, 0x00b6, 0x00a7, 0x25ac, 0x21a8,
    0x2191, 0x2193, 0x2192, 0x2190, 0x221f, 0x2194, 0x25b2, 0x25bc))
KEEP = (0x09, 0x0a, 0x0d, 0x1a, 0x1b)

#: A blank cell: a space in the default attributes, which is what a piece
#: is drawn onto and what an untouched cell stays.
BLANK = (" ", 7, 0, False)

SAUCE = b"SAUCE00"
COMNT = b"COMNT"


def metadata(raw):
    """(art bytes, width, height, ice), reading what the file says of itself.

    SAUCE is 128 bytes at the end, added to the format in 1994, and a
    COMNT block of 64-byte lines may sit before it; both are text and both
    would paint. The width matters more than it looks -- reading a 200
    column piece at 80 does not crop it, it wraps every line -- and `ice`
    is the convention that the blink bit means a bright background rather
    than blinking, which is why nothing here flashes.
    """
    width, height, ice = 80, 0, False
    if raw[-128:][:7] == SAUCE:
        body, sauce = raw[:-128], raw[-128:]
        told_w = sauce[96] | (sauce[97] << 8)
        told_h = sauce[98] | (sauce[99] << 8)
        if 8 <= told_w <= 1024:
            width = told_w
        if 0 < told_h <= 20000:
            height = told_h
        ice = bool(sauce[105] & 0x01)
        comments = sauce[104]
        if comments:
            block = 5 + comments * 64
            if body[-block:][:5] == COMNT:
                body = body[:-block]
        raw = body
    cut = raw.find(b"\x1a")
    if cut >= 0:
        raw = raw[:cut]
    return raw, width, height, ice


def text_of(raw):
    """CP437 bytes as characters, with the low graphics kept as graphics."""
    out = []
    for b in raw:
        out.append(LOW[b] if b < 0x20 and b not in KEEP
                   else bytes([b]).decode("cp437"))
    return "".join(out)


class Art:
    """A piece being drawn, and the grid it is drawn onto.

    Rows appear as they are written to, so a piece is exactly as tall as it
    drew and no taller. Cells hold a palette index rather than a colour, so
    the whole thing is a few integers until the moment it is handed over.
    """

    def __init__(self, width, ice):
        self.w = width
        self.ice = ice
        self.rows = []
        self.x = self.y = 0
        self.saved = (0, 0)
        self.reset()

    def reset(self):
        self.fg, self.bg = 7, 0
        self.bold = self.blink = self.reverse = False

    def row(self, y):
        while len(self.rows) <= y:
            self.rows.append([BLANK] * self.w)
        return self.rows[y]

    def put(self, ch):
        """One character, at the cursor, and the cursor moves on.

        **The wrap is immediate**, which is the whole difference from a
        terminal: writing in the last column leaves the cursor at the start
        of the next row rather than pending in place. DOS did it this way
        and the art was drawn against DOS, so a line that fills the width
        and is then followed by a newline costs two rows -- which is what
        the artist saw and composed around.
        """
        fg = self.fg + (8 if self.bold else 0)
        bg = self.bg + (8 if (self.ice and self.blink) else 0)
        if self.reverse:
            fg, bg = bg, fg
        self.row(self.y)[self.x] = (ch, fg, bg, self.bold)
        self.x += 1
        if self.x >= self.w:
            self.x = 0
            self.y += 1

    def sgr(self, params):
        for p in params or [0]:
            if p == 0:
                self.reset()
            elif p == 1:
                self.bold = True
            elif p == 5:
                self.blink = True
            elif p == 7:
                self.reverse = True
            elif p == 22:
                self.bold = False
            elif p == 25:
                self.blink = False
            elif p == 27:
                self.reverse = False
            elif 30 <= p <= 37:
                self.fg = p - 30
            elif 40 <= p <= 47:
                self.bg = p - 40
            elif p == 39:
                self.fg = 7
            elif p == 49:
                self.bg = 0

    def move(self, final, params):
        """One cursor sequence. Moves clamp; they never wrap."""
        n = params[0] if params and params[0] else 1
        if final == "A":
            self.y = max(0, self.y - n)
        elif final == "B":
            self.y += n
        elif final == "C":
            self.x = min(self.w - 1, self.x + n)
        elif final == "D":
            self.x = max(0, self.x - n)
        elif final in ("H", "f"):
            self.y = max(0, (params[0] if params else 1) - 1)
            self.x = max(0, (params[1] if len(params or []) > 1 else 1) - 1)
            self.x = min(self.x, self.w - 1)
        elif final == "s":
            self.saved = (self.x, self.y)
        elif final == "u":
            self.x, self.y = self.saved
        elif final == "K":
            # To the end of the line, in the current background, which is
            # how a piece paints a bar of colour across a row.
            what = params[0] if params else 0
            row = self.row(self.y)
            first = 0 if what in (1, 2) else self.x
            last = self.w if what in (0, 2) else self.x + 1
            for x in range(first, min(last, self.w)):
                row[x] = (" ", self.fg, self.bg, False)


def parse(raw):
    """A file's bytes as (rows of cells, width).

    One pass, because an escape sequence is short and unambiguous: ESC [,
    then digits and semicolons, then the letter that says what it was.
    Anything else is a character to draw, including an escape that never
    became a sequence -- a file forty years old has a few.
    """
    raw, width, _, ice = metadata(raw)
    text = text_of(raw)
    art = Art(width, ice)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and (text[j].isdigit() or text[j] in ";?"):
                j += 1
            if j >= n:
                break                       # a sequence that never ends
            final = text[j]
            body = text[i + 2:j].lstrip("?")
            params = [int(p) if p else 0 for p in body.split(";") if p != ""]
            if final == "m":
                art.sgr(params)
            else:
                art.move(final, params)
            i = j + 1
            continue
        if ch == "\r":
            art.x = 0
        elif ch == "\n":
            art.y += 1
            art.x = 0
        elif ch == "\t":
            art.x = min(art.w - 1, (art.x // 8 + 1) * 8)
        else:
            art.put(ch)
        i += 1
    return art.rows, width


def cells(raw, cell):
    """The art as bitwall cells, given bitwall's `cell` function.

    The palette is resolved here rather than while parsing, so everything
    up to this point is integers and the colours are chosen once.
    """
    rows, width = parse(raw)
    out = [[cell(ch, VGA[fg], VGA[bg], bold=bold)
            for (ch, fg, bg, bold) in row] for row in rows]
    # Trim blank rows off the end, not the start: a piece that begins with
    # space meant to.
    while len(out) > 1 and all(c[0] == " " and c[2] == VGA[0]
                               for c in out[-1]):
        out.pop()
    return out, width
