"""The conversation between a sender and a receiver.

A session's two pipes. Frames and requests go down its `in`; everything a
receiver has to say to it comes back up its `out`. See PROTOCOL.md for why.

Both directions are text a person can read, and deliberately so: the frame
stream stays paintable by a terminal that knows nothing about bitwall, and
what comes back is lines that `grep` and `awk` are enough to handle. A
receiver that wants to act on commands can `tee` its input and pull them
out with a pipeline rather than a program.

    down (in)   ESC _ bitwall font 14 ESC \\
    up   (out)  area 174x42 14

Requests are advisory and announcements are the truth. Nothing here
acknowledges anything: a sender asks for a font size and learns what
happened by being told the new area, not by being answered. So a command
that is dropped is not a silent failure -- the state simply does not
change, and that is visible.
"""
import re

#: An Application Programming Command string. A terminal ignores one it
#: does not recognise, which is what the sequence is for -- so commands
#: can travel in the frame stream without a terminal trying to draw them,
#: and the stream stays paintable.
APC = "\x1b_"
ST = "\x1b\\"

#: Every command carries this so that ours are distinguishable from
#: anyone else's APC strings in the same stream.
MARK = "bitwall"

#: The version of the framing, and the grammar it is framed in. This is
#: the first line a receiver sends and the one thing whose encoding can
#: never change: it says how to read everything after it, so it cannot
#: live inside the thing it describes.
VERSION = 1
GRAMMAR = "line"

#: How the version is written on the wire. Prefixed so it reads as a
#: version rather than as some number whose meaning you have to know --
#: the same reason HTTP says HTTP/1.1 and not 1.1. This spelling is
#: frozen with the rest of the `hello` line.
TAG = f"v{VERSION}"

#: Bounds. A pipe is fed by whoever can write to it.
MAX_MESSAGE = 4096
MAX_TOKENS = 64

#: What a token may be. No spaces, no controls, no escapes -- so a message
#: can never contain its own terminator, and a receiver can split on
#: whitespace without a parser.
TOKEN = re.compile(r"^[^\s\x00-\x1f\x7f=]{1,256}\Z")

#: What a value or an argument may hold that a token may not, and so is
#: written percent-encoded, %XX for each of its UTF-8 bytes: whitespace,
#: the controls, `=`, and `%` itself. Nothing else is encoded, so a value
#: without these is on the wire as it always was.
_ENCODED = re.compile(r"[\s\x00-\x1f\x7f=%]")
_ESCAPE = re.compile(r"(%[0-9A-Fa-f]{2})+")

_APC_RE = re.compile(re.escape(APC) + r"(.*?)" + re.escape(ST), re.S)


class Message:
    """One thing said, in either direction.

    `name` is what it is, `args` the positional words after it, and `opts`
    the `key=value` pairs. A reader that does not recognise the name
    ignores the whole thing.
    """

    __slots__ = ("name", "args", "opts", "raw")

    def __init__(self, name, args=(), opts=None, raw=None):
        self.name = name
        self.args = tuple(args)
        self.opts = dict(opts or {})
        #: The words as they arrived, for whoever repeats what was heard.
        self.raw = raw

    def __repr__(self):
        return f"Message({self.name!r}, {self.args!r}, {self.opts!r})"

    def __eq__(self, other):
        return (isinstance(other, Message) and self.name == other.name
                and self.args == other.args and self.opts == other.opts)

    def get(self, key, default=None):
        return self.opts.get(key, default)

    def int(self, key, low, high, default=None):
        """An option as an integer in range, or the default."""
        value = number(self.opts.get(key))
        if value is None or not low <= value <= high:
            return default
        return value


#: A number as this grammar writes one, and nothing else.
DIGITS = re.compile(r"-?[0-9]{1,19}\Z")


#: How many ranges a receiver may claim. Long enough for any real font's
#: interesting parts, short enough that the claim cannot be a payload.
MAX_RANGES = 32

#: `U+2588` or `U+2500-257F`, which is how CSS `unicode-range` spells it.
RANGE = re.compile(r"^[uU]\+([0-9a-fA-F]{1,6})(?:-([0-9a-fA-F]{1,6}))?\Z")


def ranges(text):
    """Code point ranges as [(low, high)], or () if the text says none.

    The spelling is CSS's `unicode-range` descriptor, because declaring
    which code points a font covers is exactly what that exists for and a
    vocabulary of our own would be a worse version of it. Ranges rather
    than categories because the evidence a receiver has is code points: a
    font either has U+2588 or it does not, and a name for the group it
    belongs to is a guess laid over that.

    Unordered and overlapping are both fine. A receiver claiming nothing
    is not a receiver claiming ASCII -- it has said nothing, and a sender
    that has been told nothing may do as it likes.
    """
    if not isinstance(text, str) or not text:
        return ()
    out = []
    for part in text.split(",")[:MAX_RANGES]:
        found = RANGE.match(part.strip())
        if found is None:
            continue
        low = int(found.group(1), 16)
        high = int(found.group(2), 16) if found.group(2) else low
        if low > high or high > 0x10FFFF:
            continue
        out.append((low, high))
    return tuple(out)


def covers(spans, text):
    """Whether every character of `text` falls inside `spans`.

    True for empty spans: a receiver that has not said what it can draw
    has not said it cannot draw this.
    """
    if not spans:
        return True
    return all(any(low <= ord(ch) <= high for low, high in spans)
               for ch in text)


def number(text, default=None):
    """`text` as an integer, or the default.

    The one test, so that deciding a word is a number and turning it into
    one cannot disagree. `str.isdigit` is true of characters `int` will
    not take -- superscripts, and the digits of other scripts -- so a
    receiver that answers `watching \u00b2` reaches `int` through a guard
    that let it past, and the ValueError lands in whatever loop asked.
    """
    if not isinstance(text, str) or not DIGITS.match(text):
        return default
    return int(text)


def word(value):
    """A value as one word: what a token cannot hold, percent-encoded.

    `word("my value")` is `my%20value`. A command's name and an option's
    key are not encoded: they are vocabulary, and one that is not a word
    is refused.
    """
    return _ENCODED.sub(lambda m: "".join(f"%{b:02X}" for b in
                                          m.group().encode()), str(value))


def unword(text):
    """A word as the value it carries: each run of %XX decoded, leniently.

    Only whole %XX runs that are UTF-8 are decoded, so a `%` that is not
    an escape, as in a `50%` written before values were encoded, stays.
    """
    def one(m):
        raw = bytes.fromhex(m.group().replace("%", ""))
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return m.group()
    return _ESCAPE.sub(one, text) if "%" in text else text


def _token(value):
    """One word of a message, or a refusal.

    Refused rather than escaped: a value that cannot be written as a word
    is a sign the wrong thing is being sent, and quietly mangling it would
    hide that until somebody read a wall and did not believe it.
    """
    text = str(value)
    if not TOKEN.match(text):
        raise ValueError(f"not sendable as one word: {text!r}")
    return text


def render(name, *args, **opts):
    """A message as a line of text, without a terminator."""
    words = [_token(name)]
    words += [_token(word(a)) for a in args]
    words += [f"{_token(k)}={_token(word(v))}" for k, v in sorted(opts.items())]
    if len(words) > MAX_TOKENS:
        raise ValueError(f"{len(words)} words is too many for one message")
    line = " ".join(words)
    if len(line) > MAX_MESSAGE:
        raise ValueError(f"{len(line)} characters is too long for one message")
    return line


def parse(line):
    """A line of text as a Message, or None if it is not one.

    Its arguments and values decoded: `title=my%20value` is `my value`.
    `raw` stays as it came, encoded, for whoever repeats what was heard.

    None rather than an exception: this reads a pipe anyone can write to,
    and a malformed line is a thing to drop rather than an event.
    """
    if not isinstance(line, str):
        return None
    line = line.strip()
    if not line or len(line) > MAX_MESSAGE:
        return None
    words = line.split()
    if len(words) > MAX_TOKENS or not TOKEN.match(words[0]):
        return None
    args, opts = [], {}
    for each in words[1:]:
        key, sep, value = each.partition("=")
        if sep and TOKEN.match(key) and TOKEN.match(value):
            opts[key] = unword(value)
        elif not sep and TOKEN.match(each):
            args.append(unword(each))
        else:
            return None            # one bad word spoils the message
    return Message(words[0], args, opts)


# ------------------------------------------------- down: frames and commands

def command(name, *args, **opts):
    """A request, wrapped so it can travel in the frame stream."""
    return APC + MARK + " " + render(name, *args, **opts) + ST


class Commands:
    """Splits a frame stream into what to paint and what to act on.

    Fed whatever arrived, however it was cut up: a command split across
    two reads is held until the rest of it turns up, because the pipe
    breaks a stream wherever it likes.
    """

    def __init__(self):
        self.rest = ""

    def feed(self, text):
        """Returns (paintable text, messages)."""
        text = self.rest + text
        self.rest = ""
        out, found, at = [], [], 0
        for m in _APC_RE.finditer(text):
            out.append(text[at:m.start()])
            at = m.end()
            body = m.group(1)
            if body.startswith(MARK + " "):
                said = parse(body[len(MARK) + 1:])
                if said is not None:
                    said.raw = " ".join(body[len(MARK) + 1:].split())
                    found.append(said)
            # An APC that is not ours is dropped rather than painted:
            # it was never meant for a screen.
        tail = text[at:]
        # An unterminated APC is the start of one that has not all
        # arrived. Anything before it is safe to paint now.
        cut = tail.rfind(APC)
        if cut >= 0 and ST not in tail[cut:]:
            held = tail[cut:]
            if len(held) > MAX_MESSAGE:
                # Longer than a command may be, so it is not a command
                # being assembled. Holding it costs a growing buffer and
                # a rescan of all of it every time more arrives, and a
                # wall that stopped painting says nothing about why. Give
                # up on the escape and let it be seen instead.
                self.rest = ""
                out.append(held)
                return "".join(out), found
            self.rest = held
            tail = tail[:cut]
        elif tail.endswith("\x1b"):
            # The pipe can cut between the escape and the underscore, and
            # a lone escape painted now is a sequence broken in half.
            self.rest = tail[-1:]
            tail = tail[:-1]
        out.append(tail)
        return "".join(out), found


# -------------------------------------------------------- up: announcements

def hello():
    """The first line a receiver sends, and the one that cannot change."""
    return render("hello", TAG, GRAMMAR)


def version_of(said):
    """The version a `hello` claims, as a number, or None.

    Tolerant of the prefix being absent, because a receiver written
    against an earlier reading of this file may not have it.
    """
    if said is None or said.name != "hello" or not said.args:
        return None
    text = said.args[0]
    text = text[1:] if text[:1] in ("v", "V") else text
    return number(text)


def announce(name, *args, **opts):
    """One thing a receiver has to say, as a line."""
    return render(name, *args, **opts) + "\n"


class Lines:
    """Reads what a receiver says, a line at a time."""

    def __init__(self):
        self.rest = ""

    def feed(self, text):
        """Returns the messages that have arrived whole."""
        text = self.rest + text
        if len(text) > MAX_MESSAGE * 16:
            # Somebody is writing without newlines. Keep the tail rather
            # than growing without bound.
            text = text[-MAX_MESSAGE:]
        lines = text.split("\n")
        self.rest = lines.pop()
        return [m for m in (parse(line) for line in lines) if m is not None]


#: What a picture on a video layer's `in` may be, as `frame <codec>` names
#: it: QOI, which every wall that takes video draws, then PNG and JPEG
#: where its `caps` says them in `images=`.
IMAGES = ("qoi", "png", "jpeg")

#: The JPEG markers that start a frame and carry its size: SOF0-SOF15,
#: less DHT (C4), JPG (C8) and DAC (CC), which share the range.
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def image_of(data):
    """The codec a picture's first bytes say it is, or None."""
    head = bytes(data[:8])
    if head[:4] == b"qoif":
        return "qoi"
    if head == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


def image_size(codec, data):
    """(width, height) a picture's own header says, or None where `data`
    is not a whole picture of `codec` a wall draws: its header and its
    end are checked, not its pixels. A JPEG in four components, CMYK, is
    None: no wall draws one."""
    data = memoryview(data)
    n = len(data)
    if codec == "qoi":
        if n < 22 or data[:4] != b"qoif" or data[-8:] != b"\0" * 7 + b"\1":
            return None
        return int.from_bytes(data[4:8], "big"), int.from_bytes(data[8:12], "big")
    if codec == "png":
        if (n < 45 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR"
                or data[-8:-4] != b"IEND"):
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if codec == "jpeg":
        if n < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
            return None
        p = 2
        while p + 4 <= n:
            if data[p] != 0xFF:
                return None
            marker = data[p + 1]
            if marker == 0xFF:
                p += 1
                continue
            if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
                p += 2
                continue
            length = int.from_bytes(data[p + 2:p + 4], "big")
            if marker in _SOF:
                if length < 8 or p + 10 > n or data[p + 9] not in (1, 3):
                    return None
                h = int.from_bytes(data[p + 5:p + 7], "big")
                w = int.from_bytes(data[p + 7:p + 9], "big")
                return (w, h) if w and h else None
            if marker == 0xDA or length < 2:
                return None
            p += 2 + length
        return None
    return None
