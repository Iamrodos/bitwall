#!/usr/bin/env python3
"""A wall in Python that speaks doors and sessions, for tests.

    from fakewall import FakeWall
    wall = FakeWall(run, "0", cols=60, rows=16)
    ...
    wall.remove()

    tests/fakewall.py <run> [<wall>] [--cols N] [--rows N] [--font N] [--scale S]

It is the receiver side of PROTOCOL.md with no glass behind it. It claims
`<run>/bitwall/<wall>/`, reads its door, makes and reaps sessions, keeps
the command table and the per-session backpressure rules, and records
what every session sent and was told, so a test can drive the fifos as a
sender or a controller would and then assert on what reached the wall.
Its picture is a grid of cells fed by the base's frames, so `picture` and
`mirror` are the wall as it stands. It drives no monitor and offers no
input, so it says no `power` and a bare `accepts`.

The area comes from a font on a fixed glass: the wall is made with a
size and a font, which fixes the glass in pixels, and a `font` or `grid`
request is answered with the cells that font makes on it. `resize` is the
glass changing under it, as a window dragged.

Timings are PROTOCOL.md's, scaled by `Timings(scale=...)` or the
`BITWALL_FAKE_SCALE` environment variable, so a test waits a tenth of a
second where a sender would wait ten.
"""
import codecs
import errno
import os
import random
import re
import select
import shutil
import sys
import threading
import time
import traceback
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bitwall import protocol                                   # noqa: E402
from bitwall.receivers import booted                           # noqa: E402
from bitwall.grid import Grid, LayerGrid                        # noqa: E402

RESET = "\x1b[0m"

#: A session's name, and an id: one to thirty-two of these.
NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
#: A meta key.
META_KEY = re.compile(r"^[a-z0-9._-]{1,32}$")
#: A token as a `from` or an `instance` is.
TOKEN = NAME

#: Lines that say a current value: merged into the latest when a
#: session's `out` backs up, never dropped.
STATE = ("area", "watching", "accepts", "display", "power", "presence")
#: The lines a session's queue holds however far behind its reader is.
KEEP = ("opened", "closed")

#: The most a session's queue holds while a picture waits in it.
QUEUE_LIMIT = 8 << 20
#: What is kept for a reader that has not yet opened `out`.
HELD_LIMIT = 16 << 10

#: Who may say what: the roles the command table allows each command.
#: `base` is the lowest cells layer, `vbase` a video base, `video` a
#: video layer above it, `layer` any other, `controller` a controller.
ALL = frozenset({"base", "layer", "video", "vbase", "controller"})
MAY = {
    "font": frozenset({"base", "layer", "controller"}),
    "grid": frozenset({"base", "layer", "controller"}),
    "blank": frozenset({"base", "vbase", "controller"}),
    "want": frozenset({"base", "layer", "video", "vbase"}),
    "frame": frozenset({"video", "vbase"}),
    "clear": frozenset({"video", "vbase", "layer"}),
    "align": frozenset({"video", "vbase"}),
    "box": frozenset({"video", "vbase"}),
    "presence": frozenset({"controller"}),
    "stats": frozenset({"controller"}),
    "picture": ALL,
    "mirror": ALL,
    "follow": frozenset({"controller"}),
    "measures": frozenset({"controller"}),
    "report": ALL, "sessions": ALL, "ping": ALL, "pong": ALL,
    "meta": ALL, "detach": ALL, "close": ALL,
}

CAPS = ("area font grid meta blank want presence repaint watching ping detach "
        "close screen picture mirror stats sessions follow measures frame clear data align box")
#: What a stream on a layer's `data` may be.
CODECS = ("qoi", "hevc", "h264", "mjpeg")
#: What a frame on a video layer's `in` may be.
IMAGES = ("qoi", "png", "jpeg")
#: The layers it takes, the base included: the base at 0, video above it.
LAYERS = 8
#: Its version, as a receiver's is: a hash of what it is made of.
with open(__file__, "rb") as _fh:
    VERSION = __import__("hashlib").sha256(_fh.read()).hexdigest()[:12]
#: A video frame's limits, and its hold when it says none, as PROTOCOL.md
#: has them.
FRAME_SIDE = 4096
FRAME_MAX = 16 << 20
HOLD_MS = 1000


#: `size=`'s value: ASCII digits, an `x`, ASCII digits.
SIDES = re.compile(r"([0-9]{1,4})x([0-9]{1,4})\Z")


def side_pair(word):
    """`<w>x<h>` as (w, h), each 1..FRAME_SIDE, or (0, 0)."""
    m = SIDES.match(word)
    if not (m and 1 <= int(m.group(1)) <= FRAME_SIDE and 1 <= int(m.group(2)) <= FRAME_SIDE):
        return 0, 0
    return int(m.group(1)), int(m.group(2))



#: `at=`'s value: an optional `-` and ASCII digits, twice, a comma between.
AT = re.compile(r"(-?[0-9]{1,6}),(-?[0-9]{1,6})\Z")


def at_pair(word):
    """`<x>,<y>` as (x, y), each within 65535 of 0 either way, or
    (None, None)."""
    m = AT.match(word)
    if not m:
        return None, None
    x, y = int(m.group(1)), int(m.group(2))
    if abs(x) > 65535 or abs(y) > 65535:
        return None, None
    return x, y


def frame_words(codec, w, h, x, y, size):
    """A frame as a picture or a mirror of it says it: its codec, its own
    size, where it went, and the box it was drawn to where it was scaled."""
    return (f"{codec} {w}x{h} at={x},{y}"
            + (f" size={size[0]}x{size[1]}" if size else ""))


MEASURES = ("wall", "grid", "fps", "in", "out", "inbox", "sender", "pong",
            "display")
POINTS = ("n", "ne", "e", "se", "s", "sw", "w", "nw", "c")
FONT_LOW, FONT_HIGH = 4, 96


class ClaimRefused(Exception):
    """The wall asked for belongs to a live receiver: its door is read."""


class Timings:
    """PROTOCOL.md's clocks, in seconds, all scaled together."""

    def __init__(self, scale=None):
        if scale is None:
            scale = float(os.environ.get("BITWALL_FAKE_SCALE", "1"))
        self.scale = scale
        #: Quiet on a session's `in` before the wall asks `ping`.
        self.quiet = 5.0 * scale
        #: How long a session has to answer `pong`.
        self.pong = 2.0 * scale
        #: Between pings to a session that answers, on a wall nobody watches.
        self.unwatched = 60.0 * scale
        #: A session nobody writes to after `opened` is removed after this.
        self.unwritten = 10.0 * scale
        #: A `closed` or a refusal nobody has read is kept this long.
        self.keep = 10.0 * scale
        #: How often the wall looks at its clocks and queues.
        self.tick = min(0.02, self.pong / 4)


def door_has_reader(path):
    """Whether something reads the fifo at `path`: a live wall's door."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    os.close(fd)
    return True


class _Item:
    """One thing waiting in a session's queue: `kind` is `keep`, `state`,
    `event` or `blob`; `key` names the state a `state` line carries."""

    __slots__ = ("at", "data", "key", "kind")

    def __init__(self, kind, key, data):
        self.kind, self.key, self.data, self.at = kind, key, data, 0


def rgb_of(colour):
    """A colour as looks() gives it, 0xRRGGBB, as (r, g, b)."""
    return (colour >> 16 & 255, colour >> 8 & 255, colour & 255)


class Session:
    """One session as the wall holds it: its fifos, its place, its clocks,
    and a record of everything that passed either way."""

    def __init__(self, name, path, now):
        self.name = name
        self.path = path
        self.role = None
        self.z = None
        self.from_top = False
        self.type = None
        #: A cells layer above the base: its grid, what it asked of its
        #: own (None to follow the base), and the area it has.
        self.lgrid = None
        self.own = None
        self.larea = None
        #: `instance=` on its open, what `sessions` lists it under.
        self.instance = None
        self.in_fd = self.out_fd = None
        #: `opening`, then `open`, `refused` or `closed`.
        self.state = "opening"
        self.reason = None
        self.opened_at = now
        self.ended_at = None
        self.connected = False
        self.last_in = now
        self.pinged_at = None
        self.awaiting = False
        self.answered = False
        self.pings = 0
        self.pong_ms = None
        self.idle_until = 0.0
        #: key -> (value, set at, hidden)
        self.meta = {}
        self.want = {}
        self.follow = "off"
        self.measures = 0.0
        self.measure_due = None
        self.measure_mark = None
        #: What this session mirrors: target -> its clock and what its
        #: reader already has.
        self.mirrors = {}
        self.in_bytes = self.out_bytes = 0
        #: What arrived: paintable text, commands, and everything raw.
        self.frames = []
        self.commands = []
        self.raw = []
        #: Every line written to `out`, as it went.
        self.said = []
        self.queue = deque()
        self.queued = 0
        self.dropped = 0
        #: Lines let go since it opened, and frames it put up.
        self.dropped_all = 0
        self.frame_count = 0
        #: A controller's last look at each session, for its measure lines:
        #: name -> (when, in, out, frames).
        self.session_marks = {}
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.parser = protocol.Commands()
        #: A video layer's unread bytes, the payload bytes its last `frame`
        #: said were coming, whether they are let go, and what it said.
        self.vbuf = b""
        self.payload = 0
        self.skip = True
        self.fhead = None
        #: Every frame a video layer sent, as (when, w, h, x, y, hold, taken),
        #: and the one it shows, as (w, h, x, y, hold, bytes).
        self.video = []
        self.vlast = None
        #: A stream on `data`: whether one was asked for, its codec, its
        #: frames a second and first frame's Unix ms; the fifo read, the
        #: bytes of the stream under way, how many streams there have been,
        #: its frames waiting their time, as (index, w, h, bytes), and the
        #: clock its frame 0 is shown by.
        self.data = False
        self.codec = None
        self.fps = None
        self.start = None
        #: Where the stream's frames sit on a glass of another shape, as
        #: `align` says: "top", "centre" or "bottom".
        self.align = "centre"
        #: The box the stream's frames are fitted to, (x, y, w, h), as
        #: `box` says, or None for the glass.
        self.box = None
        self.data_fd = None
        self.data_idle_until = 0.0
        self.streaming = False
        self.dbuf = b""
        self.dbytes = 0
        self.streams = 0
        self.dnext = 0
        self.dqueue = deque()
        self.t0 = None
        #: A stream that did not decode, let go until its writer leaves.
        self.discard = False
        #: Every data frame, as (when, stream, index, w, h, what): `shown`,
        #: `dropped` or `waiting` never; and each stream's end, as
        #: (when, stream, why).
        self.data_frames = []
        self.data_ends = []

    @property
    def open(self):
        return self.state == "open"

    def __repr__(self):
        return f"Session({self.name!r}, {self.role!r}, {self.state!r})"


class FakeWall:
    """A wall under `<run>/bitwall/<wall>/`, served by a thread of its own.

    `cols`, `rows` and `font` fix the glass. `controllers` is how many it
    takes at once. `presence` names the presence it follows. `rate` and
    `glyphs` go into its `screen` line. `hw` is the codecs it says it
    decodes in hardware, as `hw=` in `caps`; it decodes nothing, so it is
    whatever a test wants it to say.
    """

    def __init__(self, run, wall="0", cols=60, rows=16, font=14,
                 controllers=8, presence=None, rate=None, glyphs=None,
                 timings=None, start=True, hw=()):
        self.run = run
        self.hw = tuple(hw)
        self.wall = wall
        self.started = time.time()
        self.t = timings or Timings()
        self.font = font
        self.cols, self.rows = cols, rows
        self.glass_w = -(-cols * 3 * font // 5)
        self.glass_h = -(-rows * 6 * font // 5)
        self.controllers = controllers
        self.presence_name = presence
        self.rate = rate
        self.glyphs = glyphs
        self.present = True
        self.feeder = None
        self.fed_at = None
        self.blank = False
        self.painted = False
        self.stats = {"on": 0, "at": "ne", "size": 14, "show": "fps"}
        self.grid = Grid(cols, rows)
        self.sessions = {}
        self.base = None
        #: The session holding each layer above the base, by z, and what
        #: each shows: {z: {"w", "h", "x", "y", "until"}}.
        self.above = {}
        self.pics = {}
        #: (when, name, text) for every piece of paint from a layer.
        self.paint = []
        #: (when, Message) for every command from every session.
        self.asked = []
        #: (when, line, reason) for every request dropped at the door.
        self.dropped_requests = []
        #: Tracebacks of anything that went wrong while serving. A wall
        #: keeps serving past a fault, and a test reads these.
        self.errors = []
        self._resets = deque()
        self._in_total = self._out_total = 0
        #: When a layer above the base put the glass up anew.
        self._redraws = deque()
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread = None
        self.dir = None
        self.door_fd = None
        self._door_rest = b""
        self._door_idle = 0.0
        if start:
            self.start()

    # ------------------------------------------------------------ claiming

    @property
    def door(self):
        return os.path.join(self.run, "bitwall", self.wall, "door.in")

    def start(self):
        """Claim the wall: build it under a `.` name, take over a wall whose
        door nobody reads, and rename into place."""
        walls = os.path.join(self.run, "bitwall")
        os.makedirs(walls, exist_ok=True)
        tmp = os.path.join(walls, f".{self.wall}-{os.getpid()}-{random.randrange(1 << 20):05x}")
        os.makedirs(os.path.join(tmp, "s"))
        os.mkfifo(os.path.join(tmp, "door.in"), 0o600)
        door_fd = os.open(os.path.join(tmp, "door.in"), os.O_RDONLY | os.O_NONBLOCK)
        final = os.path.join(walls, self.wall)
        if os.path.lexists(final):
            if door_has_reader(os.path.join(final, "door.in")):
                os.close(door_fd)
                shutil.rmtree(tmp, ignore_errors=True)
                raise ClaimRefused(f"{final}: its door is read")
            shutil.rmtree(final)
        os.rename(tmp, final)
        self.dir = final
        self.door_fd = door_fd
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name=f"fakewall-{self.wall}")
        self._thread.start()

    def stop(self):
        """Die as a killed receiver dies: every fifo closed, nothing said,
        the directory left where it is."""
        self._halt.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None
        with self._lock:
            if self.door_fd is not None:
                os.close(self.door_fd)
                self.door_fd = None
            for s in self.sessions.values():
                self._close_fds(s)
            self.sessions.clear()
            self.base = None

    def remove(self):
        """Stop in good order: die, and take the wall away."""
        self.stop()
        if self.dir and os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.remove()

    # ------------------------------------------------------------- serving

    def _serve(self):
        t = self.t
        while not self._halt.is_set():
            now = time.monotonic()
            with self._lock:
                fds = []
                if self.door_fd is not None and self._door_idle <= now:
                    fds.append(self.door_fd)
                by_fd, data_fd = {}, {}
                for s in self.sessions.values():
                    if s.in_fd is not None and s.idle_until <= now:
                        fds.append(s.in_fd)
                        by_fd[s.in_fd] = s
                    if s.data_fd is not None and s.data_idle_until <= now:
                        fds.append(s.data_fd)
                        data_fd[s.data_fd] = s
            try:
                ready, _, _ = select.select(fds, [], [], t.tick)
            except (OSError, ValueError):
                if self._halt.is_set():
                    return
                time.sleep(t.tick)
                continue
            with self._lock:
                if self._halt.is_set():
                    return
                now = time.monotonic()
                try:
                    for fd in ready:
                        if fd == self.door_fd:
                            self._read_door(now)
                        elif fd in by_fd:
                            self._read_in(by_fd[fd], now)
                        elif fd in data_fd and data_fd[fd].data_fd == fd:
                            self._read_data(data_fd[fd], now)
                    self._tick(now)
                except Exception:                              # noqa: BLE001
                    self.errors.append(traceback.format_exc())

    def _read_door(self, now):
        try:
            data = os.read(self.door_fd, 65536)
        except BlockingIOError:
            return
        if not data:
            self._door_idle = now + self.t.tick
            return
        buf = self._door_rest + data
        *lines, self._door_rest = buf.split(b"\n")
        if len(self._door_rest) > 4096:
            self._door_rest = b""
        for line in lines:
            self._request(line.decode("utf-8", "replace"), now)

    def _request(self, line, now):
        words = line.split()
        if (len(words) < 2 or words[0] != "open" or not NAME.match(words[1])
                or words[1] == "glass"):
            self.dropped_requests.append((now, line, "bad-name"))
            return
        name = words[1]
        role = words[2] if len(words) > 2 else None
        opts = {}
        for w in words[3:]:
            key, sep, value = w.partition("=")
            if not sep or not key or not value:
                self.dropped_requests.append((now, line, "bad-name"))
                return
            opts[key] = value
        path = os.path.join(self.dir, "s", name)
        if os.path.lexists(path):
            self.dropped_requests.append((now, line, "name-in-use"))
            return
        os.makedirs(path)
        s = Session(name, path, now)
        os.mkfifo(os.path.join(path, "in"), 0o600)
        s.in_fd = os.open(os.path.join(path, "in"), os.O_RDONLY | os.O_NONBLOCK)
        os.mkfifo(os.path.join(path, "out"), 0o600)
        s.out_fd = os.open(os.path.join(path, "out"), os.O_RDWR | os.O_NONBLOCK)
        self.sessions[name] = s
        self._say(s, protocol.hello(), kind="keep")
        reason = self._admit(s, role, opts)
        if reason:
            s.state, s.reason, s.ended_at = "refused", reason, now
            os.close(s.in_fd)
            s.in_fd = None
            self._say(s, f"error open {name} {reason}")
            return
        s.state = "open"
        if s.role == "layer" and s.z == 0:
            self.base = s
            if s.type == "video":
                self.above[0] = s
        elif s.role == "layer":
            self.above[s.z] = s
            if s.type == "cells":
                s.larea = (self.cols, self.rows, self.font)
                s.lgrid = LayerGrid(self.cols, self.rows)
        if s.data:
            # Made before `opened` says it is there.
            os.mkfifo(os.path.join(path, "data"), 0o600)
            s.data_fd = os.open(os.path.join(path, "data"), os.O_RDONLY | os.O_NONBLOCK)
        self._say(s, (f"opened {name} z={s.z}" + (" data" if s.data else ""))
                  if s.role == "layer" else f"opened {name}")
        self._tell_controllers(f"session opened {name} {s.role} "
                               f"{self._place(s)} {s.type or '-'}"
                               + (f" instance={s.instance}" if s.instance else ""), but=s)

    def _admit(self, s, role, opts):
        """Take the request or the word that refuses it. The base is
        displaced where `take=1` asks; a layer above it is given by place,
        `above` the lowest free, `top` the highest free, a number exactly
        that one, and never taken from its holder."""
        if role not in ("layer", "controller"):
            return "no-such-role"
        s.role = role
        if role == "controller":
            # Its one option is the instance a controller started it under.
            for key, value in opts.items():
                if key != "instance" or not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", value):
                    return "no-such-key"
                s.instance = value
            if sum(1 for x in self.sessions.values()
                   if x.open and x.role == "controller") >= self.controllers:
                return "full"
            return None
        z, place, take, video = 0, None, False, False
        data, codec, fps, start = False, None, None, None
        bad_key = bad_type = False
        for key, value in opts.items():
            if key == "z":
                if value in ("above", "top"):
                    place = value
                    continue
                if not value.isdigit():
                    bad_key = True
                    continue
                z, place = int(value), None
            elif key == "type":
                if value not in ("cells", "video"):
                    bad_type = True
                video = value == "video"
            elif key == "take":
                bad_key |= value != "1"
                take = True
            elif key == "data":
                bad_key |= value != "1"
                data = True
            elif key == "codec":
                codec = value
            elif key == "fps":
                try:
                    fps = float(value) if re.match(r"^\d{1,3}(\.\d{1,6})?$", value) else None
                except ValueError:
                    fps = None
                if fps is None or not 0 < fps <= 240:
                    bad_key = True
            elif key == "start":
                if not (value.isdigit() and len(value) <= 16):
                    bad_key = True
                else:
                    start = int(value)
            elif key == "instance":
                if re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", value):
                    s.instance = value
                else:
                    bad_key = True
            else:
                bad_key = True
        if data and not video:
            bad_key = True
        if not data and (codec is not None or "fps" in opts or "start" in opts):
            bad_key = True
        if "start" in opts and "fps" not in opts:
            bad_key = True
        if take and (place is not None or z > 0):
            bad_key = True
        if bad_key:
            return "no-such-key"
        if place is None and z >= LAYERS:
            return "full"
        if bad_type:
            return "no-such-type"
        if data and codec not in CODECS:
            return "no-such-codec"

        def held(z):
            x = self.above.get(z)
            return x is not None and x.open
        if z >= LAYERS:
            return "full"
        if place is not None or z > 0:
            # Two stacks with no gaps (PROTOCOL.md, *Places*).
            lo = sum(1 for k in range(1, LAYERS) if held(k) and not self.above[k].from_top)
            hi = sum(1 for k in range(1, LAYERS) if held(k) and self.above[k].from_top)
            if lo + hi >= LAYERS - 1:
                return "full"
            if place == "top":
                z, s.from_top = LAYERS - 1 - hi, True
            else:
                z = lo + 1 if place == "above" or z > lo + 1 else z
                for k in range(lo, z - 1, -1):
                    self._move(k, k + 1)
        s.z, s.type = z, "video" if video else "cells"
        s.data, s.codec, s.fps, s.start = data, codec, fps, start
        if z > 0:
            return None
        holder = self.base if self.base is not None and self.base.open else None
        if holder is not None:
            if not take:
                return "busy"
            self._end(holder, "displaced")
        return None

    def _place(self, s):
        return "-" if s.z is None else str(s.z)

    def _read_in(self, s, now):
        try:
            data = os.read(s.in_fd, 1 << 20)
        except BlockingIOError:
            return
        if not data:
            if s.connected:
                self._end(s, "eof")
            else:
                s.idle_until = now + self.t.tick
            return
        s.in_bytes += len(data)
        self._in_total += len(data)
        s.last_in = now
        if not s.connected:
            s.connected = True
            self._say(s, "repaint")
        if s.type == "video":
            self._read_video(s, data, now)
            return
        text = s.decoder.decode(data)
        s.raw.append(text)
        paint, said = s.parser.feed(text)
        if paint and s.role == "layer":
            s.frames.append((now, paint))
            s.frame_count += paint.count(RESET)
            self.paint.append((now, s.name, paint))
            if s is self.base:
                self.grid.feed(paint)
                self.painted = True
                for _ in range(paint.count(RESET)):
                    self._resets.append(now)
            elif s.lgrid is not None:
                s.lgrid.feed(paint)
        for m in said:
            self._command(s, m, now)

    def _command(self, s, m, now):
        s.commands.append((now, m))
        self.asked.append((now, m))
        for c in self.sessions.values():
            if c is not s and c.open and c.follow == "in" and m.name != "frame":
                self._say(c, f"< {s.name} {m.raw}")
        self._obey(s, m, now)

    def _read_video(self, s, data, now):
        """A video layer's bytes: commands as they come, each frame's
        payload gathered whole, and anything else let go."""
        b = s.vbuf + data
        p = 0
        while p < len(b) and s.open:
            if s.payload:
                have = len(b) - p
                if s.skip:
                    k = min(have, s.payload)
                    p += k
                    s.payload -= k
                    continue
                if have < s.payload:
                    break
                self._video_frame(s, b[p:p + s.payload], now)
                p += s.payload
                s.payload = 0
                continue
            esc = b.find(b"\x1b_", p)
            if esc < 0:
                p = len(b) - 1 if b.endswith(b"\x1b") else len(b)
                break
            end = b.find(b"\x1b\\", esc + 2)
            if end < 0:
                p = esc + 2 if len(b) - esc > protocol.MAX_MESSAGE + 2 else esc
                if p == esc:
                    break
                continue
            body = b[esc + 2:end].decode("utf-8", "replace")
            p = end + 2
            if body.startswith(protocol.MARK + " "):
                m = protocol.parse(body[len(protocol.MARK) + 1:])
                if m is not None:
                    m.raw = " ".join(body[len(protocol.MARK) + 1:].split())
                    self._command(s, m, now)
        s.vbuf = b[p:]

    def _video_frame(self, s, data, now):
        """One frame's payload, whole: put up if its QOI header and end are
        what its line said, or refused with the line's id."""
        codec, w, h, x, y, size, hold, tag = s.fhead
        ok = protocol.image_size(codec, data) == (w, h)
        s.video.append((now, w, h, x, y, hold, ok))
        if ok:
            s.frame_count += 1
            self._redraws.append(now)
            if s is self.base:
                self.painted = True
        if ok and self.above.get(s.z) is s:
            self.pics[s.z] = {"w": w, "h": h, "x": x, "y": y, "codec": codec, "size": size,
                              "until": float("inf") if hold is None else now + hold / 1000}
            s.vlast = (w, h, x, y, hold, bytes(data), codec, size)
            for c in list(self.sessions.values()):
                mirror = c.mirrors.get(s.name)
                if (mirror is None or not c.open or now < mirror["due"]
                        or any(i.kind == "blob" for i in c.queue)):
                    continue
                mirror["due"] = now + mirror["every"]
                self._blob(c, f"mirror {s.name} {frame_words(codec, w, h, x, y, size)} "
                              f"hold={'still' if hold is None else hold} "
                              f"{len(data)}", bytes(data))
        elif not ok:
            self._say(s, f"error frame {codec}: does not decode{tag}")

    # ------------------------------------------------------------ data

    def _read_data(self, s, now):
        """A layer's `data`: a stream starts with its first bytes and ends
        when its writer leaves, what is decoded by then played out."""
        try:
            data = os.read(s.data_fd, 1 << 20)
        except BlockingIOError:
            return
        if not data:
            s.data_idle_until = now + self.t.tick
            s.discard = False
            if s.streaming:
                self._data_end(s, now, "eof")
            return
        if s.discard:
            return              # the rest of a stream that did not decode
        if not s.streaming:
            s.streaming = True
            s.streams += 1
            s.dbuf, s.dnext, s.t0 = b"", 0, None
            if s.streams == 1 and s.start is not None and s.fps:
                s.t0 = now + (s.start / 1000 - time.time())
        s.in_bytes += len(data)
        s.dbytes += len(data)
        self._in_total += len(data)
        s.last_in = now
        if not s.connected:
            s.connected = True
            self._say(s, "repaint")
        if s.codec == "mjpeg":
            self._read_jpegs(s, now, data)
            return
        if s.codec != "qoi":
            # Opaque to the fake, which decodes nothing: the stream is up
            # as its bytes arrive.
            self._data_show(s, now, (s.dnext, 0, 0, bytes(data)))
            s.dnext += 1
            return
        s.dbuf += data
        while s.dbuf and s.streaming:
            end = qoi_end(s.dbuf)
            if end is None:
                break
            if end < 0:
                self._say(s, f"error data {s.codec}: does not decode")
                self._data_end(s, now, "does not decode")
                s.discard = True
                break
            img, s.dbuf = s.dbuf[:end], s.dbuf[end:]
            self._data_frame(s, now, int.from_bytes(img[4:8], "big"),
                             int.from_bytes(img[8:12], "big"), img)

    def _read_jpegs(self, s, now, data):
        """MJPEG: JPEGs back to back, each ended by its end-of-image marker,
        and each a JPEG a wall draws."""
        s.dbuf += data
        while s.dbuf and s.streaming:
            if not s.dbuf[:2] == b"\xff\xd8"[:len(s.dbuf[:2])]:
                size = None         # not a JPEG's start
            else:
                end = s.dbuf.find(b"\xff\xd9", 2)
                if end < 0:
                    break
                img, s.dbuf = s.dbuf[:end + 2], s.dbuf[end + 2:]
                size = protocol.image_size("jpeg", img)
            if size is None or not all(1 <= x <= FRAME_SIDE for x in size):
                self._say(s, f"error data {s.codec}: does not decode")
                self._data_end(s, now, "does not decode")
                s.discard = True
                break
            self._data_frame(s, now, size[0], size[1], img)

    def _data_frame(self, s, now, w, h, img):
        """One decoded frame, queued for its time or shown at once."""
        s.dqueue.append((s.dnext, w, h, img))
        s.dnext += 1
        self._play(s, now)

    def _data_end(self, s, now, why):
        """The stream is over: it is let go, the layer cleared, and the
        session kept for the next."""
        s.streaming = False
        s.dbuf = b""
        s.dqueue.clear()
        s.data_ends.append((now, s.streams, why))
        if self.above.get(s.z) is s:
            self._clear(s.z)

    def _play(self, s, now):
        """Frame i is shown at t0 + i/fps: early waits, and a frame whose
        successor is due is late, let go and counted. Without fps each is
        shown as it came."""
        if not s.dqueue:
            return
        if not s.fps:
            while s.dqueue:
                self._data_show(s, now, s.dqueue.popleft())
            return
        if s.t0 is None:
            s.t0 = now - s.dqueue[0][0] / s.fps
        while s.dqueue:
            i = s.dqueue[0][0]
            if now >= s.t0 + (i + 1) / s.fps:
                s.dqueue.popleft()
                s.dropped_all += 1
                s.data_frames.append((now, s.streams, i, 0, 0, "dropped"))
                continue
            if now >= s.t0 + i / s.fps:
                self._data_show(s, now, s.dqueue.popleft())
            break

    def _data_show(self, s, now, frame):
        i, w, h, img = frame
        s.frame_count += 1
        self._redraws.append(now)
        s.data_frames.append((now, s.streams, i, w, h, "shown"))
        if s is self.base:
            self.painted = True
        if self.above.get(s.z) is not s:
            return
        bx, by, bw, bh = s.box or (0, 0, self.glass_w, self.glass_h)
        if w and h:
            k = min(bw / w, bh / h)
            dw, dh = max(1, int(w * k + 0.5)), max(1, int(h * k + 0.5))
            x = bx + (bw - dw) // 2
            y = by + {"top": 0, "bottom": bh - dh}.get(s.align, (bh - dh) // 2)
            s.vlast = (w, h, x, y, 0, img, "jpeg" if s.codec == "mjpeg" else "qoi",
                       (dw, dh) if (dw, dh) != (w, h) else None)
        else:
            x, y, dw, dh = bx, by, bw, bh
        self.pics[s.z] = {"w": dw, "h": dh, "x": x, "y": y, "until": float("inf")}

    def _clear(self, z):
        """Layer z taken down, and every mirror of it told."""
        if self.pics.pop(z, None) is not None and self.above.get(z) is not None:
            self._mirror_clear(self.above[z])

    def _mirror_clear(self, v):
        for c in list(self.sessions.values()):
            if c.open and v.name in c.mirrors:
                self._say(c, f"mirror {v.name} clear", kind="keep")

    # -------------------------------------------------------------- saying

    def _say(self, s, line, kind=None):
        """One line to a session, queued when its `out` is full, followed
        to the controllers that asked. `kind` is worked out from the first
        word unless given: the `hello` that opens an `out` is kept where a
        report's is not."""
        first = line.split(" ", 1)[0]
        if kind is not None:
            pass
        elif first in KEEP or line.startswith("error open "):
            kind = "keep"
        elif first in STATE:
            kind = "state"
        else:
            kind = "event"
        if first not in ("measure", "dropped") and not line.startswith(("> ", "< ")):
            for c in self.sessions.values():
                if c is not s and c.open and c.follow in ("said", "in"):
                    self._say(c, f"> {s.name} {line}")
        self._put(s, _Item(kind, first, (line + "\n").encode()))

    def _blob(self, s, header, data):
        """A picture or a mirror frame: a header line and its bytes, which
        wait in the queue rather than being dropped."""
        self._put(s, _Item("blob", header.split(" ", 1)[0],
                           (header + "\n").encode() + data))

    def _put(self, s, item):
        if s.out_fd is None:
            return
        if s.queue:
            waiting = any(x.kind == "blob" for x in s.queue)
            if item.kind == "event" and not waiting:
                s.dropped += 1
                s.dropped_all += 1
                return
            if item.kind == "state" and not waiting:
                for x in s.queue:
                    if x.kind == "state" and x.key == item.key:
                        x.data = item.data
                        return
            if item.kind == "event" and s.queued + len(item.data) > QUEUE_LIMIT:
                s.dropped += 1
                s.dropped_all += 1
                return
        s.queue.append(item)
        s.queued += len(item.data)
        self._flush(s)

    def _write(self, s, data):
        """Bytes into `out` without blocking; how many went."""
        sent = 0
        while sent < len(data):
            try:
                n = os.write(s.out_fd, data[sent:sent + 4096])
            except BlockingIOError:
                break
            if n <= 0:
                break
            sent += n
        s.out_bytes += sent
        self._out_total += sent
        return sent

    def _flush(self, s):
        while s.queue:
            item = s.queue[0]
            if s.dropped and item.at == 0:
                note = f"dropped {s.dropped}\n".encode()
                if self._write(s, note) < len(note):
                    return
                s.said.append((time.monotonic(), note[:-1].decode()))
                s.dropped = 0
            n = self._write(s, item.data[item.at:])
            item.at += n
            s.queued -= n
            if item.at < len(item.data):
                return
            s.queue.popleft()
            if item.kind != "blob":
                s.said.append((time.monotonic(), item.data[:-1].decode()))
            else:
                s.said.append((time.monotonic(), item.data.split(b"\n", 1)[0].decode()))

    def _tell_controllers(self, line, but=None, cause=None, tag=""):
        for c in list(self.sessions.values()):
            if c.open and c.role == "controller" and c is not but:
                self._say(c, line + (tag if c is cause else ""))

    def _tell_layers(self, line, cause=None, tag=""):
        for s in list(self.sessions.values()):
            if s.open and s.role == "layer":
                self._say(s, line + (tag if s is cause else ""))

    def _tell_cells(self, line, cause=None, tag=""):
        """To the base and every cells layer following the base's grid."""
        for s in list(self.sessions.values()):
            if s.open and s.type != "video" and s.own is None:
                self._say(s, line + (tag if s is cause else ""))

    def _layer_fit(self, s):
        """(cols, rows, font) for a cells layer: the base's where it follows
        it, its own font's, or exactly its own grid in the font that holds
        it."""
        if s.own is None:
            return self.cols, self.rows, self.font
        if s.own[0] == "grid":
            _, want_c, want_r = s.own
            for font in range(FONT_HIGH, FONT_LOW - 1, -1):
                cols, rows = self._cells_for(font)
                if cols >= want_c and rows >= want_r:
                    break
            return want_c, want_r, font
        return (*self._cells_for(s.own[1]), s.own[1])

    def _layer_screen(self, s):
        return self.screen_line() + f" cell={-(-3 * s.larea[2] // 5)}x{-(-6 * s.larea[2] // 5)}"

    def _layer_refit(self, s, cause=None, tag="", always=False):
        was = s.larea
        s.larea = self._layer_fit(s)
        if s.larea[:2] != (s.lgrid.cols, s.lgrid.rows):
            s.lgrid.resize(*s.larea[:2])
        if (s.own is not None and s.larea != was) or always:
            self._say(s, f"area {s.larea[0]}x{s.larea[1]} {s.larea[2]}"
                         + (tag if s is cause else ""))
            self._say(s, self._layer_screen(s))
            self._say(s, "repaint")

    def _layer_ask(self, s, own, tag):
        s.own = own
        self._layer_refit(s, cause=s, tag=tag, always=True)

    def _tell_all(self, line, cause=None, tag=""):
        for s in list(self.sessions.values()):
            if s.open:
                self._say(s, line + (tag if s is cause else ""))

    # ---------------------------------------------------------------- state

    def display(self):
        if self.blank:
            return "blank"
        if not self.present:
            return "away"
        return "lit"

    def watching(self):
        return 1 if self.display() == "lit" else 0

    def area_line(self):
        return f"area {self.cols}x{self.rows} {self.font}"

    def presence_line(self):
        line = f"presence {1 if self.present else 0}"
        if self.presence_name:
            line += f" name={self.presence_name}"
        if self.feeder:
            line += f" from={self.feeder} age={int(time.monotonic() - self.fed_at)}"
        return line

    def stats_line(self):
        st = self.stats
        return f"stats {st['on']} at={st['at']} size={st['size']} show={st['show']}"

    def screen_line(self):
        line = f"screen colour=true pixels={self.glass_w}x{self.glass_h}"
        if self.rate:
            line += f" rate={self.rate}"
        if self.glyphs:
            line += f" glyphs={self.glyphs}"
        return line

    def receiver_line(self):
        """Which program this is, as a receiver says it: the fake, its
        version a hash of this file, as the receivers' are of theirs."""
        up = booted()
        return (f"receiver fakewall version={VERSION} started={int(self.started)}"
                + (f" booted={up}" if up else ""))

    def caps_line(self):
        hw = ",".join(c for c in CODECS if c in self.hw)
        return (f"caps {CAPS} layers={LAYERS} types=cells,video above=video,cells "
                f"codecs={','.join(CODECS)} "
                + (f"hw={hw} " if hw else "")
                + f"images={','.join(IMAGES)} "
                f"controllers={self.controllers}")

    def _cells_for(self, font):
        return (max(1, self.glass_w * 5 // (3 * font)),
                max(1, self.glass_h * 5 // (6 * font)))

    def _dark_changed(self, was_display, was_watching, cause=None, tag=""):
        if self.display() != was_display:
            self._tell_controllers(f"display {self.display()}", cause=cause, tag=tag)
        if self.watching() != was_watching:
            self._tell_layers(f"watching {self.watching()}", cause=cause, tag=tag)

    def _set_area(self, cols, rows, font, cause=None, tag="", pixels=False):
        """A new area, said to every session but a video layer, which is
        pixels over pixels: it is cleared and told `repaint` only when
        the glass's pixels change, `pixels`."""
        changed = (cols, rows) != (self.cols, self.rows)
        self.cols, self.rows, self.font = cols, rows, font
        if pixels:
            self.pics.clear()
            for x in self.above.values():
                if x.open:
                    self._say(x, self.screen_line())
                    self._say(x, "repaint")
                    self._mirror_clear(x)
        if changed:
            self.grid.resize(cols, rows)
            for s in self.sessions.values():
                for mirror in s.mirrors.values():
                    mirror["was"] = None
        self._tell_cells(self.area_line(), cause=cause, tag=tag)
        if changed:
            self._tell_cells("repaint", cause=cause, tag=tag)
        for x in list(self.above.values()):
            if x.open and x.lgrid is not None:
                self._layer_refit(x)

    def resize(self, cols, rows):
        """The glass changes under the wall, at its current font."""
        with self._lock:
            self.glass_w = -(-cols * 3 * self.font // 5)
            self.glass_h = -(-rows * 6 * self.font // 5)
            self._set_area(cols, rows, self.font, pixels=True)

    announce_area = resize

    # ------------------------------------------------------------- commands

    def _obey(self, s, m, now):
        name = m.name
        id_ = m.opts.get("id")
        if id_ is not None and not NAME.match(id_):
            self._say(s, f"error {name} id: not a token")
            return
        tag = f" id={id_}" if id_ else ""
        if name not in MAY:
            if id_:
                self._say(s, f"error {name} unknown command{tag}")
            return
        who = "controller" if s.role == "controller" else (
            ("vbase" if s.type == "video" else "base") if s is self.base
            else "video" if s.type == "video" else "layer")
        if who not in MAY[name]:
            self._say(s, f"error {name} not-for-role{tag}")
            return
        getattr(self, "_cmd_" + name)(s, m, tag, now)

    def _cmd_report(self, s, m, tag, now):
        def say(line):
            self._say(s, line + tag)
        say(protocol.hello())
        say(self.receiver_line())
        say(self.caps_line())
        say("measures " + " ".join(MEASURES))
        say("pictures qoi grid")
        say(self._layer_screen(s) if s.lgrid is not None else self.screen_line())
        say("accepts")
        if self.base is not None and self.base.open:
            for line in self._meta_lines(self.base, now):
                say(line)
        for x in self.sessions.values():
            if x.open and x.role == "layer" and x is not self.base:
                for line in self._meta_lines(x, now):
                    say(f"meta {x.name} {line[5:]}")
        say(self.stats_line())
        say(self.presence_line())
        say(f"display {self.display()}")
        say(f"watching {self.watching()}")
        say(f"area {s.larea[0]}x{s.larea[1]} {s.larea[2]}" if s.lgrid is not None
            else self.area_line())

    def _cmd_sessions(self, s, m, tag, now):
        live = [x for x in self.sessions.values() if x.open]
        for x in live:
            inst = f" instance={x.instance}" if x.instance else ""
            self._say(s, f"session {x.name} {x.role} {self._place(x)} {x.type or '-'}{inst}{tag}")
        self._say(s, f"sessions {len(live)}{tag}")

    def _cmd_ping(self, s, m, tag, now):
        self._say(s, f"pong{tag}")

    def _cmd_pong(self, s, m, tag, now):
        if s.awaiting:
            s.awaiting = False
            s.answered = True
            s.pong_ms = (now - s.pinged_at) * 1000

    def _cmd_frame(self, s, m, tag, now):
        """`frame <codec> <w>x<h> [at=<x>,<y>] [size=<w>x<h>] [hold=<ms>|still]
        <bytes>`: what
        follows, and whether it is taken. The bytes are read next whatever
        is wrong with the line, so long as it says how many."""
        words = [w for w in m.raw.split() if not w.startswith("id=")]
        s.payload, s.skip = 0, True

        def no(why):
            self._say(s, f"error frame {why}{tag}")
        if len(words) < 2 or not (words[-1].isdigit() and len(words[-1]) <= 18):
            return no("no count")
        s.payload = int(words[-1])
        if s.payload > FRAME_MAX:
            return no(f"{words[-1]}: too big")
        if len(words) >= 3 and words[1] not in IMAGES:
            return no(f"{words[1]}: not offered")
        if len(words) < 4:
            return no("no size")
        w, h = side_pair(words[2])
        if not w:
            return no(f"{words[2]}: bad size")
        x = y = 0
        size = None
        hold = HOLD_MS
        for word in words[3:-1]:
            key, _, value = word.partition("=")
            if key == "at":
                ax, ay = at_pair(value)
                good = ax is not None
                if good:
                    x, y = ax, ay
            elif key == "size":
                size = side_pair(value)
                good = bool(size[0])
            elif key == "hold":
                good = value == "still" or (value.isdigit() and len(value) <= 8
                                            and int(value) > 0)
                hold = (None if value == "still" else int(value)) if good else hold
            else:
                return no(f"{word}: unknown option")
            if not good:
                return no(f"{word}: bad value")
        s.skip = False
        s.fhead = (words[1], w, h, x, y, size, hold, tag)

    def _cmd_clear(self, s, m, tag, now):
        if self.above.get(s.z) is s and s.lgrid is not None:
            s.lgrid.wipe()
        elif self.above.get(s.z) is s:
            self._clear(s.z)

    def _cmd_align(self, s, m, tag, now):
        word = m.args[0] if m.args else ""
        if word not in ("top", "centre", "bottom"):
            self._say(s, f"error align {word}: not top, centre or bottom{tag}")
            return
        s.align = word

    def _cmd_box(self, s, m, tag, now):
        x = y = 0
        size = None
        words = [w for w in m.raw.split()[1:] if not w.startswith("id=")]
        for word in words:
            key, eq, value = word.partition("=")
            if eq and key == "at":
                x, y = at_pair(value)
                good = x is not None
            elif eq and key == "size":
                size = side_pair(value)
                good = bool(size[0])
            else:
                self._say(s, f"error box {word}: unknown option{tag}")
                return
            if not good:
                self._say(s, f"error box {word}: bad value{tag}")
                return
        if words and size is None:
            self._say(s, f"error box no size{tag}")
            return
        s.box = (x, y, *size) if size else None

    def _cmd_close(self, s, m, tag, now):
        self._end(s, "asked")

    _cmd_detach = _cmd_close

    def _cmd_font(self, s, m, tag, now):
        if s.lgrid is not None and tuple(m.args[:1]) == ("base",):
            return self._layer_ask(s, None, tag)
        if not m.args:
            if tag:
                self._say(s, f"error font no size{tag}")
            return
        size = protocol.number(m.args[0])
        if size is None:
            self._say(s, f"error font {protocol.word(m.args[0])}: not a number{tag}")
            return
        size = min(FONT_HIGH, max(FONT_LOW, size))
        if s.lgrid is not None:
            return self._layer_ask(s, ("font", size), tag)
        cols, rows = self._cells_for(size)
        self._set_area(cols, rows, size, cause=s, tag=tag)

    def _cmd_grid(self, s, m, tag, now):
        if s.lgrid is not None and tuple(m.args[:1]) == ("base",):
            return self._layer_ask(s, None, tag)
        if not m.args:
            self._say(s, f"error grid no size{tag}")
            return
        found = re.match(r"^(\d{1,4})x(\d{1,4})$", m.args[0])
        if not found or int(found.group(1)) < 1 or int(found.group(2)) < 1:
            self._say(s, f"error grid {protocol.word(m.args[0])}: not COLSxROWS{tag}")
            return
        want_c, want_r = int(found.group(1)), int(found.group(2))
        if s.lgrid is not None:
            return self._layer_ask(s, ("grid", want_c, want_r), tag)
        for font in range(FONT_HIGH, FONT_LOW - 1, -1):
            cols, rows = self._cells_for(font)
            if cols >= want_c and rows >= want_r:
                break
        self._set_area(cols, rows, font, cause=s, tag=tag)

    def _cmd_blank(self, s, m, tag, now):
        if not m.args:
            self._say(s, f"error blank no value{tag}")
            return
        if m.args[0] not in ("0", "1"):
            self._say(s, f"error blank {protocol.word(m.args[0])}: not 0 or 1{tag}")
            return
        was = self.display(), self.watching()
        self.blank = m.args[0] == "1"
        self._dark_changed(*was, cause=s, tag=tag)

    def _cmd_want(self, s, m, tag, now):
        for key, value in m.opts.items():
            if key == "id":
                continue
            if key not in ("touch", "keyboard", "buttons"):
                self._say(s, f"error want {key}: unknown option{tag}")
            elif value not in ("0", "1"):
                self._say(s, f"error want {key}={protocol.word(value)}: not 0 or 1{tag}")
            else:
                s.want[key] = value == "1"

    def _cmd_presence(self, s, m, tag, now):
        if not m.args:
            self._say(s, f"error presence no value{tag}")
            return
        if m.args[0] not in ("0", "1"):
            self._say(s, f"error presence {protocol.word(m.args[0])}: not 0 or 1{tag}")
            return
        feeder = m.opts.get("from")
        if feeder is None:
            self._say(s, f"error presence no from{tag}")
            return
        if not TOKEN.match(feeder):
            self._say(s, f"error presence from={feeder}: not a token{tag}")
            return
        for key in m.opts:
            if key not in ("from", "id"):
                self._say(s, f"error presence {key}: unknown option{tag}")
                return
        was = self.display(), self.watching()
        was_present = self.present
        self.present = m.args[0] == "1"
        self.feeder, self.fed_at = feeder, now
        self._say(s, self.presence_line() + tag)
        if self.present != was_present:
            self._tell_controllers(self.presence_line(), but=s)
        self._dark_changed(*was, cause=s, tag=tag)

    def _cmd_stats(self, s, m, tag, now):
        was = self.stats_line()
        if not m.args:
            self._say(s, f"error stats no value{tag}")
            return
        if m.args[0] not in ("0", "1"):
            self._say(s, f"error stats {protocol.word(m.args[0])}: not 0 or 1{tag}")
            return
        st = self.stats
        st["on"] = int(m.args[0])
        for key, value in m.opts.items():
            if key == "id":
                continue
            if key == "at":
                if value in POINTS:
                    st["at"] = value
                else:
                    self._say(s, f"error stats at={protocol.word(value)}: unknown point{tag}")
            elif key == "size":
                size = protocol.number(value)
                if size is None:
                    self._say(s, f"error stats size={protocol.word(value)}: not a number{tag}")
                else:
                    st["size"] = min(40, max(8, size))
            elif key == "show":
                names = MEASURES if value == "all" else \
                    [x for x in value.split(",") if x in MEASURES]
                if names:
                    st["show"] = ",".join(names)
                else:
                    self._say(s, f"error stats show={protocol.word(value)}: none known{tag}")
            else:
                self._say(s, f"error stats {key}: unknown option{tag}")
        self._say(s, self.stats_line() + tag)
        if self.stats_line() != was:
            self._tell_controllers(self.stats_line(), but=s)

    def _capture(self, s, m, tag, cmd, mirror=False):
        """A picture's or a mirror's target and options, each checked:
        (target session or None for the glass, (w, h), every, off), or None
        with the error said."""
        if not m.args:
            self._say(s, f"error {cmd} no target{tag}")
            return None
        target = m.args[0]
        if mirror and tuple(m.args[1:]) == ("off",) and set(m.opts) <= {"id"}:
            return None, None, None, True
        who = None
        if target != "glass":
            who = self.sessions.get(target)
            if who is None or not who.open:
                self._say(s, f"error {cmd} {target}: no such session{tag}")
                return None
            if who.role != "layer":
                self._say(s, f"error {cmd} {target}: not a layer{tag}")
                return None
        for word in m.args[1:]:
            self._say(s, f"error {cmd} {word}: unknown option{tag}")
            return None
        size, every, fmt, quality = None, None, None, None
        for key, value in m.opts.items():
            if key == "id":
                continue
            if key == "every" and mirror:
                every = protocol.number(value)
                if every is None:
                    self._say(s, f"error {cmd} every={protocol.word(value)}: not a number{tag}")
                    return None
                every = min(60000, max(50, every))
            elif key == "size":
                w, sep, h = value.partition("x")
                if not (sep and w.isdigit() and h.isdigit() and len(w) <= 4 and len(h) <= 4
                        and 1 <= int(w) <= FRAME_SIDE and 1 <= int(h) <= FRAME_SIDE):
                    self._say(s, f"error {cmd} size={protocol.word(value)}: bad size{tag}")
                    return None
                size = (int(w), int(h))
            elif key == "format":
                # Its glass is made QOI by hand: it makes no JPEG.
                if value != "qoi":
                    self._say(s, f"error {cmd} format={protocol.word(value)}: not offered{tag}")
                    return None
                fmt = value
            elif key == "quality":
                if not (value.isdigit() and len(value) <= 3 and 1 <= int(value) <= 100):
                    self._say(s, f"error {cmd} quality={protocol.word(value)}: "
                                 f"bad quality{tag}")
                    return None
                quality = int(value)
            else:
                self._say(s, f"error {cmd} {key}={protocol.word(value)}: unknown option{tag}")
                return None
        if who is not None and (size or fmt or quality):
            said = (f"size={size[0]}x{size[1]}" if size else f"format={fmt}" if fmt
                    else f"quality={quality}")
            self._say(s, f"error {cmd} {target} {said}: only for the glass{tag}")
            return None
        if quality:
            self._say(s, f"error {cmd} quality={quality}: only for jpeg{tag}")
            return None
        return who, size, every, False

    def fit(self, size):
        """The glass's size fitted inside `size` with its shape kept, and
        never larger than the glass; the glass's own without one."""
        gw, gh = self.glass_w, self.glass_h
        if size is None or (size[0] >= gw and size[1] >= gh):
            return gw, gh
        w, h = size
        if gw * h <= gh * w:
            w = (gw * h + gh // 2) // gh
        else:
            h = (gh * w + gw // 2) // gw
        return max(1, w), max(1, h)

    def glass(self, w, h):
        """The fake's glass as QOI, w by h: each cell its ground, drawn at
        whatever size is asked. Encoded a run at a time, since a cell is a
        run of one colour."""
        rows = [[list(c) for c in self.grid.looks(y)] for y in range(self.rows)]
        # Each cells layer's grounds over the base's, in the order they
        # stack, mixed by their alpha, where it shares the base's grid.
        for z in sorted(self.above):
            x_ = self.above[z]
            g = x_.lgrid
            if not x_.open or g is None or (g.cols, g.rows) != (self.cols, self.rows):
                continue
            for y in range(g.rows):
                for x in range(g.cols):
                    v = g.bgs[y][x]
                    if g.ch[y][x] is None or v < 0:
                        continue
                    a = (v >> 24) - 1 if v >> 24 else 255
                    top, under = rgb_of(v & 0xffffff), rgb_of(rows[y][x][2])
                    mix = tuple((t * a + u * (255 - a)) // 255 for t, u in zip(top, under))
                    rows[y][x][2] = (mix[0] << 16) | (mix[1] << 8) | mix[2]
        out = bytearray(b"qoif" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x04\x00")
        prev, run = (0, 0, 0), 0

        def flush():
            nonlocal run
            while run:
                k = min(run, 62)
                out.append(0xC0 | (k - 1))
                run -= k
        for y in range(h):
            looks = rows[y * self.rows // h]
            for x in range(w):
                ground = looks[x * self.cols // w][2]
                rgb = rgb_of(ground)
                if rgb == prev:
                    run += 1
                    continue
                flush()
                out += bytes((0xFE,) + rgb)
                prev = rgb
        flush()
        return bytes(out) + b"\0" * 7 + b"\1"

    def _cmd_picture(self, s, m, tag, now):
        got = self._capture(s, m, tag, "picture")
        if got is None:
            return
        who, size, _, _ = got
        w, h = self.fit(size)
        target = who.name if who else "glass"
        if s.queued >= QUEUE_LIMIT:
            self._say(s, f"error picture {target}: too far behind{tag}")
            return
        if who is None:
            data = self.glass(w, h)
            self._blob(s, f"picture glass qoi {w}x{h} {len(data)}{tag}", data)
        elif who.type == "video":
            if who.vlast is None or who.z not in self.pics:
                self._say(s, f"error picture {target}: nothing shown{tag}")
                return
            fw, fh, fx, fy, _, data, codec, size = who.vlast
            self._blob(s, f"picture {target} {frame_words(codec, fw, fh, fx, fy, size)} "
                          f"{len(data)}{tag}", data)
        elif who.lgrid is not None:
            data, _ = who.lgrid.picture(None)
            self._blob(s, f"picture {target} grid {who.larea[0]}x{who.larea[1]} "
                          f"{len(data)}{tag}", data)
        else:
            data, _ = self.grid.picture(None)
            data = data or RESET.encode()
            self._blob(s, f"picture {target} grid {self.cols}x{self.rows} {len(data)}{tag}",
                       data)

    def _cmd_mirror(self, s, m, tag, now):
        got = self._capture(s, m, tag, "mirror", mirror=True)
        if got is None:
            return
        who, size, every, off = got
        target = m.args[0]
        if who is not None and who.lgrid is not None:
            self._say(s, f"error mirror {target}: not offered{tag}")
            return
        if off:
            s.mirrors.pop(target, None)
            return
        running = sum(len(x.mirrors) for x in self.sessions.values() if x.open)
        if target not in s.mirrors and (len(s.mirrors) >= 2 or running >= 4):
            self._say(s, f"error mirror {target}: "
                         f"{2 if len(s.mirrors) >= 2 else 4} mirrors already{tag}")
            return
        if every is None:
            every = 0 if who is not None and who.type == "video" else 100
        s.mirrors[target] = {"every": every / 1000, "due": now, "was": None,
                             "size": size, "sent": None}

    def _cmd_follow(self, s, m, tag, now):
        if not m.args:
            self._say(s, f"error follow no value{tag}")
            return
        if m.args[0] not in ("said", "in", "off"):
            self._say(s, f"error follow {protocol.word(m.args[0])}: unknown option{tag}")
            return
        s.follow = m.args[0]

    def _cmd_measures(self, s, m, tag, now):
        if not m.args:
            self._say(s, f"error measures no value{tag}")
            return
        every = protocol.number(m.args[0])
        if every is None:
            self._say(s, f"error measures {protocol.word(m.args[0])}: not a number{tag}")
            return
        if every == 0:
            s.measures = 0.0
            s.measure_due = None
            return
        s.measures = min(60000, max(200, every)) / 1000
        s.measure_due = now + s.measures
        s.measure_mark = (now, self._in_total, self._out_total)

    def _cmd_meta(self, s, m, tag, now):
        words = [w for w in (m.raw or "").split() if not w.startswith("id=")]
        verb = words[1] if len(words) > 1 else None
        rest = words[2:]
        if verb == "report":
            for line in self._meta_lines(s, now):
                self._say(s, line + tag)
            return
        if verb == "clear":
            if rest:
                s.meta.pop(rest[0], None)
                self._say(s, f"meta clear {rest[0]}{tag}")
            else:
                s.meta.clear()
                self._say(s, f"meta clear{tag}")
            return
        if verb not in ("set", "hide", "show"):
            self._say(s, f"error meta {verb or 'no verb'}: unknown verb{tag}")
            return
        if not rest:
            self._say(s, f"error meta {verb} no key{tag}")
            return
        key = rest[0]
        if not META_KEY.match(key):
            self._say(s, f"error meta {verb} {key}: not a key{tag}")
            return
        if verb in ("hide", "show"):
            if key not in s.meta:
                self._say(s, f"error meta {verb} {key}: not set{tag}")
                return
            value, at, _ = s.meta[key]
            s.meta[key] = (value, at, verb == "hide")
            self._say(s, f"meta {verb} {key}{tag}")
            return
        value = " ".join(rest[1:])
        if not value:
            self._say(s, f"error meta set {key}: no value{tag}")
            return
        if len(value.encode()) > 256 or not value.isprintable():
            self._say(s, f"error meta set {key}: too long{tag}")
            return
        if key not in s.meta and len(s.meta) >= 32:
            self._say(s, f"error meta set {key}: too many keys{tag}")
            return
        size = sum(len(k) + len(v[0].encode()) for k, v in s.meta.items() if k != key)
        if size + len(key) + len(value.encode()) > 4096:
            self._say(s, f"error meta set {key}: too much{tag}")
            return
        hidden = s.meta[key][2] if key in s.meta else False
        s.meta[key] = (value, now, hidden)
        self._say(s, f"meta set {key} {value}{tag}")

    def _meta_lines(self, s, now):
        out = []
        for key, (value, at, hidden) in s.meta.items():
            out.append(f"meta set {key} {value}")
            if hidden:
                out.append(f"meta hide {key}")
            out.append(f"meta age {key} {int(now - at)}")
        return out

    # --------------------------------------------------------------- clocks

    def _tick(self, now):
        t = self.t
        while self._resets and self._resets[0] < now - 1.0:
            self._resets.popleft()
        while self._redraws and self._redraws[0] < now - 60.0:
            self._redraws.popleft()
        for z, pic in list(self.pics.items()):
            if now >= pic["until"]:
                self._clear(z)          # no frame within its hold
        for s in list(self.sessions.values()):
            if s.open and s.data:
                self._play(s, now)
        for s in list(self.sessions.values()):
            self._flush(s)
            if s.open:
                if not s.connected and now - s.opened_at >= t.unwritten:
                    self._end(s, "timeout")
                    continue
                if s.connected:
                    self._ping_clock(s, now)
                if s.open:
                    self._mirror_frames(s, now)
                if s.open and s.role == "controller":
                    if s.measure_due is not None and now >= s.measure_due:
                        self._say(s, self._measure_line(s, now))
                        for x in list(self.sessions.values()):
                            if x.open:
                                self._say(s, self._session_measure(s, x, now))
                        s.measure_due = now + s.measures
            elif s.state in ("refused", "closed"):
                drained = not s.queue and not select.select([s.out_fd], [], [], 0)[0]
                if drained or now - s.ended_at >= t.keep:
                    self._remove(s)

    def _ping_clock(self, s, now):
        t = self.t
        if s.awaiting:
            if now - s.pinged_at >= t.pong:
                if s.answered or s.role == "controller":
                    self._end(s, "timeout")
                else:
                    s.awaiting = False
            return
        since = now - max(s.last_in, s.pinged_at or 0.0)
        if self.watching():
            due = since >= t.quiet
        elif s.answered:
            due = since >= t.unwatched
        else:
            due = s.pings == 0 and since >= t.quiet
        if due:
            self._say(s, "ping")
            s.awaiting = True
            s.pinged_at = now
            s.pings += 1

    def _measure_line(self, s, now):
        """The wall's own line: the glass and what it costs, `fps` the
        times it was redrawn by any layer."""
        then, in_then, out_then = s.measure_mark
        elapsed = max(now - then, 1e-6)
        frames = sum(1 for r in self._resets if r >= then) \
            + sum(1 for r in self._redraws if r >= then)
        s.measure_mark = (now, self._in_total, self._out_total)
        return (f"measure wall={self.wall} grid={self.cols}x{self.rows} font={self.font} "
                f"fps={frames / elapsed:.1f} in={(self._in_total - in_then) / elapsed:.0f} "
                f"out={(self._out_total - out_then) / elapsed:.0f} "
                f"display={self.display()}")

    def _session_measure(self, s, x, now):
        """A line for session `x`, to controller `s`: its rates since
        `s` last looked at it."""
        then, in_then, out_then, frames_then = s.session_marks.get(
            x.name, (s.measure_mark[0] - s.measures, x.in_bytes, x.out_bytes, x.frame_count))
        elapsed = max(now - then, 1e-6)
        s.session_marks[x.name] = (now, x.in_bytes, x.out_bytes, x.frame_count)
        place = f" z={x.z} type={x.type}" if x.role == "layer" else ""
        fps = f"{(x.frame_count - frames_then) / elapsed:.1f}" if x.role == "layer" else "-"
        pong = f"{x.pong_ms:.0f}" if x.pong_ms is not None else "-"
        decode = " decode=0.10" if x.type == "video" else ""
        return (f"measure wall={self.wall} session={x.name} role={x.role}{place} fps={fps} "
                f"in={(x.in_bytes - in_then) / elapsed:.0f} "
                f"out={(x.out_bytes - out_then) / elapsed:.0f} dropped={x.dropped_all} "
                f"queue={x.queued} quiet={now - x.last_in:.1f} pong={pong}{decode}")

    def _mirror_frames(self, s, now):
        """A session's mirrors that are due: the glass sent when it changed,
        a cells layer whole and then what changed. A mirror whose reader
        still has a frame waiting is not sent the next; a mirror of a
        session that has gone stops. A video layer's go as its frames come."""
        for target, mirror in list(s.mirrors.items()):
            who = None
            if target != "glass":
                who = self.sessions.get(target)
                if who is None or not who.open:
                    del s.mirrors[target]
                    continue
                if who.type == "video":
                    continue
            if now < mirror["due"]:
                continue
            mirror["due"] = now + mirror["every"]
            if any(x.kind == "blob" for x in s.queue):
                continue
            if who is None:
                w, h = self.fit(mirror["size"])
                data = self.glass(w, h)
                if data == mirror["sent"]:
                    continue
                mirror["sent"] = data
                self._blob(s, f"mirror glass qoi {w}x{h} {len(data)}", data)
                continue
            data, rows = self.grid.picture(mirror["was"])
            if mirror["was"] is None:
                data = data or RESET.encode()
                header = f"mirror {target} grid {self.cols}x{self.rows} {len(data)}"
            elif data:
                header = f"mirror {target} diff {len(data)}"
            else:
                continue
            mirror["was"] = rows
            self._blob(s, header, data)

    # --------------------------------------------------------------- ending

    def _end(self, s, reason):
        if not s.open:
            return
        now = time.monotonic()
        s.state, s.reason, s.ended_at = "closed", reason, now
        self._say(s, f"closed {s.name} {reason}")
        if s.in_fd is not None:
            os.close(s.in_fd)
            s.in_fd = None
        if s.data_fd is not None:
            os.close(s.data_fd)
            s.data_fd = None
        s.dqueue.clear()
        s.mirrors = {}
        s.measure_due = None
        if s is self.base:
            self.base = None
            self.painted = False
        if s.z is not None and self.above.get(s.z) is s:
            self._clear(s.z)
            del self.above[s.z]
            if s.z > 0:
                step = 1 if s.from_top else -1
                k = s.z - step
                while 1 <= k < LAYERS and self.above.get(k) is not None \
                        and self.above[k].open and self.above[k].from_top == s.from_top:
                    self._move(k, k + step)
                    k -= step
        self._tell_controllers(f"session closed {s.name} {reason}", but=s)

    def _move(self, frm, to):
        """The layer at `frm` moved to the free slot `to`, with its picture,
        and told so, as every controller is."""
        x = self.above.pop(frm, None)
        if x is None:
            return
        self.above[to] = x
        x.z = to
        if frm in self.pics:
            self.pics[to] = self.pics.pop(frm)
        self._say(x, f"moved {x.name} z={to}")
        self._tell_controllers(f"session moved {x.name} {to}")

    def _close_fds(self, s):
        for attr in ("in_fd", "out_fd", "data_fd"):
            fd = getattr(s, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(s, attr, None)

    def _remove(self, s):
        self._close_fds(s)
        shutil.rmtree(s.path, ignore_errors=True)
        self.sessions.pop(s.name, None)

    # ------------------------------------------------------- for the tests

    def session(self, name):
        with self._lock:
            return self.sessions.get(name)

    def live(self):
        """The sessions the wall holds, in the order they were opened."""
        with self._lock:
            return [s for s in self.sessions.values() if s.open]

    def say(self, name, *args, **opts):
        """A line to the base, as the wall would say it."""
        with self._lock:
            if self.base is not None and self.base.open:
                self._say(self.base, protocol.render(name, *args, **opts))

    def say_to(self, session, line):
        with self._lock:
            s = self.sessions.get(session) if isinstance(session, str) else session
            if s is not None and s.open:
                self._say(s, line)

    def text(self, since=0.0, until=None):
        """Paint from every layer, in the order it arrived."""
        with self._lock:
            return "".join(t for when, _, t in self.paint
                           if when >= since and (until is None or when < until))

    def frames(self, since=0.0):
        """Every frame body that arrived since, full or not."""
        return [b for b in self.text(since).split(RESET) if b]

    def full_frames(self, since=0.0):
        return [b for b in self.frames(since) if covers(b, self.cols, self.rows)]

    def commands(self, name):
        with self._lock:
            return [m for _, m in self.asked if m.name == name]

    def stream(self):
        """Everything that arrived on every session, commands and all."""
        with self._lock:
            return "".join("".join(s.raw) for s in self.sessions.values())


def qoi_end(b):
    """Where the QOI image at the start of `b` ends: its length, None when
    it has not all arrived, or -1 when it is not a QOI image."""
    if len(b) < 14:
        return None if b"qoif".startswith(b[:4]) else -1
    if b[:4] != b"qoif":
        return -1
    w, h = int.from_bytes(b[4:8], "big"), int.from_bytes(b[8:12], "big")
    if not (1 <= w <= FRAME_SIDE and 1 <= h <= FRAME_SIDE) or b[12] not in (3, 4):
        return -1
    n, p, px = len(b), 14, 0
    total = w * h
    while px < total:
        if p >= n:
            return None
        op = b[p]
        if op == 0xFE:
            p += 4
            px += 1
        elif op == 0xFF:
            p += 5
            px += 1
        elif op >> 6 == 2:
            p += 2
            px += 1
        elif op >> 6 == 3:
            p += 1
            px += (op & 0x3F) + 1
        else:
            p += 1
            px += 1
    if px > total:
        return -1
    if p + 8 > n:
        return None
    return p + 8 if b[p:p + 8] == b"\0" * 7 + b"\1" else -1


_WIRE = re.compile(r"\x1b\[([\d;]*)([A-Za-z])|([^\x1b])", re.DOTALL)


def covers(body, cols, rows):
    """Whether a frame body writes every cell of a cols x rows grid."""
    seen, x, y = set(), 0, 0
    for m in _WIRE.finditer(body):
        if m.group(3) is not None:
            if 0 <= x < cols and 0 <= y < rows:
                seen.add((x, y))
            x += 1
        elif m.group(2) == "H":
            p = (m.group(1) or "").split(";") + ["", ""]
            y, x = int(p[0] or 1) - 1, int(p[1] or 1) - 1
    return len(seen) == cols * rows


def main(argv):
    import argparse
    import signal
    ap = argparse.ArgumentParser(description="a fake wall that speaks doors and sessions")
    ap.add_argument("run")
    ap.add_argument("wall", nargs="?", default="0")
    ap.add_argument("--cols", type=int, default=60)
    ap.add_argument("--rows", type=int, default=16)
    ap.add_argument("--font", type=int, default=14)
    ap.add_argument("--controllers", type=int, default=8)
    ap.add_argument("--presence")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--hw", default="", help="codecs it says it decodes in hardware, a,b")
    a = ap.parse_args(argv)
    wall = FakeWall(a.run, a.wall, a.cols, a.rows, a.font, a.controllers,
                    presence=a.presence, timings=Timings(a.scale),
                    hw=[c for c in a.hw.split(",") if c])
    print(wall.dir, flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    wall.remove()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
