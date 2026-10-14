"""Big text drawn from cells, in five fonts and their variants.

    from bitwall import letters

    letters.draw(grid, "12:30", 2, 1, "ffd75f", font=letters.SEGMENT,
                 height=9, smooth=True, depth=w.depth)
    got = letters.fit("Hello there", cols, rows, fonts=(letters.DOTS,
                      letters.HALF, letters.BLOCK), wall=w)
    if got:
        letters.draw(grid, x=0, y=0, fg="ffffff", **got)
    letters.centred(grid, "ON AIR", "ff5f5f", wall=w)

A receiver's own type is one size, so anything meant to be read across a
room is drawn: each letter is a picture made of cells. Four ways of making
that picture, each a trade between what the wall must be able to draw and
how good the letter looks:

    BLOCK        3x5 bitmap, one pixel a cell      needs nothing but spaces
    HALF         the same 3x5, two pixels a cell   needs ▀ and ▄
    DOTS         5x7 letters as strokes on the
                 braille dot grid, 2x4 a cell      needs U+2800-28FF
    SEGMENT      a 14-segment display, as shapes   needs ▀ and ▄
    SEGMENT_CUT  SEGMENT with edges="cut"          needs ▀ ▄ and ◢◣◤◥
    SEGMENT7     a 7-segment display, the clock's  needs ▀ and ▄
    SEGMENT7_CUT SEGMENT7 with edges="cut"         needs ▀ ▄ and ◢◣◤◥
    TILE         the 3x5 bitmap as a panel of lit  needs a wall offering
                 tiles, a kitty picture over its   kitty, and Pillow
                 cells, any height from one row
    WHITE_RABBIT outline fonts for headings, drawn  the same
    BYTES256     as a picture too: White Rabbit,
    VT323        256 Bytes, and the VT320's own

BLOCK and HALF have capitals, digits and common marks; DOTS has lower case
as well; SEGMENT has capitals, digits and a few marks, a digit lit on its
seven outer bars as a 14-segment display shows numbers, and all fourteen
behind every character; SEGMENT7 has digits and the letters seven bars can
make, in the case they make them in, with only its seven bars behind. `plain` says what a font
will draw of any text. `usable(font, w)` says whether a wall can draw the
font, and `fit` skips what it cannot.

**The options, the same for every function and every font:**

    scale     whole steps of the font's smallest size (default 1)
    height    rows to fill instead: a shape font fills them exactly, a
              bitmap one takes the biggest whole step that fits
    spacing   "proportional" (an i narrow, a w wide) or "mono"; digits are
              one width in both, so a changing number keeps its place. The
              faces keep their own spacing, as their files set it
    turn      None, "up" (a quarter turn, reading bottom to top) or "down"
    align     "left", "centre" or "right", for text of several lines
    smooth    anti-alias a shape font by how much of each pixel it
              covers; only where `depth` is "true" or "256" and the
              colours are hex, crisp otherwise; bitmaps are always crisp
    depth     the wall's colour depth, `w.depth` (default "true")
    cell      BLOCK and HALF: how much taller than wide a cell is, so a
              pixel is widened until a letter is letter-shaped (default 2)
    ghost     SEGMENT and SEGMENT7: a colour for the unlit segments, as on
              a real display. DOTS: a colour for the unlit dots, shown by the
              receiver (faint braille, PROTOCOL.md): with mono spacing a
              box of them to each letter and black between, like a
              character LCD, otherwise one field behind each line. None
              for neither
    edges     BLOCK and the segment fonts, drawn crisp: "square",
              hard cell edges, or "cut", where a slope passes through a
              cell it is drawn with a diagonal half ◢◣◤◥, so a round
              letter loses its staircase and a segment its steps. The
              _CUT fonts default to "cut", the others to "square".
              HALF is always square: its edges fall on half cells
    wall      the wall it is for: what it can draw, its colour depth and
              the size of its cells in pixels (`resolution`), so the
              letters suit the resolution there is. Anything given
              outright stands over what the wall says
    px        a cell's size in screen pixels, (wide, tall), where there
              is no wall to ask or to say otherwise

A line break in the text starts a new line. `height`, like the rest, is
the rows the whole thing takes, so a turned word given a height is as long
as that.

**What `rows` returns** is the picture as rows of cells, each None where
nothing is drawn or `(ch, fg, bg)`, where a colour of None means whatever
is already there, with a fourth field, True, on braille that shows its
unlit dots. With no `bg`, the text is transparent: a half-block pixel
lies over the grid it is drawn on. `draw` places those rows into a grid of
`bitwall.cell`s; a caller wanting only a mask can take "not None" as ink.

Everything drawn is cached, the finished rows by their whole description
and SEGMENT's shapes by size, so a sender asking for the same text every
frame pays for it once.
"""
import functools
import math
import unicodedata
from collections import OrderedDict

from . import stream as _stream
from .cells import cell as _cell

BLOCK = "block"
HALF = "half"
DOTS = "dots"
SEGMENT = "segment"
#: SEGMENT with `edges="cut"` as its own default: the same glyphs, with
#: its slopes edged in the diagonal halves ◢◣◤◥.
SEGMENT_CUT = "segment_cut"
#: A seven-segment display, the clock's: digits and the letters seven
#: bars can make, and only those seven bars behind them.
SEGMENT7 = "segment7"
SEGMENT7_CUT = "segment7_cut"
#: BLOCK's 3x5 bitmap as a panel of lit tiles, drawn as a kitty picture
#: over the cells it covers rather than in them: see "TILE" below.
TILE = "tile"
#: Outline fonts for headings, drawn as a kitty picture like TILE: files in
#: bitwall/fonts, each with its licence (see the README there).
WHITE_RABBIT = "white_rabbit"
BYTES256 = "256_bytes"
VT323 = "vt323"
#: Each face's file, beside this in fonts/.
FACES = {WHITE_RABBIT: "WhiteRabbit.ttf", BYTES256: "256Bytes.otf",
         VT323: "VT323-Regular.ttf"}
FONTS = (BLOCK, HALF, DOTS, SEGMENT, SEGMENT_CUT, SEGMENT7, SEGMENT7_CUT, TILE,
         WHITE_RABBIT, BYTES256, VT323)

#: What each font needs the wall to draw.
NEEDS = {BLOCK: "", HALF: "▀▄", DOTS: "⠁⣿", SEGMENT: "▀▄",
         SEGMENT_CUT: "▀▄◢◣◤◥", SEGMENT7: "▀▄", SEGMENT7_CUT: "▀▄◢◣◤◥",
         TILE: "", WHITE_RABBIT: "", BYTES256: "", VT323: ""}

#: The fonts drawn as a picture over their cells rather than in them.
_PICTURED = (TILE,) + tuple(FACES)

#: What a font drawn in cells is drawn as on a wall that cannot draw it,
#: nearest first: the same segments with square edges, then BLOCK, which
#: any wall draws.
FONT_INSTEAD = {DOTS: (HALF, BLOCK), HALF: (BLOCK,),
                SEGMENT: (BLOCK,), SEGMENT7: (BLOCK,),
                SEGMENT_CUT: (SEGMENT, BLOCK), SEGMENT7_CUT: (SEGMENT7, BLOCK)}

#: The segment fonts, and which of them are seven-segment or cut.
_SEGMENTED = (SEGMENT, SEGMENT_CUT, SEGMENT7, SEGMENT7_CUT)

#: Pixels to a cell, across and down.
_PER_CELL = {BLOCK: (1, 1), HALF: (1, 2), DOTS: (2, 4), SEGMENT: (1, 2)}

# ------------------------------------------------------------------ tables

#: Capitals, digits and marks, 3 pixels by 5, for BLOCK and HALF.
SMALL = {
    '0': "### #.# #.# #.# ###", '1': "..# ..# ..# ..# ..#", '2': "### ..# ### #.. ###",
    '3': "### ..# ### ..# ###", '4': "#.# #.# ### ..# ..#", '5': "### #.. ### ..# ###",
    '6': "### #.. ### #.# ###", '7': "### ..# ..# ..# ..#", '8': "### #.# ### #.# ###",
    '9': "### #.# ### ..# ###", 'A': "### #.# ### #.# #.#", 'B': "##. #.# ##. #.# ##.",
    'C': "### #.. #.. #.. ###", 'D': "##. #.# #.# #.# ##.", 'E': "### #.. ### #.. ###",
    'F': "### #.. ### #.. #..", 'G': "### #.. #.# #.# ###", 'H': "#.# #.# ### #.# #.#",
    'I': "### .#. .#. .#. ###", 'J': "### ..# ..# #.# ###", 'K': "#.# #.# ##. #.# #.#",
    'L': "#.. #.. #.. #.. ###", 'M': "#.# ### ### #.# #.#", 'N': "##. #.# #.# #.# #.#",
    'O': "### #.# #.# #.# ###", 'P': "### #.# ### #.. #..", 'Q': "### #.# #.# ### ..#",
    'R': "### #.# ##. #.# #.#", 'S': "### #.. ### ..# ###", 'T': "### .#. .#. .#. .#.",
    'U': "#.# #.# #.# #.# ###", 'V': "#.# #.# #.# #.# .#.", 'W': "#.# #.# ### ### #.#",
    'X': "#.# #.# .#. #.# #.#", 'Y': "#.# #.# .#. .#. .#.", 'Z': "### ..# .#. #.. ###",
    ':': "... .#. ... .#. ...", '.': "... ... ... ... .#.", '-': "... ... ### ... ...",
    '/': "..# ..# .#. #.. #..", '!': ".#. .#. .#. ... .#.", '?': "### ..# .## ... .#.",
    '%': "#.# ..# .#. #.. #.#", '+': "... .#. ### .#. ...", ' ': "... ... ... ... ...",
    "'": ".#. .#. ... ... ...", ',': "... ... ... .#. #..", '"': "#.# #.# ... ... ...",
    '(': "..# .#. .#. .#. ..#", ')': "#.. .#. .#. .#. #..", '&': "##. ##. ### #.# ###",
    ';': "... .#. ... .#. #..", '#': "#.# ### #.# ### #.#", '=': "... ### ... ### ...",
    '*': "#.# .#. #.# ... ...", '_': "... ... ... ... ###",
    '←': ".#. #.. ### #.. .#.", '→': ".#. ..# ### ..# .#.",
    '↑': ".#. ### #.# .#. .#.", '↓': ".#. .#. #.# ### .#.",
}

#: Upper and lower case, digits and marks, 5 pixels by 7 with an eighth
#: row for descenders, for DOTS. Lower case stands two rows under the
#: capitals' height; a descender takes the last row, lifting its bowl one.
FIVE = {
    'A': ".###. #...# #...# ##### #...# #...# #...#",
    'B': "####. #...# #...# ####. #...# #...# ####.",
    'C': ".###. #...# #.... #.... #.... #...# .###.",
    'D': "####. #...# #...# #...# #...# #...# ####.",
    'E': "##### #.... #.... ####. #.... #.... #####",
    'F': "##### #.... #.... ####. #.... #.... #....",
    'G': ".###. #...# #.... #.### #...# #...# .####",
    'H': "#...# #...# #...# ##### #...# #...# #...#",
    'I': ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    'J': "..### ...#. ...#. ...#. ...#. #..#. .##..",
    'K': "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    'L': "#.... #.... #.... #.... #.... #.... #####",
    'M': "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    'N': "#...# #...# ##..# #.#.# #..## #...# #...#",
    'O': ".###. #...# #...# #...# #...# #...# .###.",
    'P': "####. #...# #...# ####. #.... #.... #....",
    'Q': ".###. #...# #...# #...# #.#.# #..#. .##.#",
    'R': "####. #...# #...# ####. #.#.. #..#. #...#",
    'S': ".#### #.... #.... .###. ....# ....# ####.",
    'T': "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    'U': "#...# #...# #...# #...# #...# #...# .###.",
    'V': "#...# #...# #...# #...# #...# .#.#. ..#..",
    'W': "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",
    'X': "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    'Y': "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    'Z': "##### ....# ...#. ..#.. .#... #.... #####",
    'a': "..... ..... .###. ....# .#### #...# .####",
    'b': "#.... #.... #.##. ##..# #...# #...# ####.",
    'c': "..... ..... .###. #.... #.... #...# .###.",
    'd': "....# ....# .##.# #..## #...# #...# .####",
    'e': "..... ..... .###. #...# ##### #.... .###.",
    'f': "..##. .#..# .#... ###.. .#... .#... .#...",
    'g': "..... ..... .#### #...# #...# .#### ....# .###.",
    'h': "#.... #.... #.##. ##..# #...# #...# #...#",
    'i': "..#.. ..... .##.. ..#.. ..#.. ..#.. .###.",
    'j': "...#. ..... ..##. ...#. ...#. ...#. #..#. .##..",
    'k': "#.... #.... #..#. #.#.. ##... #.#.. #..#.",
    'l': ".##.. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    'm': "..... ..... ##.#. #.#.# #.#.# #...# #...#",
    'n': "..... ..... #.##. ##..# #...# #...# #...#",
    'o': "..... ..... .###. #...# #...# #...# .###.",
    'p': "..... ..... ####. #...# #...# ####. #.... #....",
    'q': "..... ..... .#### #...# #...# .#### ....# ....#",
    'r': "..... ..... #.##. ##..# #.... #.... #....",
    's': "..... ..... .###. #.... .###. ....# ####.",
    't': ".#... .#... ###.. .#... .#... .#..# ..##.",
    'u': "..... ..... #...# #...# #...# #..## .##.#",
    'v': "..... ..... #...# #...# #...# .#.#. ..#..",
    'w': "..... ..... #...# #...# #.#.# #.#.# .#.#.",
    'x': "..... ..... #...# .#.#. ..#.. .#.#. #...#",
    'y': "..... ..... #...# #...# #...# .#### ....# .###.",
    'z': "..... ..... ##### ...#. ..#.. .#... #####",
    '0': ".###. #...# #..## #.#.# ##..# #...# .###.",
    '1': "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    '2': ".###. #...# ....# ...#. ..#.. .#... #####",
    '3': "##### ...#. ..#.. ...#. ....# #...# .###.",
    '4': "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    '5': "##### #.... ####. ....# ....# #...# .###.",
    '6': "..##. .#... #.... ####. #...# #...# .###.",
    '7': "##### ....# ...#. ..#.. .#... .#... .#...",
    '8': ".###. #...# #...# .###. #...# #...# .###.",
    '9': ".###. #...# #...# .#### ....# ...#. .##..",
    ' ': "",
    '!': "..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..",
    '"': ".#.#. .#.#.",
    '#': ".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.",
    '$': "..#.. .#### #.#.. .###. ..#.# ####. ..#..",
    '%': "##... ##..# ...#. ..#.. .#... #..## ...##",
    '&': ".##.. #..#. #.#.. .#... #.#.# #..#. .##.#",
    "'": "..#.. ..#..",
    '(': "...#. ..#.. .#... .#... .#... ..#.. ...#.",
    ')': ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    '*': "..... ..#.. #.#.# .###. #.#.# ..#.. .....",
    '+': "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    ',': "..... ..... ..... ..... ..... .##.. ..#.. .#...",
    '-': "..... ..... ..... ####. ..... ..... .....",
    '.': "..... ..... ..... ..... ..... .##.. .##..",
    '/': "....# ....# ...#. ..#.. .#... #.... #....",
    ':': "..... .##.. .##.. ..... .##.. .##.. .....",
    ';': "..... .##.. .##.. ..... .##.. .##.. ..#.. .#...",
    '<': "...#. ..#.. .#... #.... .#... ..#.. ...#.",
    '=': "..... ..... ##### ..... ##### ..... .....",
    '>': ".#... ..#.. ...#. ....# ...#. ..#.. .#...",
    '?': ".###. #...# ....# ...#. ..#.. ..... ..#..",
    '@': ".###. #...# #.### #.#.# #.### #.... .####",
    '[': ".###. .#... .#... .#... .#... .#... .###.",
    '\\': "#.... #.... .#... ..#.. ...#. ....# ....#",
    ']': ".###. ...#. ...#. ...#. ...#. ...#. .###.",
    '^': "..#.. .#.#. #...#",
    '_': "..... ..... ..... ..... ..... ..... ..... #####",
    '`': ".#... ..#..",
    '{': "...#. ..#.. ..#.. .#... ..#.. ..#.. ...#.",
    '|': "..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    '}': ".#... ..#.. ..#.. ...#. ..#.. ..#.. .#...",
    '~': "..... ..... .#... #.#.# ...#. ..... .....",
    '←': "..... ..#.. .#... ##### .#... ..#.. .....",
    '→': "..... ..#.. ...#. ##### ...#. ..#.. .....",
    '↑': "..#.. .###. #.#.# ..#.. ..#.. ..#.. ..#..",
    '↓': "..#.. ..#.. ..#.. ..#.. #.#.# .###. ..#..",
}

#: Which segments each character lights. a top, b upper right, c lower
#: right, d bottom, e lower left, f upper left; g and h the middle bar's
#: left and right halves; i and j the upper and lower centre bars; k and l
#: the upper diagonals from the left and right corners, m and n the lower
#: ones. The digits light only the seven bars a clock has.
SEGMENTS = {
    '0': "abcdef", '1': "bc", '2': "abdegh", '3': "abcdgh", '4': "bcfgh",
    '5': "acdfgh", '6': "acdefgh", '7': "abc", '8': "abcdefgh",
    '9': "abcdfgh",
    'A': "abcefgh", 'B': "abcdhij", 'C': "adef", 'D': "abcdij", 'E': "adefgh",
    'F': "aefgh", 'G': "acdefh", 'H': "bcefgh", 'I': "adij", 'J': "bcde",
    'K': "efgln", 'L': "def", 'M': "bcefkl", 'N': "bcefkn", 'O': "abcdef",
    'P': "abefgh", 'Q': "abcdefn", 'R': "abefghn", 'S': "acdfgh", 'T': "aij",
    'U': "bcdef", 'V': "eflm", 'W': "bcefmn", 'X': "klmn", 'Y': "klj",
    'Z': "adlm",
    '-': "gh", '_': "d", '+': "ghij", '*': "ghijklmn", '/': "lm",
    '\\': "kn", '=': "dgh", '<': "ln", '>': "km", '(': "ln", ')': "km",
    '[': "adef", ']': "abcd", '?': "abhj", '$': "acdfghij", '%': "cflm",
    '"': "fi", '#': "bcdghij", '&': "adeghkn", '@': "abdefhi",
    ' ': "", ':': "", '.': "", ',': "", "'": "", '!': "",
}

#: Which of the seven bars each character lights on SEGMENT7: a top, b
#: upper right, c lower right, d bottom, e lower left, f upper left, g
#: the middle. The letters a seven-segment display can make, in the case
#: it makes them in; the other case is drawn the same where it has none.
SEVENS = {
    '0': "abcdef", '1': "bc", '2': "abdeg", '3': "abcdg", '4': "bcfg",
    '5': "acdfg", '6': "acdefg", '7': "abc", '8': "abcdefg", '9': "abcdfg",
    'A': "abcefg", 'b': "cdefg", 'C': "adef", 'c': "deg", 'd': "bcdeg",
    'E': "adefg", 'F': "aefg", 'G': "acdef", 'H': "bcefg", 'h': "cefg",
    'I': "ef", 'J': "bcde", 'L': "def", 'n': "ceg", 'o': "cdeg",
    'O': "abcdef", 'P': "abefg", 'q': "abcfg", 'r': "eg", 'S': "acdfg",
    't': "defg", 'U': "bcdef", 'u': "cde", 'y': "bcdfg",
    '-': "g", '_': "d", '=': "dg", '"': "bf", '?': "abeg", '[': "adef",
    ']': "abcd",
    ' ': "", ':': "", '.': "", ',': "", "'": "", '!': "",
}
_SEVEN_BARS = set("abcdefG")

#: The marks SEGMENT draws as shapes of their own, narrower than a letter.
_NARROW = ":.,'!"
_FOURTEEN = set("abcdefghijklmn")


def _table(src, width, height):
    """A table's strings as rows of 0 and 1, padded to the font's box."""
    out = {}
    for ch, spec in src.items():
        rows = [r.ljust(width, ".") for r in spec.split()]
        rows += ["." * width] * (height - len(rows))
        out[ch] = tuple(tuple(1 if c == "#" else 0 for c in r) for r in rows)
    return out


#: Each bitmap font: its glyphs, the box they are drawn in, the width of a
#: space when proportional, and whether it has lower case.
_BITMAPS = {
    BLOCK: (_table(SMALL, 3, 5), 3, 5, 2, False),
    HALF: (_table(SMALL, 3, 5), 3, 5, 2, False),
    DOTS: (_table(FIVE, 5, 8), 5, 8, 3, True),
    TILE: (_table(SMALL, 3, 5), 3, 5, 2, False),
}

#: SEGMENT's smallest size, in rows, and the step `scale` takes it in.
SEGMENT_BASE = 9
SEGMENT_MIN = 4
#: The smallest SEGMENT `fit` offers while another font would do: below
#: it the diagonals of a 14-segment letter have too little room to read.
#: SEGMENT7, which has none, is not held to it.
SEGMENT_READABLE = 7

#: TILE's size step: a tile's side is this many steps to a row, so a line
#: of capitals, five tiles tall, is a whole number of rows at every even
#: step -- two rows at 4, three at 6, five at 10, which is `scale` 1.
TILE_STEPS = 10
TILE_MIN = 2
#: The gap between tiles and the round of a letter's outer corner, as a
#: share of a tile's side: a hairline, and a corner that reads as round.
TILE_GAP = 0.07
TILE_RADIUS = 0.35
#: A cell's pixels to draw TILE at where the wall says none: twice the
#: 10x20 a terminal cell usually is, so a Retina screen gets it sharp.
TILE_PX = 20
#: A face's rows a line at `scale` 1: a heading two rows tall.
FACE_ROWS = 2

# ------------------------------------------------------------------ text

#: Punctuation that has a plain form a font can draw.
INSTEAD = {"’": "'", "‘": "'", "“": '"', "”": '"',
           "—": "-", "–": "-", "…": "...", "×": "X",
           "·": ".", "•": "."}


def _chars(font):
    if font in (SEGMENT7, SEGMENT7_CUT):
        return SEVENS
    if font in _SEGMENTED:
        return SEGMENTS
    return _BITMAPS[font][0]


def _lower(font):
    return font in (SEGMENT7, SEGMENT7_CUT) or (
        font in _BITMAPS and _BITMAPS[font][4])


def plain(text, font=BLOCK):
    """`text` as characters `font` can draw.

    Accents are folded rather than dropped, so Cezanne keeps its e, curly
    quotes and dashes become straight ones, a font without lower case
    gets capitals, a letter a font has in only one case is drawn in that
    one, and anything still unknown becomes a space, which at least keeps
    the words apart. Line breaks are kept.
    """
    if font in FACES:
        return "".join(INSTEAD.get(ch, ch) for ch in (text or ""))
    known = _chars(font)
    lower = _lower(font)
    out = []
    for ch in (text or ""):
        if ch == "\n":
            out.append(ch)
            continue
        ch = INSTEAD.get(ch, ch)
        if not lower:
            ch = ch.upper()
        if len(ch) == 1 and ch not in known:
            ch = (unicodedata.normalize("NFKD", ch)
                  .encode("ascii", "ignore").decode())
            if not lower:
                ch = ch.upper()
        out.append("".join(
            c if c in known else c.swapcase() if c.swapcase() in known
            else " " for c in ch) or " ")
    return "".join(out)


def usable(font, wall=None):
    """Whether `wall` can draw `font`: `w.can` on what the font needs.

    No wall is a yes, the same as a wall that has not said.
    """
    if font in _PICTURED:
        return _pillow() and wall is not None and hasattr(wall, "offers") \
            and wall.offers("kitty")
    need = NEEDS[font]
    return not need or wall is None or wall.can(need)


# ---------------------------------------------------------------- options

_DEFAULTS = {"scale": 1, "height": None, "spacing": "proportional",
             "turn": None, "align": "left", "smooth": False,
             "depth": "true", "cell": 2.0, "ghost": None, "edges": None,
             "wall": None, "px": None}

#: A cell of the Pi painter's font, MesloLGS, as a share of its size:
#: its advance across and its line height down.
MESLO_CELL = (0.6, 1.26)

#: Smoothing shades a stroke's edge pixel by how much of it is covered,
#: and a letter pixel is a half cell. Where a stroke is only a pixel or
#: two wide that shaded pixel is as wide as the stroke itself, a smudge
#: rather than an edge, unless the cells are so small on the glass that a
#: half cell is a fine grain: at most this many screen pixels across.
SMOOTH_STROKE = 3
SMOOTH_FINE_CELL = 6

#: A braille dot is 0.3 of a cell across and 0.15 of it down, rounded to
#: whole pixels as the painter rounds it. Below two screen pixels each
#: way dots blur into a smear and DOTS stops reading as dots: a cell this
#: many pixels wide and tall at least.
DOTS_DISTINCT = (5, 10)


def resolution(wall):
    """How many screen pixels a cell of `wall` is, (wide, tall), or None
    where that cannot be known.

    The screen's own word first (`screen` saying `cell=WxH`); then an
    estimate from the font size the wall reports its area at, for the
    font the painter draws in; then nothing, which leaves every choice as
    it is on a terminal, whose cells nobody can see.
    """
    if wall is None:
        return None
    said = wall.screen() if hasattr(wall, "screen") else {}
    if said.get("cell"):
        return tuple(said["cell"])
    size = getattr(wall, "font_size", None)
    if size:
        return (max(1, round(size * MESLO_CELL[0])),
                max(1, round(size * MESLO_CELL[1])))
    return None


def _options(opts):
    bad = set(opts) - set(_DEFAULTS)
    if bad:
        raise TypeError("unknown option %s" % ", ".join(sorted(bad)))
    o = dict(_DEFAULTS, **opts)
    if o["spacing"] not in ("proportional", "mono"):
        raise ValueError("spacing is proportional or mono")
    if o["turn"] not in (None, "up", "down"):
        raise ValueError("turn is None, up or down")
    if o["align"] not in ("left", "centre", "center", "right"):
        raise ValueError("align is left, centre or right")
    if o["edges"] not in (None, "square", "cut"):
        raise ValueError("edges is square or cut")
    return o


def _setup(font, opts):
    """The font the glyphs come from and the options, settled.

    A ready-made font's own edges where the caller gave none. From a
    `wall`, what it can tell: whether it draws the font's characters, and
    where it does not, the nearest font it does (FONT_INSTEAD), so `size`,
    `rows` and `draw` agree on one and nothing unreadable reaches the
    wall; whether it draws ◢◣◤◥, and square edges where it does not; its
    colour depth, and its cells' size in pixels, `px`, and from that
    their shape, `cell`. Whatever the caller says outright stands over
    what the wall would say, except a font or an edge it cannot draw.
    """
    if font not in FONTS:
        raise ValueError(f"no font {font!r}")
    o = _options(opts)
    if o["wall"] is not None and not usable(font, o["wall"]):
        font = next((f for f in FONT_INSTEAD.get(font, ())
                     if usable(f, o["wall"])), font)
    if o["edges"] is None:
        o["edges"] = "cut" if font in (SEGMENT_CUT, SEGMENT7_CUT) else "square"
    if o["edges"] == "cut" and o["wall"] is not None \
            and not o["wall"].can("◢◣◤◥"):
        o["edges"] = "square"
    o["seven"] = font in (SEGMENT7, SEGMENT7_CUT)
    wall = o.pop("wall")
    if o["px"] is None and wall is not None:
        o["px"] = resolution(wall)
    if o["px"] is not None:
        o["px"] = (max(1, int(o["px"][0])), max(1, int(o["px"][1])))
        if "cell" not in opts:
            o["cell"] = o["px"][1] / o["px"][0]
    if wall is not None and "depth" not in opts:
        o["depth"] = getattr(wall, "depth", o["depth"]) or o["depth"]
    return (SEGMENT if font in _SEGMENTED else font), o


def _plain(text, font, o):
    """plain(), for the font the glyphs come from and the options."""
    return plain(text, SEGMENT7 if o.get("seven") else font)


def _smooth_helps(n, px):
    """Whether shading SEGMENT's edges at size n reads better than crisp
    on cells of `px` screen pixels: see SMOOTH_STROKE. With nothing known
    of the cells, it is taken to, as it was asked for."""
    if px is None:
        return True
    return _seg_box(n)[2] >= SMOOTH_STROKE or px[0] <= SMOOTH_FINE_CELL


#: A ghost has to show above black to be a ghost at all, and stay well
#: under the ink to be one: no dimmer than this luma, and no brighter than
#: this share of the ink's.
GHOST_FLOOR = 6
GHOST_UNDER_INK = 0.5


def _luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


@functools.lru_cache(maxsize=1024)
def ghost_for(ghost, fg, depth):
    """The ghost colour to send for a wall of `depth`, or None for none.

    A dim tint reduced to a coarse palette by nearest match can land on a
    much lighter entry, and a ghost as bright as the ink is no ghost. So
    at 256 colours it is the palette's own colour nearest the tint that is
    no brighter than it, shows above black, and stays under half the ink;
    on a grey ramp, the step no brighter than it; with 16 colours or one
    there is nothing that dark but black, and there is no ghost.
    """
    if ghost is None or depth in ("true", None) or not _is_hex(ghost):
        return ghost
    if depth in ("16", "mono"):
        return None
    want = _rgb(ghost)
    most = _luma(want)
    if _is_hex(fg):
        most = min(most, _luma(_rgb(fg)) * GHOST_UNDER_INK)
    if depth == "grey":
        steps = [8 + 10 * k for k in range(24)]
        fits = [v for v in steps if GHOST_FLOOR <= v <= most]
        return "%02x%02x%02x" % ((max(fits),) * 3) if fits else None
    fits = [c for c in (_stream.palette_256(i) for i in range(16, 256))
            if GHOST_FLOOR <= _luma(c) <= most]
    if not fits:
        return None
    best = min(fits, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, want)))
    return "%02x%02x%02x" % best


def _dots_distinct(px):
    """Whether braille dots on cells of `px` screen pixels are dots."""
    return px is None or (px[0] >= DOTS_DISTINCT[0]
                          and px[1] >= DOTS_DISTINCT[1])


def _widen(font, cell):
    """How many of a font's pixels wide one of its bitmap pixels is drawn.

    A capital reads as a capital at about three units tall to two wide.
    The bitmap is 3x5, so at a cell `r` times taller than it is wide a
    pixel wants 5r/4.5 cells: two on the 2:1 cell bitwall assumes and
    three on the 2.75:1 cell a small font makes. HALF's pixel is half as
    tall, so it wants half as many. Braille dots already sit square.
    """
    if font == BLOCK:
        return max(1, round(cell * 10 / 9))
    if font == HALF:
        return max(1, round(cell * 5 / 9))
    return 1


# ----------------------------------------------------------- bitmap fonts

_bits_cache = {}


def _bits(font, ch, spacing):
    """A bitmap glyph as rows of 0 and 1, trimmed when proportional.

    Digits keep their whole box either way, so a number that changes does
    not shuffle along the line.
    """
    key = (font, ch, spacing)
    got = _bits_cache.get(key)
    if got is not None:
        return got
    table, width, height, space, _lower_ = _BITMAPS[font]
    g = table.get(ch, table[" "])
    if spacing == "proportional" and not ch.isdigit():
        used = [x for x in range(width) if any(r[x] for r in g)]
        if used:
            g = tuple(r[used[0]:used[-1] + 1] for r in g)
        else:
            g = tuple((0,) * space for _ in g)
    _bits_cache[key] = g
    return g


def _bitmap_widths(font, line, spacing):
    return [len(_bits(font, ch, spacing)[0]) for ch in line]


# --------------------------------------------------------------- dot fonts

#: DOTS: half a stroke's width, in the bitmap's own pixels. A stroke is a
#: pixel wide, the weight the bitmap has, and its round ends take the
#: corner of a corner pixel off. Under half the diagonal between two pixel
#: centres, so at scale 1, where a dot sits on each pixel centre, a dot is
#: lit on every lit pixel and on no other and the bitmap comes back.
DOTS_STROKE = 0.5

_stroke_cache = {}

#: The arrows as the lines they are, in the bitmap's pixel centres: a
#: shaft and two barbs at 45 degrees from the tip. Found from the bitmap,
#: the barbs would be cut short where they leave the tip beside the shaft,
#: and the head would read as a bar; given as lines, it is straight at
#: every size.
ARROW_STROKES = {
    '↑': ((2.5, 0.5, 2.5, 6.5), (2.5, 0.5, 0.5, 2.5), (2.5, 0.5, 4.5, 2.5)),
    '↓': ((2.5, 0.5, 2.5, 6.5), (2.5, 6.5, 0.5, 4.5), (2.5, 6.5, 4.5, 4.5)),
    '→': ((0.5, 3.5, 4.5, 3.5), (4.5, 3.5, 2.5, 1.5), (4.5, 3.5, 2.5, 5.5)),
    '←': ((0.5, 3.5, 4.5, 3.5), (0.5, 3.5, 2.5, 1.5), (0.5, 3.5, 2.5, 5.5)),
}


def _strokes(ch, spacing):
    """A DOTS glyph as strokes: each lit pixel centre joined to its lit
    neighbours, as (ax, ay, bx, by) in the bitmap's pixels.

    Across and down always; corner to corner only where both pixels
    between are unlit, so a diagonal is one stroke wide rather than a
    staircase filled in. A pixel with no neighbour is a stroke of no
    length, which is a round dot.
    """
    key = (ch, spacing)
    got = _stroke_cache.get(key)
    if got is not None:
        return got
    g = _bits(DOTS, ch, spacing)
    high, wide = len(g), len(g[0])
    if ch in ARROW_STROKES:
        dx = (wide - 5) / 2
        got = (tuple((ax + dx, ay, bx + dx, by)
                     for ax, ay, bx, by in ARROW_STROKES[ch]), wide, high)
        _stroke_cache[key] = got
        return got

    def on(x, y):
        return 0 <= x < wide and 0 <= y < high and g[y][x]

    out = []
    for y in range(high):
        for x in range(wide):
            if not g[y][x]:
                continue
            cx, cy = x + 0.5, y + 0.5
            out.append((cx, cy, cx, cy))
            if on(x + 1, y):
                out.append((cx, cy, cx + 1, cy))
            if on(x, y + 1):
                out.append((cx, cy, cx, cy + 1))
            if on(x + 1, y + 1) and not on(x + 1, y) and not on(x, y + 1):
                out.append((cx, cy, cx + 1, cy + 1))
            if on(x - 1, y + 1) and not on(x - 1, y) and not on(x, y + 1):
                out.append((cx, cy, cx - 1, cy + 1))
    got = (tuple(out), wide, high)
    _stroke_cache[key] = got
    return got


def _near(px, py, ax, ay, bx, by):
    """The squared distance from a point to a stroke."""
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if not length else max(0.0, min(1.0, ((px - ax) * dx
                                                    + (py - ay) * dy) / length))
    ex, ey = px - ax - t * dx, py - ay - t * dy
    return ex * ex + ey * ey


_dots_cache = {}
_DOTS_MAX = 4096


def _dots_glyph(ch, spacing, size, fx, fy):
    """The dots a DOTS glyph lights at `size` dots to a bitmap pixel, as
    (column, row) from the dot its top left falls in; (fx, fy) is how far
    into that dot the glyph starts.

    A dot is lit where its centre falls inside a stroke, so the letter is
    its shape laid over the dot grid at any size: corners come out round
    and diagonals as lines of single dots, the way a finer dot-matrix sign
    draws them.
    """
    key = (ch, spacing, size, fx, fy)
    got = _dots_cache.get(key)
    if got is not None:
        return got
    strokes, wide, high = _strokes(ch, spacing)
    r2 = DOTS_STROKE * DOTS_STROKE + 1e-9
    out = []
    for j in range(math.ceil(high * size + fy) + 1):
        y = (j + 0.5 - fy) / size
        near = [st for st in strokes
                if min(st[1], st[3]) - DOTS_STROKE <= y
                <= max(st[1], st[3]) + DOTS_STROKE]
        if not near:
            continue
        for i in range(math.ceil(wide * size + fx) + 1):
            x = (i + 0.5 - fx) / size
            for st in near:
                if _near(x, y, *st) <= r2:
                    out.append((i, j))
                    break
    got = tuple(out)
    if len(_dots_cache) >= _DOTS_MAX:
        _dots_cache.clear()
    _dots_cache[key] = got
    return got


def _dot_size(n):
    """Dots to a bitmap pixel at size step n: a step is half a dot, so
    a line of DOTS is exactly n rows and a height can be filled."""
    return n / 2


def _dots_place(n, lines_, o):
    """Where each DOTS glyph goes: [(ch, left, top)] in dots, the block's
    width and height in dots, and the boxes of unlit dots a ghost shows,
    as (x0, y0, x1, y1).

    A cell carries one mark for its unlit dots, all or none, so a ghost's
    boxes are laid on whole cells: lines a cell apart, and with mono
    spacing each letter in a box an even number of dots wide with a cell
    of black between boxes, like a character LCD. Proportional, or
    turned, the unlit dots are one field behind each whole line.
    """
    size = _dot_size(n)
    spacing = o["spacing"]
    ghost = o["ghost"] is not None
    boxes = ghost and spacing == "mono" and not o["turn"]
    tall = 8 * size                             # 4n: whole cells
    pitch = tall + 4 if ghost else 9 * size
    nat = [sum(_bitmap_widths(DOTS, line, spacing)) + max(0, len(line) - 1)
           for line in lines_]
    box = 2 * math.ceil(5 * size / 2)
    if boxes:
        widths = [len(line) * (box + 2) - 2 if line else 0 for line in lines_]
    else:
        widths = [w * size for w in nat]
    wide = max(widths) if widths else 0
    places, field = [], []
    for i, line in enumerate(lines_):
        top = i * pitch
        if boxes:
            left = _offset(widths[i], wide, o["align"])
        else:
            left = _offset(nat[i], max(nat), o["align"]) * size
        if ghost:
            left = 2 * math.floor(left / 2)
        if boxes:
            for ch in line:
                places.append((ch, left + (box - 5 * size) // 2, top))
                field.append((left, top, left + box, top + tall))
                left += box + 2
            continue
        x = 0
        for ch in line:
            places.append((ch, left + x * size, top))
            x += len(_bits(DOTS, ch, spacing)[0]) + 1
        if ghost and line:
            field.append((left, top, left + widths[i], top + tall))
    down = (len(lines_) - 1) * pitch + tall if lines_ else 0
    return places, math.ceil(wide), math.ceil(down), field


# ---------------------------------------------------------- segment shapes

#: Samples a pixel along each side where an edge crosses it: 17 levels.
SAMPLES = 4
_EDGE = 0.7072          # half a pixel's diagonal: nearer than this, sample


def _seg_box(n):
    """A SEGMENT letter n rows tall: its width and stroke, in pixels."""
    high = 2 * n
    wide = max(3, round(high * 0.66))
    # Small, a stroke a pixel wide leaves the diagonals room to slant.
    return wide, high, 1 if n < 12 else max(1, round(wide * 0.14))


def _seg_width(ch, n, spacing):
    wide, _high, t = _seg_box(n)
    if spacing == "mono" or ch.isdigit():
        return wide
    if ch == " ":
        return max(2, round(wide * 0.6))
    if ch in _NARROW:
        return max(2, round(t * 2.5), round(wide * 0.35))
    return wide


def _seg_gap(n):
    return max(1, round(_seg_box(n)[0] * 0.28))


#: The slope of a cell's own diagonal, in SEGMENT's square pixels: a
#: cell is one pixel across and two down. Every slant in the font runs at
#: it -- the points of the bars, the gaps between them, the diagonal
#: strokes -- so a slant crosses whole cells corner to corner, and cut
#: edges can draw it exactly with ◢◣◤◥.
_STEEP = 2.0


def _bar(x0, y0, x1, y1, thick):
    """A bar from one tip to the other, as a signed distance and its box.

    Its ends come to points along the cell's diagonal, so the ends of two
    bars meeting at a corner face each other across a parallel gap.
    """
    half = thick / 2
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if y0 == y1:
        reach = (x1 - x0) / 2
        norm = math.hypot(1, 1 / _STEEP)

        def sd(x, y):
            dx, dy = abs(x - cx), abs(y - cy)
            return max(dy - half, (dx + dy / _STEEP - reach) / norm)
        return sd, (x0, cy - half, x1, cy + half)
    reach = (y1 - y0) / 2
    norm = math.hypot(1, _STEEP)

    def sd(x, y):
        dx, dy = abs(x - cx), abs(y - cy)
        return max(dx - half, (dy + _STEEP * dx - reach) / norm)
    return sd, (cx - half, y0, cx + half, y1)


def _stroke(a, b, thick):
    """A diagonal stroke along the line from a to b, cut square to the box
    they span. The letter's proportions put that line close to the cell's
    own diagonal, so cut edges draw it with ◢◣◤◥."""
    (x0, y0), (x1, y1) = a, b
    lx, ly = x1 - x0, y1 - y0
    norm = math.hypot(lx, ly) or 1.0
    nx, ny = -ly / norm, lx / norm
    half = thick / 2
    ax, bx = min(x0, x1), max(x0, x1)
    ay, by = min(y0, y1), max(y0, y1)

    def sd(x, y):
        return max(abs((x - x0) * nx + (y - y0) * ny) - half,
                   ax - x, x - bx, ay - y, y - by)
    return sd, (ax, ay, bx, by)


def _path(a, b):
    """The pixels a diagonal must light, whatever its size: a line one
    pixel wide from a to b, each pixel touching the last on a side or a
    corner. A stroke thinner than a pixel on a steep slope comes out as
    dashes crisp, and this is what keeps it one line. It starts half a
    pixel in from a, the stroke's own end, and finishes in the pixel b
    lies in, the centre every diagonal reaches."""
    (x0, y0), (x1, y1) = a, b
    sx = 1 if x1 > x0 else -1
    sy = 1 if y1 > y0 else -1
    px, py = math.floor(x0 + sx * 0.5), math.floor(y0 + sy * 0.5)
    qx, qy = math.floor(x1 - (0.5 if sx > 0 else 0)), \
        math.floor(y1 - (0.5 if sy > 0 else 0))
    steps = max(abs(qx - px), abs(qy - py))
    out = []
    for k in range(steps + 1):
        f = k / steps if steps else 0.0
        out.append((round(px + (qx - px) * f), round(py + (qy - py) * f)))
    return out


def _dot(cx, cy, r):
    return (lambda x, y: math.hypot(x - cx, y - cy) - r,
            (cx - r, cy - r, cx + r, cy + r))


def _coverage(shape, wide, high):
    """How much of each pixel a shape covers, 0..1, as {(x, y): amount}.

    A pixel whose centre is well inside or well outside is decided from
    the centre alone; only one an edge crosses is sampled, which is what
    keeps a big letter quick to make.
    """
    sd, (x0, y0, x1, y1) = shape
    out = {}
    step = 1 / SAMPLES
    offs = [(i + 0.5) * step for i in range(SAMPLES)]
    for py in range(max(0, int(y0) - 1), min(high, int(y1) + 2)):
        for px in range(max(0, int(x0) - 1), min(wide, int(x1) + 2)):
            d = sd(px + 0.5, py + 0.5)
            if d <= -_EDGE:
                out[(px, py)] = 1.0
            elif d < _EDGE:
                hit = sum(1 for i in offs for j in offs
                          if sd(px + i, py + j) <= 0)
                if hit:
                    out[(px, py)] = hit / (SAMPLES * SAMPLES)
    return out


_shape_cache = {}


def _seg_dark(n, t):
    """The dark between two segments, in pixels: a whole pixel wherever
    the letter has room for it, so each segment reads as one drawn crisp,
    and less as the letter shrinks, where a gap would eat the strokes it
    separates. Segments nearly touching read better than broken ones."""
    if n >= 8:
        return max(1.0, t * 0.35)
    return 0.5 if n >= 6 else 0.25


def _poly(planes, box):
    """A convex shape as half-planes (nx, ny, c), inside where
    nx * x + ny * y <= c, each normal a unit one, so the largest is a
    signed distance; and the box it lies in."""
    def sd(x, y):
        return max(nx * x + ny * y - c for nx, ny, c in planes)
    return sd, box


def _mitred(w, h, t, cx, cy, gap):
    """The bars of a letter w by h, stroke t, middle at cx, cy, mitred
    where they meet and a gap apart.

    At the four outer corners the upright bar reaches the corner and the
    bar across gives the gap, so every character has the same height; at
    the middle, and where a bar meets the centre, the gap is shared.
    """
    r = math.sqrt(0.5)
    g, s = gap, gap / 2
    mid = (cy - t / 2, cy + t / 2)
    across = lambda y0, y1: [(0, -1, -y0), (0, 1, y1)]   # noqa: E731
    down = lambda x0, x1: [(-1, 0, -x0), (1, 0, x1)]     # noqa: E731
    # A bar across ending in a point at (px, cy), facing left or right.
    point_left = lambda px, k: [(-r, r, (cy - px) * r - k),       # noqa: E731
                                (-r, -r, (-px - cy) * r - k)]
    point_right = lambda px, k: [(r, r, (px + cy) * r - k),       # noqa: E731
                                 (r, -r, (px - cy) * r - k)]
    return {
        "a": _poly(across(0, t) + [(-r, r, -g), (r, r, w * r - g)],
                   (0, 0, w, t)),
        "d": _poly(across(h - t, h) + [(-r, -r, -h * r - g),
                                       (r, -r, (w - h) * r - g)],
                   (0, h - t, w, h)),
        "G": _poly(across(*mid) + point_left(0, s) + point_right(w, s),
                   (0, mid[0], w, mid[1])),
        "g": _poly(across(*mid) + point_left(0, s) + point_right(cx, s),
                   (0, mid[0], cx, mid[1])),
        "h": _poly(across(*mid) + point_left(cx, s) + point_right(w, s),
                   (cx, mid[0], w, mid[1])),
        "f": _poly(down(0, t) + [(r, -r, 0), (r, r, cy * r - s)],
                   (0, 0, t, cy)),
        "e": _poly(down(0, t) + [(r, -r, -cy * r - s), (r, r, h * r)],
                   (0, cy, t, h)),
        "b": _poly(down(w - t, w) + [(-r, -r, -w * r),
                                     (-r, r, (cy - w) * r - s)],
                   (w - t, 0, w, cy)),
        "c": _poly(down(w - t, w) + [(-r, -r, -(cy + w) * r - s),
                                     (-r, r, (h - w) * r)],
                   (w - t, cy, w, h)),
        # The centre bars come to a point under the top bar, over the
        # bottom one, and at the middle.
        "i": _poly(down(cx - t / 2, cx + t / 2)
                   + [(r, -r, (cx - t - g) * r), (-r, -r, (-cx - t - g) * r),
                      (r, r, (cx + cy) * r - s), (-r, r, (cy - cx) * r - s)],
                   (cx - t / 2, t + g, cx + t / 2, cy)),
        "j": _poly(down(cx - t / 2, cx + t / 2)
                   + [(r, -r, (cx - cy) * r - s), (-r, -r, -(cx + cy) * r - s),
                      (r, r, (cx + h - t - g) * r),
                      (-r, r, (h - t - g - cx) * r)],
                   (cx - t / 2, cy, cx + t / 2, h - t - g)),
    }


def _seg_shapes(n):
    """Every segment of a letter n rows tall, as {name: (sd, box)} in its
    pixels, made once a size.

    Laid out as a real display is: each bar runs between the centre
    lines of the bars it meets, and meets them in a 45 degree mitre with
    only the gap of dark between, so every character fills the same box
    and a 1 is as tall as an 8. A diagonal runs from the corner of the
    box the bars leave to the centre.
    """
    key = ("shapes", n)
    got = _shape_cache.get(key)
    if got is not None:
        return got
    w, h, t = _seg_box(n)
    gap = _seg_dark(n, t)
    # The middle bar and the centre bars lie on whole pixels, so a stroke
    # is never split between two half-lit ones.
    cx = math.floor((w - t) / 2) + t / 2
    cy = math.floor((h - t) / 2) + t / 2
    # Each diagonal from the corner of its box towards the centre, as the
    # ends of its middle line, stopping where its edge is the gap clear of
    # the bars that meet there: every segment its own, at the middle as
    # at the corners, the way a real display is made.
    def short(corner):
        """The diagonal from `corner`, its end drawn back from the centre
        by a stroke and the gap, where the bars' own points meet."""
        dx, dy = corner[0] - cx, corner[1] - cy
        length = math.hypot(dx, dy)
        d = min(t + gap, length - t)
        return corner, (cx + dx / length * d, cy + dy / length * d)

    ends = {
        "k": short((t + gap, t + gap)),
        "l": short((w - t - gap, t + gap)),
        "m": short((t + gap, h - t - gap)),
        "n": short((w - t - gap, h - t - gap)),
    }
    got = _mitred(w, h, t, cx, cy, gap)
    for name, (a, b) in ends.items():
        got[name] = _stroke(a, b, t)
    _shape_cache[("paths", n)] = {name: _path(a, b)
                                  for name, (a, b) in ends.items()}
    _shape_cache[key] = got
    return got


def _moved(shape, dx):
    """A shape moved dx pixels across."""
    sd, (x0, y0, x1, y1) = shape
    return (lambda x, y: sd(x - dx, y)), (x0 + dx, y0, x1 + dx, y1)


def _lit(segs):
    """The shapes a set of segments is drawn as: each its own, the two
    halves of the middle bar too, with the gap between them."""
    return set(segs)


def _glyph_shapes(ch, n, spacing, seven=False):
    """A SEGMENT character as its lit and unlit shapes, and its width.

    Every character of the fourteen-segment font has all fourteen behind
    it, a digit included, as a real display has at every place; the seven
    segment one has only its seven.
    """
    key = ("glyph shapes", ch, n, spacing, seven)
    got = _shape_cache.get(key)
    if got is not None:
        return got
    w, h, t = _seg_box(n)
    width = _seg_width(ch, n, spacing)
    lit, dim, lit_paths, dim_paths = [], [], [], []
    if ch in _NARROW:
        r = max(0.8, t * 0.75)
        cx = width / 2
        lit = {
            ":": [_dot(cx, h * 0.3, r), _dot(cx, h * 0.7, r)],
            ".": [_dot(cx, h - r, r)],
            ",": [_dot(cx, h - r, r)],
            "'": [_bar(cx, 0, cx, h * 0.3, t)],
            "!": [_bar(cx, h * 0.03, cx, h * 0.73, t), _dot(cx, h - r, r)],
        }[ch]
    elif ch != " ":
        if seven:
            on = {"G" if k == "g" else k for k in SEVENS.get(ch, "")}
            off = _SEVEN_BARS - on
        else:
            on = _lit(SEGMENTS.get(ch, ""))
            off = _FOURTEEN - set(SEGMENTS.get(ch, ""))
            if "G" in on:
                off -= {"g", "h"}
        shift = (width - w) // 2
        shapes = _seg_shapes(n)
        paths = _shape_cache[("paths", n)]
        lit = [_moved(shapes[k], shift) for k in sorted(on)]
        dim = [_moved(shapes[k], shift) for k in sorted(off)]
        lit_paths = [[(x + shift, y) for x, y in paths[k]]
                     for k in sorted(on) if k in paths]
        dim_paths = [[(x + shift, y) for x, y in paths[k]]
                     for k in sorted(off) if k in paths]
    got = (lit, dim, width, lit_paths, dim_paths)
    _shape_cache[key] = got
    return got


def _seg_glyph(ch, n, spacing, seven=False):
    """A SEGMENT character as (lit, unlit) coverage and its width."""
    key = ("glyph", ch, n, spacing, seven)
    got = _shape_cache.get(key)
    if got is not None:
        return got
    shapes_on, shapes_off, width, on_paths, off_paths = _glyph_shapes(
        ch, n, spacing, seven)
    h = 2 * n
    lit, dim = {}, {}
    for into, shapes, paths in ((lit, shapes_on, on_paths),
                                (dim, shapes_off, off_paths)):
        for shape in shapes:
            for p, a in _coverage(shape, width, h).items():
                # Small, a sliver of a tip is a smudge rather than an edge.
                if n >= 8 or a >= 0.35:
                    into[p] = min(1.0, into.get(p, 0) + a)
        for path in paths:
            for p in path:
                if 0 <= p[0] < width and 0 <= p[1] < h:
                    into[p] = 1.0
    got = (lit, dim, width)
    _shape_cache[key] = got
    return got


#: The ways a cell can be split between two colours, as which of a 4 x 8
#: grid of points in it each takes: the halves a block element gives and
#: the four diagonal ones, with the character that draws the first part.
_SPLITS = (
    ("▀", lambda u, v: v < 0.5), ("▄", lambda u, v: v >= 0.5),
    ("◢", lambda u, v: u + v >= 1), ("◣", lambda u, v: v >= u),
    ("◤", lambda u, v: u + v < 1), ("◥", lambda u, v: v < u),
)
_POINTS = [((i + 0.5) / 4, (j + 0.5) / 8) for j in range(8) for i in range(4)]
_SPLIT_IN = [(ch, [k for k, (u, v) in enumerate(_POINTS) if part(u, v)])
             for ch, part in _SPLITS]
_SPLIT_OF = dict(_SPLITS)
#: The part of a cell each split leaves, drawn the other way round.
_OTHER = {"▀": "▄", "▄": "▀", "◢": "◤", "◤": "◢", "◣": "◥", "◥": "◣"}


def _seg_cut(ch, n, spacing, ghosted, seven=False):
    """A SEGMENT character with its edges cut: each cell as the split of
    it, and which of nothing (0), lit (1) and unlit (2) each part shows.

    Every cell is looked at on a grid of points and given whichever of
    whole, the two halves and the four diagonal halves matches them best,
    so a slope is drawn as a slope rather than a staircase and a bar's
    point as a point.
    """
    key = ("cut", ch, n, spacing, ghosted, seven)
    got = _shape_cache.get(key)
    if got is not None:
        return got
    on, off, width, on_paths, off_paths = _glyph_shapes(ch, n, spacing,
                                                        seven)
    if not ghosted:
        off, off_paths = [], []
    out = []
    for r in range(n):
        line = []
        for c in range(width):
            near_on = [sd for sd, (x0, y0, x1, y1) in on
                       if x0 - 1 <= c + 1 and c <= x1 + 1
                       and y0 - 1 <= 2 * r + 2 and 2 * r <= y1 + 1]
            near_off = [sd for sd, (x0, y0, x1, y1) in off
                        if x0 - 1 <= c + 1 and c <= x1 + 1
                        and y0 - 1 <= 2 * r + 2 and 2 * r <= y1 + 1]
            if not near_on and not near_off:
                line.append(("", 0, 0))
                continue
            seen = []
            for u, v in _POINTS:
                x, y = c + u, 2 * r + 2 * v
                if any(sd(x, y) <= 0 for sd in near_on):
                    seen.append(1)
                elif any(sd(x, y) <= 0 for sd in near_off):
                    seen.append(2)
                else:
                    seen.append(0)
            counts = [seen.count(k) for k in range(3)]
            most = max(range(3), key=lambda k: counts[k])
            best, wrong = ("", most, most), 32 - counts[most]
            for split, inside in _SPLIT_IN:
                if not wrong:
                    break
                part = [0, 0, 0]
                for k in inside:
                    part[seen[k]] += 1
                rest = [counts[k] - part[k] for k in range(3)]
                a = max(range(3), key=lambda k: part[k])
                b = max(range(3), key=lambda k: rest[k])
                miss = (len(inside) - part[a]) + (32 - len(inside) - rest[b])
                if a != b and miss < wrong:
                    best, wrong = (split, a, b), miss
            line.append(best)
        out.append(line)
    # A diagonal's line of pixels stays lit, and an unlit one's stays
    # unlit-coloured, whatever the best match chose, so a stroke is never
    # broken into dashes.
    for paths, want in ((off_paths, 2), (on_paths, 1)):
        for path in paths:
            for x, y in path:
                if not (0 <= x < width and 0 <= y < 2 * n):
                    continue
                r, top = y // 2, y % 2 == 0
                split, a, b = out[r][x]
                v = 0.25 if top else 0.75
                here = a if not split or _SPLIT_OF[split](0.5, v) else b
                if here == want or (want == 2 and here == 1):
                    continue
                if not split:
                    out[r][x] = ("▀" if top else "▄", want, a)
                else:
                    out[r][x] = ("", want, want)
    got = (tuple(tuple(line) for line in out), width)
    _shape_cache[key] = got
    return got


def _cut_cells(parts, fg, bg, ghost):
    """Cut cells in their colours."""
    colour = (bg, fg, ghost)
    out = []
    for line in parts:
        row = []
        for split, a, b in line:
            inside, outside = colour[a], colour[b]
            if not split:
                row.append(None if inside is None else (" ", None, inside))
                continue
            if inside is None:
                split, inside, outside = _OTHER[split], outside, None
            row.append((split, inside, outside))
        out.append(tuple(row))
    return tuple(out)


# ---------------------------------------------------------------- layout

def _lines(text):
    return text.split("\n")


def _native(font, n, lines_, o):
    """The block's size in the font's own pixels, before turning or
    scaling, and the line widths and the gap between lines."""
    if font == SEGMENT:
        widths = [sum(_seg_width(ch, n, o["spacing"]) for ch in line)
                  + _seg_gap(n) * max(0, len(line) - 1) for line in lines_]
        tall = 2 * n
        gap = 2 * max(1, round(n / 3))
    else:
        widths = [sum(_bitmap_widths(font, line, o["spacing"]))
                  + max(0, len(line) - 1) for line in lines_]
        tall = _BITMAPS[font][2]
        gap = 1
    wide = max(widths) if widths else 0
    high = len(lines_) * tall + (len(lines_) - 1) * gap
    return widths, wide, high, tall, gap


def _dims(font, n, lines_, o):
    if font in FACES:
        # n rows a line; a line as wide as the face makes it at that height.
        line_em, width = _face_metrics(font)
        across = max((width(line) for line in lines_), default=0) * n / line_em
        wide, high = across * o["cell"], n * len(lines_)
        if o["turn"]:
            wide, high = high * o["cell"], across
            return (math.ceil(wide - 1e-9), math.ceil(high - 1e-9))
        return (math.ceil(wide - 1e-9), high)
    if font == TILE:
        _w, wide, high, _t, _g = _native(font, n, lines_, o)
        if o["turn"]:
            wide, high = high, wide
        t = n / TILE_STEPS                   # a tile's side, in rows
        return (math.ceil(wide * t * o["cell"] - 1e-9),
                math.ceil(high * t - 1e-9))
    if font == DOTS:
        _p, wide, high, _f = _dots_place(n, lines_, o)
    else:
        _w, wide, high, _t, _g = _native(font, n, lines_, o)
    if o["turn"]:
        wide, high = high, wide
    if font in (BLOCK, HALF):
        wide *= n * _widen(font, o["cell"])
        high *= n
    cx, cy = _PER_CELL[font]
    return -(-wide // cx), -(-high // cy)


def _smallest(font):
    return {SEGMENT: SEGMENT_MIN, DOTS: 2, TILE: TILE_MIN}.get(font, 1)


def _step(font, lines_, o):
    """The size step the options ask for, or None where nothing fits."""
    if o["height"] is None:
        s = max(1, int(o["scale"]))
        if font in FACES:
            return FACE_ROWS * s
        return {SEGMENT: SEGMENT_BASE * s, DOTS: 2 * s,
                TILE: TILE_STEPS * s}.get(font, s)
    want = int(o["height"])
    lo = _smallest(font)
    if _dims(font, lo, lines_, o)[1] > want:
        return None
    hi = lo
    while _dims(font, hi * 2, lines_, o)[1] <= want:
        hi *= 2
    hi *= 2
    # The largest step whose rows still fit: rows only grow with the step.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _dims(font, mid, lines_, o)[1] <= want:
            lo = mid
        else:
            hi = mid
    return lo


def size(text, font=BLOCK, **opts):
    """(cols, rows) the text will take, without drawing it."""
    font, o = _setup(font, opts)
    lines_ = _lines(_plain(text, font, o))
    n = _step(font, lines_, o)
    if n is None or not any(lines_):
        return (0, 0)
    return _dims(font, n, lines_, o)


# --------------------------------------------------------------- drawing

def _turned(grid, turn):
    """A grid given a quarter turn: "up" anticlockwise, so it reads from
    the bottom up, "down" clockwise."""
    if not grid:
        return grid
    high, wide = len(grid), len(grid[0])
    if turn == "up":
        return [[grid[y][wide - 1 - x] for y in range(high)]
                for x in range(wide)]
    return [[grid[high - 1 - y][x] for y in range(high)]
            for x in range(wide)]


def _offset(width, wide, align):
    if align == "right":
        return wide - width
    if align in ("centre", "center"):
        return (wide - width) // 2
    return 0


def _dotted(n, lines_, o):
    """DOTS text as a grid of dots, each glyph's strokes laid over it, and
    a grid of where a ghost's unlit dots show."""
    places, across, down, boxes = _dots_place(n, lines_, o)
    size = _dot_size(n)
    lit = [[0.0] * across for _ in range(down)]
    field = [[0.0] * across for _ in range(down)]
    for ch, left, top in places:
        col0, row0 = math.floor(left), math.floor(top)
        for gx, gy in _dots_glyph(ch, o["spacing"], size, left - col0,
                                  top - row0):
            if 0 <= row0 + gy < down and 0 <= col0 + gx < across:
                lit[row0 + gy][col0 + gx] = 1.0
    for x0, y0, x1, y1 in boxes:
        for y in range(int(y0), min(down, math.ceil(y1))):
            row = field[y]
            for x in range(int(x0), min(across, math.ceil(x1))):
                row[x] = 1.0
    if o["turn"]:
        lit, field = _turned(lit, o["turn"]), _turned(field, o["turn"])
    return lit, field


def _pixels(font, n, lines_, o):
    """The block as two grids of coverage, lit and unlit, in the pixels
    the font packs into cells."""
    if font == DOTS:
        return _dotted(n, lines_, o)
    widths, wide, high, tall, gap = _native(font, n, lines_, o)
    lit = [[0.0] * wide for _ in range(high)]
    dim = [[0.0] * wide for _ in range(high)]
    for i, line in enumerate(lines_):
        top = i * (tall + gap)
        x = _offset(widths[i], wide, o["align"])
        for ch in line:
            if font == SEGMENT:
                on, off, width = _seg_glyph(ch, n, o["spacing"], o["seven"])
                for (gx, gy), a in on.items():
                    if 0 <= x + gx < wide:
                        lit[top + gy][x + gx] = a
                for (gx, gy), a in off.items():
                    if 0 <= x + gx < wide:
                        dim[top + gy][x + gx] = a
                x += width + _seg_gap(n)
            else:
                g = _bits(font, ch, o["spacing"])
                for gy, row in enumerate(g):
                    dst = lit[top + gy]
                    for gx, v in enumerate(row):
                        if v:
                            dst[x + gx] = 1.0
                x += len(g[0]) + 1
    if o["turn"]:
        lit, dim = _turned(lit, o["turn"]), _turned(dim, o["turn"])
    if font != SEGMENT:
        across = n * _widen(font, o["cell"])
        lit = [[v for v in row for _ in range(across)]
               for row in lit for _ in range(n)]
        dim = [[0.0] * len(lit[0]) for _ in lit] if lit else []
    return lit, dim


def _rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _is_hex(colour):
    if not isinstance(colour, str) or len(colour) != 6:
        return False
    try:
        int(colour, 16)
    except ValueError:
        return False
    return True


def _mix(top, under, amount):
    """`top` laid over `under` at `amount`, as the wall's hex."""
    return "".join("%02x" % round(u + (c - u) * amount)
                   for c, u in zip(top, under))


def _colours(lit, dim, fg, bg, ghost, smooth, base):
    """Each pixel's colour, or None where it is the background."""
    mixes = {}
    if smooth:
        f, b = _rgb(fg), _rgb(base)
        gh = _rgb(ghost) if ghost else None

    def paint(a, g):
        if smooth:
            if a <= 0 and (g <= 0 or gh is None):
                return bg
            key = (a, g)
            got = mixes.get(key)
            if got is None:
                under = _rgb(_mix(gh, b, g)) if gh and g > 0 else b
                got = _mix(f, under, a) if a > 0 else _mix(under, under, 1)
                mixes[key] = got
            return got
        if a >= 0.5:
            return fg
        if ghost and g >= 0.5:
            return ghost
        return bg

    return [[paint(a, g) for a, g in zip(ra, rg)]
            for ra, rg in zip(lit, dim)]


_BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def _pack(font, pix, cols, rows, fg, bg):
    """Pixels into cells, in the font's way of making a cell."""
    cx, cy = _PER_CELL[font]
    high = len(pix)
    wide = len(pix[0]) if pix else 0

    def at(x, y):
        return pix[y][x] if y < high and x < wide else bg

    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            if font == BLOCK:
                v = at(c, r)
                line.append(None if v is None else (" ", None, v))
            else:
                up, down = at(c, 2 * r), at(c, 2 * r + 1)
                if up == down:
                    line.append(None if up is None else (" ", None, up))
                elif up is None:
                    line.append(("▄", down, None))
                elif down is None:
                    line.append(("▀", up, None))
                else:
                    line.append(("▀", up, down))
        out.append(tuple(line))
    return tuple(out)


def _pack_dots(lit, field, cols, rows, fg, bg, ghost):
    """Dots into braille cells. A cell any of whose dots lie in a ghost's
    field carries the mark that shows its unlit dots, in the ghost's
    colour: (ch, fg, ghost, True)."""
    high = len(lit)
    wide = len(lit[0]) if lit else 0
    blank = None if bg is None else (" ", None, bg)
    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            bits, faint = 0, False
            for dy in range(4):
                y = 4 * r + dy
                if y >= high:
                    break
                for dx in range(2):
                    x = 2 * c + dx
                    if x < wide:
                        if lit[y][x]:
                            bits |= _BRAILLE[dy][dx]
                        if field[y][x]:
                            faint = True
            if faint and ghost is not None:
                line.append((chr(0x2800 + bits), fg, ghost, True))
            elif bits:
                line.append((chr(0x2800 + bits), fg, bg))
            else:
                line.append(blank)
        out.append(tuple(line))
    return tuple(out)


def _corners(at, x, y):
    """The corners of bitmap pixel x, y to cut, as [(corner, the diagonal
    half drawn there)], the way pixel-art scalers round a bitmap.

    An inked pixel's outer corner is taken off: one where the pixel
    beside it, the one above or below it and the one diagonally out from
    it on that side are all empty, and the stroke carries on along the
    other two sides. A stroke's end stays square.

    Two inked pixels that touch only corner to corner are joined by a
    slope: each of the two empty pixels between them has the corner
    facing the diagonal filled, where that pixel is open to the outside
    (`_pack_cut` sees to it: the inside of a counter is never filled). An
    empty pixel whose diagonal neighbour is inked as well is the inside
    corner of a solid, and stays empty.
    """
    out = []
    here = at(x, y)
    for corner, dy, dx in (("tl", -1, -1), ("tr", -1, 1),
                           ("bl", 1, -1), ("br", 1, 1)):
        side, vert, diag = at(x + dx, y), at(x, y + dy), at(x + dx, y + dy)
        if here:
            if (not side and not vert and not diag
                    and at(x - dx, y) and at(x, y - dy)):
                out.append((corner, _KEEP[corner]))
        elif side and vert and not diag:
            out.append((corner, _FILL[corner]))
    return out


#: The half a cut corner leaves, and the half a filled one adds.
_KEEP = {"tl": "◢", "tr": "◣", "bl": "◥", "br": "◤"}
_FILL = {"tl": "◤", "tr": "◥", "bl": "◣", "br": "◢"}


def _pack_cut(font, bits, n, across, square, fg, bg):
    """BLOCK or HALF cells with their corners cut and their diagonal steps
    joined (`_corners`). `bits` is the bitmap as drawn, before scaling,
    and each of its pixels is `across` cells wide and n of the font's
    pixels tall; the cell at a corner of a pixel is given the diagonal
    half, where that cell is wholly the pixel's."""
    high = len(bits)
    wide = len(bits[0]) if bits else 0
    per = 1 if font == BLOCK else 2
    out = [list(row) for row in square]
    rows, cols = len(out), len(out[0]) if out else 0
    full = (" ", None, fg)
    blank = None if bg is None else (" ", None, bg)

    def at(x, y):
        return 1 if 0 <= y < high and 0 <= x < wide and bits[y][x] else 0

    # The empty pixels open to the outside, found by flooding in from
    # round the edge through empty pixels side to side, never across a
    # corner. What is left is the inside of a counter, an O's or a D's,
    # which a slope is never filled into.
    outside = set()
    todo = [(x, y) for x in range(-1, wide + 1) for y in (-1, high)] + \
        [(x, y) for y in range(high) for x in (-1, wide)]
    while todo:
        x, y = todo.pop()
        if (x, y) in outside or not (-1 <= x <= wide and -1 <= y <= high):
            continue
        if at(x, y):
            continue
        outside.add((x, y))
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

    for py in range(high):
        top, bottom = py * n, py * n + n          # the pixel's font rows
        first = -(-top // per)                    # cells wholly inside it
        last = bottom // per - 1
        if last < first:
            continue
        for px in range(wide):
            here = at(px, py)
            if not here and (px, py) not in outside:
                continue
            for corner, half in _corners(at, px, py):
                # The cell has to be flush with the pixel's corner, or the
                # cut would sit inside the pixel and leave a strip below.
                if (top if corner[0] == "t" else bottom) % per:
                    continue
                c = px * across + (0 if corner[1] == "l" else across - 1)
                r = first if corner[0] == "t" else last
                if not (0 <= r < rows and 0 <= c < cols):
                    continue
                if out[r][c] != (full if here else blank):
                    continue                    # taken by another corner
                out[r][c] = (half, fg, bg)
    return tuple(tuple(row) for row in out)


_drawn = OrderedDict()
_DRAWN_MAX = 256


def _render(text, font, fg, bg, base, o):
    """The text's cells, `font` the one the glyphs come from and `o` the
    options as `_setup` settled them."""
    lines_ = _lines(_plain(text, font, o))
    n = _step(font, lines_, o)
    smooth = (font == SEGMENT and o["smooth"] and n is not None
              and _smooth_helps(n, o["px"])
              and o["depth"] in ("true", "256")
              and _is_hex(fg) and _is_hex(base or "000000")
              and (o["ghost"] is None or _is_hex(o["ghost"])))
    ghost = ghost_for(o["ghost"], fg, o["depth"]) \
        if font in (SEGMENT, DOTS) else None
    # Cut edges are a crisp thing: smoothing already shades the slopes,
    # and a turned segment's cells would need halves a cell does not have.
    # HALF's edges fall on half cells, where a whole-cell ◢◣◤◥ cannot
    # follow them, so HALF stays square.
    cut = (o["edges"] == "cut" and font not in (DOTS, HALF)
           and not (font == SEGMENT and (smooth or o["turn"])))
    key = (text, font, o["seven"], fg, bg, base if smooth else None, smooth,
           ghost, cut,
           o["scale"], o["height"], o["spacing"], o["turn"], o["align"],
           o["cell"] if font in (BLOCK, HALF) + _PICTURED else None)
    got = _drawn.get(key)
    if got is not None:
        _drawn.move_to_end(key)
        return got
    if n is None or not any(lines_):
        got = ()
    elif font in _PICTURED:
        cols, rows_ = _dims(font, n, lines_, o)
        blank = (" ", None, bg)
        got = tuple((blank,) * cols for _ in range(rows_))
    elif font == SEGMENT and not o["turn"]:
        got = _seg_rows(lines_, n, o, fg, bg, ghost, smooth, base or "000000",
                        cut)
    elif font == DOTS:
        cols, rows_ = _dims(font, n, lines_, o)
        lit, field = _dotted(n, lines_, o)
        got = _pack_dots(lit, field, cols, rows_, fg, bg, ghost)
    else:
        cols, rows_ = _dims(font, n, lines_, o)
        lit, dim = _pixels(font, n, lines_, o)
        pix = _colours(lit, dim, fg, bg, ghost, smooth, base or "000000")
        got = _pack(font, pix, cols, rows_, fg, bg)
        if cut:
            got = _pack_cut(font, _pixels(font, 1, lines_, dict(o, cell=0))[0],
                            n, n * _widen(font, o["cell"]), got, fg, bg)
    _drawn[key] = got
    if len(_drawn) > _DRAWN_MAX:
        _drawn.popitem(last=False)
    return got


_cells = {}
_CELLS_MAX = 4096


def _seg_cells(ch, n, spacing, fg, bg, ghost, smooth, base, cut=False,
               seven=False):
    """One SEGMENT character as cells, made once for its size and colours.

    A pixel of it is a whole cell wide, so a character is a whole number
    of columns and a line of them is its characters' columns side by
    side: a clock that changes a digit makes that digit and no more.
    """
    key = (ch, n, spacing, fg, bg, ghost, smooth, base, cut, seven)
    got = _cells.get(key)
    if got is not None:
        return got
    if cut:
        parts, _width = _seg_cut(ch, n, spacing, ghost is not None, seven)
        got = _cut_cells(parts, fg, bg, ghost)
        if len(_cells) >= _CELLS_MAX:
            _cells.clear()
        _cells[key] = got
        return got
    on, off, width = _seg_glyph(ch, n, spacing, seven)
    lit = [[0.0] * width for _ in range(2 * n)]
    dim = [[0.0] * width for _ in range(2 * n)]
    for (x, y), a in on.items():
        if 0 <= x < width:
            lit[y][x] = a
    for (x, y), a in off.items():
        if 0 <= x < width:
            dim[y][x] = a
    pix = _colours(lit, dim, fg, bg, ghost, smooth, base)
    got = _pack(SEGMENT, pix, width, n, fg, bg)
    if len(_cells) >= _CELLS_MAX:
        _cells.clear()
    _cells[key] = got
    return got


def _seg_rows(lines_, n, o, fg, bg, ghost, smooth, base, cut=False):
    """Upright SEGMENT text, assembled from its characters' cells."""
    widths, _wide, _high, _tall, gap = _native(SEGMENT, n, lines_, o)
    cols, _rows = _dims(SEGMENT, n, lines_, o)
    blank = None if bg is None else (" ", None, bg)
    space = _seg_gap(n)
    out = []
    for i, line in enumerate(lines_):
        if i:
            out.extend([(blank,) * cols] * (gap // 2))
        left = _offset(widths[i], cols, o["align"])
        rows_ = [[blank] * left for _ in range(n)]
        for j, ch in enumerate(line):
            if j:
                for r in rows_:
                    r.extend([blank] * space)
            for r, part in zip(rows_, _seg_cells(ch, n, o["spacing"], fg, bg,
                                                 ghost, smooth, base, cut,
                                                 o["seven"])):
                r.extend(part)
        out.extend(tuple(r + [blank] * (cols - len(r))) for r in rows_)
    return tuple(out)


def rows(text, font=BLOCK, fg="ffffff", bg=None, **opts):
    """The text as rows of cells: None, or (ch, fg, bg) with None for a
    colour that is left as it is, and a fourth field, True, on a braille
    cell whose unlit dots a ghost shows (`bitwall.cell`'s faint). Shared
    with the cache: do not change.

    Smoothing mixes the edge of a letter into `bg`, or into black where
    there is none; `draw` mixes it into the grid's own colour instead.
    """
    font, o = _setup(font, opts)
    return _render(text, font, fg, bg, bg, o)


def draw(grid, text, x=0, y=0, fg="ffffff", bg=None, font=BLOCK, **opts):
    """Paint the text into a grid of `bitwall.cell`s, its top left at x, y.

    Clipped to the grid. Returns the (cols, rows) it takes. Given the
    `wall`, a font it cannot draw is drawn as the nearest it can, as
    `size` measures it. TILE needs the `wall`, which it sends its picture
    to; without one that offers kitty it is drawn as BLOCK with its
    corners cut.
    """
    if font in _PICTURED:
        return _draw_tile(font, grid, text, x, y, fg, bg, opts)
    font, o = _setup(font, opts)
    base = bg
    if base is None and 0 <= y < len(grid) and 0 <= x < len(grid[y]):
        under = grid[y][x][2]
        base = under if _is_hex(under) else None
    out = _render(text, font, fg, bg, base, o)
    for dy, line in enumerate(out):
        gy = y + dy
        if not 0 <= gy < len(grid):
            continue
        row = grid[gy]
        for dx, c in enumerate(line):
            gx = x + dx
            if c is None or not 0 <= gx < len(row):
                continue
            old = row[gx]
            row[gx] = _cell(c[0], old[1] if c[1] is None else c[1],
                            old[2] if c[2] is None else c[2],
                            faint=len(c) > 3 and c[3])
    return (len(out[0]) if out else 0, len(out))


# -------------------------------------------------------------------- TILE
#
# TILE is BLOCK's bitmap as a panel of lit tiles: each pixel a square tile
# with a hairline between them, a letter's outer corners rounded where its
# stroke turns, and the corner facing away from a diagonal step rounded
# too, so B, D and R hold together. It is drawn as one kitty picture laid
# over the cells the text takes, which are left blank -- geometry no
# character can be, and exact at whatever size the wall's cells are. So a
# tile need not be a whole cell: at `height` 2 five tiles stand in two rows.
#
# The picture is placed by the x and y `draw` is given, so the grid is the
# wall's own, or a part of it that starts at its top left, as a list of its
# first rows is.
#
# A picture is several kilobytes and is sent again whenever its text
# changes, where a cell font sends only the cells that changed. So TILE and
# the faces are for what stands still -- a heading, a name, a label that
# changes now and then -- and not for a clock's ticking seconds, a counter
# or anything animated, which belong in the cell fonts.
#
# `usable(TILE, w)` is a wall that offers kitty and a Pillow to draw with;
# where either is missing `draw` falls back to BLOCK with its corners cut,
# and `fit` passes TILE over for the next font in its list. `picture` and
# `kitty` are the same drawing for a terminal of one's own.

_pillow_ok = None


def _pillow():
    """Whether Pillow is there to draw TILE with; asked once."""
    global _pillow_ok
    if _pillow_ok is None:
        try:
            import PIL.Image                                   # noqa: F401
            _pillow_ok = True
        except ImportError:
            _pillow_ok = False
    return _pillow_ok


def _tile_turns(at, x, y):
    """Which of a lit tile's corners, tl tr br bl, round: where the tiles
    beside, above or below and diagonal on that side are all empty, and
    the stroke carries on round the other two sides -- or only across the
    diagonal, as B's bumps do, so a bump is a lobe and not a loose tile."""
    out = []
    for dy, dx in ((-1, -1), (-1, 1), (1, 1), (1, -1)):
        if at(x + dx, y) or at(x, y + dy) or at(x + dx, y + dy):
            out.append(False)
            continue
        out.append(bool((at(x - dx, y) and at(x, y - dy))
                        or at(x - dx, y - dy)))
    return tuple(out)


def _tile_grid(lines_, o):
    """The block in tiles, as (lit, ghost) grids of 0 and 1: lit where a
    letter's pixel is, ghost where it is not but lies in a letter's box."""
    widths, wide, high, tall, gap = _native(TILE, 1, lines_, o)
    lit = [[0] * wide for _ in range(high)]
    dim = [[0] * wide for _ in range(high)]
    for i, line in enumerate(lines_):
        top = i * (tall + gap)
        x = _offset(widths[i], wide, o["align"])
        for ch in line:
            g = _bits(TILE, ch, o["spacing"])
            for gy, row in enumerate(g):
                for gx, v in enumerate(row):
                    (lit if v else dim)[top + gy][x + gx] = 1
            x += len(g[0]) + 1
    return lit, dim


_tile_drawn = OrderedDict()


def _tile_png(text, fg, ground, o):
    """TILE text as a PNG of whole cells, and the (cols, rows) it covers,
    or None where it does not fit or says nothing. Cached."""
    lines_ = _lines(_plain(text, TILE, o))
    n = _step(TILE, lines_, o)
    if n is None or not any(lines_):
        return None
    pw, ph = o["px"] or (TILE_PX, round(TILE_PX * o["cell"]))
    ghost = o["ghost"] if _is_hex(o["ghost"]) else None
    key = (text, fg, ground, ghost, n, pw, ph, o["spacing"], o["align"],
           o["turn"])
    got = _tile_drawn.get(key)
    if got is not None:
        _tile_drawn.move_to_end(key)
        return got
    from PIL import Image, ImageDraw
    import io
    cols, rows_ = _dims(TILE, n, lines_, dict(o, cell=ph / pw))
    lit, dim = _tile_grid(lines_, o)
    high, wide = len(lit), len(lit[0]) if lit else 0
    side = n / TILE_STEPS * ph                  # a tile, in screen pixels
    up_w, up_h = (rows_ * ph, cols * pw) if o["turn"] else (cols * pw, rows_ * ph)
    ss = 4                                      # drawn larger, then scaled down
    big = Image.new("RGB", (up_w * ss, up_h * ss), "#" + ground)
    pen = ImageDraw.Draw(big)
    g = max(0.5, TILE_GAP * side)
    r = max(0.0, min(TILE_RADIUS * side, side / 2 - g))

    def at(x, y):
        return 0 <= y < high and 0 <= x < wide and lit[y][x]

    for y in range(high):
        for x in range(wide):
            if not (lit[y][x] or (ghost and dim[y][x])):
                continue
            box = [(x * side + g) * ss, (y * side + g) * ss,
                   ((x + 1) * side - g) * ss - 1, ((y + 1) * side - g) * ss - 1]
            colour = "#" + (fg if lit[y][x] else ghost)
            turns = _tile_turns(at, x, y) if lit[y][x] else (False,) * 4
            if any(turns) and r > 0:
                pen.rounded_rectangle(box, radius=r * ss, fill=colour, corners=turns)
            else:
                pen.rectangle(box, fill=colour)
    img = big.resize((up_w, up_h), Image.Resampling.LANCZOS)
    if o["turn"] == "up":
        img = img.transpose(Image.Transpose.ROTATE_90)
    elif o["turn"] == "down":
        img = img.transpose(Image.Transpose.ROTATE_270)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    got = (out.getvalue(), cols, rows_)
    _tile_drawn[key] = got
    if len(_tile_drawn) > 64:
        _tile_drawn.popitem(last=False)
    return got


# ------------------------------------------------------------------ faces
#
# WHITE_RABBIT, BYTES256 and VT323 are outline fonts, for a heading in a
# look no grid of cells can give: drawn by Pillow at the height asked for
# into one picture over the cells the text takes, placed and taken down as
# TILE's is. `height` is the rows a line takes, from one; `scale` is two
# rows a step. Where one cannot be drawn, `draw` falls back to DOTS, which
# has lower case too, then HALF and BLOCK.

_face_cache = {}
#: A face's size to take its measure at: widths are proportional to it.
_FACE_MEASURE = 100


def _face_file(font):
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts",
                        FACES[font])


def _face_metrics(font):
    """(a line's height, a function giving a text's width), in the same
    units, for a face; without Pillow a monospaced guess, so `size` still
    answers where the face cannot be drawn."""
    got = _face_cache.get(font)
    if got is None:
        if _pillow():
            from PIL import ImageFont
            f = ImageFont.truetype(_face_file(font), _FACE_MEASURE)
            ascent, descent = f.getmetrics()
            got = (ascent + descent, f.getlength)
        else:
            got = (1.2, lambda text: 0.6 * len(text))
        _face_cache[font] = got
    return got


def _face_png(font, text, fg, ground, o):
    """A face's text as a PNG of whole cells and the (cols, rows) it
    covers, or None where it says nothing. Cached with TILE's pictures."""
    lines_ = _lines(_plain(text, font, o))
    n = _step(font, lines_, o)
    if n is None or not any(lines_):
        return None
    pw, ph = o["px"] or (TILE_PX, round(TILE_PX * o["cell"]))
    key = (font, text, fg, ground, n, pw, ph, o["align"], o["turn"])
    got = _tile_drawn.get(key)
    if got is not None:
        _tile_drawn.move_to_end(key)
        return got
    from PIL import Image, ImageDraw, ImageFont
    import io
    cols, rows_ = _dims(font, n, lines_, dict(o, cell=ph / pw))
    line_em, _width = _face_metrics(font)
    size = max(1, round(n * ph * _FACE_MEASURE / line_em))
    f = ImageFont.truetype(_face_file(font), size)
    up_w, up_h = (rows_ * ph, cols * pw) if o["turn"] else (cols * pw, rows_ * ph)
    img = Image.new("RGB", (up_w, up_h), "#" + ground)
    pen = ImageDraw.Draw(img)
    for i, line in enumerate(lines_):
        x = _offset(round(f.getlength(line)), up_w, o["align"])
        pen.text((x, i * n * ph), line, font=f, fill="#" + fg, anchor="la")
    if o["turn"] == "up":
        img = img.transpose(Image.Transpose.ROTATE_90)
    elif o["turn"] == "down":
        img = img.transpose(Image.Transpose.ROTATE_270)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    got = (out.getvalue(), cols, rows_)
    _tile_drawn[key] = got
    if len(_tile_drawn) > 64:
        _tile_drawn.popitem(last=False)
    return got


def _png(font, text, fg, ground, o):
    return _tile_png(text, fg, ground, o) if font == TILE \
        else _face_png(font, text, fg, ground, o)


def picture(text, fg="ffffff", bg="000000", font=TILE, **opts):
    """TILE or a face's text as a PNG and the (cols, rows) of cells it
    covers, or None where it does not fit. For a terminal of one's own:
    `kitty` places it. `px` says a cell's pixels, else it is drawn at
    TILE_PX a cell across; `bg` is the ground it is drawn on, since a
    terminal may not honour transparency."""
    if font not in _PICTURED:
        raise ValueError("picture draws TILE and the faces")
    if not _pillow():
        return None
    font, o = _setup(font, opts)
    return _png(font, text, fg, bg if _is_hex(bg) else "000000", o)


def kitty(png, cols, rows, ident=None, at=None):
    """The kitty graphics string that shows `png` over cols x rows cells
    from the cursor -- or from `at`, (col, row) counted from 0 -- leaving
    the cursor where it was and asking the terminal to answer nothing."""
    import base64
    data = base64.b64encode(png).decode()
    parts = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    keys = f"a=T,f=100,c={cols},r={rows},C=1,q=2"
    if ident is not None:
        keys += f",i={ident}"
    out = [] if at is None else [f"\x1b[{at[1] + 1};{at[0] + 1}H"]
    for k, part in enumerate(parts):
        more = 1 if k < len(parts) - 1 else 0
        head = f"{keys},m={more}" if k == 0 else f"m={more}"
        out.append(f"\x1b_G{head};{part}\x1b\\")
    return "".join(out)


class _Placed:
    """The TILE pictures one wall shows, where, and what of.

    A picture is sent when its place first holds TILE text or the text
    there changes, and not again while the same text is drawn there every
    frame. After each frame (`Wall.after_frame`) a place no longer drawn
    is taken down, and on leaving (`Wall.before_leaving`) all of them are,
    since a terminal keeps a picture after the sender that sent it."""

    #: Kitty image ids start here: out of the way of a sender's own.
    BASE = 0x5EED0000

    def __init__(self, wall):
        self.wall = wall
        self.shown = {}                  # (x, y) -> (key, id)
        self.now = set()
        self.next = 0
        wall.after_frame(self.frame)
        wall.before_leaving(self.clear)
        if hasattr(wall, "when_lost"):
            wall.when_lost(self.forget)

    def want(self, spot, key, make):
        self.now.add(spot)
        had = self.shown.get(spot)
        if had is not None and had[0] == key:
            return
        got = make()
        if got is None:
            return
        png, cols, rows = got
        if had is not None:
            ident = had[1]
        else:
            ident = self.BASE + self.next
            self.next += 1
        self.wall.passthrough((f"\x1b_Ga=d,d=I,i={ident},q=2\x1b\\"
                               + kitty(png, cols, rows, ident, spot)).encode())
        self.shown[spot] = (key, ident)

    def frame(self):
        gone = [spot for spot in self.shown if spot not in self.now]
        if gone:
            self.wall.passthrough("".join(
                f"\x1b_Ga=d,d=I,i={self.shown.pop(spot)[1]},q=2\x1b\\"
                for spot in gone).encode())
        self.now = set()

    def forget(self):
        """The wall is gone, and the one that comes back may hold none of
        these: each is sent again when next drawn."""
        self.shown = {}

    def clear(self):
        if self.shown:
            self.wall.passthrough("".join(
                f"\x1b_Ga=d,d=I,i={ident},q=2\x1b\\"
                for _key, ident in self.shown.values()).encode())
        self.shown = {}


_placed = {}


def _placed_on(wall):
    got = _placed.get(id(wall))
    if got is None or got.wall is not wall:
        got = _placed[id(wall)] = _Placed(wall)
    return got


#: What TILE is drawn as where it cannot be, best first: the same 3x5
#: letters with their corners cut, whole cells a pixel, from five rows;
#: the same letters in half blocks, from three; then DOTS, from two; and
#: where none of those can be drawn or fits, the text itself on one row.
TILE_INSTEAD = ((BLOCK, {"edges": "cut"}), (HALF, {}), (DOTS, {}))
#: A face's: DOTS first, lower case and all, from two rows; then capitals.
FACE_INSTEAD = ((DOTS, {}), (HALF, {}), (BLOCK, {"edges": "cut"}))


def _tile_fallback(pictured, grid, text, x, y, fg, bg, opts):
    """A pictured font's text in the first of its INSTEAD list the wall can
    draw and the size asked for fits, so a height it fills is filled."""
    wall = opts.get("wall")
    base = {k: v for k, v in opts.items() if k not in ("ghost", "edges")}
    for font, extra in (TILE_INSTEAD if pictured == TILE else FACE_INSTEAD):
        if not usable(font, wall):
            continue
        these = dict(base, **extra)
        if these.get("edges") == "cut" and wall is not None \
                and not wall.can("◢◣◤◥"):
            these["edges"] = "square"
        if size(text, font, **these) != (0, 0):
            return draw(grid, text, x, y, fg, bg, font=font, **these)
    # Nothing drawn fits: the words themselves, on the first row.
    line = plain(text, BLOCK).replace("\n", " ").strip()
    if not line or not 0 <= y < len(grid):
        return (0, 0)
    row = grid[y]
    for i, ch in enumerate(line):
        if 0 <= x + i < len(row):
            row[x + i] = _cell(ch, fg, row[x + i][2] if bg is None else bg)
    return (min(len(line), max(0, len(row) - x)), 1)


def _draw_tile(pictured, grid, text, x, y, fg, bg, opts):
    """draw() for TILE and the faces: blank cells, the picture over them."""
    wall = opts.get("wall")
    if not (usable(pictured, wall) and _is_hex(fg)):
        return _tile_fallback(pictured, grid, text, x, y, fg, bg, opts)
    font, o = _setup(pictured, opts)
    base = bg
    if base is None and 0 <= y < len(grid) and 0 <= x < len(grid[y]):
        under = grid[y][x][2]
        base = under if _is_hex(under) else None
    ground = base if _is_hex(base) else "000000"
    out = _render(text, font, fg, bg, base, o)
    cols, rows_ = (len(out[0]) if out else 0, len(out))
    for dy in range(rows_):
        gy = y + dy
        if not 0 <= gy < len(grid):
            continue
        row = grid[gy]
        for dx in range(cols):
            gx = x + dx
            if 0 <= gx < len(row):
                row[gx] = _cell(" ", row[gx][1], row[gx][2] if bg is None else bg)
    # Only what lies on the grid is shown: a picture past its edge would
    # scroll a terminal.
    seen_c = min(cols, len(grid[0]) - x) if grid else 0
    seen_r = min(rows_, len(grid) - y)
    if cols and rows_ and x >= 0 and y >= 0 and seen_c > 0 and seen_r > 0:
        def make():
            got = _png(font, text, fg, ground, o)
            if got is None or (seen_c, seen_r) == got[1:]:
                return got
            from PIL import Image
            import io
            png, c, r = got
            img = Image.open(io.BytesIO(png))
            img = img.crop((0, 0, img.width * seen_c // c, img.height * seen_r // r))
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            return buf.getvalue(), seen_c, seen_r
        key = (font, text, fg, ground, o["ghost"], o["scale"], o["height"],
               o["spacing"], o["align"], o["turn"], o["px"], o["cell"],
               seen_c, seen_r)
        _placed_on(wall).want((x, y), key, make)
    return (cols, rows_)


# -------------------------------------------------------------- pictures

#: The ways a cell can be split between two colours, each as the points of
#: a cell two across and four down (numbered across then down, 0..7) that
#: the character's own colour covers. Tried in this order, and a later one
#: has to be strictly better, so the plainer shape wins a tie.
SHAPES = (("▀", (0, 1, 2, 3)), ("▄", (4, 5, 6, 7)),
          ("▌", (0, 2, 4, 6)), ("▐", (1, 3, 5, 7)),
          ("◢", (3, 5, 6, 7)), ("◣", (2, 4, 6, 7)),
          ("◤", (0, 1, 2, 4)), ("◥", (0, 1, 3, 5)),
          ("▘", (0, 2)), ("▝", (1, 3)), ("▖", (4, 6)), ("▗", (5, 7)))


def shapes(wall=None):
    """The splits of `SHAPES` that `wall` can draw, for `pack`; all of
    them where there is no wall, the same as a wall that has not said."""
    return tuple(s for s in SHAPES if wall is None or wall.can(s[0]))


def _major(colours):
    counts = {}
    for c in colours:
        counts[c] = counts.get(c, 0) + 1
    best = max(counts, key=counts.get)
    return best, counts[best]


@functools.lru_cache(maxsize=1 << 16)
def pack_cell(points, allowed=SHAPES):
    """Eight colours, a cell's points across then down, as the cell that
    shows them best: one colour, or the split of `allowed` that gets the
    most points right, the character in one colour and the ground in the
    other. Colours are whatever `bitwall.cell` takes."""
    first = points[0]
    if points.count(first) == 8:
        return _cell(" ", "default", first)
    major, n = _major(points)
    best = (8 - n, " ", major, major)
    for ch, inside in allowed:
        a, na = _major([points[k] for k in inside])
        b, nb = _major([points[k] for k in range(8) if k not in inside])
        miss = 8 - na - nb
        if a != b and miss < best[0]:
            best = (miss, ch, a, b)
            if not miss:
                break
    _miss, ch, a, b = best
    return _cell(ch, a, b) if ch != " " else _cell(" ", "default", a)


def pack(points, allowed=SHAPES):
    """A picture of colours, two points across and four down to a cell,
    as rows of cells: each cell the shape of `allowed` that shows its
    points best (`pack_cell`). Pass `shapes(wall)` for the shapes a wall
    can draw. A picture whose sides are not whole cells is cut to them."""
    rows = len(points) // 4
    cols = len(points[0]) // 2 if rows else 0
    out = []
    for cy in range(rows):
        a, b, c, d = points[cy * 4:cy * 4 + 4]
        out.append([pack_cell((a[x], a[x + 1], b[x], b[x + 1],
                               c[x], c[x + 1], d[x], d[x + 1]), allowed)
                    for x in range(0, cols * 2, 2)])
    return out


# ------------------------------------------------------------------- fit

def _wrapped(font, n, words, cols, o):
    """Words broken into lines no wider than cols at step n, or None
    where one word alone is wider."""
    lines_, line = [], ""
    for word in words:
        if _dims(font, n, [word], o)[0] > cols:
            return None
        trial = word if not line else line + " " + word
        if not line or _dims(font, n, [trial], o)[0] <= cols:
            line = trial
        else:
            lines_.append(line)
            line = word
    if line:
        lines_.append(line)
    return lines_


def fit(text, cols, rows, fonts=(HALF, BLOCK), wall=None, wrap=False,
        **opts):
    """The biggest the text can be drawn in cols x rows, or None.

    Fonts are tried in the order given, skipping any the wall cannot
    draw, and the first that fits at all is the answer, at its biggest.
    With `wrap`, upright text may be broken between words onto more
    lines where that lets it be bigger.

    The answer is the keywords to draw it with -- text, font, height and
    the options given -- so `draw(grid, x=.., y=.., fg=.., **got)` and
    `size(**got)` both take it as it is. Pass it alone: its text is the
    text as it is to be drawn, broken onto lines where it was wrapped, so
    a text given beside it as well is one text too many.

    With a `wall` (or `px`), the answer suits the resolution it has:
    SEGMENT is passed over below `SEGMENT_READABLE` rows, and DOTS where
    its dots would blur (DOTS_DISTINCT), while another font in the list
    fits; either is used only where nothing else does. The cells' size
    in pixels, and the wall's depth, go into the answer, so drawing it
    makes the same choices the fitting did.
    """
    # A poor choice for the resolution is the answer only when nothing
    # else, the same font at a size that reads included, fits at all.
    for strict in (True, False):
        got = _fit_once(text, cols, rows, fonts, wall, wrap, opts, strict)
        if got is not None:
            return got
    return None


def _fit_once(text, cols, rows, fonts, wall, wrap, opts, strict):
    """fit, passing over poor choices for the resolution when strict."""
    for asked in fonts:
        if not usable(asked, wall):
            continue
        font, o = _setup(asked, dict(opts, wall=wall))
        if strict and font == DOTS and not _dots_distinct(o["px"]):
            continue
        # Seven bars have no diagonals to crowd, so SEGMENT7 reads at its
        # smallest.
        low = SEGMENT_READABLE if strict and font == SEGMENT \
            and not o["seven"] else _smallest(font)
        t = _plain(text, font, o)
        paras = _lines(t)
        best = None
        if o["turn"]:
            # Turned, a height is a length: the largest that also fits
            # across is the one wanted.
            for h in range(rows, 0, -1):
                trial = dict(o, height=h)
                n = _step(font, paras, trial)
                if n is not None and n >= low \
                        and _dims(font, n, paras, trial)[0] <= cols:
                    best = (t, _dims(font, n, paras, trial)[1])
                    break
        else:
            probe = dict(o, height=None, scale=1)
            top = TILE_STEPS * rows // 5 if font == TILE \
                else rows if font in (SEGMENT,) + tuple(FACES) \
                else max(1, rows)
            for n in range(top, low - 1, -1):
                if wrap:
                    split = []
                    for para in paras:
                        got = _wrapped(font, n, para.split(), cols, probe)
                        if got is None:
                            split = None
                            break
                        split += got or [""]
                    if split is None:
                        continue
                    lines_ = split
                else:
                    lines_ = paras
                c, r = _dims(font, n, lines_, probe)
                if c <= cols and r <= rows:
                    best = ("\n".join(lines_), r)
                    break
            # The answer is a height in rows, and drawing at a height takes
            # the largest step whose rows fit it: for TILE, whose steps are
            # finer than a row, a larger step than the one that fitted, and
            # too wide. So the rows are given back only where the step they
            # draw at fits too, a row less at a time until one does.
            if best and font == TILE:
                lines_ = best[0].split("\n")
                r = best[1]
                while r >= 1:
                    n = _step(font, lines_, dict(probe, height=r))
                    if n is not None and n >= low and \
                            _dims(font, n, lines_, probe)[0] <= cols:
                        break
                    r -= 1
                best = (best[0], r) if r >= 1 else None
        if best and best[0].strip():
            o2 = {k: v for k, v in opts.items() if k not in ("scale",
                                                               "height")}
            if o["px"] is not None:
                o2["px"] = o["px"]
            if wall is not None and "depth" not in opts:
                o2["depth"] = o["depth"]
            return dict(o2, text=best[0], font=asked, height=best[1])
    return None


def centred(grid, text, fg="ffffff", bg=None, fonts=(HALF, BLOCK),
            wall=None, margin=0, wrap=True, **opts):
    """Fit the text to the grid and draw it in the middle.

    Where no font fits, the text is written as it is across the middle
    row, cut to the width. Returns what `fit` chose, or None.
    """
    high = len(grid)
    wide = len(grid[0]) if grid else 0
    got = fit(text, wide - 2 * margin, high - 2 * margin, fonts=fonts,
              wall=wall, wrap=wrap, align=opts.pop("align", "centre"),
              **opts)
    if got is None:
        line = text.replace("\n", " ")[:wide]
        if high and line:
            x0 = (wide - len(line)) // 2
            row = grid[high // 2]
            for i, ch in enumerate(line):
                old = row[x0 + i]
                row[x0 + i] = _cell(ch, fg, old[2] if bg is None else bg)
        return None
    c, r = size(**{k: v for k, v in got.items()
                   if k not in ("smooth", "depth", "ghost")})
    draw(grid, x=(wide - c) // 2, y=(high - r) // 2, fg=fg, bg=bg, **got)
    return got
