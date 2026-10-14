"""What a sender can learn of the wall it is on: who is there, and what
they show.

    for s in w.sessions():                     # as `sessions` lists them
        if s.z == 0:
            base = s.name
    p = w.picture(base)                        # a cells layer: its grid
    ch, fg, bg = p.cell(0, 0)
    m = w.mirror(base, every=1000)             # kept up to date from then on
    if m.version != seen:                      # something changed
        seen = m.version
        ch, fg, bg = m.cell(x, y)
    m.off()

`picture` and `mirror` are PROTOCOL.md's, asked on the sender's own
session, so they need no controller. A cells layer comes back as its
grid, read into cells a sender can index; the glass comes back as QOI,
or as JPEG with `format="jpeg"` (and `quality`, 1 to 100) where the
wall's `pictures` says it makes one, and a video layer as the frame it
was sent; each is its bytes as the wall sent them, with its `format`,
its size and, for a video layer, where it is. A mirror keeps one current copy: a grid painted with each
diff as it comes, or the latest QOI frame, with a count and a time for
each change, so a sender can see that something moved without looking
at every cell. It is asked for again whenever the session is opened
again, and starts again from a whole frame.

Colours are 0xRRGGBB, or -1 where a cell has the wall's own.
"""
import collections
import threading
import time

from . import protocol
from .grid import DEFAULT, Grid

#: One session on the wall, as `sessions` says it: its place and type are
#: None for a controller.
SessionInfo = collections.namedtuple("SessionInfo", "name role z type")


class CaptureError(RuntimeError):
    """The wall would not answer, and said why, or did not answer at all."""


def _size(word):
    w, _, h = (word or "").partition("x")
    return (int(w), int(h)) if w.isdigit() and h.isdigit() else (0, 0)


def _options(size, format, quality):
    """A picture's or a mirror's options of the glass, as words."""
    words = ""
    if size is not None:
        words += f" size={size[0]}x{size[1]}"
    if format is not None:
        words += f" format={format}"
    if quality is not None:
        words += f" quality={int(quality)}"
    return words


def _at(said):
    x, _, y = str(said.opts.get("at", "")).partition(",")
    try:
        return int(x), int(y)
    except ValueError:
        return None


class _Cells:
    """Indexing by cell, for a picture or a mirror of a cells layer."""

    grid = None

    def cell(self, x, y):
        """(character, ink, ground) of the cell at column x, row y."""
        g = self.grid
        if g is None or not (0 <= y < g.rows and 0 <= x < g.cols):
            raise IndexError(f"no cell {x},{y}")
        return g.ch[y][x], g.fgs[y][x], g.bgs[y][x]

    def row(self, y):
        """Row y's characters, as a string."""
        return "".join(self.grid.ch[y])


class Picture(_Cells):
    """A picture as it came: `format` is `grid`, or `qoi`, `png` or `jpeg`
    for pixels; `width` and `height` are cells for a grid and the image's
    own pixels otherwise; `at` is where a video frame goes, else None, and
    `size` the box it was drawn to where it was scaled, else None; `data`
    is the bytes."""

    def __init__(self, said, data):
        self.target = said.args[0] if said.args else ""
        self.format = said.args[1] if len(said.args) > 1 else ""
        self.width, self.height = _size(said.args[2] if len(said.args) > 2 else "")
        self.at = _at(said)
        self.size = _size(said.opts.get("size")) if said.opts.get("size") else None
        self.data = data
        if self.format == "grid":
            self.grid = Grid(self.width, self.height)
            self.grid.feed(data.decode("utf-8", "replace"))

    def __repr__(self):
        return f"Picture({self.target!r}, {self.format} {self.width}x{self.height})"


class Mirror(_Cells):
    """A mirror's current copy of its target, and its end.

    `version` counts the changes it has taken and `changed` is when the
    last came, by time.monotonic(); `shown` is False once a video layer
    has been taken down. `error` is the wall's refusal, if it refused."""

    def __init__(self, capture, target, every, size, format=None, quality=None):
        self._capture = capture
        self.target, self.every, self.size = target, every, size
        self.asked = (size, format, quality)
        self.format = None
        self.width = self.height = 0
        self.at = None
        self.size = None
        self.hold = None
        self.data = None
        self.shown = False
        self.version = 0
        self.changed = None
        self.error = None
        self.stopped = False
        self._lock = threading.Lock()

    def command(self):
        words = f"mirror {self.target}"
        if self.every is not None:
            words += f" every={int(self.every)}"
        return words + _options(*self.asked)

    def _take(self, said, data):
        kind = said.args[1] if len(said.args) > 1 else ""
        with self._lock:
            if kind == "grid":
                self.width, self.height = _size(said.args[2] if len(said.args) > 2 else "")
                self.grid = Grid(self.width, self.height)
                self.grid.feed(data.decode("utf-8", "replace"))
            elif kind == "diff":
                if self.grid is None:
                    return          # nothing to paint it onto; a whole one follows
                self.grid.feed(data.decode("utf-8", "replace"))
            elif kind in ("qoi", "png", "jpeg"):
                self.width, self.height = _size(said.args[2] if len(said.args) > 2 else "")
                self.at = _at(said)
                self.size = _size(said.opts.get("size")) if said.opts.get("size") else None
                hold = said.opts.get("hold")
                self.hold = (int(hold) if str(hold).isdigit()
                             else "still" if hold == "still" else None)
            else:
                return
            self.format = "grid" if kind in ("grid", "diff") else kind
            self.data = data
            self.shown = True
            self.version += 1
            self.changed = time.monotonic()

    def _clear(self):
        with self._lock:
            self.shown = False
            self.data = None
            self.version += 1
            self.changed = time.monotonic()

    def cell(self, x, y):
        with self._lock:
            return super().cell(x, y)

    def off(self):
        """Stop the mirror; what it last held stays readable."""
        self._capture.stop(self)


class Capture:
    """The asking, for one session: commands out with ids of their own, the
    answers matched to them, and the mirrors kept up. `send(text)` puts a
    command on the session's `in`; `feed(said)` is given everything its
    `out` says."""

    def __init__(self, send, timeout=5.0):
        self._send = send
        self.timeout = timeout
        self._lock = threading.Lock()
        self._n = 0
        self._waiting = {}          # id -> [items, done event, finished-by]
        self._mirrors = {}          # target -> Mirror

    # ------------------------------------------------------------ asking

    def _ask(self, words, finished, timeout):
        with self._lock:
            self._n += 1
            id_ = f"cap{self._n}"
            entry = [[], threading.Event(), finished]
            self._waiting[id_] = entry
        try:
            self._send(f"{words} id={id_}")
            if not entry[1].wait(self.timeout if timeout is None else timeout):
                raise CaptureError(f"{words}: no answer")
            return entry[0]
        finally:
            with self._lock:
                self._waiting.pop(id_, None)

    def feed(self, said):
        """One thing the session's `out` said."""
        blob = hasattr(said, "data") and hasattr(said, "said")
        line = said.said if blob else said
        if not isinstance(line, protocol.Message):
            return
        if line.name == "mirror" and line.args:
            m = self._mirrors.get(line.args[0])
            if m is not None and not m.stopped:
                if blob:
                    m._take(line, said.data)
                elif line.args[1:2] == ("clear",):
                    m._clear()
        id_ = line.get("id")
        if id_ is None:
            return
        with self._lock:
            entry = self._waiting.get(id_)
        if entry is None:
            if line.name == "error" and line.args[:1] == ("mirror",):
                for m in self._mirrors.values():
                    if line.args[1:2] and line.args[1].rstrip(":") == m.target:
                        m.error = line.raw
            return
        entry[0].append(said)
        if line.name == "error" or entry[2](line):
            entry[1].set()

    def sessions(self, timeout=None):
        """Every session on the wall, in the order they were opened."""
        got = self._ask("sessions", lambda m: m.name == "sessions", timeout)
        out = []
        for m in got:
            if m.name == "session" and len(m.args) >= 4:
                name, role, z, kind = m.args[:4]
                out.append(SessionInfo(name, role, int(z) if z.isdigit() else None,
                                       None if kind == "-" else kind))
            elif m.name == "error":
                raise CaptureError(m.raw)
        return out

    def picture(self, target, size=None, timeout=None, format=None, quality=None):
        """A Picture of `target`: `glass`, or a session's name. `size`,
        `format` (`qoi` or `jpeg`) and `quality` are the glass's."""
        words = f"picture {target}" + _options(size, format, quality)
        got = self._ask(words, lambda m: m.name == "picture", timeout)
        for item in got:
            if hasattr(item, "data") and hasattr(item, "said"):
                return Picture(item.said, item.data)
            if item.name == "error":
                raise CaptureError(item.raw)
        raise CaptureError(f"{words}: no picture")

    def mirror(self, target, every=None, size=None, format=None, quality=None):
        """A Mirror of `target`, kept current from here on."""
        with self._lock:
            m = self._mirrors.get(target)
            if m is None or m.stopped:
                m = self._mirrors[target] = Mirror(self, target, every, size, format, quality)
            else:
                m.every, m.size = every, size
                m.asked = (size, format, quality)
        self._send(m.command())
        return m

    def stop(self, m):
        with self._lock:
            m.stopped = True
            if self._mirrors.get(m.target) is m:
                del self._mirrors[m.target]
        self._send(f"mirror {m.target} off")

    def resay(self):
        """Every mirror asked for again, on a session just opened: the wall
        that opened it holds none of them, and each starts whole."""
        for m in list(self._mirrors.values()):
            with m._lock:
                m.grid = None
            self._send(m.command())

    def fail_waiting(self):
        """Wake whoever waits on an answer from a session that has gone."""
        with self._lock:
            for entry in self._waiting.values():
                entry[1].set()


__all__ = ["Capture", "CaptureError", "Mirror", "Picture", "SessionInfo", "DEFAULT"]
