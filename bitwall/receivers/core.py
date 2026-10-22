"""A wall's receiver side, for any glass: a door and its sessions.

    from bitwall.receivers.core import Wall
    wall = Wall(run, "kitchen", glass, presence="kitchen")
    ...
    wall.remove()

It is the receiver side of PROTOCOL.md, as the painter is, for a glass it
cannot see: it claims `<run>/bitwall/<name>/`, reads its door, makes and
reaps sessions, keeps the command table and each session's backpressure,
and hands what the layers draw to its glass, which stacks and paints
them. A thread of its own reads every `in` the moment it is written, so a
sender is never held up by a clock. The web receiver's glass is the
browser tabs showing the wall (`web/`); a pane of `senders/panes` is a
rectangle of another wall (PANES.md).

What only the glass knows it says through `set_box`: its box in device
pixels and how big a cell is at every font, from which the area follows
the font as the painter's does, or a grid it is fixed at; how often it
put the glass up; and, when asked, a look at the glass as QOI, or as
JPEG where its `pictures` says `jpeg`, for `picture glass` and `mirror
glass`. The base's cells are kept here as
well, as a sender would read them back, so a picture of a layer needs no
glass and a glass joining late is given the wall as it stands. What the
glass can show and what the wall takes -- `colour`, `rate`, `glyphs`,
`layers`, `types` -- are given when it is made.

The base's cells may carry kitty graphics (`kitty.py`): they are read out
of its stream here, before anything else sees it, and the glass is
handed each picture and the placements, never the strings.

`glass` is what reaches it: `cells(bytes)`, `frame(z, head, bytes)`,
`clear(z)`, `kitty(head, bytes or None)`, `changed()`, `stats()` and
`look(w, h, done)`, with `quality=` for a JPEG where its `pictures`
says `jpeg`, each called under the wall's lock and each returning at
once; `done(bytes or None)` comes back later from another thread.
A glass that decodes films on `data` has `vstart`, `vchunk`, `vend`,
`vgone`, `vmove`, `valign`, `vbox`, `films()`, `backlog()` and
`look_layer` as well: the wall cuts the stream into frames and times it,
says where they go, and the glass decodes.

Timings are PROTOCOL.md's, scaled by `BITWALL_CLOCK_SCALE` as the
painter's are, for tests.
"""
import codecs
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
from collections import deque

from bitwall import local, protocol
from bitwall.grid import Grid, LayerGrid
from bitwall.receivers import kitty as kittyg
from bitwall.receivers import stream as streams

try:
    import resource
except ImportError:                     # Windows
    resource = None

RESET = "\x1b[0m"

#: A session's name, and an id: one to thirty-two of these.
NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
#: A meta key.
META_KEY = re.compile(r"^[a-z0-9._-]{1,32}\Z")
#: A token as a `from` or an `instance` is.
TOKEN = NAME
INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

#: Lines that say a current value: merged into the latest when a
#: session's `out` backs up, never dropped.
STATE = ("area", "watching", "accepts", "display", "power", "presence", "screen")
#: The lines a session's queue holds however far behind its reader is.
KEEP = ("opened", "closed")

#: The most a session's queue holds while a picture waits in it.
QUEUE_LIMIT = 8 << 20

#: Who may say what: the roles the command table allows each command.
#: `base` is the lowest cells layer, `layer` any other cells layer,
#: `video` a video layer, `controller` a controller.
ALL = frozenset({"base", "layer", "video", "controller"})
#: `align`'s words, and where each leaves the glass spare.
ALIGN = {"top": -1, "centre": 0, "bottom": 1}
MAY = {
    "font": frozenset({"base", "layer", "controller"}),
    "grid": frozenset({"base", "layer", "controller"}),
    "blank": frozenset({"base", "controller"}),
    "want": frozenset({"base", "layer", "video"}),
    "frame": frozenset({"video"}),
    "clear": frozenset({"video", "layer"}),
    "align": frozenset({"video"}),
    "box": frozenset({"video"}),
    "presence": frozenset({"controller"}),
    "stats": frozenset({"controller"}),
    "picture": ALL,
    "mirror": ALL,
    "follow": frozenset({"controller"}),
    "measures": frozenset({"controller"}),
    "report": ALL, "sessions": ALL, "ping": ALL, "pong": ALL,
    "meta": ALL, "detach": ALL, "close": ALL,
}

CAPS = ("area font grid meta blank want input presence repaint watching ping detach "
        "close screen picture mirror stats sessions follow measures frame clear kitty")
#: The layers it takes, the base included: the base at 0, video above it.
LAYERS = 8
CONTROLLERS = 8
#: A video frame's limits, and its hold when it says none.
FRAME_SIDE = 4096
#: A JPEG picture's quality where `format=jpeg` says none.
JPEG_QUALITY = 85
FRAME_MAX = 16 << 20
HOLD_MS = 1000
#: What a frame on a video layer's `in` may be: the browser decodes all three.
IMAGES = protocol.IMAGES
#: The streams on `data` that are pictures, each cut out and shown as a
#: frame is, by the codec a frame says it in; any other is a film, which
#: the glass decodes.
DATA_PICTURES = {"qoi": "qoi", "mjpeg": "jpeg"}
#: How far ahead of its time a frame of a film on `data` goes to the glass,
#: which decodes it, in seconds; and how many seconds of a timed stream
#: are read off `data` beyond that, which is what paces its writer.
LEAD = 0.5
AHEAD = 2.0
#: The most of a film kept since its last key for a glass that joins.
GOP_MAX = 32 << 20
#: The most a glass may have waiting before `data` is read no further.
BACKLOG = 4 << 20


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


def plain(word):
    """A word of a request as an error says it back, as the painter does:
    at most 47 characters, each outside printable ASCII a `?`."""
    return "".join(c if " " < c < "\x7f" else "?" for c in word[:47])


def frame_words(codec, w, h, x, y, size):
    """A frame as a picture or a mirror of it says it: its codec, its own
    size, where it went, and the box it was drawn to where it was scaled."""
    return (f"{codec} {w}x{h} at={x},{y}"
            + (f" size={size[0]}x{size[1]}" if size else ""))


def placed(w, h, x, y, size):
    """Where a frame goes on the glass, as the glass is told it."""
    sw, sh = size or (w, h)
    return {"x": x, "y": y, "w": sw, "h": sh}
#: What the overlay can show: the painter's, less what a canvas and a
#: server cannot know -- an atlas, a swap, a monitor's power, a board's
#: temperature -- and an inbox, since what is read here is parsed at once.
MEASURES = ("wall", "grid", "fps", "in", "out", "frame", "draw", "sender", "pong",
            "display", "cpu", "render")
POINTS = ("n", "ne", "e", "se", "s", "sw", "w", "nw", "c")
#: The fonts a cell may be drawn at, as the page measures them.
FONT_LOW, FONT_HIGH = 4, 400
#: With no base, how long the status page shows before the wall dozes.
DOZE = 300.0
#: How often the overlay is drawn while it is up, in seconds.
STATS_PERIOD = 0.5


#: The cell a font makes before the glass has said, in device pixels, and
#: the box it is laid in: a nominal glass, so a sender arriving before
#: anybody is looking has an area to compose for.
NOMINAL_BOX = (1600, 900)


def nominal_cell(font):
    w = max(2, -(-font * 3 // 5))
    h = max(4, -(-font * 6 // 5))
    return w + w % 2, h + (4 - h % 4) % 4


class ClaimRefused(Exception):
    """The wall asked for belongs to a live receiver: its door is read."""


class Timings:
    """PROTOCOL.md's clocks, in seconds, all scaled together."""

    def __init__(self, scale=None):
        if scale is None:
            try:
                scale = float(os.environ.get("BITWALL_CLOCK_SCALE", "1"))
            except ValueError:
                scale = 1.0
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
    """Whether something reads the door at `path`: a live wall's."""
    return local.door_live(path)


class _Item:
    """One thing waiting in a session's queue: `kind` is `keep`, `state`,
    `event` or `blob`; `key` names the state a `state` line carries. A
    blob whose `data` is None is a picture the glass has yet to hand back,
    and everything behind it waits for it."""

    __slots__ = ("at", "data", "key", "kind")

    def __init__(self, kind, key, data):
        self.kind, self.key, self.data, self.at = kind, key, data, 0


class Session:
    """One session as the wall holds it: its fifos, its place, its clocks
    and its counters."""

    def __init__(self, name, path, now):
        self.name = name
        self.path = path
        self.role = None
        self.z = None
        self.from_top = False   # placed in the stack from the highest slot down
        #: A cells layer above the base: its grid, what it asked of its own
        #: -- None to follow the base, ("font", n) or ("grid", cols, rows)
        #: -- and the area it has, (cols, rows, font).
        self.lgrid = None
        self.own = None
        self.larea = None
        self.type = None
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
        #: The input it asked for: touch, keyboard, buttons.
        self.wants = {}
        self.follow = "off"
        self.measures = 0.0
        self.measure_due = None
        self.measure_mark = None
        #: What this session mirrors: target -> its clock and what its
        #: reader already has.
        self.mirrors = {}
        self.in_bytes = self.out_bytes = 0
        self.queue = deque()
        self.queued = 0
        self.dropped = 0
        #: Lines let go since it opened, and frames it put up.
        self.dropped_all = 0
        self.frame_count = 0
        #: A controller's last look at each session, for its measure lines:
        #: name -> (when, in, out, frames).
        self.session_marks = {}
        #: Its rates over the last whole second, as the painter keeps them,
        #: so its measure line and its row on the overlay say the same:
        #: -1 until there is a second to go on.
        self.rates_at = now
        self.rates_mark = (0, 0, 0)
        self.r_in = self.r_out = self.r_fps = -1.0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.parser = protocol.Commands()
        #: A video layer's unread bytes, the payload bytes its last `frame`
        #: said were coming, whether they are let go, and what it said.
        self.vbuf = b""
        self.vwant = 0
        self.skip = True
        self.fhead = None
        #: The frame it shows, as (w, h, x, y, hold, bytes).
        self.vlast = None
        #: As the base: its stream cut round kitty strings, and its kitty
        #: graphics.
        self.split = None
        self.kitty = None
        #: A stream on `data`: whether one was asked for, its codec, its
        #: frames a second and first frame's Unix ms; the fifo read, the
        #: stream under way and how many there have been, the frames cut
        #: from it and not yet shown or handed on, as (index, key, bytes),
        #: the clock its frame 0 is due by, and a film's frames since its
        #: last key, for a glass that joins.
        self.data = False
        self.codec = None
        self.fps = None
        self.start = None
        #: Where the stream's frames leave their box spare: -1 all below,
        #: 0 even, 1 all above, as `align` says.
        self.align = 0
        #: The box the stream's frames are fitted to, (x, y, w, h) in the
        #: glass's pixels as `box` says, or None for the glass.
        self.box = None
        self.data_fd = None
        self.data_idle_until = 0.0
        self.streaming = False
        self.ending = False
        self.streams = 0
        self.cut = None
        self.dnext = 0
        self.dqueue = deque()
        self.t0 = None
        self.gop = []
        self.gop_bytes = 0
        self.announced = False
        #: A stream that did not decode, let go until its writer leaves.
        self.discard = False

    @property
    def film(self):
        """A stream the glass decodes, rather than pictures this wall cuts."""
        return self.data and self.codec not in DATA_PICTURES

    @property
    def open(self):
        return self.state == "open"


class Wall:
    """A wall under `<run>/bitwall/<name>/`, served by a thread of its
    own, painting on whatever `glass` reaches.

    `presence` names the presence it follows; `font` is the size a cell is
    drawn at until somebody asks for another. `receiver` and `reach` (a line,
    or a list of them, one a way in) are
    the report's lines saying which program keeps the wall and how its
    glass is reached from elsewhere, each left out where it is None.
    """

    def __init__(self, run, name, glass, presence=None, font=14, timings=None,
                 receiver=None, reach=None, colour="true", rate=30, glyphs=None,
                 layers=LAYERS, types=("cells", "video"), kitty=True,
                 commands=None, above=("video",), data=(), video_base=False):
        self.run = run
        self.wall = name
        #: What the glass can show, said in `screen`, and what the wall
        #: takes, said in `caps`: the glass's to say, not this class's.
        self.colour, self.rate, self.glyphs = colour, rate, glyphs
        self.layers, self.types, self.kitty_on = layers, tuple(types), kitty
        #: The types a layer above the base may be, said as `above=` in caps.
        self.above_types = tuple(above)
        #: What a stream on a video layer's `data` may be, none where the
        #: wall takes no data pipe; and whether the base may be video.
        self.data_codecs = self._data_fixed = tuple(data)
        #: Of those, the ones the glass decodes in hardware, said as `hw=`.
        self.data_hw = ()
        self.video_base = video_base
        #: Commands of the glass's own, for controllers: each name to a
        #: function of the Message giving the lines that answer it, or
        #: raising ValueError with the reason it is refused. Said in
        #: `caps`, as PROTOCOL.md lets a wall add commands of its own.
        self.commands = dict(commands or {})
        self.receiver = receiver
        self.reach = reach
        self.glass = glass
        self.t = timings or Timings()
        self.presence_name = presence
        self.present = True
        self.feeder = None
        self.fed_at = None
        self.blank = False
        #: Whether the base has connected: until then, and after it goes,
        #: the glass shows the status page, and after DOZE the wall dozes.
        self.connected = False
        self.dozing = False
        self.last_data = time.monotonic()
        self.stats = {"on": 0, "at": "ne", "size": 14, "show": "fps"}
        #: What the cells are asked to be: ("font", n) or ("grid", cols, rows).
        self.request = ("font", font)
        self.default_font = font
        self.asked = False
        #: The glass's box, as it last said it: None until one has.
        self.box = None
        self.pixels = None
        self.font = font
        self.cell = nominal_cell(font)
        self.cols, self.rows = self._fit(*NOMINAL_BOX, nominal_cell(font))
        self.grid = Grid(self.cols, self.rows)
        #: Tabs in view, and what they can give.
        self.visible = 0
        self.accepts = ()
        #: What the glass measured of its own painting, and when.
        self.glass_numbers = {}
        self.sessions = {}
        self.base = None
        #: The session holding each layer above the base, by z, and what
        #: each shows: {z: {"w", "h", "x", "y", "until"}}.
        self.above = {}
        self.pics = {}
        #: Decoded bytes of kitty images held across the wall, and the last
        #: key a picture was given.
        self.kitty_total = [0]
        self.kitty_key = 0
        #: Tracebacks of anything that went wrong while serving. A wall
        #: keeps serving past a fault.
        self.errors = []
        self._resets = deque()
        self._in_total = self._out_total = 0
        self._redraws = deque()
        self._cpu_mark = None
        self._overlay_mark = None
        self._overlay_due = 0.0
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread = None
        self.dir = None
        self.door_fd = None
        self._door_rest = b""
        self._door_idle = 0.0

    # ------------------------------------------------------------ claiming

    @property
    def door(self):
        return os.path.join(self.run, "bitwall", self.wall, "door.in")

    def start(self):
        """Claim the wall: build it under a `.` name, take over a wall whose
        door nobody reads, and rename into place. On Windows the door is a
        pipe whose first instance is the claim, and a dead wall's pipes
        went with it (WINDOWS.md)."""
        if local.PIPES:
            final = self.run.rstrip("/") + "/bitwall/" + self.wall
            try:
                self.door_fd = local.make_door(final + "/door.in")
            except local.Claimed as e:
                raise ClaimRefused(f"{final}: its door is read") from e
            self.dir = final
            self._halt = threading.Event()
            self._thread = threading.Thread(target=self._serve, daemon=True,
                                            name=f"wall-{self.wall}")
            self._thread.start()
            return
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
                                        name=f"wall-{self.wall}")
        self._thread.start()

    def stop(self):
        """Stop serving: every fifo closed, nothing said."""
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(3)
            self._thread = None
        with self._lock:
            if self.door_fd is not None:
                local.close(self.door_fd)
                self.door_fd = None
            for s in self.sessions.values():
                self._close_fds(s)
            self.sessions.clear()
            self.base = None

    def remove(self):
        """Stop in good order: stop, and take the wall away."""
        self.stop()
        if self.dir and not local.PIPES and os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)

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
                    if s.data_fd is not None and s.data_idle_until <= now and self._room(s):
                        fds.append(s.data_fd)
                        data_fd[s.data_fd] = s
            try:
                ready = local.select(fds, t.tick)
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
                    trace = traceback.format_exc()
                    self.errors.append(trace)
                    del self.errors[:-20]
                    print(f"{time.strftime('%H:%M:%S')} {self.wall}: {trace}",
                          file=sys.stderr, flush=True)

    def _read_door(self, now):
        try:
            data = local.read(self.door_fd, 65536)
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
            return
        name = words[1]
        role = words[2] if len(words) > 2 else None
        opts = {}
        for w in words[3:]:
            key, sep, value = w.partition("=")
            if not sep or not key or not value:
                return
            opts[key] = value
        path = os.path.join(self.dir, "s", name)
        if any(x.lower() == name.lower() for x in self.sessions):
            return
        if not local.PIPES:
            if os.path.lexists(path):
                return
            try:
                os.makedirs(path)
            except FileExistsError:
                return
        s = Session(name, path, now)
        s.in_fd = local.make_in(os.path.join(path, "in"))
        # Made last, so a session whose `out` is there is whole.
        s.out_fd = local.make_out(os.path.join(path, "out"))
        self.sessions[name] = s
        self._say(s, protocol.hello(), kind="keep")
        reason = self._admit(s, role, opts)
        if reason:
            s.state, s.reason, s.ended_at = "refused", reason, now
            local.close(s.in_fd)
            s.in_fd = None
            self._say(s, f"error open {name} {reason}")
            return
        s.state = "open"
        if s.role == "layer" and s.z == 0:
            self.base = s
            if s.type == "video":
                self.above[0] = s
            self.glass.changed()
        elif s.role == "layer":
            self.above[s.z] = s
            if s.type == "cells":
                s.larea = (self.cols, self.rows, self.font)
                s.lgrid = LayerGrid(self.cols, self.rows)
                self._lglass("lgrid", s.z, *s.larea)
        if s.data:
            # Made before `opened` says it is there.
            s.data_fd = local.make_in(os.path.join(path, "data"))
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
            # Its one option is the instance a controller started it under,
            # as a layer's is, so `sessions` finds a sender that only talks
            # to the wall.
            for key, value in opts.items():
                if key != "instance" or not INSTANCE.match(value):
                    return "no-such-key"
                s.instance = value
            if sum(1 for x in self.sessions.values()
                   if x.open and x.role == "controller") >= CONTROLLERS:
                return "full"
            return None
        z, place, take, video = 0, None, False, False
        data, codec, fps, start = False, None, None, None
        for key, value in opts.items():
            if key == "z":
                if value in ("above", "top"):
                    place = value
                    continue
                if not value.isdigit() or len(value) > 4:
                    return "no-such-key"
                z, place = int(value), None
            elif key == "type":
                if value not in self.types:
                    return "no-such-type"
                video = value == "video"
            elif key == "take":
                if value != "1":
                    return "no-such-key"
                take = True
            elif key == "instance":
                if not INSTANCE.match(value):
                    return "no-such-key"
                s.instance = value
            elif key == "data" and self.data_codecs:
                if value != "1":
                    return "no-such-key"
                data = True
            elif key == "codec" and self.data_codecs:
                codec = value
            elif key == "fps" and self.data_codecs:
                if not re.match(r"^\d{1,3}(\.\d{1,6})?$", value) or not 0 < float(value) <= 240:
                    return "no-such-key"
                fps = float(value)
            elif key == "start" and self.data_codecs:
                if not (value.isdigit() and len(value) <= 16):
                    return "no-such-key"
                start = int(value)
            else:
                return "no-such-key"
        if data and not video:
            return "no-such-key"
        if not data and (codec is not None or fps is not None or start is not None):
            return "no-such-key"
        if start is not None and fps is None:
            return "no-such-key"
        if data and codec not in self.data_codecs:
            return "no-such-codec"
        s.data, s.codec, s.fps, s.start = data, codec, fps, start

        def held(z):
            x = self.above.get(z)
            return x is not None and x.open
        cells_above = "cells" in self.above_types
        if not video and (place is not None or z > 0) and not cells_above:
            return "no-such-type"
        if z >= self.layers:
            return "full"
        if place is None and video and z == 0 and not self.video_base:
            return "no-such-type"
        if place is not None or z > 0:
            # Two stacks with no gaps: one up from the base, one down from
            # the highest slot. `above` goes on top of the first, `top`
            # under the second, and a number in at that slot of the first,
            # or on top of it where it is not that high yet, the layers
            # from there up moving one to make room.
            if take:
                return "no-such-key"
            lo = sum(1 for k in range(1, self.layers) if held(k) and not self.above[k].from_top)
            hi = sum(1 for k in range(1, self.layers) if held(k) and self.above[k].from_top)
            if lo + hi >= self.layers - 1:
                return "full"
            if place == "top":
                z, s.from_top = self.layers - 1 - hi, True
            else:
                z = lo + 1 if place == "above" or z > lo + 1 else z
                for k in range(lo, z - 1, -1):
                    self._move(k, k + 1)
            s.z, s.type = z, "video" if video else "cells"
            return None
        s.z, s.type = z, "video" if video else "cells"
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
            data = local.read(s.in_fd, 1 << 20)
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
            if s is self.base:
                self._base_connected(now)
        if s is self.base:
            self.last_data = now
        if s.type == "video":
            self._read_video(s, data, now)
            return
        if s is not self.base:
            self._read_cells(s, data, now)
            return
        if s.split is None:
            s.split = kittyg.Splitter()
        for kind, part in s.split.feed(data):
            if not s.open or s is not self.base:
                break
            if kind == "text":
                self._read_cells(s, part, now)
            elif kind == "kitty":
                self._kitty(s).string(part)
            else:
                self._kitty(s).overlong()

    def _read_cells(self, s, data, now):
        """A cells session's bytes, kitty strings taken out: the base's
        paint to the grid and the glass, and every session's commands."""
        text = s.decoder.decode(data)
        paint, said = s.parser.feed(text)
        if paint and s is self.base:
            frames = paint.count(RESET)
            s.frame_count += frames
            self.grid.feed(paint)
            self.glass.cells(paint.encode())
            for _ in range(frames):
                self._resets.append(now)
        elif paint and s.lgrid is not None:
            s.frame_count += paint.count(RESET)
            s.lgrid.feed(paint)
            self._lglass("lcells", s.z, paint.encode())
        for m in said:
            self._command(s, m, now)

    def _kitty(self, s):
        """The session's kitty graphics, placed at the grid's cursor."""
        if s.kitty is None:
            def move(dx, dy):
                g = self.grid
                g.x = min(g.x + dx, self.cols)
                g.y = max(0, min(g.y + dy, self.rows - 1))

            def tabs(kind, arg):
                if kind == "image":
                    self.glass.kitty({"t": "kimg", "key": arg.key, "f": arg.f,
                                     "w": arg.w, "h": arg.h}, arg.data)
                elif kind == "forget":
                    self.glass.kitty({"t": "kgone", "key": arg}, None)
                else:
                    self.glass.kitty({"t": "kplaces", "list": arg}, None)

            def keyer():
                self.kitty_key += 1
                return self.kitty_key

            s.kitty = kittyg.Kitty(lambda line: self._say(s, line),
                                   lambda: (self.grid.x, self.grid.y), move,
                                   lambda: self.cell, self.kitty_total, tabs, keyer)
        return s.kitty

    def _base_connected(self, now):
        """The base's first byte: the wall has a sender, the status page and
        any doze go, and the cells go back to the wall's own font unless
        somebody has asked for another since the last sender went."""
        was = self.display(), self.watching()
        self.connected = True
        self.dozing = False
        if not self.asked:
            self.request = ("font", self.default_font)
            self._relayout()
        self.grid.resize(self.cols, self.rows)
        self._dark_changed(*was)

    def _command(self, s, m, now):
        """A command from a session, followed to the controllers that asked,
        but for a video frame's line, which is framing, as its bytes are."""
        for c in self.sessions.values():
            if m.name == "frame":
                break
            if c is not s and c.open and c.follow == "in":
                self._say(c, f"< {s.name} {m.raw}")
        self._obey(s, m, now)

    def _read_video(self, s, data, now):
        """A video layer's bytes: commands as they come, each frame's
        payload gathered whole, and anything else let go."""
        b = s.vbuf + data
        p = 0
        while p < len(b) and s.open:
            if s.vwant:
                have = len(b) - p
                if s.skip:
                    k = min(have, s.vwant)
                    p += k
                    s.vwant -= k
                    continue
                if have < s.vwant:
                    break
                self._video_frame(s, b[p:p + s.vwant], now)
                p += s.vwant
                s.vwant = 0
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
        """One frame's payload, whole: put up if its header and end are what
        its line said, or refused with the line's id. The glass decodes it
        and draws it to its box; a mirror of the layer is passed its bytes
        as they came."""
        codec, w, h, x, y, size, hold, tag = s.fhead
        if protocol.image_size(codec, data) != (w, h):
            self._say(s, f"error frame {codec}: does not decode{tag}")
            return
        if self.above.get(s.z) is not s:
            return
        data = bytes(data)
        s.frame_count += 1
        self._redraws.append(now)
        self.pics[s.z] = {"w": w, "h": h, "x": x, "y": y, "codec": codec, "size": size,
                          "until": float("inf") if hold is None else now + hold / 1000}
        s.vlast = (w, h, x, y, hold, data, codec, size)
        self.glass.frame(s.z, {**placed(w, h, x, y, size), "codec": codec}, data)
        for c in list(self.sessions.values()):
            mirror = c.mirrors.get(s.name)
            if mirror is None or not c.open or now < mirror["due"] or c.queue:
                continue
            mirror["due"] = now + mirror["every"]
            self._blob(c, f"mirror {s.name} {frame_words(codec, w, h, x, y, size)} "
                          f"hold={'still' if hold is None else hold} {len(data)}", data)

    # ---------------------------------------------------------- the data pipe

    def _room(self, s):
        """Whether a layer's `data` is read now: not while what is cut from
        it already reaches far enough ahead of its time, nor while a glass
        is behind, so that a full pipe paces the writer as a decoder's
        does on a Pi."""
        if self.glass_backlog() > BACKLOG:
            return False
        ahead = int(s.fps * (AHEAD + LEAD)) + 8 if s.fps else 8
        return len(s.dqueue) < ahead

    def glass_backlog(self):
        fn = getattr(self.glass, "backlog", None)
        return fn() if fn is not None else 0

    def _glass_films(self):
        """Whether a glass is there to decode a film: none, and a film's
        frames are let go at their time, counted as dropped."""
        fn = getattr(self.glass, "films", None)
        return bool(fn()) if fn is not None else False

    def _read_data(self, s, now):
        """A layer's `data`: a stream starts with its first bytes and ends
        when its writer leaves, what was cut from it by then played out."""
        try:
            data = local.read(s.data_fd, 1 << 20)
        except BlockingIOError:
            return
        if not data:
            s.data_idle_until = now + self.t.tick
            s.discard = False
            if s.streaming and not s.ending:
                try:
                    units = s.cut.flush()
                except ValueError:
                    units = []
                for u in units:
                    self._data_unit(s, u)
                s.ending = True
                self._play(s, now)
            return
        if s.discard:
            return              # the rest of a stream that did not decode
        if s.ending:
            self._data_end(s)   # a new writer before the last stream played out
        if not s.streaming:
            s.streaming, s.ending = True, False
            s.streams += 1
            s.cut = streams.cutter(s.codec)
            s.dnext, s.t0, s.announced = 0, None, False
            s.gop, s.gop_bytes = [], 0
            if s.streams == 1 and s.start is not None and s.fps:
                s.t0 = now + (s.start / 1000 - time.time())
        s.in_bytes += len(data)
        self._in_total += len(data)
        s.last_in = now
        if s is self.base:
            self.last_data = now
        if not s.connected:
            s.connected = True
            self._say(s, "repaint")
            if s is self.base:
                self._base_connected(now)
        try:
            units = s.cut.feed(data)
        except ValueError:
            self._say(s, f"error data {s.codec}: does not decode")
            self._data_end(s)
            s.discard = True
            return
        for u in units:
            self._data_unit(s, u)
        self._play(s, now)

    def _data_unit(self, s, unit):
        """One frame cut from the stream: a picture, or a film's access
        unit and whether it is a key."""
        if s.film:
            data, key = unit
        else:
            data, key = unit, True
        s.dqueue.append((s.dnext, key, data))
        s.dnext += 1

    def _play(self, s, now):
        """Frame i is due at t0 + i/fps. A picture early waits, and one
        whose successor is due is late, let go and counted; without fps
        each goes up as it is cut. A film's frames go to the glass LEAD
        ahead of their time, every one, since each is decoded from those
        before it, and the glass shows them at their time; with no glass
        to decode them they are let go at their time."""
        if not s.streaming:
            return
        if s.dqueue and s.fps and s.t0 is None:
            i = s.dqueue[0][0]
            s.t0 = now + (LEAD if s.film else 0.0) - i / s.fps
        films = s.film and self._glass_films()
        while s.dqueue:
            i, key, data = s.dqueue[0]
            if not s.fps:
                pass
            elif s.film and films:
                if now < s.t0 + i / s.fps - LEAD:
                    break
            elif now >= s.t0 + (i + 1) / s.fps:
                s.dqueue.popleft()
                s.dropped_all += 1
                if s.film:
                    self._keep(s, i, key, data)
                continue
            elif now < s.t0 + i / s.fps:
                break
            s.dqueue.popleft()
            if s.film:
                if films or not s.fps:
                    self._hand_on(s, i, key, data)
                else:
                    self._keep(s, i, key, data)
            else:
                self._data_show(s, now, data)
        if s.ending and not s.dqueue:
            # A picture's last frame is up for its time; a film's glass
            # plays out what it was handed.
            if s.film or not s.fps or s.t0 is None or now >= s.t0 + s.dnext / s.fps:
                self._data_end(s)

    def _keep(self, s, i, key, data):
        """A film's frame kept for a glass that joins: everything since the
        last key, or nothing once that is more than it should hold."""
        if key:
            s.gop, s.gop_bytes = [], 0
        if s.gop_bytes + len(data) > GOP_MAX or (not s.gop and not key):
            s.gop, s.gop_bytes = [], 0
            return
        s.gop.append((i, key, data))
        s.gop_bytes += len(data)

    def film_head(self, s):
        """What a glass is told of a film as it starts: the stream, its
        codec and rate, and frame 0's time on the Unix clock in ms."""
        t0 = None if s.t0 is None else (s.t0 + time.time() - time.monotonic()) * 1000
        return {"s": s.streams, "codec": s.codec, "fps": s.fps or 0, "t0": t0, "a": s.align,
                "b": s.box}

    def _hand_on(self, s, i, key, data):
        """A film's frame to the glass, the stream announced first."""
        if self.above.get(s.z) is not s:
            return
        if not s.announced:
            s.announced = True
            self._lglass("vstart", s.z, self.film_head(s))
        self._keep(s, i, key, data)
        self._lglass("vchunk", s.z, {"s": s.streams, "i": i, "key": key}, data)

    def _data_show(self, s, now, img):
        """A picture from `data`, QOI or JPEG, up now: filling its `box`, or
        the glass where it has none, with its shape kept, centred, or
        against the top or bottom as `align` says. The cutter has checked
        its header."""
        if self.above.get(s.z) is not s:
            return
        codec = DATA_PICTURES[s.codec]
        w, h = protocol.image_size(codec, img)
        bx, by, bw, bh = s.box or (0, 0, *(self.pixels or NOMINAL_BOX))
        k = min(bw / w, bh / h)
        dw, dh = max(1, int(w * k + 0.5)), max(1, int(h * k + 0.5))
        x = bx + (bw - dw) // 2
        y = by + (0 if s.align < 0 else bh - dh if s.align > 0 else (bh - dh) // 2)
        s.frame_count += 1
        self._redraws.append(now)
        size = (dw, dh) if (dw, dh) != (w, h) else None
        self.pics[s.z] = {"w": w, "h": h, "x": x, "y": y, "codec": codec, "size": size,
                          "until": float("inf")}
        s.vlast = (w, h, x, y, None, img, codec, size)
        self.glass.frame(s.z, {**placed(w, h, x, y, size), "codec": codec}, img)

    def _data_end(self, s):
        """The stream is over: let go, the layer cleared -- by the glass,
        once it has played out, for a film -- and the session kept for
        the next."""
        was = s.streaming
        s.streaming = s.ending = False
        s.cut = None
        s.dqueue.clear()
        s.gop, s.gop_bytes = [], 0
        if not was or self.above.get(s.z) is not s:
            return
        if s.film:
            if s.announced:
                self._lglass("vend", s.z, s.streams)
        else:
            self._clear(s.z)

    def film_error(self, z, stream):
        """The glass could not decode a film: said once, the stream let go
        until its writer leaves."""
        with self._lock:
            s = self.above.get(z)
            if s is None or not s.open or not s.film or s.streams != stream or not s.streaming:
                return
            self._say(s, f"error data {s.codec}: does not decode")
            self._data_end(s)
            s.discard = True

    def films_now(self):
        """Each film under way as it stands, for a glass arriving: (z, its
        head, its frames since the last key, the last already handed on)."""
        with self._lock:
            out = []
            for z, s in sorted(self.above.items()):
                if s.open and s.film and s.streaming:
                    out.append((z, self.film_head(s), list(s.gop)))
            return out

    def _clear(self, z):
        """Layer z taken down, on the glass, and every mirror of it told."""
        if self.pics.pop(z, None) is not None:
            self.glass.clear(z)
            if self.above.get(z) is not None:
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
        elif first in STATE and " id=" not in line:
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
        wait in the queue rather than being dropped, so long as the queue
        has room for them. Whether it had."""
        data = (header + "\n").encode() + data
        if s.queued + len(data) > QUEUE_LIMIT:
            return False
        self._put(s, _Item("blob", header.split(" ", 1)[0], data))
        return True

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
        s.queued += len(item.data or b"")
        self._flush(s)

    def _write(self, s, data):
        """Bytes into `out` without blocking; how many went."""
        sent = 0
        while sent < len(data):
            try:
                n = local.write(s.out_fd, data[sent:sent + 65536])
            except (BlockingIOError, BrokenPipeError):
                break
            if n <= 0:
                break
            sent += n
        s.out_bytes += sent
        self._out_total += sent
        return sent

    def _flush(self, s):
        if s.out_fd is None:
            return
        while s.queue:
            item = s.queue[0]
            if item.data is None:
                return                  # a picture the glass is still taking
            if s.dropped and item.at == 0:
                note = f"dropped {s.dropped}\n".encode()
                if self._write(s, note) < len(note):
                    return
                s.dropped = 0
            n = self._write(s, item.data[item.at:])
            item.at += n
            s.queued -= n
            if item.at < len(item.data):
                return
            s.queue.popleft()

    def _tell_controllers(self, line, but=None, cause=None, tag=""):
        for c in list(self.sessions.values()):
            if c.open and c.role == "controller" and c is not but:
                self._say(c, line + (tag if c is cause else ""))

    def _tell_layers(self, line, cause=None, tag=""):
        for s in list(self.sessions.values()):
            if s.open and s.role == "layer":
                self._say(s, line + (tag if s is cause else ""))

    def _tell_cells(self, line, cause=None, tag=""):
        """To the base and every cells layer that follows the base's grid; a
        layer with a grid of its own hears its own (_layer_said)."""
        for s in list(self.sessions.values()):
            if s.open and s.type != "video" and s.own is None:
                self._say(s, line + (tag if s is cause else ""))

    def _lglass(self, kind, *args):
        """A cells layer's news for the glass, where the glass draws them."""
        fn = getattr(self.glass, kind, None)
        if fn is not None:
            fn(*args)

    def _layer_fit(self, s):
        """(cols, rows, font) for a cells layer: the base's where it follows
        the base; at its own font, times the tab's zoom as the base's is,
        where it asked one; exactly its grid, at the largest font that holds
        it, where it asked a grid."""
        if s.own is None:
            return self.cols, self.rows, self.font
        box = self.box
        w, h = (box["w"], box["h"]) if box else NOMINAL_BOX
        if s.own[0] == "grid":
            _, want_c, want_r = s.own
            for font in range(FONT_HIGH, FONT_LOW - 1, -1):
                cols, rows = self._fit(w, h, self._cell_at(font))
                if cols >= want_c and rows >= want_r:
                    break
            return want_c, want_r, font
        zoom = box.get("zoom", 1) if box else 1
        font = min(FONT_HIGH, max(FONT_LOW, round(s.own[1] * zoom)))
        cols, rows = self._fit(w, h, self._cell_at(font))
        return cols, rows, font

    def _layer_screen(self, s):
        """The screen line as a cells layer has it: its own cell."""
        cell = self._cell_at(s.larea[2])
        line = f"screen colour={self.colour} rate={self.rate}"
        if self.pixels:
            line += f" pixels={self.pixels[0]}x{self.pixels[1]} cell={cell[0]}x{cell[1]}"
        if self.glyphs:
            line += f" glyphs={self.glyphs}"
        return line

    def _layer_refit(self, s, cause=None, tag="", always=False):
        """A cells layer's area worked out again: its grid made the size,
        the glass told, and the layer told its area, its screen and
        `repaint` where the size changed -- where it has a grid of its own,
        or `always`; one that follows the base hears the base's."""
        was = s.larea
        s.larea = self._layer_fit(s)
        if s.larea[:2] != (s.lgrid.cols, s.lgrid.rows):
            s.lgrid.resize(*s.larea[:2])
        if s.larea != was:
            self._lglass("lgrid", s.z, *s.larea)
        if (s.own is not None and s.larea != was) or always:
            self._say(s, f"area {s.larea[0]}x{s.larea[1]} {s.larea[2]}"
                         + (tag if s is cause else ""))
            self._say(s, self._layer_screen(s))
            self._say(s, "repaint")

    # ---------------------------------------------------------------- state

    def display(self):
        if self.blank:
            return "blank"
        if not self.present:
            return "away"
        if self.dozing:
            return "doze"
        return "lit"

    @property
    def dark(self):
        return self.blank or not self.present

    def watching(self):
        """Tabs in view, none while the wall is dark."""
        return 0 if self.dark else self.visible

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
        line = f"screen colour={self.colour} rate={self.rate}"
        if self.pixels:
            line += f" pixels={self.pixels[0]}x{self.pixels[1]} cell={self.cell[0]}x{self.cell[1]}"
        if self.glyphs:
            line += f" glyphs={self.glyphs}"
        return line

    def accepts_line(self):
        return " ".join(("accepts",) + tuple(self.accepts))

    def caps_line(self):
        took = ("qoi",) + tuple(c for c in self.data_codecs if c != "qoi")
        hw = [c for c in took if c in self.data_hw]
        codecs = (f" codecs={','.join(took)}{' hw=' + ','.join(hw) if hw else ''}"
                  f" images={','.join(IMAGES)}" if "video" in self.types else "")
        words = CAPS if self.kitty_on else CAPS.replace(" kitty", "")
        if self.data_codecs:
            words += " data align box"
        extra = "".join(" " + c for c in self.commands)
        above = f" above={','.join(self.above_types)}" if self.layers > 1 else ""
        return (f"caps {words}{extra} layers={self.layers} "
                f"types={','.join(self.types)}{above}{codecs} controllers={CONTROLLERS}")

    def _dark_changed(self, was_display, was_watching, cause=None, tag=""):
        if self.display() != was_display:
            self._tell_controllers(f"display {self.display()}", cause=cause, tag=tag)
        if self.watching() != was_watching:
            if self.watching() and not was_watching and self.base is not None:
                # Back in view: a base told nobody was watching stopped
                # drawing, and its silence is not held against it.
                self.base.last_in = time.monotonic()
                self.base.awaiting = False
            self._tell_layers(f"watching {self.watching()}", cause=cause, tag=tag)
        self.glass.changed()

    # ----------------------------------------------------------- the area

    def _cell_at(self, font):
        box = self.box
        if box is None:
            return nominal_cell(font)
        return tuple(box["cells"][font - FONT_LOW])

    @staticmethod
    def _fit(w, h, cell):
        return max(1, w // cell[0]), max(1, h // cell[1])

    def _layout(self):
        """(cols, rows, font) for the request on the glass's box: a
        font times the tab's zoom gives the cell and the area is what fits;
        a grid is the largest font whose cells cover it; a page that fixes
        its grid gets that grid at the largest font that fits."""
        box = self.box
        w, h = (box["w"], box["h"]) if box else NOMINAL_BOX
        if box and box.get("fixed"):
            fc, fr = box["fixed"]
            for font in range(FONT_HIGH, FONT_LOW - 1, -1):
                cw, ch = self._cell_at(font)
                if fc * cw <= w and fr * ch <= h:
                    break
            return fc, fr, font
        if self.request[0] == "grid":
            _, want_c, want_r = self.request
            for font in range(FONT_HIGH, FONT_LOW - 1, -1):
                cols, rows = self._fit(w, h, self._cell_at(font))
                if cols >= want_c and rows >= want_r:
                    break
            return cols, rows, font
        zoom = box.get("zoom", 1) if box else 1
        font = min(FONT_HIGH, max(FONT_LOW, round(self.request[1] * zoom)))
        cols, rows = self._fit(w, h, self._cell_at(font))
        return cols, rows, font

    def _relayout(self, cause=None, tag="", always=False):
        """The area worked out again and said, to every session but a video
        layer, when it changed or somebody asked; `repaint` when the size
        changed, and the screen line after it, since the cell moves with
        the font."""
        cols, rows, font = self._layout()
        changed = (cols, rows) != (self.cols, self.rows)
        moved = changed or font != self.font
        self.cols, self.rows, self.font = cols, rows, font
        self.cell = self._cell_at(font)
        if changed:
            self.grid.resize(cols, rows)
            for s in self.sessions.values():
                for mirror in s.mirrors.values():
                    mirror["was"] = None
        if moved:
            self.glass.changed()
        if moved or always:
            self._tell_cells(self.area_line(), cause=cause, tag=tag)
            self._tell_cells(self.screen_line())
        if changed:
            self._tell_cells("repaint")
        for x in list(self.above.values()):
            if x.open and x.lgrid is not None:
                self._layer_refit(x)

    def set_box(self, box):
        """The glass's box, as it said it: `w` and `h` in device
        pixels, `cells` a cell's size at every font from FONT_LOW, `zoom`,
        and `fixed` where the page fixes its grid. A change of pixels is a
        new glass: video layers are cleared and told the screen and
        `repaint`, as a new mode is on a Pi."""
        with self._lock:
            cells = box.get("cells")
            if not (isinstance(cells, list) and len(cells) == FONT_HIGH - FONT_LOW + 1):
                return
            if self.box is None and isinstance(box.get("font"), int) \
                    and FONT_LOW <= box["font"] <= FONT_HIGH:
                self.default_font = box["font"]
                if not self.asked:
                    self.request = ("font", box["font"])
            self.box = box
            pixels = (box["w"], box["h"])
            new_glass = pixels != self.pixels
            self.pixels = pixels
            if new_glass:
                for z in list(self.pics):
                    self.pics.pop(z)
                    self.glass.clear(z)
            self._relayout(always=new_glass)
            if new_glass:
                for x in list(self.above.values()):
                    if x.open:
                        self._say(x, self.screen_line())
                        self._say(x, "repaint")
                        self._mirror_clear(x)
            self.glass.changed()

    def set_visible(self, n):
        """How many tabs are in view."""
        with self._lock:
            was = self.display(), self.watching()
            self.visible = n
            self._dark_changed(*was)

    def set_accepts(self, kinds):
        """What the glass will pass on when asked, said to every layer."""
        with self._lock:
            kinds = tuple(k for k in ("touch", "keyboard", "buttons") if k in kinds)
            if kinds != self.accepts:
                self.accepts = kinds
                self._tell_layers(self.accepts_line())

    def press(self, kind, opts):
        """Something a viewer did, said to the base if it asked for it."""
        with self._lock:
            want = {"tap": "touch", "key": "keyboard", "button": "buttons"}.get(kind)
            b = self.base
            if b is None or not b.open or not b.wants.get(want):
                return
            self._say(b, " ".join(["input", kind] + [f"{k}={v}" for k, v in opts]))

    def take_glass(self, numbers):
        """What the glass measured of its own painting, and of each film
        it plays, the frames it showed and those it let go late since it
        last said."""
        with self._lock:
            for key, attr in (("shown", "frame_count"), ("late", "dropped_all")):
                for z, n in (numbers.pop(key, None) or {}).items():
                    s = self.above.get(int(z))
                    if s is not None and s.open and s.film:
                        setattr(s, attr, getattr(s, attr) + n)
                        if key == "shown":
                            self._redraws.extend([time.monotonic()] * n)
            numbers["t"] = time.monotonic()
            self.glass_numbers = numbers

    def set_codecs(self, extra, hw=()):
        """The films the glass says it can decode, beyond those the wall
        takes whatever glass it has, and those of them it decodes in
        hardware: said in `caps` from now on."""
        with self._lock:
            fixed = self._data_fixed
            self.data_codecs = fixed + tuple(c for c in extra if c not in fixed)
            self.data_hw = tuple(c for c in hw if c in self.data_codecs)

    def toggle_stats(self, on):
        """A viewer's key, taken as a controller's `stats`, and every
        controller told."""
        with self._lock:
            was = self.stats["on"]
            self.stats["on"] = 1 if on else 0
            self._overlay_due = 0.0
            self.glass.stats()
            if self.stats["on"] != was:
                self._tell_controllers(self.stats_line())

    def want(self):
        """What the base asked to be given, for the glass."""
        b = self.base
        return dict(b.wants) if b is not None and b.open else {}

    def view(self):
        """What the glass shows of the wall: the layout, whether it is dark,
        whether there is a sender, and what the base wants."""
        with self._lock:
            return {"font": self.font, "cols": self.cols, "rows": self.rows,
                    "pixels": list(self.pixels) if self.pixels else None,
                    "dark": self.dark, "display": self.display(),
                    "sender": self.connected, "dozing": self.dozing,
                    "want": self.want(), "name": self.wall}

    def says(self):
        """What a page arranging walls wants to know of this one: the name
        its base gives itself, and the grid it asked for, if it did."""
        with self._lock:
            b = self.base
            name = b.meta.get("name", ("",))[0] if b is not None and b.open else ""
            nominal = self.request[1:] if self.request[0] == "grid" else None
            return {"name": name, "nominal": nominal}

    def catch_up(self):
        """The wall as it stands, for a glass arriving: the base's cells as a
        full repaint, and each video layer's frame."""
        with self._lock:
            cells = b""
            if self.connected:
                cells, _ = self.grid.picture(None)
            frames = []
            for z, pic in sorted(self.pics.items()):
                v = self.above.get(z)
                if v is not None and v.vlast is not None:
                    w, h, x, y, _, data, codec, size = v.vlast
                    frames.append((z, {**placed(w, h, x, y, size), "codec": codec}, data))
            return cells, frames

    def layers_now(self):
        """Each cells layer as it stands, for a glass arriving: (z, cols,
        rows, font, its cells as a full repaint)."""
        with self._lock:
            return [(z, *x.larea, x.lgrid.picture(None)[0])
                    for z, x in sorted(self.above.items())
                    if x.open and x.lgrid is not None]

    def kitty_now(self):
        """The base's kitty graphics as they stand, for a glass arriving: each
        picture as (head, bytes), and the placements' message."""
        with self._lock:
            k = self.base.kitty if self.base is not None else None
            if k is None:
                return [], None
            images = [({"t": "kimg", "key": im.key, "f": im.f, "w": im.w, "h": im.h},
                       im.data) for im in k.images.values()]
            return images, {"t": "kplaces", "list": k.listing()}

    # ------------------------------------------------------------- commands

    def _obey(self, s, m, now):
        name = m.name
        id_ = m.opts.get("id")
        if id_ is not None and not NAME.match(id_):
            self._say(s, f"error {name} id: not a token")
            return
        tag = f" id={id_}" if id_ else ""
        if name in self.commands:
            if s.role != "controller":
                self._say(s, f"error {name} not-for-role{tag}")
                return
            try:
                lines = self.commands[name](m)
            except ValueError as e:
                self._say(s, f"error {name} {e}{tag}")
                return
            for line in lines:
                self._say(s, line + tag)
            return
        if name not in MAY:
            if id_:
                self._say(s, f"error {name} unknown command{tag}")
            return
        who = "controller" if s.role == "controller" else (
            "video" if s.type == "video" else "base" if s is self.base else "layer")
        if who not in MAY[name]:
            self._say(s, f"error {name} not-for-role{tag}")
            return
        getattr(self, "_cmd_" + name)(s, m, tag, now)

    def _cmd_report(self, s, m, tag, now):
        def say(line):
            self._say(s, line + tag)
        say(protocol.hello())
        if self.receiver:
            say(self.receiver)
        say(self.caps_line())
        say("measures " + " ".join(MEASURES))
        say("pictures " + " ".join(self.pictures()) + " grid")
        say(self._layer_screen(s) if s.lgrid is not None else self.screen_line())
        for line in [self.reach] if isinstance(self.reach, str) else self.reach or ():
            say(line)
        say(self.accepts_line())
        if self.base is not None and self.base.open:
            for line in self._meta_lines(self.base, now):
                say(line)
        # Each layer above the base says its own, tagged with its session.
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
        s.vwant, s.skip = 0, True

        def no(why):
            self._say(s, f"error frame {why}{tag}")
        if len(words) < 2 or not (words[-1].isdigit() and len(words[-1]) <= 18):
            return no("no count")
        s.vwant = int(words[-1])
        if s.vwant > FRAME_MAX:
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
            self._lglass("lgrid", s.z, *s.larea, True)
        elif self.above.get(s.z) is s:
            self._clear(s.z)

    def _cmd_align(self, s, m, tag, now):
        """`align top|centre|bottom`: where the stream's frames sit on a
        glass of another shape, from the next frame."""
        word = m.args[0] if m.args else ""
        if word not in ALIGN:
            self._say(s, f"error align {protocol.word(word)}: not top, centre or bottom{tag}")
            return
        s.align = ALIGN[word]
        if self.above.get(s.z) is s:
            self._lglass("valign", s.z, s.align)

    def _cmd_box(self, s, m, tag, now):
        """`box [at=<x>,<y>] size=<w>x<h>`: the box in the glass's pixels
        the stream's frames are fitted to, from the next frame; `box`
        alone, the whole glass again. Kept through a change of glass,
        what falls off it not drawn."""
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
                self._say(s, f"error box {plain(word)}: unknown option{tag}")
                return
            if not good:
                self._say(s, f"error box {plain(word)}: bad value{tag}")
                return
        if words and size is None:
            self._say(s, f"error box no size{tag}")
            return
        s.box = (x, y, *size) if size else None
        if self.above.get(s.z) is s:
            self._lglass("vbox", s.z, s.box)

    def _cmd_close(self, s, m, tag, now):
        self._end(s, "asked")

    _cmd_detach = _cmd_close

    def _cmd_font(self, s, m, tag, now):
        """`font <size>`: the first word that is a whole number of 1 to
        400, held to the fonts there are, as the painter takes it."""
        size = None
        for word in m.args:
            n = protocol.number(word[2:] if word.startswith("n=") else word)
            if n is not None and 1 <= n <= 400:
                size = n
                break
        if s.lgrid is not None:
            return self._layer_ask(s, None if tuple(m.args[:1]) == ("base",) else
                                   ("font", min(FONT_HIGH, max(FONT_LOW, size)))
                                   if size is not None else "none", tag, "font")
        if size is None:
            if tag:
                self._say(s, f"error font no size{tag}")
            return
        self.request = ("font", min(FONT_HIGH, max(FONT_LOW, size)))
        self.asked = True
        self._relayout(cause=s, tag=tag, always=True)

    def _cmd_grid(self, s, m, tag, now):
        """`grid <cols>x<rows>`: the largest font whose cells cover it on
        this glass."""
        if s.lgrid is not None and tuple(m.args[:1]) == ("base",):
            return self._layer_ask(s, None, tag, "grid")
        found = re.match(r"^(\d{1,4})x(\d{1,4})$", m.args[0]) if m.args else None
        if not found or int(found.group(1)) < 1 or int(found.group(2)) < 1:
            self._say(s, f"error grid {protocol.word(m.args[0]) if m.args else ''}: not <cols>x<rows>{tag}")
            return
        want_c, want_r = int(found.group(1)), int(found.group(2))
        w, h = (self.box["w"], self.box["h"]) if self.box else NOMINAL_BOX
        cols, rows = self._fit(w, h, self._cell_at(FONT_LOW))
        if cols < want_c or rows < want_r:
            self._say(s, f"error grid {want_c}x{want_r}: too many cells for this screen{tag}")
            return
        if s.lgrid is not None:
            return self._layer_ask(s, ("grid", want_c, want_r), tag, "grid")
        self.request = ("grid", want_c, want_r)
        self.asked = True
        self._relayout(cause=s, tag=tag, always=True)

    def _layer_ask(self, s, own, tag, cmd):
        """A cells layer's `font` or `grid`: a grid of its own, or None,
        `base`, to follow the base's again; answered with its area, the
        request's id, its screen and `repaint`."""
        if own == "none":
            self._say(s, f"error {cmd} no size{tag}")
            return
        s.own = own
        self._layer_refit(s, cause=s, tag=tag, always=True)

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
                s.wants[key] = value == "1"
        if s is self.base:
            self.glass.changed()

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
        self._overlay_due = 0.0
        self.glass.stats()

    def pictures(self):
        """What a picture of the glass may be: QOI, and JPEG where the
        glass makes one (its `pictures`)."""
        return tuple(getattr(self.glass, "pictures", ("qoi",)))

    def _capture(self, s, m, tag, cmd, mirror=False):
        """A picture's or a mirror's target and options, each checked:
        (target session or None for the glass, (w, h), every, off,
        quality), quality 0 for QOI and 1 to 100 for JPEG, or None with
        the error said."""
        if not m.args:
            self._say(s, f"error {cmd} no target{tag}")
            return None
        target = m.args[0]
        if mirror and tuple(m.args[1:]) == ("off",) and set(m.opts) <= {"id"}:
            return None, None, None, True, 0
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
                if value not in self.pictures():
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
        if quality and fmt != "jpeg":
            self._say(s, f"error {cmd} quality={quality}: only for jpeg{tag}")
            return None
        return who, size, every, False, (quality or JPEG_QUALITY) if fmt == "jpeg" else 0

    def fit(self, size):
        """The glass's size fitted inside `size` with its shape kept, and
        never larger than the glass; the glass's own without one."""
        gw, gh = self.pixels
        if size is None or (size[0] >= gw and size[1] >= gh):
            return gw, gh
        w, h = size
        if gw * h <= gh * w:
            w = (gw * h + gh // 2) // gh
        else:
            h = (gh * w + gw // 2) // gw
        return max(1, w), max(1, h)

    def _locked(self, fn, *args):
        with self._lock:
            fn(*args)

    def _look(self, w, h, done, quality=0):
        """Ask the glass for a look at w by h; `done(data)` is
        called under the lock with its QOI bytes, or its JPEG's at
        `quality` where that is above 0, or None if it gave none. False
        when there is no glass to ask."""
        def back(data):
            with self._lock:
                done(data)
        if quality:
            return self.glass.look(w, h, back, quality=quality)
        return self.glass.look(w, h, back)

    def _cmd_picture(self, s, m, tag, now):
        got = self._capture(s, m, tag, "picture")
        if got is None:
            return
        who, size, _, _, quality = got
        target = who.name if who else "glass"
        if s.queued >= QUEUE_LIMIT:
            self._say(s, f"error picture {target}: too far behind{tag}")
            return
        if who is None:
            if self.pixels is None:
                self._say(s, f"error picture glass: not offered{tag}")
                return
            w, h = self.fit(size)
            item = _Item("blob", "picture", None)

            def done(data):
                if data is None:
                    item.data = f"error picture glass: not offered{tag}\n".encode()
                    item.kind = "event"
                elif s.queued + len(data) > QUEUE_LIMIT:
                    item.data = f"error picture glass: too far behind{tag}\n".encode()
                    item.kind = "event"
                else:
                    fmt = "jpeg" if quality else "qoi"
                    item.data = (f"picture glass {fmt} {w}x{h} {len(data)}{tag}\n".encode()
                                 + data)
                s.queued += len(item.data)
                self._flush(s)
            if not self._look(w, h, done, quality):
                self._say(s, f"error picture glass: not offered{tag}")
                return
            if item.data is None:
                s.queue.append(item)
        elif who.film:
            item = _Item("blob", "picture", None)

            def done(data, x=0, y=0):
                if data is None:
                    item.data = f"error picture {target}: nothing shown{tag}\n".encode()
                    item.kind = "event"
                else:
                    fw, fh = int.from_bytes(data[4:8], "big"), int.from_bytes(data[8:12], "big")
                    item.data = (f"picture {target} {frame_words('qoi', fw, fh, x, y, None)} "
                                 f"{len(data)}{tag}\n".encode() + data)
                s.queued += len(item.data)
                self._flush(s)
            fn = getattr(self.glass, "look_layer", None)
            if fn is None or not fn(who.z, lambda data, x=0, y=0: self._locked(done, data, x, y)):
                self._say(s, f"error picture {target}: nothing shown{tag}")
                return
            if item.data is None:
                s.queue.append(item)
        elif who.type == "video":
            if who.vlast is None or who.z not in self.pics:
                self._say(s, f"error picture {target}: nothing shown{tag}")
                return
            fw, fh, fx, fy, _, data, codec, size = who.vlast
            if not self._blob(s, f"picture {target} {frame_words(codec, fw, fh, fx, fy, size)} "
                                 f"{len(data)}{tag}", data):
                self._say(s, f"error picture {target}: too far behind{tag}")
        elif who.lgrid is not None:
            data, _ = who.lgrid.picture(None)
            if not self._blob(s, f"picture {target} grid {who.larea[0]}x{who.larea[1]} "
                                 f"{len(data)}{tag}", data):
                self._say(s, f"error picture {target}: too far behind{tag}")
        else:
            data, _ = self.grid.picture(None) if who is self.base else (b"", None)
            data = data or RESET.encode()
            if not self._blob(s, f"picture {target} grid {self.cols}x{self.rows} "
                                 f"{len(data)}{tag}", data):
                self._say(s, f"error picture {target}: too far behind{tag}")

    def _cmd_mirror(self, s, m, tag, now):
        got = self._capture(s, m, tag, "mirror", mirror=True)
        if got is None:
            return
        who, size, every, off, quality = got
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
                             "size": size, "quality": quality, "sent": None,
                             "looking": False}

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
        s.measure_mark = (now, self._in_total, self._out_total, self._cpu())

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
        while self._resets and self._resets[0] < now - 60.0:
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
                self._rates(s, now)
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
                drained = not s.queue and local.drained(s.out_fd)
                if drained or now - s.ended_at >= t.keep:
                    self._remove(s)
        if not self.connected and not self.dozing and now - self.last_data >= DOZE:
            was = self.display(), self.watching()
            self.dozing = True
            self._dark_changed(*was)
        if self.stats["on"] and now >= self._overlay_due:
            self._overlay_due = now + STATS_PERIOD
            self.glass.stats()

    @staticmethod
    def _rates(x, now):
        """A session's rates, worked out once a second from its counts."""
        dt = now - x.rates_at
        if dt < 1.0:
            return
        i, o, f = x.rates_mark
        x.r_in = (x.in_bytes - i) / dt
        x.r_out = (x.out_bytes - o) / dt
        x.r_fps = (x.frame_count - f) / dt
        x.rates_mark = (x.in_bytes, x.out_bytes, x.frame_count)
        x.rates_at = now

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

    @staticmethod
    def _cpu():
        if resource is None:
            t = os.times()
            return t.user + t.system
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    def _glass(self, key):
        """A number the glass sent within the last few seconds."""
        g = self.glass_numbers
        if key in g and time.monotonic() - g["t"] < 3:
            return g[key]
        return None

    def _fps_since(self, then, now):
        """The times the glass was put up since `then`, by any layer: the
        glass's count where it has one, else the frames the layers
        sent."""
        fps = self._glass("fps")
        if fps is not None:
            return fps
        frames = sum(1 for r in self._resets if r >= then) \
            + sum(1 for r in self._redraws if r >= then)
        return frames / max(now - then, 1e-6)

    def _measure_line(self, s, now):
        """The wall's own line: the glass and what it costs."""
        then, in_then, out_then, cpu_then = s.measure_mark
        elapsed = max(now - then, 1e-6)
        cpu = self._cpu()
        s.measure_mark = (now, self._in_total, self._out_total, cpu)
        line = (f"measure wall={self.wall} grid={self.cols}x{self.rows} font={self.font} "
                f"fps={self._fps_since(then, now):.1f} "
                f"in={(self._in_total - in_then) / elapsed:.0f} "
                f"out={(self._out_total - out_then) / elapsed:.0f}")
        for key in ("frame", "draw"):
            v = self._glass(key)
            line += f" {key}=" + (f"{v:.2f}" if v is not None else "-")
        return (line + f" display={self.display()} cpu={100 * (cpu - cpu_then) / elapsed:.0f}"
                f" render=canvas")

    def _session_measure(self, s, x, now):
        """A line for session `x`: its place, and its rates over the last
        whole second, `-` until there is one."""
        place = f" z={x.z} type={x.type}" if x.role == "layer" else ""
        fps = f"{x.r_fps:.1f}" if x.role == "layer" and x.r_fps >= 0 else "-"
        rin = f"{x.r_in:.0f}" if x.r_in >= 0 else "-"
        rout = f"{x.r_out:.0f}" if x.r_in >= 0 else "-"
        pong = f"{x.pong_ms:.0f}" if x.pong_ms is not None else "-"
        decode = ""
        if x.type == "video":
            got = (self._glass("decode") or {}).get(str(x.z))
            decode = f" decode={got:.2f}" if isinstance(got, (int, float)) else " decode=-"
        return (f"measure wall={self.wall} session={x.name} role={x.role}{place} fps={fps} "
                f"in={rin} out={rout} dropped={x.dropped_all} "
                f"queue={x.queued} quiet={now - x.last_in:.1f} pong={pong}{decode}")

    def overlay(self):
        """The overlay as the painter draws it: a line a measure chosen by
        `show`, name and value, and under them a row a session in the
        order they were opened -- name, place and type, frames a second,
        bytes in a second, lines dropped. Nothing while the wall is blank
        or has no sender: numbers about a picture are nothing to read when
        there is no picture."""
        with self._lock:
            out = {"on": bool(self.stats["on"]), "at": self.stats["at"],
                   "size": self.stats["size"], "lines": []}
            if not self.stats["on"] or self.blank or not self.connected:
                return out
            now = time.monotonic()
            cpu = self._cpu()
            mark, self._overlay_mark = self._overlay_mark, {
                "t": now, "in": self._in_total, "out": self._out_total, "cpu": cpu}
            dt = now - mark["t"] if mark else 0
            val = {k: "-" for k in MEASURES}
            val["wall"] = self.wall
            val["grid"] = f"{self.cols}x{self.rows} @{self.font}"
            if dt > 0:
                val["in"] = human((self._in_total - mark["in"]) / dt) + "/s"
                val["out"] = human((self._out_total - mark["out"]) / dt) + "/s"
                val["cpu"] = f"{100 * (cpu - mark['cpu']) / dt:.0f}%"
            fps = self._glass("fps")
            if fps is not None:
                val["fps"] = f"{fps:.1f}"
            for key in ("frame", "draw"):
                v = self._glass(key)
                if v is not None:
                    val[key] = f"{v:.2f}ms"
            val["sender"] = f"{'yes' if self.connected else 'no'} {now - self.last_data:.0f}s"
            b = self.base
            if b is not None and b.open and b.pong_ms is not None:
                val["pong"] = f"{b.pong_ms:.0f}ms"
            val["display"] = self.display()
            val["render"] = "canvas"
            lines = [f"{k:<6} {val[k]}" for k in self.stats["show"].split(",")]
            rows = []
            # As they stack: the top layer first and down to the base, as
            # the glass shows them, then the controllers.
            order = sorted((x for x in self.sessions.values() if x.open),
                           key=lambda x: (x.role != "layer", -(x.z or 0)))
            for x in order:
                if not x.open:
                    continue
                place = f"{x.z} {x.type}" if x.role == "layer" else "controller"
                fps_ = f"{x.r_fps:.0f}fps" if x.role == "layer" and x.r_fps >= 0 else "-"
                rate = human(x.r_in) + "/s" if x.r_in >= 0 else "-"
                rows.append(f"{x.name[:18]:<18} {place:<10} {fps_:>6} {rate:>9} "
                            f"{x.dropped_all} dropped")
            out["lines"] = lines + ([""] + rows if rows else [])
            return out

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
            if now < mirror["due"] or s.queue:
                continue
            due = mirror["due"]
            mirror["due"] = due + mirror["every"] if now - due < mirror["every"] \
                else now + mirror["every"]
            if who is None:
                if mirror["looking"] or self.pixels is None:
                    continue
                w, h = self.fit(mirror["size"])
                mirror["looking"] = True

                def done(data, s=s, mirror=mirror, w=w, h=h):
                    mirror["looking"] = False
                    if (data is None or not s.open or s.mirrors.get("glass") is not mirror
                            or data == mirror["sent"] or s.queue):
                        return
                    fmt = "jpeg" if mirror["quality"] else "qoi"
                    if self._blob(s, f"mirror glass {fmt} {w}x{h} {len(data)}", data):
                        mirror["sent"] = data
                if not self._look(w, h, done, mirror["quality"]):
                    mirror["looking"] = False
                continue
            if who is not self.base:
                continue
            data, rows = self.grid.picture(mirror["was"])
            if mirror["was"] is None:
                data = data or RESET.encode()
                header = f"mirror {target} grid {self.cols}x{self.rows} {len(data)}"
            elif data:
                header = f"mirror {target} diff {len(data)}"
            else:
                continue
            if self._blob(s, header, data):
                mirror["was"] = rows

    # --------------------------------------------------------------- ending

    def _end(self, s, reason):
        if not s.open:
            return
        now = time.monotonic()
        s.state, s.reason, s.ended_at = "closed", reason, now
        self._say(s, f"closed {s.name} {reason}")
        if s.in_fd is not None:
            local.close(s.in_fd)
            s.in_fd = None
        if s.data_fd is not None:
            local.close(s.data_fd)
            s.data_fd = None
        if s.film and s.streaming and self.above.get(s.z) is s:
            self._lglass("vgone", s.z)
        s.streaming = s.ending = False
        s.dqueue.clear()
        s.gop = []
        s.mirrors = {}
        s.measure_due = None
        if s.kitty is not None:
            s.kitty.clear()
            s.kitty = None
        if s is self.base:
            if self.above.get(0) is s:
                self._clear(0)
                del self.above[0]
            self.base = None
            self.connected = False
            self.asked = False
            self.last_data = now
            self.grid.resize(self.cols, self.rows)
            self.glass.changed()
        if s.z and self.above.get(s.z) is s:
            self._clear(s.z)
            if s.lgrid is not None:
                self._lglass("lgone", s.z)
            del self.above[s.z]
            # No gap is left: the rest of its stack closes up behind it.
            step = 1 if s.from_top else -1
            k = s.z - step
            while 1 <= k < self.layers and self.above.get(k) is not None \
                    and self.above[k].open and self.above[k].from_top == s.from_top:
                self._move(k, k + step)
                k -= step
        self._tell_controllers(f"session closed {s.name} {reason}", but=s)

    def _move(self, frm, to):
        """The layer at slot `frm` moved to the free slot `to`, with the
        picture it shows, and told so, as every controller is. Its name,
        session and pipes are as they were; only its place in the stack,
        and so the order it is drawn in, has changed."""
        x = self.above.pop(frm, None)
        if x is None:
            return
        self.above[to] = x
        x.z = to
        if x.lgrid is not None:
            self._lglass("lgone", frm)
            self._lglass("lgrid", to, *x.larea)
            self._lglass("lcells", to, x.lgrid.picture(None)[0])
        pic = self.pics.pop(frm, None)
        if pic is not None:
            self.glass.clear(frm)
            self.pics[to] = pic
            if x.vlast is not None:
                w, h, px, py, _hold, data, codec, size = x.vlast
                self.glass.frame(to, {**placed(w, h, px, py, size), "codec": codec}, data)
        if x.film and x.streaming:
            self._lglass("vmove", frm, to)
        self._say(x, f"moved {x.name} z={to}")
        self._tell_controllers(f"session moved {x.name} {to}")

    def _close_fds(self, s):
        for attr in ("in_fd", "out_fd", "data_fd"):
            fd = getattr(s, attr)
            if fd is not None:
                local.close(fd)
                setattr(s, attr, None)

    def _remove(self, s):
        self._close_fds(s)
        if not local.PIPES:
            shutil.rmtree(s.path, ignore_errors=True)
        self.sessions.pop(s.name, None)


def human(v):
    """Bytes as the painter says them: 6.9MB, 2.1KB, 312B."""
    if v >= 1e6:
        return f"{v / 1e6:.1f}MB"
    if v >= 1e3:
        return f"{v / 1e3:.1f}KB"
    return f"{v:.0f}B"
