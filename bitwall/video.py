"""A video layer: pictures in pixels over a wall's cells.

    layer = bitwall.layer("pi.local:1")           # z=above, type=video
    layer.z                                       # the place it was given
    layer.frame(bitwall.video.qoi(rgba), at=(40, 900), hold=1000)
    layer.clear()
    layer.close()

A layer is a session of its own beside the base (PROTOCOL.md, "A video
layer"), so a sender draws over whatever the wall's base shows without
taking the base from it. It asks for a place, not a number: `above`, the
lowest free layer over the base; `top`, the highest free; or a number,
exactly that one. The wall says the number it gave, and `z` holds it.
Left unsaid, the place is `BITWALL_PLACE` from the environment, which a
controller sets from its catalogue, or `above`. Each frame is an image and a line saying what
follows; the wall blends it over the cells by its alpha and shows it until
the next frame or until `hold` runs out, or for good with `hold="still"`.

**A frame is QOI, PNG or JPEG**, read from its first bytes. QOI is what
every wall that takes video draws; `takes("jpeg")` says whether this one
draws the others (its `caps` says them in `images=`), and a sender that
has a photograph passes the JPEG through as it came where it does.
`size=(w, h)` is the box in the glass's pixels the wall scales it to,
its shape the sender's to keep.

**The sender paces, and a full pipe drops a frame rather than queue it.**
`frame()` sends only when the pipe to the wall has room now, and says
whether it did, so a wall that falls behind costs frames rather than a
growing delay. A frame once begun goes whole, since its count is what
tells the wall where the next command starts.

**A stream on `data`** is a film, or a run of QOI images or JPEGs, that
the wall decodes and plays out itself (PROTOCOL.md, "The data pipe"):

    film = bitwall.layer("pi.local", place=0, take=True, data=True,
                         codec="hevc", fps=24, start=int(time.time() * 1000) + 2000)
    film.data_write(chunk)                        # blocks while the wall is full
    film.data_end()                               # the stream is over

The stream goes on a fifo of its own, `data`, through a writer of its own,
so commands on `in` never wait behind it; and a write waits for room
rather than dropping, since a compressed stream cannot lose bytes. Each
writer is one stream: `data_end()` ends it, and the next write starts the
next. `place=0` is the base, a video base, taken from its holder with
`take=True`.

**A layer that loses its wall opens again on its own**, as the base does,
and stays away when the wall displaced or refused it. What the wall says
about its glass (`screen pixels=`), who is watching and whether it wants a
`repaint` is kept, for a sender to read.
"""
import collections
import itertools
import os
import posixpath
import re
import sys
import threading

from . import capture, moving, portable, protocol, session

#: How long a layer waits between asking again for a wall that is not there.
RETRY_MOST = 5.0

#: The places a layer may ask for by name; a number asks for exactly that
#: one, and 0 is the base.
PLACES = ("above", "top")

#: What a stream on `data` may be.
CODECS = ("hevc", "h264", "qoi", "mjpeg")
#: Where a stream's frames may sit on a glass of another shape.
ALIGNS = ("top", "centre", "bottom")

#: A controller's instance id, as `bitwall.INSTANCE` has it.
INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

#: Which layer of this process each is, so two never share a session name.
_count = itertools.count()


def qoi(rgba):
    """A QOI image of `rgba`, a (height, width, 4) uint8 numpy array.

    Every pixel is said against the one before it, as a run, a small
    difference, a luma difference, or whole; QOI's colour index is not
    used, since it is the one op that cannot be worked out a row at a time.
    Any QOI decoder reads it, with alpha exact.

    A layer's frame is often the whole glass, or a wide band of it, with a
    little drawn on it, and every pixel that repeats the one before is part
    of a run, said 62 at a time. So the encoder finds the pixels that
    differ from the one before and works out ops for those alone: the work
    goes with how much is drawn rather than with the size of the frame, a
    couple of milliseconds at 1920x1080 where every pixel is two hundred.
    Where nearly every pixel differs, a photograph or noise, gathering them
    costs more than it saves, and every pixel is worked out in place.
    """
    import numpy as np

    h, w = rgba.shape[:2]
    head = b"qoif" + int(w).to_bytes(4, "big") + int(h).to_bytes(4, "big") + b"\x04\x00"
    tail = b"\x00" * 7 + b"\x01"
    px = np.ascontiguousarray(rgba, dtype=np.uint8).reshape(-1, 4)
    n = len(px)
    if not n:
        return head + tail
    u = px.view(np.uint32).ravel()
    first = np.array([0, 0, 0, 255], np.uint8)
    change = np.empty(n, bool)
    change[0] = u[0] != first.view(np.uint32)[0]
    np.not_equal(u[1:], u[:-1], out=change[1:])
    c = np.flatnonzero(change)
    if len(c) > n * _QOI_DENSE:
        body = _qoi_dense(px, first, ~change)
    else:
        body = _qoi_sparse(px, first, c)
    return head + body.tobytes() + tail


# Above this share of pixels differing from the one before, `qoi` works
# every pixel out in place rather than gathering the ones that differ:
# measured at 1920x1080, the two cost the same at about 70 per cent.
_QOI_DENSE = 0.7


def _qoi_ops(cur, prv):
    """Each pixel's op against the one before it, both (k, 4) int16: the
    bytes, (k, 5) uint8, and how many of them it takes."""
    import numpy as np

    d = (cur - prv + 128) % 256 - 128
    dr, dg, db = d[:, 0], d[:, 1], d[:, 2]
    keep_a = cur[:, 3] == prv[:, 3]
    small = keep_a & (np.abs(d[:, :3] + 0.5) <= 2).all(axis=1)
    rg, bg = dr - dg, db - dg
    luma = keep_a & ~small & (dg >= -32) & (dg <= 31) & (rg >= -8) & (rg <= 7) \
        & (bg >= -8) & (bg <= 7)
    whole = keep_a & ~small & ~luma

    op = np.empty((len(cur), 5), np.uint8)
    count = np.full(len(cur), 5, np.int64)
    op[:, 0] = 0xFF
    op[:, 1:5] = cur
    op[small, 0] = 0x40 | (dr[small] + 2) << 4 | (dg[small] + 2) << 2 | (db[small] + 2)
    count[small] = 1
    op[luma, 0] = 0x80 | (dg[luma] + 32)
    op[luma, 1] = (rg[luma] + 8) << 4 | (bg[luma] + 8)
    count[luma] = 2
    op[whole, 0] = 0xFE
    count[whole] = 4
    return op, count


def _qoi_sparse(px, first, c):
    """The body of a QOI image from the pixels that differ from the one
    before, at `c`: their ops, and between them runs of 62 and what is left."""
    import numpy as np

    n = len(px)
    lead = int(c[0]) if len(c) else n
    run = np.append(c[1:], n) - c - 1               # repeats after each change
    cur = px[c].astype(np.int16)
    prv = np.empty_like(cur)
    if len(c):
        prv[1:] = px[c[1:] - 1]
        prv[0] = px[c[0] - 1] if c[0] else first
    op, count = _qoi_ops(cur, prv)

    runs = -(-run // 62)
    lead_n = -(-lead // 62)
    size = count + runs
    at = lead_n + np.concatenate(([0], np.cumsum(size)[:-1])).astype(np.int64)
    out = np.full(lead_n + int(size.sum()), 0xC0 | 61, np.uint8)    # full runs
    for j in range(5):
        m = count > j
        out[at[m] + j] = op[m, j]
    m = run > 0
    out[at[m] + count[m] + runs[m] - 1] = 0xC0 | ((run[m] - 1) % 62)
    if lead:
        out[lead_n - 1] = 0xC0 | ((lead - 1) % 62)
    return out


def _qoi_dense(px, first, same):
    """The body of a QOI image worked out at every pixel: its op, or where it
    repeats the one before, a run said where the run or its 62 ends."""
    import numpy as np

    n = len(px)
    prev = np.empty_like(px)
    prev[0] = first
    prev[1:] = px[:-1]
    idx = np.arange(n)
    # Where each run of repeats began, and how far into it each pixel is.
    start = np.maximum.accumulate(np.where(same, -1, idx)) + 1
    k = (idx - start) % 62
    nxt = np.append(same[1:], False)
    run_end = same & ((k == 61) | ~nxt)

    op, count = _qoi_ops(px.astype(np.int16), prev.astype(np.int16))
    count[same] = 0
    op[run_end, 0] = 0xC0 | k[run_end]
    count[run_end] = 1
    return op[np.arange(5) < count[:, None]]


def _apc(text):
    """A command as it goes down `in`, built rather than rendered: a frame
    line's words and a meta value are not all tokens."""
    return (protocol.APC + protocol.MARK + " " + text + protocol.ST).encode()


def _box(x, y, w, h):
    """A box checked as the wall checks it: x and y within 65535 of 0,
    w and h 1..4096."""
    for v in (x, y, w, h):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"bitwall: a box is four whole numbers, not {(x, y, w, h)!r}")
    if abs(x) > 65535 or abs(y) > 65535 or not (1 <= w <= 4096 and 1 <= h <= 4096):
        raise ValueError(f"bitwall: a box is at most 65535 off the glass and 1..4096 "
                         f"each way, not {(x, y, w, h)!r}")
    return x, y, w, h


def _box_command(box):
    """`box` as it goes down `in`: the box, or alone for the glass."""
    if box is None:
        return protocol.command("box").encode()
    x, y, w, h = box
    return _apc(f"box at={x},{y} size={w}x{h}")


def _log(msg):
    print(f"bitwall: {msg}", file=sys.stderr, flush=True)


class Layer:
    """A layer above a wall's base, held as a session of its own."""

    def __init__(self, target, place=None, type="video", user=None, run=None,
                 take=False, data=False, codec=None, fps=None, start=None,
                 instance=None, align="centre", box=None, compress=False):
        if place is None:
            # A sender placed `controller` holds no layer of its own: one it
            # opens goes above unless it says where.
            told = os.environ.get("BITWALL_PLACE")
            place = told if told and told != "controller" else "above"
        if not (place in PLACES or (isinstance(place, int) and not isinstance(place, bool)
                                    and place >= 0)
                or (isinstance(place, str) and place.isdigit())):
            raise ValueError(f"bitwall: a layer's place is above, top or a number, not {place!r}")
        if take and str(place) != "0":
            raise ValueError("bitwall: only the base is taken; take=True is for place=0")
        if data and codec not in CODECS:
            raise ValueError(f"bitwall: a stream's codec is one of {', '.join(CODECS)}, "
                             f"not {codec!r}")
        if not data and (codec is not None or fps is not None or start is not None):
            raise ValueError("bitwall: codec, fps and start are for a stream, data=True")
        if start is not None and fps is None:
            raise ValueError("bitwall: start= needs fps=")
        if align not in ALIGNS:
            raise ValueError(f"bitwall: align is one of {', '.join(ALIGNS)}, not {align!r}")
        #: Where the stream's frames sit on a glass of another shape; said
        #: each time the layer opens.
        self._align = align
        #: The box (x, y, w, h) the stream's frames are fitted to, or None
        #: for the glass; said each time the layer opens.
        self._box = _box(*box) if box is not None else None
        #: Whether its ssh compresses: pictures and video are compressed
        #: already, so not unless told.
        self._compress = compress
        #: Called when the wall goes and the layer will be asked for again.
        self._when_lost = []
        #: The `start` the next opening asks for, where the sender gave one.
        self._start_next = None
        self._link = session.Link(target, user, run)
        self.target = target
        self.place, self.type = str(int(place)) if str(place).isdigit() else place, type
        #: What the layer asks for on `open` besides its place and type.
        self._opts = {}
        if take:
            self._opts["take"] = 1
        if data:
            self._opts["data"] = 1
            self._opts["codec"] = codec
            if fps is not None:
                self._opts["fps"] = f"{float(fps):g}"
            if start is not None:
                self._opts["start"] = int(start)
        #: Whether the wall said `data` on `opened`: its fifo is there.
        self.has_data = False
        self._data = None
        #: The session `_data` writes into: a writer opened for one the
        #: wall has since lost is dropped, not written to.
        self._data_for = None
        self._dlock = threading.Lock()
        #: The layer the wall gave, once it has said; None before.
        self.z = None
        self._index = next(_count)
        self._session = None
        self._opens = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._settled = threading.Event()
        self._recovering = False
        #: The last thing the wall said of each kind, and the errors it
        #: said about our frames, newest last.
        self._said = {}
        #: Set once the wall has said its `caps` to the session open now.
        self._caps_said = threading.Event()
        self.errors = collections.deque(maxlen=16)
        self._repaint = threading.Event()
        self._meta = {}
        #: Why the layer has left the wall for good, or None.
        self.stopped = None
        self.frames_sent = self.frames_dropped = self.bytes_sent = 0
        #: Pictures, mirrors and the sessions list, asked on this layer.
        self._capture = capture.Capture(lambda text: self._write(_apc(text)))
        # A layer a controller started carries its instance id, as a base
        # does, so the controller can find what it started on the wall. A
        # layer of a Wall's is given the Wall's, since the Wall has already
        # taken it from the environment; one opened alone takes it there.
        if instance is None:
            instance = os.environ.pop("BITWALL_INSTANCE", None)
        self._instance = instance if instance and INSTANCE.match(instance) else None
        if self._instance:
            self._meta["instance"] = self._instance
            moving.hold(self, os.environ.get("BITWALL_RECORD"))

    # ------------------------------------------------------------ opening

    def open(self, wait=session.OPEN_WAIT + 10):
        """Ask the wall for the layer, and wait up to `wait` seconds for it."""
        self._open()
        self._settled.wait(wait)
        return self

    def _open(self):
        self._settled.clear()
        self._caps_said.clear()
        # Asked again -- the wall restarted, or the layer reopened -- a layer
        # placed from below asks for the slot it had, which puts it back in
        # its place in the stack whoever comes back first; one placed `top`
        # asks for the top again.
        again = self.z is not None and self.z > 0 and self.place != "top"
        opts = {"z": str(self.z) if again else self.place, "type": self.type, **self._opts}
        # `start` is the first stream's of the session it opens with. The
        # wall counts a new session's frames from 0 again, so the first
        # `start`, said again, would put every frame in the past: a layer
        # asked for again starts when its first frame is decoded, or when
        # the sender says with `start_at`.
        if self._opens:
            opts.pop("start", None)
        if self._start_next is not None:
            opts["start"] = self._start_next
        if self._instance:
            opts["instance"] = self._instance
        self.has_data = False
        # `l<k>` for which layer of this process it is, so it never has the
        # name of the process's base, and `r<k>` for each time it asks again.
        tag = f"l{self._index}" + (f"-r{self._opens}" if self._opens else "")
        self._session = session.Session(self._link, session.session_name(tag=tag),
                                        "layer", opts, heard=self._heard,
                                        ended=self._ended, compress=self._compress)
        self._opens += 1
        self._session.open()

    @property
    def open_now(self):
        """Whether the wall holds the layer for us at this moment."""
        s = self._session
        return s is not None and s.state == "open" and s.writable

    def _write(self, data):
        s = self._session
        if s is None:
            return False
        return s.write(data, self._stop) is not None

    def _heard(self, sess, said):
        if sess is not self._session:
            return
        self._capture.feed(said)
        if not isinstance(said, protocol.Message):
            return
        name = said.name
        if name == "opened":
            z = protocol.number(said.opts.get("z", ""))
            self.z = z if isinstance(z, int) else None
            self.has_data = "data" in said.args[1:]
            # A `start_at` is spent once an opening has it, and kept through
            # a request the wall dropped.
            self._start_next = None
            self._write(protocol.command("report").encode())
            if self._align != "centre":
                self._write(protocol.command("align", self._align).encode())
            if self._box is not None:
                self._write(_box_command(self._box))
            for key, value in self._meta.items():
                self._write(_apc(f"meta set {key} {value}"))
                if key == "instance":
                    self._write(_apc("meta hide instance"))
            self._capture.resay()
            self._settled.set()
        elif name == "moved":
            # A layer beneath went, or one came in under it: the stack
            # moved it, and its slot is now this.
            z = protocol.number(said.opts.get("z", ""))
            self.z = z if isinstance(z, int) else self.z
        elif name == "ping":
            self._write(protocol.command("pong").encode())
        elif name == "repaint":
            self._repaint.set()
        elif name == "error" and said.args[:1] == ("frame",):
            self.errors.append(said.raw)
        elif name in ("area", "screen", "caps", "watching"):
            self._said[name] = said
            if name == "caps":
                self._caps_said.set()

    def _ended(self, sess):
        if sess is not self._session or self._stop.is_set():
            return
        self._settled.set()
        if sess.state == "refused":
            self.stopped = f"the wall refused the layer: {sess.refused}"
        elif sess.state == "closed" and sess.closed == "displaced":
            self.stopped = "another layer has taken its place"
        if self.stopped:
            _log(f"{self.target}: {self.stopped}; stopping")
            return
        with self._lock:
            if self._recovering:
                return
            self._recovering = True
        threading.Thread(target=self._recover, args=(sess.state,), daemon=True).start()

    def _recover(self, why):
        """Open the layer again once the wall is back."""
        try:
            _log(f"{self.target}: layer {why}; opening it again when the wall is back")
            for fn in list(self._when_lost):
                try:
                    fn()
                except Exception as e:                                  # noqa: BLE001
                    _log(f"{self.target}: when_lost: {e}")
            wait = 0.25
            while not self._stop.is_set():
                old, self._session = self._session, None
                if old is not None:
                    old.kill()
                self._open()
                self._settled.wait(session.OPEN_WAIT + 30)
                if self.open_now or self.stopped:
                    break
                self._stop.wait(wait)
                wait = min(wait * 2, RETRY_MOST)
            if self.open_now:
                _log(f"{self.target}: layer open again")
        finally:
            with self._lock:
                self._recovering = False

    def retarget(self, target, run=None):
        """Move to the wall `target` names, at the place first asked for,
        with the same meta and instance, and ask the sender for a frame that
        stands on its own (`repaint_asked`). A stream on `data` is ended, not
        carried: its start is a time on the old wall's clock, and a sender
        playing one is not one a controller moves."""
        if self.stopped:
            raise RuntimeError(f"this layer has left {self.target} for good: {self.stopped}")
        link = session.Link(target, self._link.user, run)
        if (link.host, link.wall) == (self._link.host, self._link.wall) and run is None:
            return self
        was = self.target
        with self._dlock:
            pipe, self._data = self._data, None
        if pipe is not None:
            pipe.close()
            pipe.proc.kill()
        self._link, self.target = link, target
        self._said.clear()
        self.z = None
        self._repaint.set()
        _log(f"moving from {was} to {target}")
        with self._lock:
            if self._recovering:
                self._settled.set()
                return self
            self._recovering = True
        threading.Thread(target=self._recover, args=(f"moved from {was}",),
                         daemon=True).start()
        return self

    # ----------------------------------------------------------- drawing

    def frame(self, data, at=(0, 0), hold=1000, size=None):
        """Show one image, QOI, PNG or JPEG, its top left at `at` in the
        glass's pixels, scaled to `size` (w, h) where given, for up to
        `hold` ms, or until the next frame or a clear with `hold="still"`.
        False when it was dropped: no layer open, a pipe too full to take
        it now, or a codec the wall does not draw (`takes`)."""
        codec = protocol.image_of(data)
        got = protocol.image_size(codec, data) if codec else None
        if got is None:
            raise ValueError("bitwall: a frame is a whole QOI, PNG or JPEG image")
        if hold != "still" and not (isinstance(hold, int) and hold > 0):
            raise ValueError(f"bitwall: hold is a number of ms or 'still', not {hold!r}")
        s = self._session
        if not self.open_now:
            self.frames_dropped += 1
            return False
        if not s.ready or not self.takes(codec):
            self.frames_dropped += 1
            return False
        w, h = got
        box = f" size={int(size[0])}x{int(size[1])}" if size else ""
        head = _apc(f"frame {codec} {w}x{h} at={int(at[0])},{int(at[1])}{box} "
                    f"hold={hold} {len(data)}")
        if s.write(head + data, self._stop) is None:
            self.frames_dropped += 1
            return False
        self.frames_sent += 1
        self.bytes_sent += len(head) + len(data)
        return True

    # ------------------------------------------------------------ data

    def data_argv(self):
        """The command that carries this layer's `data`, `cat` into the fifo
        on the wall's host or the relay into a Windows pipe, for a caller
        who pipes a stream straight in."""
        s = self._session
        if s is None:
            raise RuntimeError("bitwall: the layer is not open")
        return self._link.writer_argv(
            posixpath.join(self._link.session_dir(s.name), "data"), compress=self._compress)

    def data_write(self, chunk, timeout=None):
        """Put `chunk` whole on the layer's `data`, waiting for room as long
        as it takes, or `timeout` seconds; how many bytes went, or None when
        there is no stream to write to: the layer has no `data`, or its
        writer or the layer has gone."""
        with self._dlock:
            if not (self.open_now and self.has_data):
                return None
            pipe = self._data
            if pipe is not None and self._data_for is not self._session:
                # The writer of a session the wall has lost: its `cat`
                # learns of it only when a write fails, so what it was
                # handed would be lost, and a stream that has lost its
                # first frame, the one with its headers, decodes nothing
                # until its next keyframe and has every frame counted that
                # many late on a timed layer.
                pipe.close()
                pipe.proc.kill()
                pipe = self._data = None
            if pipe is None or pipe.closed or pipe.proc.poll() is not None:
                try:
                    proc = self._link.writer(
                        posixpath.join(self._link.session_dir(self._session.name), "data"),
                        compress=self._compress)
                except OSError:
                    return None
                pipe = self._data = portable.Pipe(proc, name="bitwall-data")
                self._data_for = self._session
            sent = pipe.write(chunk, self._stop, timeout)
            if not sent:
                return None
            self.bytes_sent += sent
            return sent

    def data_end(self, wait=5.0):
        """End the stream on `data`: its writer closes, the wall plays out
        what it has and clears the layer, and the layer stays for the next."""
        with self._dlock:
            pipe, self._data = self._data, None
        if pipe is None:
            return
        pipe.close()
        proc = pipe.proc
        try:
            proc.wait(timeout=wait)
        except Exception:
            try:
                proc.kill()
                proc.wait(1)
            except Exception:
                pass

    def clear(self):
        """Take the picture down; the layer stays."""
        return self._write(protocol.command("clear").encode())

    def when_lost(self, fn):
        """Call `fn()` when the wall goes and the layer will be asked for
        again once it is back. What the layer held is gone with it, and a
        stream on `data` starts again from its first frame: a sender that
        times its stream itself starts its count again, and may say when
        with `start_at`."""
        if fn not in self._when_lost:
            self._when_lost.append(fn)
        return self

    def start_at(self, ms):
        """The Unix time in ms the stream's first frame is due at, on the
        next opening of a layer with `fps`: for a sender that keeps its own
        clock and starts its count again when the layer is asked for again.
        Without it, a layer asked for again starts when its first frame is
        decoded."""
        if "fps" not in self._opts:
            raise ValueError("bitwall: start_at() is for a stream with fps")
        self._start_next = int(ms)
        return self

    def align(self, where):
        """Where the stream on `data` sits on a glass of another shape, from
        its next frame: "top", "centre" or "bottom". Its frames are scaled
        to fit with their shape kept; this is where the glass left over
        goes, all below, even, or all above. A wall whose `caps` do not say
        `align` centres them."""
        if where not in ALIGNS:
            raise ValueError(f"bitwall: align is one of {', '.join(ALIGNS)}, not {where!r}")
        self._align = where
        if self.open_now:
            self._write(protocol.command("align", where).encode())
        return self

    def box(self, x=None, y=None, w=None, h=None):
        """Fit the stream on `data` to the box w by h at x, y in the
        glass's pixels, from its next frame, or to the whole glass again
        with no arguments. Its frames are scaled to fit the box with their
        shape kept, `align` placing them within it, and the layer is
        transparent outside it. The box is kept through a change of glass,
        what falls off the glass not drawn. A wall whose `caps` do not say
        `box` fills the glass, and a sender that needs the box sends
        `frame`s instead."""
        self._box = None if (x, y, w, h) == (None,) * 4 else _box(x, y, w, h)
        if self.open_now:
            self._write(_box_command(self._box))
        return self

    def meta(self, **keys):
        """Say what this layer is, as a sender's meta says it; said again
        whenever the layer opens again."""
        for key, value in keys.items():
            self._meta[key] = " ".join(str(value).split())
            if self.open_now:
                self._write(_apc(f"meta set {key} {self._meta[key]}"))
        return self

    # ------------------------------------------------------ what it says

    def sessions(self, timeout=None):
        """Every session on the wall, as `Wall.sessions()` has them."""
        return self._capture.sessions(timeout)

    def picture(self, target, size=None, timeout=None, format=None, quality=None):
        """A picture of `target`, as `Wall.picture()` has it."""
        return self._capture.picture(target, size, timeout, format, quality)

    def mirror(self, target, every=None, size=None, format=None, quality=None):
        """A mirror of `target`, as `Wall.mirror()` has it."""
        return self._capture.mirror(target, every, size, format, quality)

    def pixels(self):
        """The glass, (width, height) in pixels, as the wall last said."""
        said = self._said.get("screen")
        value = said.opts.get("pixels", "") if said else ""
        w, _, h = value.partition("x")
        if w.isdigit() and h.isdigit():
            return int(w), int(h)
        return None

    def watching(self):
        """How many are watching, as the wall last said; None before it has."""
        said = self._said.get("watching")
        n = protocol.number(said.args[0]) if said and said.args else None
        return n

    def repaint_asked(self):
        """Whether the wall has asked for a frame that stands on its own
        since this was last asked: its layer was taken down under it."""
        asked = self._repaint.is_set()
        self._repaint.clear()
        return asked

    def caps(self):
        """The words and keys of the wall's `caps`, or None before it said."""
        said = self._said.get("caps")
        return said

    def offers(self, word, wait=5.0):
        """Whether the wall's `caps` says `word`, as `box` from a wall that
        fits a stream to a box. The layer asks for them once it opens, so
        this waits up to `wait` seconds for the answer; a wall that has
        not said them by then offers nothing."""
        if not self._caps_said.wait(wait):
            return False
        said = self._said.get("caps")
        return said is not None and (word in said.args or word in said.opts)

    def takes(self, codec):
        """Whether the wall draws a frame in `codec`, `qoi`, `png` or
        `jpeg`, as its `caps` says in `images=`. A wall that takes video
        and says no `images` takes QOI alone; one that has not said its
        caps yet is taken at its word for QOI and not for the rest."""
        said = self._said.get("caps")
        images = said.opts.get("images") if said is not None else None
        if not images:
            return codec == "qoi"
        return codec in str(images).split(",")

    # ------------------------------------------------------------- ending

    def close(self):
        """Leave the wall: the layer is cleared and the session ends.

        Stopping comes first: a `data_write` waiting on a wall too busy to
        read holds the data lock, and gives it up only once it sees the
        stop, so ending the stream before stopping would wait on it for
        as long as the wall stays busy."""
        self._stop.set()
        self.data_end(wait=1.0)
        s, self._session = self._session, None
        if s is not None:
            s.close(last=protocol.command("close").encode(), linger=0.5)
