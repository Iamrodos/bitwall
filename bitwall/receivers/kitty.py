"""Kitty graphics in a wall's grid, for the receiver core (`core.py`):
the strings read out of the base's stream, and what each session has
defined and placed.

    split = Splitter()
    for kind, part in split.feed(data):     # "text", "kitty" or "overlong"
        ...
    k = Kitty(say, place_at, total)
    k.string(body)                           # the bytes after ESC _G
    k.clear()

It is PROTOCOL.md's "A grid may carry kitty graphics", as the painter
draws it (`kitty_apc` and its neighbours in bitwall-paint.c), whose order
of checks, wording and sizing it follows. The server does everything but
the drawing: it puts the chunks together, holds the limits and the ids,
checks a PNG is one and raw pixels are the size they say, and says every
refusal and `a=q` answer on the session's `out`. A tab is handed each
image's bytes once under a key of the wall's own, new for every picture,
so a decode that finishes after its picture was replaced or deleted is
known for stale, and the placements as one list in the order they are
drawn: by z, then as placed.
"""
import binascii
import re
import struct
import zlib

#: An image received and its sides, as a video frame's; images and
#: placements a session holds; decoded pixels a wall holds; and the
#: longest single string, past which one is refused whole.
RECEIVED = 16 << 20
SIDE = 4096
IMAGES = 32
PLACES = 64
WALL = 64 << 20
STRING = 512 << 10

INTRO = b"\x1b_G"
ST = b"\x1b\\"
_NOT_B64 = re.compile(rb"[^A-Za-z0-9+/= \n\r]")
_SPACE = re.compile(rb"[ \n\r]+")
PNG_SIG = b"\x89PNG\r\n\x1a\n"


class Splitter:
    """The base's bytes cut into the text around kitty strings and the
    strings themselves, however the pipe broke them. A string still
    arriving is gathered piece by piece, each piece looked through once,
    since a picture comes 64 KB a read; one longer than STRING is
    `overlong` and let go up to its end."""

    def __init__(self):
        self.held = b""         # text that may be the start of a string
        self.parts = None       # a string's pieces so far, from its ESC _G
        self.size = 0
        self.skip = False       # a string too long, let go up to its end
        self.esc = False        # the last piece ended with a lone ESC

    def _end(self, data):
        """Where ST ends in `data`, a string's next piece, counting one cut
        across the two: the index after it, or -1."""
        if self.esc and data[:1] == b"\\":
            return 1
        j = data.find(ST)
        return -1 if j < 0 else j + 2

    def feed(self, data):
        out = []
        while data:
            if self.parts is not None or self.skip:
                e = self._end(data)
                if e < 0:
                    self.esc = data.endswith(b"\x1b")
                    if self.skip:
                        return out
                    self.parts.append(data)
                    self.size += len(data)
                    if self.size > STRING:
                        out.append(("overlong", None))
                        self.parts, self.skip = None, True
                    return out
                self.esc = False
                if self.skip:
                    self.skip = False
                elif self.size + e - 2 > STRING:
                    self.parts = None
                    out.append(("overlong", None))
                else:
                    self.parts.append(data[:e])
                    whole = b"".join(self.parts)
                    self.parts = None
                    out.append(("kitty", whole[3:-2]))
                data = data[e:]
                continue
            b = self.held + data if self.held else data
            self.held = b""
            i = b.find(INTRO)
            if i < 0:
                keep = 2 if b.endswith(b"\x1b_") else 1 if b.endswith(b"\x1b") else 0
                if len(b) > keep:
                    out.append(("text", b[:len(b) - keep]))
                self.held = b[len(b) - keep:]
                return out
            if i:
                out.append(("text", b[:i]))
            j = b.find(ST, i + 3)
            if j >= 0:
                out.append(("kitty", b[i + 3:j]) if j - i <= STRING else ("overlong", None))
                data = b[j + 2:]
                continue
            self.parts, self.size, self.esc = [b[i:]], len(b) - i, b.endswith(b"\x1b")
            if self.size > STRING:
                out.append(("overlong", None))
                self.parts, self.skip = None, True
            return out
        return out


class Keys:
    """The keys of one string, as far as they are drawn. `other` is a key
    besides `m` and `q`, which a chunk after the first never has."""

    __slots__ = ("a", "t", "d", "o", "f", "s", "v", "m", "U", "C", "z", "c", "r",
                 "i", "p", "other")

    def __init__(self):
        self.a = self.t = self.d = self.o = ""
        self.f = self.s = self.v = self.m = self.U = self.C = self.z = 0
        self.c = self.r = self.i = self.p = 0
        self.other = False


def _num(v):
    neg = v.startswith(b"-")
    d = 0
    for ch in v[1 if neg else 0:]:
        if not 48 <= ch <= 57:
            break
        if d < 100000000000:
            d = d * 10 + ch - 48
    d = -d if neg else d
    return max(-(1 << 31), min((1 << 31) - 1, d))


def keys(body):
    """The keys, and the payload after them."""
    semi = body.find(b";")
    head, payload = (body, b"") if semi < 0 else (body[:semi], body[semi + 1:])
    k = Keys()
    for part in head.split(b","):
        if len(part) < 2 or part[1:2] != b"=":
            continue
        key, v = chr(part[0]), part[2:]
        if key not in ("m", "q"):
            k.other = True
        if key in ("a", "t", "d", "o"):
            setattr(k, key, chr(v[0]) if v else "")
        elif key in ("f", "s", "v", "m", "U", "C", "z"):
            setattr(k, key, _num(v))
        elif key in ("c", "r"):
            setattr(k, key, max(0, min(1000, _num(v))))
        elif key in ("i", "p"):
            setattr(k, key, max(0, _num(v)))
    return k, payload


class _Pending:
    """An image arriving in chunks: its first chunk's keys, what its base64
    has decoded to so far, the sextets carried across a chunk, and whether
    it was refused, when its chunks are let go."""

    def __init__(self, k):
        self.k = k
        self.data = bytearray()
        self.carry = b""
        self.bad = False
        self.refused = False

    def take(self, payload):
        """Base64 onto the image; False once it is past RECEIVED."""
        if _NOT_B64.search(payload):
            self.bad = True
            payload = re.sub(rb"[^A-Za-z0-9+/=]", b"", payload)
        elif b" " in payload or b"\n" in payload or b"\r" in payload:
            payload = _SPACE.sub(b"", payload)
        text = self.carry + payload
        self.carry = b""
        pieces = text.split(b"=")
        # every piece but the last was closed by padding
        for piece in pieces[:-1]:
            if piece:
                self._decode(piece, True)
        last = pieces[-1]
        whole = len(last) - len(last) % 4
        if whole:
            self._decode(last[:whole], False)
        self.carry = last[whole:]
        return len(self.data) <= RECEIVED

    def _decode(self, piece, closed):
        cut = len(piece) % 4
        if closed and cut:
            head, tail = piece[:len(piece) - cut], piece[len(piece) - cut:]
            if head:
                self.data += binascii.a2b_base64(head)
            if cut >= 2:
                self.data += binascii.a2b_base64(tail + b"=" * (4 - cut))
        else:
            self.data += binascii.a2b_base64(piece)

    def finish(self):
        """What is left in the carry, closed as padding would close it."""
        if len(self.carry) >= 2:
            self._decode(self.carry, True)
        self.carry = b""


def png_size(data):
    """A PNG's width and height, or None if it is not one: its signature,
    its chunks whole with their CRCs, an IHDR first and an IEND last, and
    its image data inflating to what IHDR says it holds."""
    if len(data) < 8 + 25 + 12 or not data.startswith(PNG_SIG):
        return None
    p, n = 8, len(data)
    w = h = None
    idat = []
    ended = False
    while p + 12 <= n:
        size = struct.unpack_from(">I", data, p)[0]
        kind = data[p + 4:p + 8]
        end = p + 12 + size
        if end > n:
            return None
        body = data[p + 8:p + 8 + size]
        if zlib.crc32(kind + body) != struct.unpack_from(">I", data, p + 8 + size)[0]:
            return None
        if w is None:
            if kind != b"IHDR" or size != 13:
                return None
            w, h, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
            if not w or not h or channels is None or depth not in (1, 2, 4, 8, 16):
                return None
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            ended = True
            break
        p = end
    if not ended or not idat:
        return None
    if w > SIDE or h > SIDE:
        return w, h
    bits = channels * depth
    if interlace:
        want = 0
        for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                               (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
            pw, ph = -(-(w - x0) // dx), -(-(h - y0) // dy)
            if pw > 0 and ph > 0:
                want += ph * (1 + (pw * bits + 7) // 8)
    else:
        want = h * (1 + (w * bits + 7) // 8)
    z = zlib.decompressobj()
    try:
        got = len(z.decompress(b"".join(idat), want + 1))
    except zlib.error:
        return None
    return (w, h) if got == want else None


class Image:
    """One image a session defined: its id, the wall's key for this
    picture of it, its size, its format and bytes as sent, and what it
    counts against the wall."""

    __slots__ = ("id", "key", "w", "h", "f", "data", "bytes")


class Place:
    """One placement: its image, its id, the cell it is at, the cells asked
    for (0 where kitty works them out), its z and when it was placed."""

    __slots__ = ("image", "pid", "col", "row", "c", "r", "z", "seq")


class Kitty:
    """One session's kitty graphics.

    `say(line)` puts a line on its `out`; `cursor()` is where its grid's
    cursor is and `move(dx, dy)` moves it; `cell()` is a cell's size in
    the glass's pixels; `wall` is a one-item list, the decoded bytes held
    across the wall; `tabs(kind, arg)` tells the tabs of an `image`, a
    key to `forget`, or the `places` as `listing()` has them; `keyer()`
    is a new key of the wall's."""

    def __init__(self, say, cursor, move, cell, wall, tabs, keyer):
        self.say_line = say
        self.cursor = cursor
        self.move = move
        self.cell = cell
        self.wall = wall
        self.tabs = tabs
        self.keyer = keyer
        self.images = {}
        self.places = []
        self.seq = 0
        self.auto_id = 0
        self.pending = None

    def say(self, id_, why):
        if why:
            self.say_line(f"error kitty i={id_}: {why}")
        else:
            self.say_line(f"kitty i={id_} OK")

    # -- reading -----------------------------------------------------

    def overlong(self):
        """A string too long to hold: refused, and the image it belonged
        to let go."""
        self.say(self.pending.k.i if self.pending else 0, "too big")
        if self.pending:
            self.pending.refused = True

    def string(self, body):
        """One kitty string, the bytes after `ESC _G`."""
        k, payload = keys(body)
        pend = self.pending
        if pend is not None and not k.other:
            if not pend.refused and not pend.take(payload):
                self.say(pend.k.i, "too big")
                pend.refused = True
            if not k.m:
                if not pend.refused:
                    self._finish(pend)
                self.pending = None
            return
        self.pending = None             # one whose chunks stopped coming
        a = k.a or "t"
        if k.U or k.o:
            self._refuse(k, "not offered")
            return
        if a in ("t", "T", "q"):
            if k.t and k.t != "d":
                self._refuse(k, "not offered")
                return
            pend = self.pending = _Pending(k)
            if not pend.take(payload):
                self.say(k.i, "too big")
                pend.refused = True
            if not k.m:
                if not pend.refused:
                    self._finish(pend)
                self.pending = None
        elif a == "p":
            im = self.images.get(k.i) if k.i else None
            if im is None:
                self.say(k.i, "no such image")
            else:
                self._place(k, im)
                self._send_places()
        elif a == "d":
            self._delete(k)
        else:
            self._refuse(k, "not offered")

    def _refuse(self, k, why):
        """A refusal on a string that said more chunks follow: they are
        let go."""
        self.say(k.i, why)
        if k.m:
            p = self.pending = _Pending(k)
            p.refused = True

    # -- an image, whole ---------------------------------------------

    def _finish(self, pend):
        k = pend.k
        a = k.a or "t"
        f = k.f or 32
        pend.finish()
        data = bytes(pend.data)
        old = None
        if a != "q":
            old = self.images.get(k.i) if k.i else None
            if old is None and len(self.images) >= IMAGES:
                self.say(k.i, "too many")
                return
        if f not in (100, 24, 32):
            self.say(k.i, "not offered")
            return
        if pend.bad or not data:
            self.say(k.i, "does not decode")
            return
        if f == 100:
            size = png_size(data)
            if size is None:
                self.say(k.i, "does not decode")
                return
            w, h = size
            if w > SIDE or h > SIDE:
                self.say(k.i, "too big")
                return
        else:
            w, h = k.s, k.v
            if w < 1 or h < 1:
                self.say(k.i, "does not decode")
                return
            if w > SIDE or h > SIDE:
                self.say(k.i, "too big")
                return
            if len(data) != w * h * (3 if f == 24 else 4):
                self.say(k.i, "does not decode")
                return
        count = w * h * 4
        was = old.bytes if old is not None else 0
        if self.wall[0] - was + count > WALL:
            self.say(k.i, "too big")
            return
        if a == "q":
            self.say(k.i, None)
            return
        if old is not None:
            # the same id sent again is the picture replaced where it is placed
            self.wall[0] -= old.bytes
            self.tabs("forget", old.key)
            im = old
        else:
            im = Image()
            if k.i:
                im.id = k.i
            else:
                im.id = 0xF0000000 + (self.auto_id & 0x0FFFFFFF)
                self.auto_id += 1
            self.images[im.id] = im
        im.key = self.keyer()
        im.w, im.h, im.f, im.data, im.bytes = w, h, f, data, count
        self.wall[0] += count
        self.tabs("image", im)
        if a == "T":
            self._place(k, im)
        if a == "T" or old is not None:
            self._send_places()

    # -- placing and deleting ----------------------------------------

    def box(self, pl):
        """The placement's size in glass pixels, from the cell now: c and r
        both given, stretched to fill them; one given, the other from the
        picture's shape; neither, its own pixels."""
        cw, ch = self.cell()
        im = pl.image
        if pl.c and pl.r:
            w, h = pl.c * cw, pl.r * ch
        elif pl.c:
            w = pl.c * cw
            h = int(w * im.h / im.w + 0.5)
        elif pl.r:
            h = pl.r * ch
            w = int(h * im.w / im.h + 0.5)
        else:
            w, h = im.w, im.h
        return max(1, w), max(1, h)

    def _place(self, k, im):
        """Image `im` placed at the cursor."""
        at = None
        if k.p:
            for n, pl in enumerate(self.places):
                if pl.image is im and pl.pid == k.p:
                    at = n
                    break
        if at is None and len(self.places) >= PLACES:
            self.say(k.i, "too many")
            return
        pl = Place()
        pl.image, pl.pid = im, k.p
        pl.col, pl.row = self.cursor()
        pl.c, pl.r, pl.z = k.c, k.r, k.z
        self.seq += 1
        pl.seq = self.seq
        if at is None:
            self.places.append(pl)
        else:
            self.places[at] = pl
        if not k.C:
            cw, ch = self.cell()
            w, h = self.box(pl)
            self.move(-(-w // cw), -(-h // ch) - 1)

    def _forget(self, im):
        self.places = [pl for pl in self.places if pl.image is not im]
        self.images.pop(im.id, None)
        self.wall[0] -= im.bytes
        self.tabs("forget", im.key)

    def _delete(self, k):
        d = k.d or "a"
        before = len(self.places)
        if d in ("a", "A"):
            self.places = []
            if d == "A":
                for im in list(self.images.values()):
                    self._forget(im)
        elif d in ("i", "I", "p", "P"):
            im = self.images.get(k.i) if k.i else None
            if im is None or (d in ("p", "P") and not k.p):
                return
            self.places = [pl for pl in self.places
                           if not (pl.image is im and (not k.p or pl.pid == k.p))]
            if d == "I" and not any(pl.image is im for pl in self.places):
                self._forget(im)
        else:
            self.say(k.i, "not offered")
            return
        if len(self.places) != before:
            self._send_places()

    def clear(self):
        """Everything the session had: it has ended."""
        had = bool(self.places)
        self.places = []
        for im in list(self.images.values()):
            self._forget(im)
        self.pending = None
        if had:
            self._send_places()

    # -- to the tabs -------------------------------------------------

    def order(self):
        """The placements in the order they are drawn: by z, then as placed."""
        return sorted(self.places, key=lambda pl: (pl.z, pl.seq))

    def listing(self):
        return [[pl.image.key, pl.col, pl.row, pl.c, pl.r, pl.z, pl.image.w, pl.image.h]
                for pl in self.order()]

    def _send_places(self):
        self.tabs("places", self.listing())
