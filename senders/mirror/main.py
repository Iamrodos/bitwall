#!/usr/bin/env python3
"""One wall's glass on other walls, as video.

    senders/mirror/main.py localhost:0 --to pi.local:0,pi.local:1
    controllers/bitwall-senders start bitwall/mirror pi-a.local:0 --to pi-b.local:0,pi-b.local:1

The wall it is started on is the source, and `--to` names the walls that
show it. It runs on the source's host: the painter there streams its glass
raw (PROTOCOL.md, "The glass as a raw stream"), ffmpeg here encodes it,
and the same bytes go to every destination on a video layer's `data`
(PROTOCOL.md, "The data pipe"), which the destination decodes and plays.
MIRROR.md is the design.

**The painter's frames are read here, once a size.** A painter whose
`caps` say `streams=<n>` runs several streams at once, so one is asked
for each size and rate destinations are sent, by name, and each encode
takes its frames as they come; one that says `stream` alone runs one, so
it is asked for one at the largest size and the highest rate any
destination is sent, and an encode for less scales it down and leaves
frames out. Each stream is read off its fifo by a thread of its own,
each 32-byte header taken off and the I420 planes handed whole to every
encode of it. A frame the painter let go (its numbers jump) is made up by
repeating the one before, so an encoder's frames stay one to a tick of
the painter's clock, which timed playback at the far end counts on. A
jump of more than a second is the stream begun again, and is not filled.

**Each destination may say what it is sent**, `host:wall@WxH/fps` in
`--to`: `WxH` the most it is sent, in place of `--size`, and `fps` its
rate, in place of `--fps`; either may be left out (`pi.local:0@/15`).

**Each destination is paired with a codec**, first that works: one this
machine encodes in hardware and the destination decodes in hardware
(`hw=` in its `caps`); H.264 by x264 here, decoded in hardware there;
MJPEG where the destination decodes that in hardware. Otherwise it is
refused and the log says why. `--codec` asks for one, which a destination
gets where it takes it at all, decoded in software where it must be.

**One encode a codec, size and rate.** Destinations taking the same codec
at the same size and rate share an ffmpeg, whose output is cut into access units here
and written to each of them by a writer of its own, behind a queue of
about a second. A destination that falls behind has its queue let go and
is skipped until the next keyframe, its parameter sets put in front, while
the others carry on.

**Timing.** With `--lead` above 0 each destination's layer is opened with
`fps` and a `start` on its own clock: the draw time of a keyframe a little
ahead, plus the lead, so frame i there goes up `lead` seconds after the
painter drew it, on walls whose clocks agree. The keyframe is chosen far
enough ahead for the layer to be open before it comes, and the stream
starts there. A destination whose frames reach it late, or that skipped,
or whose source jumped, is given a new layer timed afresh, the old one
closed when the new one's first frame is due. With `--lead 0` the layers
have no `fps` and show each frame as it is decoded.

**Defaults, from walls measured.** The bitrate is 3 Mb/s at 720p25,
scaled by the pixels a second sent. The lead is one for every
destination, so they show each frame together, the most any of them
wants: a quarter second; half a second where a frame is held on the way,
by VideoToolbox encoding it here or by a destination's decoder running
more than two threads for what it is sent, each holding a frame, as a
painter given `--decoder-threads` above 2 does; and
0.8 s above 720p on a destination that decodes H.264 in hardware and not
HEVC, a Pi 3, whose decoder is late with a big frame at less. A destination's
layer goes on its base where nobody holds it, since a painter with an
empty base dozes its display, layers and all, and above the base where
somebody does. Each but the lead is computed per destination, and an
option given always wins.

**Losses.** A destination that cannot be asked as the mirror starts is
out of reach, not refused: it is asked again, backing off to half a
minute, and added once it answers, at the lead the rest play at
(`Mirror`). A destination that goes away is opened again on its own,
backing off, and starts at the next keyframe. The source's controller
session going away is opened again and the stream asked for again.

**Pulling.** With `--pull` it runs on a destination's host instead,
the source elsewhere: the encode runs on the source's host, `there.py`
carried there through ssh to read the painter's stream and feed ffmpeg,
and what comes back over that ssh is the encoded stream and each
frame's draw time, handed to the destinations as an encode here is
(`PullGroup`). For a source that cannot reach the destinations, as a Pi
cannot open ssh to Windows, and for the raw glass, too heavy for ssh.

**On Windows** a glass stream is a named pipe, read here through
`bitwall/local.py`, and from another host through `relay read`; a
Windows host's clock is asked with `relay clock`, and so is every host's
from a Windows machine, whose ssh shares no connection (MIRROR.md, *A
Windows host*).
"""
import argparse
import base64
import collections
import json
import math
import os
import posixpath
import queue
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import bitwall                                                   # noqa: E402

#: A raw frame's header: `BWG1`, width, height, number, drawn (µs of the
#: wall's clock), and the bytes of I420 after it.
HEADER = struct.Struct("<4sIIQQI")
MAGIC = b"BWG1"
#: What a destination is sent at most unless `--size` says more.
CAP = (1280, 720)
#: The bitrate sent by default, in Mb/s, at RATE_AT (width, height, fps),
#: scaled from there by the pixels a second: 3 Mb/s at 720p25 is crisp
#: enough for a preview and costs a Pi 3 15% of a core to show.
RATE = 3.0
RATE_AT = (1280, 720, 25)
#: The lead by default, in seconds: room enough for a frame's trip from the
#: painter to a destination's glass, where 0.25 s left the least frame 190 ms
#: to spare at 720p25 from a Pi 5 to a Pi 3 over Wi-Fi ...
LEAD = 0.25
#: ... and where a frame is held on its way: by VideoToolbox's encoder at
#: the source, 206 to 230 ms at 720p25, which from a Mac to Windows dropped
#: a second or two of frames at 0.25 s and none at 0.3; or by a
#: destination's decoder running more than HELD_THREADS threads for what it
#: is sent, each of which holds a frame (4 held each 165 ms against 45
#: with one, the painter's default where a hardware decoder does the work,
#: which played at 0.03 s) ...
LEAD_HELD = 0.5
HELD_THREADS = 2
#: ... and above 720p on a Pi 3 class decoder (H.264 in hardware and not
#: HEVC), which at 1080p was late with every frame at 0.25 s and smooth at
#: 0.8.
LEAD_SLOW = 0.8
#: What each codec is encoded with here in hardware, tried in turn.
HARDWARE = {
    "h264": ("h264_videotoolbox", "h264_qsv", "h264_nvenc", "h264_v4l2m2m"),
    "hevc": ("hevc_videotoolbox", "hevc_qsv", "hevc_nvenc", "hevc_v4l2m2m"),
}
#: ... and in software.
SOFTWARE = {"h264": "libx264", "mjpeg": "mjpeg"}
#: Chocolatey's ffmpeg itself, not its shim in chocolatey\bin, so the
#: encoder is the child this started: on Windows the first choice after
#: BITWALL_FFMPEG, as the painter's decoder is.
CHOCO_FFMPEG = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
#: Frames waiting for an encoder: more than this and it is behind, and
#: the frame is not given it.
FEED_QUEUE = 3
#: Seconds of stream a destination's queue holds before it is skipped.
QUEUE_SECS = 1.0
#: How long a layer may take to open before it is given up.
OPEN_WAIT = 15.0
#: The fewest seconds ahead a timed layer's first keyframe is chosen.
BUDGET = 2.0
#: How much later than it was timed for a layer's first frame may have
#: been drawn before the layer is timed afresh: a time for a keyframe seconds ahead
#: is a guess from the frames fed so far, and an encoder just started
#: feeds unevenly, which put a Pi 3's frames 0.6 s past its decoder.
SLIP = 0.1
#: How often the log says how things stand.
SUMMARY = 60.0


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} mirror {msg}", flush=True)


def ffmpeg():
    """The ffmpeg that encodes: BITWALL_FFMPEG, else on Windows
    Chocolatey's where it is there, else the first on the path."""
    if os.environ.get("BITWALL_FFMPEG"):
        return os.environ["BITWALL_FFMPEG"]
    if os.name == "nt" and os.path.exists(CHOCO_FFMPEG):
        return CHOCO_FFMPEG
    return "ffmpeg"


def here(target):
    """The wall as this machine reaches it: a wall on this host is
    `localhost`, read without ssh, whatever the target called it."""
    host, wall = bitwall.session.address(target)
    me = socket.gethostname().lower().removesuffix(".local")
    if host.lower().removesuffix(".local") == me:
        return f"localhost:{wall}"
    return target


# ------------------------------------------------------------- the glass

class Broken(Exception):
    """A frame that did not come whole, or a stream that is not frames."""


def _read_into(fd, mv):
    """One read into `mv`, how much came: straight into it where the
    platform reads into a buffer, and otherwise (Windows' Python) read and
    copied."""
    if hasattr(os, "readv"):
        return os.readv(fd, [mv])
    data = os.read(fd, min(len(mv), 1 << 20))
    mv[:len(data)] = data
    return len(data)


def fill(fd, mv, stop, idle=0.2):
    """Read into `mv` until it is full; how much came. Less than asked is
    the writer gone (end of file, or on Windows a pipe broken or let go)
    or the stop: a read on a pipe returns what has arrived, not what was
    asked for, so it loops."""
    got = 0
    while got < len(mv):
        if stop.is_set():
            return got
        try:
            n = _read_into(fd, mv[got:])
        except BlockingIOError:
            select.select([fd], [], [], idle)
            continue
        except InterruptedError:
            continue
        except OSError:
            return got
        if n == 0:
            return got
        got += n
    return got


def read_frame(fd, stop):
    """The next frame on the glass fifo, as (w, h, number, drawn µs, the
    planes as a bytearray), or None where nothing came: no writer, or the
    stop. A frame cut short or a header that is not one is `Broken`."""
    head = bytearray(HEADER.size)
    got = fill(fd, memoryview(head), stop)
    if got == 0:
        return None
    if got < HEADER.size:
        raise Broken(f"a header cut short at {got} bytes")
    magic, w, h, n, us, length = HEADER.unpack(head)
    if magic != MAGIC or length != w * h * 3 // 2 or not w or not h:
        raise Broken(f"not a frame: {bytes(head[:4])!r} {w}x{h} {length} bytes")
    planes = bytearray(length)
    got = fill(fd, memoryview(planes), stop)
    if got < length:
        raise Broken(f"a frame cut short at {got} of {length} bytes")
    return w, h, n, us, planes


# ------------------------------------------------------------- choosing

def _encodes(name):
    """Whether ffmpeg encodes with `name` on this machine: tried, since
    `-encoders` lists a hardware encoder whether or not the hardware is
    there."""
    fmt = "yuvj420p" if name == "mjpeg" else "yuv420p"
    try:
        return subprocess.run(
            [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-frames:v", "2",
             "-pix_fmt", fmt, "-c:v", name, "-f", "null", "-"],
            capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Encoders:
    """What this machine encodes each codec with, found by trying, once."""

    def __init__(self, works=_encodes):
        self._works = works
        self._got = {}
        self._lock = threading.Lock()

    def _first(self, names):
        with self._lock:
            for name in names:
                if name not in self._got:
                    self._got[name] = self._works(name)
                    if self._got[name]:
                        log(f"encodes with {name}")
                if self._got[name]:
                    return name
        return None

    def hardware(self, codec):
        return self._first(HARDWARE.get(codec, ()))

    def software(self, codec):
        return self._first((SOFTWARE[codec],)) if codec in SOFTWARE else None


def pair(codecs, hw, enc, force=None):
    """What a destination taking `codecs`, `hw` of them in hardware, is
    sent: (codec, encoder, why), the encoder None where it is refused."""
    codecs, hw = set(codecs), set(hw) & set(codecs)
    said = f"codecs={','.join(sorted(codecs)) or 'none'} hw={','.join(sorted(hw)) or 'none'}"
    if force:
        if force not in codecs:
            return force, None, f"takes no {force} ({said})"
        name = enc.hardware(force) or enc.software(force)
        if name is None:
            return force, None, f"nothing here encodes {force}"
        there = "hardware" if force in hw else "software"
        return force, name, f"{force} as asked, by {name}, decoded in {there} there"
    for codec in ("hevc", "h264"):
        if codec in hw and (name := enc.hardware(codec)):
            return codec, name, f"{codec} in hardware at both ends, by {name}"
    if "h264" in hw and (name := enc.software("h264")):
        return "h264", name, f"h264 by {name} here, decoded in hardware there"
    if "mjpeg" in hw and (name := enc.software("mjpeg")):
        return "mjpeg", name, "mjpeg, decoded in hardware there"
    hint = ("; --codec h264 sends it H.264 to decode in software"
            if "h264" in codecs else "")
    return None, None, f"decodes nothing in hardware this can send ({said}){hint}"


def size_for(source, glass, most):
    """The frame a destination is sent: the source's shape, as big as
    fits `most` and the destination's glass, never bigger than the source,
    even on both sides."""
    sw, sh = source
    bw, bh = most
    if glass:
        bw, bh = min(bw, glass[0]), min(bh, glass[1])
    k = min(bw / sw, bh / sh, 1.0)
    return max(16, int(sw * k) // 2 * 2), max(16, int(sh * k) // 2 * 2)


def default_mbps(codec, size, fps):
    """The bitrate a stream is sent at when `--bitrate` does not say, in
    Mb/s: RATE at RATE_AT scaled by the pixels a second, and three times
    that for MJPEG, which compresses each frame alone."""
    w, h, f = RATE_AT
    mbps = max(0.3, RATE * size[0] * size[1] * fps / (w * h * f))
    return mbps * 3 if codec == "mjpeg" else mbps


def default_lead(codec, size, hw, decoder=None, encoder=None):
    """The lead a destination wants when `--lead` does not say: LEAD_SLOW
    above 720p where it decodes H.264 in hardware and not HEVC, as a Pi 3
    does, whose decoder takes longer over a big frame; LEAD_HELD where a
    frame is held on the way, by VideoToolbox encoding it here or by more
    than HELD_THREADS `decoder` threads there for what it is sent (its
    `threads` line), as a painter told to run more does; LEAD otherwise."""
    if size[0] * size[1] > CAP[0] * CAP[1] and codec == "h264" \
            and "h264" in hw and "hevc" not in hw:
        return LEAD_SLOW
    if (encoder or "").endswith("_videotoolbox") or (decoder or 0) > HELD_THREADS:
        return LEAD_HELD
    return LEAD


def base_holder(target):
    """The session holding `target`'s base as its `sessions` says, None
    where the base is free."""
    c = bitwall.Controller(target).open()
    try:
        return _base(c)
    finally:
        c.close()


def _base(c):
    for said in c.ask("sessions"):
        if getattr(said, "name", None) == "session" and said.args[1:3] == ("layer", "0"):
            return said.args[0]
    return None


def ask(target):
    """What a wall says of itself on a controller session: a dict of its
    caps' words, codecs, hw, how many glass streams it runs at once (0,
    1 for a painter that says `stream` alone, or `streams=`), glass
    pixels, receiver, its decoder's threads where a hardware decoder does
    the work and where ffmpeg decodes in software (None where it does not
    say; a painter that says only `decoder=` runs as many for both), and
    the session holding its base or None."""
    c = bitwall.Controller(target).open()
    try:
        got = {"words": set(), "codecs": [], "hw": [], "pixels": None, "receiver": None,
               "base": None, "streams": 0, "decoder": None, "software": None}
        for said in c.report:
            name = getattr(said, "name", None)
            if name == "caps":
                got["words"] = set(said.args)
                n = said.opts.get("streams") or ""
                got["streams"] = int(n) if n.isdigit() else int("stream" in said.args)
                if "data" in said.args:
                    got["codecs"] = [x for x in (said.opts.get("codecs") or "").split(",") if x]
                    got["hw"] = [x for x in (said.opts.get("hw") or "").split(",") if x]
            elif name == "screen":
                w, _, h = (said.opts.get("pixels") or "").partition("x")
                if w.isdigit() and h.isdigit():
                    got["pixels"] = (int(w), int(h))
            elif name == "receiver" and said.args:
                got["receiver"] = said.args[0]
            elif name == "threads":
                for key in ("decoder", "software"):
                    n = said.opts.get(key) or ""
                    got[key] = int(n) if n.isdigit() else None
                if got["software"] is None:
                    got["software"] = got["decoder"]
        got["base"] = _base(c)
        return got
    finally:
        c.close()


def _windows(link):
    """Whether a wall's host keeps its walls as Windows' named pipes,
    which are reached through the relay (WINDOWS.md); False where it
    cannot be asked."""
    try:
        return link.pipes
    except RuntimeError:
        return False


def _relay_offset(link, tries):
    """`clock_offset` through `relay clock`: its first answer says the
    relay is running, and each after it is timed from the line that asked
    for it."""
    proc = subprocess.Popen(link.relay_argv("clock"), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    watch = threading.Timer(30, proc.kill)
    watch.start()
    best = None
    try:
        if proc.stdout.readline():
            for _ in range(tries):
                t0 = time.time()
                proc.stdin.write(b"\n")
                proc.stdin.flush()
                said = proc.stdout.readline()
                t1 = time.time()
                try:
                    there = float(said)
                except ValueError:
                    break
                if best is None or t1 - t0 < best[1]:
                    best = (there - (t0 + t1) / 2, t1 - t0)
    except OSError:
        pass
    finally:
        watch.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return None if best is None else best[0]


def clock_offset(target, tries=7):
    """How far the clock of `target`'s host is ahead of this one, in
    seconds: the quickest of several answers, its time taken from half way
    through it. 0 for this machine; None where the host does not say.
    Asked with `date` over a shared ssh connection, and through `relay
    clock` where the host is Windows or this machine's ssh shares none."""
    link = bitwall.session.Link(target)
    if link.local:
        return 0.0
    if not bitwall.portable.SSH_SHARES or _windows(link):
        # Each `date` would be an ssh connection of its own, the time read
        # at the end of a long round trip; and a Windows host's shell may
        # have no `date`, or Git's, which answers just as late.
        return _relay_offset(link, tries)
    best = None
    for _ in range(tries):
        t0 = time.time()
        said = link.shell("date +%s.%N", timeout=5)
        t1 = time.time()
        try:
            there = float(said)
        except ValueError:
            continue
        if best is None or t1 - t0 < best[1]:
            best = (there - (t0 + t1) / 2, t1 - t0)
    return None if best is None else best[0]


# ---------------------------------------------------------- the encoded

class Start:
    """Where an access unit begins in the stream: whether it is a key, the
    parameter sets to put in front where a destination starts on it and
    it has none of its own, and, once its group has counted it, its index
    and when its frame was drawn (seconds of this machine's clock)."""

    __slots__ = ("key", "prefix", "index", "drawn")

    def __init__(self, key, prefix=b""):
        self.key, self.prefix = key, prefix
        self.index, self.drawn = None, None


class Cutter:
    """An Annex B stream, H.264 or HEVC, as it comes off the encoder, cut
    where each access unit begins.

    `feed` returns pieces, (bytes, Start or None), as soon as they can be
    said: a piece with a Start begins an access unit. Everything is passed
    on as it arrives but the last few bytes, which may be the start of a
    start code, and the head of an access unit until its first slice says
    whether it is a key. A unit's end is known only when the next begins,
    which is the decoder's case too, so this costs nothing it would not.
    """

    #: Bytes held back: a start code and the bytes after it that say
    #: what the unit is.
    HOLD = 8
    #: The parameter set types, H.264's and HEVC's, in the order they go.
    PARAMS = {False: (7, 8), True: (32, 33, 34)}

    def __init__(self, codec):
        self.hevc = codec == "hevc"
        self.buf = bytearray()
        self.scan = 0           # where to look for the next start code
        self.out = 0            # how much of buf has gone
        self.begun = False      # whether the first start code has come
        self.nal = None         # (start, type) of the unit being read
        self.au = None          # the access unit under way: its start, until said
        self.au_vcl = False     # whether it has had a slice
        self.au_params = False  # whether it has parameter sets of its own
        self.params = {}        # parameter set type -> its bytes, the latest

    def _kind(self, p):
        """(type, vcl, first slice of a picture, key, parameter set,
        begins an access unit) of the unit whose header is at p."""
        b = self.buf
        if self.hevc:
            t = (b[p] >> 1) & 0x3F
            vcl = t < 32
            first = vcl and bool(b[p + 2] & 0x80)
            key = 16 <= t <= 21
            params = t in (32, 33, 34)
            begins = t in (32, 33, 34, 35, 39) or 41 <= t <= 44 or 48 <= t <= 55
        else:
            t = b[p] & 0x1F
            vcl = 1 <= t <= 5
            first = vcl and bool(b[p + 1] & 0x80)
            key = t == 5
            params = t in (7, 8)
            begins = t in (6, 7, 8, 9) or 14 <= t <= 18
        return t, vcl, first, key, params, begins

    def feed(self, data):
        b = self.buf
        b += data
        pieces = []
        need = 3 if self.hevc else 2       # the header's bytes, and the slice's first
        while True:
            p = b.find(b"\x00\x00\x01", self.scan)
            if p < 0:
                self.scan = max(self.scan, len(b) - 2)
                break
            if p + 3 + need > len(b):
                self.scan = p
                break
            self.scan = p + 3
            start = p - 1 if p - 1 >= self.out and b[p - 1] == 0 else p
            t, vcl, first, key, params, begins = self._kind(p + 3)
            if self.nal is not None and self.nal[1] in self.PARAMS[self.hevc]:
                self.params[self.nal[1]] = bytes(b[self.nal[0]:start])
            self.nal = (start, t)
            if not self.begun:
                # Whatever came before the first start code is not stream.
                self.begun, self.out, self.au = True, start, start
            elif self.au_vcl and (begins or first):
                if self.out < start:
                    pieces.append((bytes(b[self.out:start]), None))
                    self.out = start
                self.au, self.au_vcl, self.au_params = start, False, False
            if params:
                self.au_params = True
            if vcl and not self.au_vcl:
                self.au_vcl = True
                prefix = b"" if self.au_params or not key else self._prefix()
                pieces.append((bytes(b[self.au:p + 3]), Start(key, prefix)))
                self.out, self.au = p + 3, None
        # The rest as far as is safe: never into an access unit not yet
        # said, nor the bytes that may begin a start code.
        if not self.begun:
            self.out = max(self.out, len(b) - self.HOLD)
        else:
            safe = len(b) - self.HOLD if self.au is None else min(len(b) - self.HOLD, self.au)
            if safe > self.out:
                pieces.append((bytes(b[self.out:safe]), None))
                self.out = safe
        keep = self.out if self.nal is None else min(self.out, self.nal[0])
        if keep > 0:
            del b[:keep]
            self.out -= keep
            self.scan = max(0, self.scan - keep)
            if self.nal is not None:
                self.nal = (self.nal[0] - keep, self.nal[1])
            if self.au is not None:
                self.au -= keep
        return pieces

    def _prefix(self):
        return b"".join(self.params[t] for t in self.PARAMS[self.hevc] if t in self.params)

    def flush(self):
        """What is left at the end of the stream."""
        rest = bytes(self.buf[self.out:]) if self.begun else b""
        self.buf.clear()
        self.out = self.scan = 0
        self.nal = self.au = None
        self.begun = self.au_vcl = self.au_params = False
        return [(rest, None)] if rest else []


class JpegCutter:
    """MJPEG off the encoder: each JPEG whole, from its start-of-image to
    its end-of-image marker, and every one a key."""

    def __init__(self, codec=None):
        self.buf = bytearray()

    def feed(self, data):
        b = self.buf
        b += data
        pieces = []
        while True:
            s = b.find(b"\xff\xd8")
            if s < 0:
                del b[:max(0, len(b) - 1)]
                return pieces
            e = b.find(b"\xff\xd9", s + 2)
            if e < 0:
                if s:
                    del b[:s]
                return pieces
            pieces.append((bytes(b[s:e + 2]), Start(True)))
            del b[:e + 2]

    def flush(self):
        self.buf.clear()
        return []


def cutter(codec):
    return JpegCutter() if codec == "mjpeg" else Cutter(codec)


def encode_argv(codec, encoder, src, size, fps, mbps, binary=None):
    """ffmpeg (`binary`, else this machine's) reading raw I420 at `src` on
    stdin and writing `codec` at `size` on stdout: BT.709 at studio range
    in and out, as the painter draws it and a wall decodes it, a keyframe
    every two seconds, no B-frames, a packet out as soon as it is made."""
    g = max(1, round(2 * fps))
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    argv = [binary or ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{src[0]}x{src[1]}",
            "-r", f"{fps:g}", *tags, "-color_range", "tv", "-i", "pipe:0", "-an"]
    vf = ["setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
    if codec == "mjpeg":
        # A JPEG is full range and BT.601 by its own standard.
        vf.append(f"scale={size[0]}:{size[1]}:flags=bilinear:in_range=tv:out_range=pc"
                  ":in_color_matrix=bt709:out_color_matrix=bt601")
        vf.append("format=yuvj420p")
        argv += ["-vf", ",".join(vf), "-c:v", encoder, "-b:v", f"{mbps:.2f}M",
                 "-color_range", "pc", "-f", "mjpeg"]
    else:
        if tuple(size) != tuple(src):
            vf.append(f"scale={size[0]}:{size[1]}:flags=bilinear")
        argv += ["-vf", ",".join(vf), "-c:v", encoder, "-b:v", f"{mbps:.2f}M",
                 "-maxrate", f"{mbps:.2f}M", "-bufsize", f"{mbps:.2f}M",
                 "-g", str(g), "-bf", "0", "-pix_fmt", "yuv420p", *tags, "-color_range", "tv"]
        if encoder == "libx264":
            argv += ["-preset", "ultrafast", "-tune", "zerolatency", "-threads", "2",
                     "-x264-params", f"repeat-headers=1:scenecut=0:keyint={g}"]
        elif encoder.endswith("_videotoolbox"):
            argv += ["-realtime", "1"]
        elif encoder.endswith("_qsv"):
            # A frame out as soon as it is made, rather than four behind,
            # and the fastest preset: the default `medium` took HEVC at
            # 1080p over a frame's time on a full-screen change.
            argv += ["-async_depth", "1", "-preset", "veryfast"]
        argv += ["-f", codec]
    return argv + ["-flush_packets", "1", "pipe:1"]


def _lower():
    """Run at a lower priority than the painter: it is the wall that must
    keep its frames."""
    try:
        os.nice(10)
    except OSError:
        pass


def lowered(encoder=None):
    """What starts an encoder at that lower priority: nice 10. Not on
    Windows for one in hardware: its own work is a frame's copy, and below
    normal it waited out every burst of the foreground's (a page turned,
    a browser), and hevc_qsv on an Intel laptop fell 35-50 frames behind."""
    if os.name == "posix":
        return {"preexec_fn": _lower}
    if encoder and encoder.endswith(("_qsv", "_nvenc", "_amf")):
        return {}
    return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)}


class Group:
    """One encode: the destinations taking one codec at one size and rate,
    fed the frames of the painter's stream `src` (size) at `src_fps`, of
    which it takes `fps` a second, and handed what ffmpeg makes of them."""

    def __init__(self, codec, encoder, size, src, fps, mbps, stop, src_fps=None):
        self.codec, self.encoder, self.size, self.src = codec, encoder, size, src
        self.fps, self.mbps, self.stop = fps, mbps, stop
        self.share = min(1.0, fps / (src_fps or fps))   # of its stream's frames
        self.owed = 0.0
        self.gop = max(1, round(2 * fps))
        self.dests = []
        self.q = queue.Queue(maxsize=FEED_QUEUE)
        self.lock = threading.Lock()
        self.fed = 0                    # frames given ffmpeg, all told
        self.out = 0                    # access units it has made, all told
        self.draws = collections.deque()
        self.last = None                # (index, drawn) of the last frame fed
        self.phase = None               # index % gop of its keyframes
        self.behind = 0                 # frames it was not ready for
        self.restarts = 0
        self.proc = None
        self.name = f"{codec} {size[0]}x{size[1]} {fps:g}fps"

    def put(self, planes, drawn):
        """A frame for the encoder, or counted as not taken where it has
        not finished the ones before. Where it takes fewer frames than its
        stream has, only its share of them, spread evenly."""
        self.owed += self.share
        if self.owed < 1 - 1e-9:
            return
        self.owed -= 1
        try:
            self.q.put_nowait((planes, drawn))
        except queue.Full:
            self.behind += 1

    def predict(self, ahead):
        """(index, drawn) of the first keyframe to be fed at least `ahead`
        seconds from now, or None before anything has been fed."""
        with self.lock:
            if self.last is None:
                return None
            idx, drawn = self.last
            k = self.fed + math.ceil(ahead * self.fps)
            phase = self.phase or 0
            k += (phase - k) % self.gop
            return k, drawn + (k - idx) / self.fps

    def jump(self, why):
        """The frames stopped being one a tick: tell the destinations."""
        for d in list(self.dests):
            d.jump(why)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"enc {self.name}").start()
        return self

    def _run(self):
        while not self.stop.is_set():
            argv = encode_argv(self.codec, self.encoder, self.src, self.size, self.fps,
                               self.mbps)
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, **lowered(self.encoder))
            except OSError as e:
                log(f"{self.name}: cannot start ffmpeg: {e}")
                self.stop.wait(5)
                continue
            self.proc = proc
            with self.lock:
                # What it makes is counted from what it is fed from here.
                self.out = self.fed
                self.draws.clear()
            err = []
            threading.Thread(target=lambda: err.extend(proc.stderr.read().decode(
                "utf-8", "replace").splitlines()), daemon=True).start()
            reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
            reader.start()
            self._feed(proc)
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            reader.join(5)
            # A stop to the whole process group reaches ffmpeg as soon as
            # this, and is no restart.
            if self.stop.wait(0.5):
                break
            self.restarts += 1
            log(f"{self.name}: ffmpeg ended ({proc.returncode}): "
                f"{' / '.join(err[-3:]) or 'nothing said'}; starting it again")
            self.jump("its encoder started again")
            self.stop.wait(2)

    def _feed(self, proc):
        fd = proc.stdin.fileno()
        while not self.stop.is_set() and proc.poll() is None:
            try:
                planes, drawn = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.lock:
                self.draws.append(drawn)
                self.last = (self.fed, drawn)
                self.fed += 1
            mv = memoryview(planes)
            try:
                while mv:
                    n = os.write(fd, mv)
                    mv = mv[n:]
            except OSError:
                return

    def _read(self, proc):
        cut = cutter(self.codec)
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                data = b""
            self._hand(cut.feed(data) if data else cut.flush())
            if not data:
                return

    def _hand(self, pieces):
        """The encoder's output, cut, to every destination: each access
        unit given its index and the time its frame was drawn."""
        for chunk, start in pieces:
            if start is not None:
                with self.lock:
                    start.index = self.out
                    self.out += 1
                    start.drawn = self.draws.popleft() if self.draws else None
                    if start.key and self.phase is None:
                        self.phase = start.index % self.gop
            for d in list(self.dests):
                d.offer(chunk, start)

    def end(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()


# ------------------------------------------------------------- pulling

#: there.py made one program for the source host's python3, worked out
#: once.
_THERE = None


def there_line(verb, *args):
    """`there.py` as one command line for a POSIX host's shell, carried
    as `bitwall/relay.py` is: a few words that unpack the program from the
    next argument, and its arguments in base64, so nothing in them needs
    quoting."""
    global _THERE
    if _THERE is None:
        with open(os.path.join(HERE, "there.py"), encoding="utf-8") as f:
            code = f.read() + "\nsys.exit(main(__import__('json').loads(" \
                "__import__('base64').b64decode(sys.argv[2]))))\n"
        _THERE = base64.b64encode(zlib.compress(code.encode(), 9)).decode()
    arg = base64.b64encode(json.dumps([verb, *map(str, args)]).encode()).decode()
    return f'python3 -c "{bitwall.relay.BOOT}" {_THERE} {arg}'


def there_encodes(link):
    """`_encodes` for the source's host: every encoder the mirror might
    choose tried there in one ssh, the first time any is asked about."""
    names = list(dict.fromkeys([*HARDWARE["hevc"], *HARDWARE["h264"], *SOFTWARE.values()]))
    got = []
    lock = threading.Lock()

    def works(name):
        with lock:
            if not got:
                try:
                    r = subprocess.run(link.argv(there_line("probe", *names), compress=False),
                                       stdin=subprocess.DEVNULL, capture_output=True,
                                       text=True, timeout=120)
                    got.append(set(r.stdout.split()))
                except (OSError, subprocess.TimeoutExpired) as e:
                    log(f"{link.host}: cannot ask what it encodes with: {e}")
                    got.append(set())
            return name in got[0]
    return works


#: A record from there.py: its type and the bytes after it; and an F's or
#: a B's: drawn (µs), filled.
RECORD = struct.Struct("<cI")
RFRAME = struct.Struct("<QB")


class PullGroup(Group):
    """One encode run on the source's host, for a mirror that pulls
    (`--pull`): `there.py` there, through ssh, reads the painter stream
    `stream` the source session asked for, and ffmpeg there encodes it, so
    only the encoded stream crosses the network. What comes back is
    records: each frame given ffmpeg and when it was drawn on the source's
    clock, turned to this one's with the source's offset, and ffmpeg's
    output, cut and handed to the destinations as an encode here is. Run
    again, backing off, when it ends or the stream is asked for afresh."""

    def __init__(self, *args, stream, feed, **kw):
        super().__init__(*args, **kw)
        self.stream, self.feed = stream, feed
        self.wire = 0                   # bytes over ssh, since the summary
        self.name += f" on {feed.link.host}"

    def _run(self):
        link, s = self.feed.link, self.stream
        wait = 2.0
        while not self.stop.is_set():
            path = self.feed.path_of(s)
            if path is None:
                continue
            argv = encode_argv(self.codec, self.encoder, self.src, self.size, self.fps,
                               self.mbps, binary="ffmpeg")
            cmd = there_line("encode", path, s.size[0], s.size[1], f"{s.fps:g}", *argv)
            try:
                proc = subprocess.Popen(link.argv(cmd, extra=link.ALIVE, compress=False),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, **bitwall.portable.detached())
            except OSError as e:
                log(f"{self.name}: cannot start ssh: {e}")
                self.stop.wait(5)
                continue
            self.proc = proc
            with self.lock:
                # Its output is counted from what it is fed from here, its
                # first frame a keyframe; and nothing is timed on it until
                # it has been fed, which the ssh and ffmpeg starting there
                # puts seconds after the last frame of the one before.
                self.out = self.fed
                self.draws.clear()
                self.phase = self.fed % self.gop
                self.last = None
            err, said = [], []
            threading.Thread(target=lambda: err.extend(proc.stderr.read().decode(
                "utf-8", "replace").splitlines()), daemon=True).start()
            threading.Thread(target=self._watch, args=(proc, path), daemon=True).start()
            t0 = time.monotonic()
            self._records(proc, said)
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            with self.lock:
                self.last = None
            if self.stop.wait(0.5):
                break
            self.restarts += 1
            why = said[-1] if said else (" / ".join(err[-3:]) or f"ssh ended ({proc.returncode})")
            if s.path != path:
                why = "its stream was asked for again"
            if time.monotonic() - t0 > 60:
                wait = 2.0
            log(f"{self.name}: the encoder there ended: {why}; starting it again")
            self.jump("its encoder started again")
            self.stop.wait(wait)
            wait = min(wait * 2, 30.0)

    def _watch(self, proc, path):
        """Let the encoder there go when the stream it reads is gone or
        the mirror stops: its stdin ending ends it, ffmpeg and all."""
        while proc.poll() is None:
            if self.stop.is_set() or self.stream.path != path:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                return
            self.stop.wait(0.5)

    def _records(self, proc, said):
        r, s = proc.stdout, self.stream
        cut = cutter(self.codec)
        while True:
            head = r.read(RECORD.size)
            if len(head) < RECORD.size:
                break
            kind, n = RECORD.unpack(head)
            body = r.read(n) if n else b""
            if len(body) < n:
                break
            self.wire += RECORD.size + n
            if kind == b"D":
                self._hand(cut.feed(body))
            elif kind in (b"F", b"B"):
                us, filled = RFRAME.unpack(body)
                if filled:
                    s.dropped += 1
                else:
                    s.frames += 1
                if kind == b"B":
                    self.behind += 1
                    continue
                drawn = us / 1e6 - self.feed.offset
                with self.lock:
                    self.draws.append(drawn)
                    self.last = (self.fed, drawn)
                    self.fed += 1
            elif kind == b"J":
                s.jumps += 1
                self.jump("the source's frames jumped")
            elif kind == b"L":
                said.append(body.decode("utf-8", "replace"))
        self._hand(cut.flush())


# --------------------------------------------------------- destinations

class Want:
    """A layer being written: the layer, the index its stream is to start
    at (None for the next key), its `start` in ms on its wall's clock where
    it is timed, and the index it actually began at."""

    __slots__ = ("layer", "k", "start", "first")

    def __init__(self, layer, k=None, start=None):
        self.layer, self.k, self.start, self.first = layer, k, start, None


class Dest:
    """A destination: its layer, held open, opened again when lost and
    opened anew to be timed afresh, and a writer of its own behind a queue
    that is let go when it is full, the stream picked up at a keyframe.

    `opener(start)` makes the layer, not yet open, `start` the ms its
    stream's first frame is due at on the wall's clock or None."""

    def __init__(self, target, group, opener, stop, fps=None, lead=0.0, offset=0.0,
                 cap=None, describe=""):
        self.target, self.group, self.opener, self.stop = target, group, opener, stop
        self.fps, self.lead, self.offset = fps, lead, offset
        self.cap = cap or (1 << 20)
        self.describe = describe
        self.cond = threading.Condition()
        self.items = collections.deque()
        self.qbytes = 0
        self.want = None
        self.accepting = False      # whether its stream has begun, at a key
        self.skipping = False       # whether it is let go until a key
        self.taking = False         # whether the access unit coming is queued
        self.need = "first"
        self.not_before = 0.0
        self.opening = False
        self.budget = BUDGET
        self.backoff = 0.5
        self.down = None
        self.late = 0
        self.last_replan = 0.0
        self.layers = []                # every layer opened and not yet closed
        # What the summary says.
        self.sent = self.units = self.skipped = self.skips = 0
        self.replans = self.losses = 0
        self.delay = []
        self.margin = None

    def start(self):
        threading.Thread(target=self._manage, daemon=True,
                         name=f"dest {self.target}").start()
        threading.Thread(target=self._write, daemon=True,
                         name=f"write {self.target}").start()
        return self

    # -------------------------------------------------- from the encoder

    def offer(self, chunk, start):
        """A piece of the group's stream, queued for this destination's
        writer where it is wanted. Only whole access units are let go: a
        queue that is full keeps the rest of the one being written."""
        with self.cond:
            w = self.want
            if w is None:
                return
            if start is not None:
                if not self.accepting:
                    if not start.key or (w.k is not None and start.index < w.k):
                        self.taking = False
                        return
                    self.accepting = True
                    chunk = start.prefix + chunk
                elif self.skipping:
                    if not start.key or self.fps:
                        # Timed, it waits for the layer timed afresh.
                        self.taking = False
                        self.skipped += 1
                        return
                    chunk = start.prefix + chunk
                self.skipping = False
                self.taking = True
            elif not self.taking:
                return
            if self.qbytes + len(chunk) > self.cap and self.items and not self.skipping:
                first = next((k for k, it in enumerate(self.items) if it[2] is not None), None)
                if first is not None or start is not None:
                    kept = list(self.items)[:first] if first is not None else list(self.items)
                    self.skipped += sum(1 for it in self.items if it[2] is not None) \
                        - sum(1 for it in kept if it[2] is not None) + (start is not None)
                    self.items = collections.deque(kept)
                    self.qbytes = sum(len(it[1]) for it in kept)
                    self.taking = False
                self.skipping = True
                self.skips += 1
                if self.fps:
                    self._replan_locked("it fell behind", force=True)
                if not self.taking:
                    return
            self.items.append((w, chunk, start))
            self.qbytes += len(chunk)
            self.cond.notify_all()

    def jump(self, why):
        """The stream's frames are no longer one a tick from where its
        timing began: a timed layer is timed afresh."""
        if self.fps:
            with self.cond:
                self._replan_locked(why, force=True)

    # ---------------------------------------------------------- the layer

    def _replan_locked(self, why, force=False):
        now = time.monotonic()
        if self.need is not None:
            return
        if not force and now - self.last_replan < 5:
            return
        self.need = f"timed afresh: {why}"
        self.not_before = self.last_replan + 2 if now - self.last_replan < 2 else 0
        self.last_replan = now
        self.cond.notify_all()

    def _lost(self, layer, why):
        with self.cond:
            if self.want is not None and self.want.layer is layer:
                self.want = None
                self.items.clear()
                self.qbytes = 0
                self.accepting = self.taking = False
                if not self.opening and self.need is None:
                    self.need = why
                    self.not_before = 0
                self.cond.notify_all()
        self._close(layer)

    def _close(self, layer, at=None):
        """Close a layer, at the time given (this clock) or now, off the
        thread that asked."""
        def go():
            if at is not None:
                self.stop.wait(max(0.0, min(10.0, at - time.time())))
            try:
                layer.close()
            except Exception:                                   # noqa: BLE001
                pass
            with self.cond:
                if layer in self.layers:
                    self.layers.remove(layer)
        threading.Thread(target=go, daemon=True).start()

    def _manage(self):
        while True:
            with self.cond:
                while not self.stop.is_set() and (
                        self.need is None or time.monotonic() < self.not_before):
                    wait = None if self.need is None else self.not_before - time.monotonic()
                    self.cond.wait(wait if wait is None else max(0.01, wait))
                if self.stop.is_set():
                    return
                why = self.need
                self.opening = True
            try:
                self._open(why)
            finally:
                with self.cond:
                    self.opening = False

    def _open(self, why):
        k = start = None
        if self.fps:
            got = self.group.predict(self.budget)
            if got is None:
                self.stop.wait(0.2)
                return
            k, drawn = got
            start = int((drawn + self.lead + self.offset) * 1000)
        layer = self.opener(start)
        with self.cond:
            self.layers.append(layer)
        layer.when_lost(lambda: self._lost(layer, "lost its wall"))
        t0 = time.monotonic()
        try:
            layer.open(wait=OPEN_WAIT)
        except Exception as e:                                  # noqa: BLE001
            layer.stopped = layer.stopped or str(e)
        took = time.monotonic() - t0
        if not (layer.open_now and layer.has_data):
            said = layer.stopped or ("no data pipe" if layer.open_now else "no answer")
            if self.down != said:
                log(f"{self.target}: cannot open a layer: {said}; trying again, backing off")
                self.down = said
                self.losses += 1
            self._close(layer)
            with self.cond:
                self.not_before = time.monotonic() + self.backoff
                self.backoff = min(self.backoff * 2, 30.0)
            return
        self.budget = min(10.0, max(self.budget, 2 * took + 0.3))
        if k is not None and self.group.fed > k:
            log(f"{self.target}: its layer took {took:.1f}s to open, past its first frame; "
                f"again, {self.budget:.1f}s ahead")
            self._close(layer)
            return
        layer.meta(name="Mirror", description=self.describe)
        if self.down is not None or why != "first":
            log(f"{self.target}: layer {layer.z} open ({why})"
                + (f", first frame due {start / 1000 - self.offset - time.time():+.2f}s"
                   if start else ""))
        elif why == "first":
            log(f"{self.target}: layer {layer.z} open in {took:.1f}s"
                + (f", timed {self.lead:.2f}s behind the source" if self.fps else
                   ", each frame shown as it is decoded"))
        self.down = None
        self.backoff = 0.5
        with self.cond:
            if why.startswith("timed afresh"):
                self.replans += 1
            self.want = Want(layer, k, start)
            self.accepting = self.skipping = self.taking = False
            if self.need == why:
                self.need = None
            self.late = 0
            self.cond.notify_all()

    # --------------------------------------------------------- the writer

    def _write(self):
        current = None
        while True:
            with self.cond:
                while not self.items and not self.stop.is_set():
                    self.cond.wait(0.5)
                if self.stop.is_set():
                    return
                w, chunk, start = self.items.popleft()
                self.qbytes -= len(chunk)
            if w is not current:
                if current is not None and current.layer is not w.layer:
                    at = None if w.start is None else w.start / 1000 - self.offset
                    self._close(current.layer, at)
                current = w
            if start is not None and w.first is None:
                w.first = start.index
            if w.layer.data_write(chunk) is None:
                if not self.stop.is_set():
                    self._lost(w.layer, "its data pipe closed")
                current = None
                continue
            self.sent += len(chunk)
            if start is None:
                continue
            self.units += 1
            now = time.time()
            if start.drawn is not None and len(self.delay) < 10000:
                self.delay.append(now - start.drawn)
            if self.fps and w.start is not None:
                due = w.start / 1000 + (start.index - w.first) / self.fps
                margin = due - (now + self.offset)
                self.margin = margin if self.margin is None else min(self.margin, margin)
                if start.index == w.first and start.drawn is not None:
                    slip = start.drawn - (due - self.lead - self.offset)
                    if slip > SLIP:
                        with self.cond:
                            self._replan_locked(f"its first frame was drawn {slip:.2f}s "
                                                "after it was timed for", force=True)
                self.late = self.late + 1 if margin < 0 else 0
                if self.late >= max(3, self.fps / 2):
                    self.late = 0
                    with self.cond:
                        self._replan_locked("its frames reached it late")

    def summary(self):
        """A line of how it stands, and the counts begun again."""
        with self.cond:
            d = sorted(self.delay)
            line = (f"{self.target}: {self.units} frames, {self.sent / 1e6:.1f} MB"
                    + (f", sent {d[len(d) // 2] * 1000:.0f} ms after drawn" if d else "")
                    + (f", least margin {self.margin * 1000:+.0f} ms" if self.margin is not None
                       else "")
                    + (f", skipped {self.skipped} in {self.skips} falls behind" if self.skips
                       else "")
                    + (f", timed afresh {self.replans}x" if self.replans else "")
                    + ("" if self.want is not None else ", NOT OPEN"))
            self.units = self.sent = self.skipped = self.skips = self.replans = 0
            self.delay, self.margin = [], None
        return line

    def close(self):
        with self.cond:
            layers = list(self.layers)
            self.want = None
            self.cond.notify_all()
        for layer in layers:
            try:
                layer.data_end(wait=1.0)
                layer.close()
            except Exception:                                   # noqa: BLE001
                pass


class Placing:
    """Where a destination's layers go when `--place` does not say: on its
    base where nobody holds it, above the base where somebody does, never
    taking it from them. A painter whose base nobody holds dozes the
    display, layers and all, so a mirror above an empty base would not be
    seen. Asked again at every opening, since the base may have been taken
    or let go meanwhile; a base held by one of this mirror's own layers,
    open or still closing, is taken from it, as a layer timed afresh
    replaces the one before."""

    def __init__(self, target, holder):
        self.target = target
        self.where = "above" if holder else 0

    def choose(self, mine):
        """(place, take) for the next layer, `mine` the layers the
        destination has open."""
        if any(x.z == 0 and x.open_now for x in mine):
            return 0, True
        try:
            holder = base_holder(self.target)
        except Exception:                                       # noqa: BLE001
            return self.where, False
        if holder and holder.startswith(bitwall.session.session_name() + "-"):
            return 0, True
        where = "above" if holder else 0
        if where != self.where:
            log(f"{self.target}: " + (f"its base is held by {holder}; going above it"
                                      if holder else "its base is free; taking it"))
            self.where = where
        return where, False


# --------------------------------------------------------------- source

class Stream:
    """One of the painter's glass streams: its name (None for the one a
    painter that runs one stream is asked for), its size and rate, the
    encodes it feeds, and its fifo while the stream runs."""

    def __init__(self, name, size, fps):
        self.name, self.size, self.fps = name, tuple(size), fps
        self.groups = []
        self.path = None
        self.refused = None             # why the source last said no, while it does
        self.frames = self.dropped = self.filled = self.jumps = 0

    def ask(self):
        """The words it is asked for with."""
        return (f"mirror glass stream=i420 size={self.size[0]}x{self.size[1]} "
                f"fps={self.fps:g}" + (f" name={self.name}" if self.name else ""))

    def fifo(self):
        return "glass" + (f"-{self.name}" if self.name else "")

    def __str__(self):
        return f"{self.size[0]}x{self.size[1]} {self.fps:g}fps"


def plan_streams(wanted, offered):
    """The painter's streams for encodes wanting `wanted`, a list of
    (size, fps): one for each where the source runs that many at once
    (`offered`, as `ask` says), named for its size and rate; otherwise
    one, unnamed, at the largest size and the highest rate wanted, as a
    painter that runs one stream at a time is asked. A dict of each
    (size, fps) to its Stream."""
    wanted = list(dict.fromkeys((tuple(s), f) for s, f in wanted))
    if 1 < len(wanted) <= offered:
        return {(s, f): Stream(f"{s[0]}x{s[1]}-{f:g}", s, f) for s, f in wanted}
    size = max((s for s, _ in wanted), key=lambda s: s[0] * s[1])
    one = Stream(None, size, max(f for _, f in wanted))
    return {k: one for k in wanted}


#: What `Source._open` says of a named pipe read here: no process, and
#: its end of file the end of the stream.
_PIPE = object()


class Source:
    """The source wall: a controller session asking for its glass as raw
    streams, asked again whenever the session is lost, and a reader for
    each stream's fifo handing each frame to every encode of it. The
    session carries the mirror's instance, which is how a controller finds
    the mirror on its source. A stream added while it runs is asked for
    on the session already open, by the thread that holds it, since only
    that thread reads the session; one the source refuses is asked again
    every few seconds, the others running on."""

    #: Seconds between asks for a stream the source refused.
    AGAIN = 5.0

    def __init__(self, target, streams, stop, offset=0.0, instance=None):
        self.target, self.streams, self.stop = target, list(streams), stop
        self.offset, self.instance = offset, instance
        self.link = bitwall.session.Link(target)
        self.c = None
        self.glock = threading.Lock()
        self.cond = threading.Condition()
        self.pending = []               # streams added, not yet asked for
        self.started = False
        #: Whether each stream is read where it is drawn, by its encoder
        #: there (`PullGroup`), rather than here.
        self.pull = False

    def add(self, stream, group):
        with self.glock:
            stream.groups.append(group)

    def add_stream(self, stream):
        """A stream more, asked for on the session open now or at the
        next."""
        with self.cond:
            self.streams.append(stream)
            self.pending.append(stream)
            started = self.started
            self.cond.notify_all()
        if started:
            self._reader(stream)

    def every(self):
        with self.cond:
            return list(self.streams)

    def path_of(self, stream):
        """The fifo of `stream` once the source session has asked for it,
        None at the stop."""
        with self.cond:
            while stream.path is None and not self.stop.is_set():
                self.cond.wait(0.5)
            return stream.path

    def _reader(self, s):
        if self.pull:
            return
        threading.Thread(target=self._read, args=(s,), daemon=True,
                         name=f"glass {s}").start()

    def start(self):
        with self.cond:
            self.started = True
            streams = list(self.streams)
        threading.Thread(target=self._held, daemon=True, name="source").start()
        for s in streams:
            self._reader(s)
        return self

    def _asked(self, c, s, again):
        """Ask for one stream on `c`; whether the source took it."""
        errors = [x.raw for x in c.ask(s.ask(), timeout=1.5)
                  if getattr(x, "name", None) == "error"]
        if errors:
            said = f"{s.ask()!r} refused: {errors[0]}"
            if said != s.refused:
                log(f"{self.target}: {said}; asking again, backing off")
                s.refused = said
            return False
        s.refused = None
        with self.cond:
            if s in self.pending:
                self.pending.remove(s)
            s.path = posixpath.join(self.link.session_dir(c.name), s.fifo())
            self.cond.notify_all()
        log(f"{self.target}: streaming its glass at {s} on {c.name}"
            + (f" as {s.fifo()}" if s.name else "") + again)
        return True

    def _hold(self):
        wait, down = 0.5, None
        while not self.stop.is_set():
            try:
                c = bitwall.Controller(self.target, instance=self.instance).open()
            except Exception as e:                              # noqa: BLE001
                if down != str(e):
                    log(f"{self.target}: no session: {e}; trying again, backing off")
                    down = str(e)
                self.stop.wait(wait)
                wait = min(wait * 2, 30.0)
                continue
            self.c = c
            with self.cond:
                streams = list(self.streams)
                self.pending.clear()
            errors = []
            for s in streams:
                errors += [(s, x.raw) for x in c.ask(s.ask(), timeout=1.5)
                           if getattr(x, "name", None) == "error"]
            if errors:
                said = f"{errors[0][0].ask()!r} refused: {errors[0][1]}"
                if said != down:
                    log(f"{self.target}: {said}; asking again, backing off")
                    down = said
                c.close()
                self.c = None
                self.stop.wait(wait)
                wait = min(wait * 2, 30.0)
                continue
            # Where the session is, from the run directory the controller
            # session already found; asked afresh only where it has none,
            # and a host that cannot say just now is asked again.
            if self.link._run is None and c.link._run is not None:
                self.link._run = c.link._run
            try:
                where = self.link.session_dir(c.name)
            except Exception as e:                              # noqa: BLE001
                said = f"cannot find its session: {e}"
                if said != down:
                    log(f"{self.target}: {said}; asking again, backing off")
                    down = said
                c.close()
                self.c = None
                self.stop.wait(wait)
                wait = min(wait * 2, 30.0)
                continue
            again = " again" if down else ""
            down, wait = None, 0.5
            with self.cond:
                for s in streams:
                    s.path = posixpath.join(where, s.fifo())
                    log(f"{self.target}: streaming its glass at {s} on {c.name}"
                        + (f" as {s.fifo()}" if s.name else "") + again)
                self.cond.notify_all()
            if not streams:
                log(f"{self.target}: session {c.name} open{again}; no stream asked yet")
            retry = 0.0
            while not self.stop.is_set() and c.state == "open":
                c.read(timeout=0.5)
                with self.cond:
                    todo = list(self.pending) if time.monotonic() >= retry else []
                for s in todo:
                    if not self._asked(c, s, ""):
                        retry = time.monotonic() + self.AGAIN
            with self.cond:
                for s in self.streams:
                    s.path = None
            if self.stop.is_set():
                break
            down = f"lost ({c.closed or c.state})"
            log(f"{self.target}: its session was {down}; asking again")
            c.close()
            self.c = None

    def _held(self):
        """The source held; anything it did not expect ends the mirror with
        a line that says so, rather than leaving it to encode nothing."""
        try:
            self._hold()
        except Exception as e:                                  # noqa: BLE001
            log(f"error: {self.target}: the source failed: {e}; stopping")
            self.stop.set()

    def _open(self, stream, path):
        """The glass's reading end, and the process carrying it where the
        source is elsewhere: a fifo here opened as it is; a named pipe
        here (Windows) through `local.py`, its end the stream's end, since
        the painter lets a reader go for the next; elsewhere `cat`, or
        `relay read` where the host is Windows."""
        if self.link.here:
            try:
                return bitwall.local.open_read(path, 5.0), _PIPE
            except OSError:
                return None, None
        if self.link.local:
            for _ in range(100):
                try:
                    return os.open(path, os.O_RDONLY | os.O_NONBLOCK), None
                except FileNotFoundError:
                    if self.stop.wait(0.05) or stream.path != path:
                        break
            return None, None
        argv = (self.link.relay_argv("read", path, compress=False) if _windows(self.link)
                else self.link.argv(f"exec cat {shlex.quote(path)}", compress=False))
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        fd = proc.stdout.fileno()
        if os.name == "posix":
            os.set_blocking(fd, False)
        return fd, proc

    def _read(self, stream):
        last = None             # (number, planes, drawn) of the last frame
        complained = None
        while not self.stop.is_set():
            with self.cond:
                while stream.path is None and not self.stop.is_set():
                    self.cond.wait(0.5)
                path = stream.path
            if path is None:
                continue
            fd, proc = self._open(stream, path)
            if fd is None:
                continue
            try:
                while not self.stop.is_set() and stream.path == path:
                    try:
                        got = read_frame(fd, self.stop)
                    except Broken as e:
                        # A frame cut short by the stream being turned off
                        # is no news.
                        if complained != str(e) and not self.stop.is_set() \
                                and stream.path == path:
                            log(f"{self.target}: {stream}: {e}; reading it again")
                            complained = str(e)
                        break
                    if got is None:
                        if proc is _PIPE or proc is not None and proc.poll() is not None:
                            break
                        self.stop.wait(0.02)
                        continue
                    last = self._frame(stream, got, last)
            finally:
                if proc is not None and proc is not _PIPE:
                    proc.kill()
                    proc.wait()
                else:
                    os.close(fd)

    def _frame(self, stream, got, last):
        w, h, n, us, planes = got
        if (w, h) != stream.size:
            return last
        stream.frames += 1
        drawn = us / 1e6 - self.offset
        with self.glock:
            groups = list(stream.groups)
        if last is not None:
            gap = n - last[0] - 1
            if n <= last[0] or gap > stream.fps:
                stream.jumps += 1
                for g in groups:
                    g.jump("the source's frames jumped")
            elif gap > 0:
                stream.dropped += gap
                stream.filled += gap
                for j in range(1, gap + 1):
                    for g in groups:
                        g.put(last[1], last[2] + j / stream.fps)
        for g in groups:
            g.put(planes, drawn)
        return n, planes, drawn

    def end(self):
        with self.cond:
            for s in self.streams:
                s.path = None
        c = self.c
        if c is not None:
            try:
                c.send("mirror glass off")
            except Exception:                                   # noqa: BLE001
                pass
            c.close()


# ----------------------------------------------------------------- main

def say_offset(t, off):
    if off is None:
        log(f"{t}: did not say its time; its clock taken to be this one's")
    elif off:
        log(f"{t}: clock {off * 1000:+.0f} ms against this one")


class Mirror:
    """The destinations and what feeds them: the encodes (`groups`, by
    codec, encoder, size and rate), the painter's streams by the size and
    rate each is wanted at, and every destination made, all guarded by
    `lock`, since a destination that could not be asked at the start is
    added while the rest run.

    A destination that cannot be asked is waited for, asked again backing
    off from a second to half a minute, since a wall out of reach is the
    network and not an answer; one that answers but takes nothing this
    can send is refused for good. A destination added late joins the
    encode of its codec, size and rate where there is one, and its
    painter stream where there is one; else a new stream is asked for
    where the source runs another, or one already running at its size and
    rate or more is scaled down, and where none can be it is refused. It
    plays at the lead the others play at, even where it wants more, and
    the log says so: timing the others afresh would break every picture
    already up for one wall coming late."""

    def __init__(self, args, source, src, place, instance, stop):
        self.args, self.source, self.src = args, source, src
        self.place, self.instance, self.stop = place, instance, stop
        self.enc = Encoders()
        self.force = None if args.codec == "auto" else args.codec
        self.most = args.size or CAP
        #: One for every destination; None until the first is chosen,
        #: where `--lead` does not say.
        self.lead = args.lead
        self.lock = threading.Lock()
        self.groups = {}
        self.streams = {}               # (size, fps) -> Stream
        self.made = []
        self.feed = None                # the Source
        self.live = False               # whether what is added is started at once
        #: Whether each encode runs on the source's host (`--pull`).
        self.pull = args.pull
        if self.pull:
            self.enc = Encoders(there_encodes(bitwall.session.Link(source)))

    def choose(self, t, got, want):
        """What `t`, which said `got`, is sent: (codec, encoder, size, fps,
        the lead it wants, its base's holder), or None where it is
        refused, the log saying why either way."""
        codec, name, why = pair(got["codecs"], got["hw"], self.enc, self.force)
        if self.pull:
            why = why.replace(" here", f" on {self.source.partition(':')[0]}")
        if name is None:
            log(f"{t}: refused: {why}")
            return None
        size = size_for(self.src["pixels"], got["pixels"], want[0] or self.most)
        fps = want[1] or self.args.fps
        threads = got["decoder"] if codec in got["hw"] else got["software"]
        wanted = default_lead(codec, size, got["hw"], threads, name)
        glass = f"{got['pixels'][0]}x{got['pixels'][1]}" if got["pixels"] else "unsaid"
        log(f"{t}: {codec} at {size[0]}x{size[1]} {fps:g}fps (its glass {glass}): {why}"
            + ("" if self.args.lead is not None else
               f"; wants a lead of {wanted:g}s (decoder threads "
               f"{threads if threads is not None else 'unsaid'})"))
        if self.place == "auto":
            log(f"{t}: " + (f"above its base, which {got['base']} holds" if got["base"]
                            else "on its base, which nobody holds"))
        return codec, name, size, fps, wanted, got["base"]

    def _stream(self, size, fps, codec=None):
        """The painter stream a destination at `size` and `fps` is fed
        from, asked for where it is new: (stream, None), or (None, why)
        where there is none. Under `lock`. Pulling, each encode reads a
        stream of its own on the source's host, so there is one a codec,
        size and rate, and none is scaled down for another."""
        key = (tuple(size), fps) + ((codec,) if self.pull else ())
        if key in self.streams:
            return self.streams[key], None
        every = list(dict.fromkeys(self.streams.values()))
        offered = self.src["streams"]
        if len(every) < offered:
            s = Stream((f"{codec}-" if self.pull else "") + f"{size[0]}x{size[1]}-{fps:g}"
                       if offered > 1 else None, size, fps)
            self.feed.add_stream(s)
        elif self.pull:
            return None, (f"the source runs {offered} stream{'s' if offered != 1 else ''} "
                          f"at once, and pulling, each encode reads one of its own")
        else:
            s = next((x for x in every if x.size[0] >= size[0] and x.size[1] >= size[1]
                      and x.fps >= fps), None)
            if s is None:
                return None, (f"the source runs {offered} stream{'s' if offered != 1 else ''}"
                              f" at once, at {', '.join(str(x) for x in every)}, none of them "
                              f"{size[0]}x{size[1]} {fps:g}fps or more")
        self.streams[key] = s
        return s, None

    def add(self, t, choice, offset):
        """A destination chosen, made, and started where the rest are."""
        codec, name, size, fps, wanted, holder = choice
        args = self.args
        with self.lock:
            if self.lead is None:
                self.lead = wanted
                log((f"lead {wanted:g}s" if wanted else "each frame shown as decoded")
                    + f" (auto: {t})")
            elif args.lead is None and wanted > self.lead:
                log(f"{t}: wants a lead of {wanted:g}s, more than the {self.lead:g}s the "
                    f"others play at; it plays at {self.lead:g}s and may drop frames "
                    f"(restart the mirror, or give --lead {wanted:g}, for more)")
            lead = self.lead
            stream, why = self._stream(size, fps, codec)
            if stream is None:
                log(f"{t}: refused: {why}")
                return
            key = (codec, name, size, fps)
            g = self.groups.get(key)
            new = g is None
            if new:
                mbps = args.bitrate if args.bitrate is not None \
                    else default_mbps(codec, size, fps)
                if self.pull:
                    g = PullGroup(codec, name, size, stream.size, fps, mbps, self.stop,
                                  src_fps=stream.fps, stream=stream, feed=self.feed)
                else:
                    g = Group(codec, name, size, stream.size, fps, mbps, self.stop,
                              src_fps=stream.fps)
                self.groups[key] = g
            d = self._dest(t, g, codec, size, lead, holder, offset)
            g.dests.append(d)
            self.made.append(d)
            if self.live:
                if new:
                    log(f"encoding {g.name} with {g.encoder} at {g.mbps:.1f} Mb/s for {t}")
                    g.start()
                else:
                    log(f"{t}: joins the encode of {g.name}")
            if new:
                self.feed.add(stream, g)
            if self.live:
                d.start()

    def _dest(self, t, g, codec, size, lead, holder, offset):
        place, instance = self.place, self.instance
        timed = g.fps if lead > 0 else None
        placing = Placing(t, holder) if place == "auto" else None
        made_for = []

        def opener(start):
            if placing is None:
                at, take = place, place == 0
            else:
                with made_for[0].cond:
                    mine = list(made_for[0].layers)
                at, take = placing.choose(mine)
            return bitwall.video.Layer(t, place=at, take=take, data=True,
                                       codec=codec, fps=timed,
                                       start=start if timed else None, instance=instance)
        d = Dest(t, g, opener, self.stop, fps=timed, lead=lead, offset=offset,
                 cap=int(g.mbps * 1e6 / 8 * QUEUE_SECS) + (256 << 10),
                 describe=f"{self.args.target}'s glass, {codec} {size[0]}x{size[1]}")
        made_for.append(d)
        return d

    def wait_for(self, t, want, why):
        """Ask `t` again, backing off, until it answers; then choose for it,
        measure its clock and add it. Said only when the reason changes,
        and when it answers."""
        wait = 1.0
        while not self.stop.wait(wait):
            wait = min(wait * 2, 30.0)
            try:
                got = ask(t)
            except Exception as e:                              # noqa: BLE001
                if str(e) != why:
                    why = str(e)
                    log(f"{t}: still cannot ask it: {why}")
                continue
            log(f"{t}: answered")
            choice = self.choose(t, got, want)
            if choice is None or self.stop.is_set():
                return
            off = clock_offset(t)
            say_offset(t, off)
            if not self.stop.is_set():
                self.add(t, choice, off or 0.0)
            return


def parse_auto(text):
    """A number of seconds or Mb/s, or `auto` for the default, as None."""
    if text == "auto":
        return None
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a number, or auto: {text!r}") from None


def parse_size(text):
    w, _, h = (text or "").lower().partition("x")
    if not (w.isdigit() and h.isdigit() and int(w) >= 16 and int(h) >= 16):
        raise argparse.ArgumentTypeError(f"a size is WxH, as 1280x720: {text!r}")
    return int(w), int(h)


def parse_dest(text):
    """A destination as `--to` names it, (target, size, fps): `host:wall`,
    and after `@` the most it is sent and its rate, `WxH`, `WxH/fps` or
    `/fps`, each None where it is not said. An `@` followed by anything
    else is part of the target, as in `pi@pi.local`."""
    target, at, spec = text.rpartition("@")
    size_s, slash, fps_s = spec.partition("/")
    looks = bool(size_s or slash) and (not size_s or "x" in size_s.lower()) \
        and all(x.isdigit() for x in size_s.lower().split("x") if size_s)
    if not at or not target or not looks:
        return text, None, None
    size = parse_size(size_s) if size_s else None
    fps = None
    if slash:
        if not fps_s.isdigit() or not 1 <= int(fps_s) <= 60:
            raise argparse.ArgumentTypeError(f"a destination's fps is 1 to 60: {text!r}")
        fps = int(fps_s)
    return target, size, fps


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", required=True,
                    help="the walls that show it, by commas, each with what it is sent "
                         "if not --size and --fps: pi.local:0@1920x1080/15,pi-b.local:1")
    ap.add_argument("--size", type=parse_size,
                    help=f"the most a destination is sent, WxH (default its glass, "
                         f"at most {CAP[0]}x{CAP[1]})")
    ap.add_argument("--fps", type=float, default=25, help="frames a second (default 25)")
    ap.add_argument("--lead", type=parse_auto, default=None,
                    help=f"seconds behind the source each frame is shown, one for every "
                         f"destination; 0 shows each as it is decoded (default auto: the "
                         f"most any wants, {LEAD:g}; {LEAD_HELD:g} encoding with "
                         f"VideoToolbox or to a decoder of more than {HELD_THREADS} "
                         f"threads; {LEAD_SLOW:g} above 720p on one decoding "
                         f"H.264 and not HEVC in hardware, as a Pi 3)")
    ap.add_argument("--codec", choices=("auto", "h264", "mjpeg"), default="auto",
                    help="what to send (default auto: what each destination decodes "
                         "in hardware)")
    ap.add_argument("--bitrate", type=parse_auto, default=None,
                    help=f"Mb/s sent, at the size and rate sent (default auto: "
                         f"{RATE:g} x (w x h x fps) / ({RATE_AT[0]} x {RATE_AT[1]} x "
                         f"{RATE_AT[2]}), at least 0.3, three times that for mjpeg; "
                         f"about 6.75 at 1080p25, 4 at 1080p15)")
    ap.add_argument("--place", default="auto",
                    help="where on each destination: above, top, or a number; 0 takes "
                         "its base from whoever holds it (default auto: its base where "
                         "nobody holds it, else above)")
    ap.add_argument("--pull", action="store_true",
                    help="run here, on a destination's host, and encode on the source's "
                         "host through ssh, so only the encoded stream crosses the network "
                         "(default: run on the source's host)")
    ap.add_argument("target", help="the source wall, host:wall")
    args = ap.parse_args()
    if not 1 <= args.fps <= 60:
        ap.error("--fps is 1 to 60")
    if args.lead is not None and args.lead < 0:
        ap.error("--lead is 0 or more")
    if args.bitrate is not None and args.bitrate <= 0:
        ap.error("--bitrate is more than 0")
    place = int(args.place) if args.place.isdigit() else args.place
    if place not in ("auto", "above", "top") and not isinstance(place, int):
        ap.error("--place is auto, above, top or a number")

    stop = threading.Event()
    # SIGBREAK is Ctrl+Break, or CTRL_BREAK_EVENT, on Windows.
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())
    # One instance for the source's controller session and every layer
    # this opens; a mirror is not moved.
    instance = os.environ.pop("BITWALL_INSTANCE", None)
    os.environ.pop("BITWALL_RECORD", None)

    source = here(args.target)
    dests, wants = [], {}
    for x in (x.strip() for x in args.to.split(",")):
        if not x:
            continue
        try:
            t, size, fps = parse_dest(x)
        except argparse.ArgumentTypeError as e:
            ap.error(str(e))
        t = here(t)
        if t not in dests:
            dests.append(t)
            wants[t] = (size, fps)
    if source in dests:
        log(f"{source} is the source; it is not shown on itself")
        dests.remove(source)
    if not dests:
        log("no destination")
        return 1
    if args.pull:
        link = bitwall.session.Link(source)
        if link.local:
            log(f"{source}: --pull encodes on the source's host, and it is this one; "
                "run without it")
            return 1
        if _windows(link):
            log(f"{source}: --pull from a Windows host is not done; run the mirror there")
            return 1

    try:
        src = ask(source)
    except Exception as e:                                      # noqa: BLE001
        log(f"{source}: cannot ask it what it is: {e}")
        return 1
    if "stream" not in src["words"]:
        log(f"{source}: does not stream its glass ({src['receiver'] or 'its receiver'} "
            f"does not say `stream` in caps); nothing to mirror")
        return 1
    if src["pixels"] is None:
        log(f"{source}: did not say its glass")
        return 1
    log(f"{source}: the source, {src['pixels'][0]}x{src['pixels'][1]} "
        f"({src['receiver'] or 'a receiver'}); to {', '.join(dests)}")

    m = Mirror(args, source, src, place, instance, stop)
    answered, waiting = {}, {}

    def look(t):
        try:
            answered[t] = ask(t)
        except Exception as e:                                  # noqa: BLE001
            waiting[t] = str(e)
    threads = [threading.Thread(target=look, args=(t,)) for t in dests]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    chosen = {}
    for t in dests:
        if t in waiting:
            log(f"{t}: cannot ask it yet: {waiting[t]}; asking again, backing off")
            continue
        choice = m.choose(t, answered[t], wants[t])
        if choice is not None:
            chosen[t] = choice
    if not chosen and not waiting:
        log("no destination can be sent the glass")
        return 1
    if chosen:
        # One lead for every destination, so they show each frame together:
        # the most any of them wants.
        if args.lead is None:
            m.lead = max(c[4] for c in chosen.values())
        log((f"lead {m.lead:g}s" if m.lead else "each frame shown as decoded")
            + (" for every destination" if len(chosen) > 1 else "")
            + ("" if args.lead is not None else
               " (auto: " + ", ".join(t for t, c in chosen.items() if c[4] == m.lead) + ")"))
    else:
        log("no destination answered yet; waiting for " + ", ".join(waiting))

    # A painter stream for each size and rate sent, or one for them all;
    # pulling, one an encode, asked as each is added.
    streams = plan_streams([(c[2], c[3]) for c in chosen.values()], src["streams"]) \
        if chosen and not args.pull else {}
    every = list(dict.fromkeys(streams.values()))
    if len(every) > 1:
        log(f"{source}: runs {src['streams']} streams at once; asking "
            + ", ".join(str(s) for s in every))
    elif len(streams) > 1:
        log(f"{source}: runs {src['streams']} stream{'s' if src['streams'] != 1 else ''} at "
            f"once; asking one at {every[0]}, each encode taking what it wants of it")
    offsets = {}

    def offset_of(t):
        offsets[t] = clock_offset(t)
    threads = [threading.Thread(target=offset_of, args=(t,)) for t in [source, *chosen]]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    for t, off in offsets.items():
        say_offset(t, off)

    m.streams = dict(streams)
    m.feed = Source(source, every, stop, offsets.get(source) or 0.0, instance=instance)
    m.feed.pull = args.pull
    for t, choice in chosen.items():
        m.add(t, choice, offsets.get(t) or 0.0)
    with m.lock:
        groups = list(m.groups.values())
    for g in groups:
        log(f"encoding {g.name} with {g.encoder} at {g.mbps:.1f} Mb/s for "
            f"{', '.join(d.target for d in g.dests)}")
        g.start()
    m.feed.start()
    with m.lock:
        m.live = True
        made = list(m.made)
    for d in made:
        d.start()
    for t in waiting:
        threading.Thread(target=m.wait_for, args=(t, wants[t], waiting[t]), daemon=True,
                         name=f"waiting {t}").start()

    # Waited for a second at a time, since a wait on Windows is not cut
    # short by a signal.
    while not any(stop.wait(1) for _ in range(int(SUMMARY))):
        with m.lock:
            groups, made = list(m.groups.values()), list(m.made)
        if not made:
            continue
        every = m.feed.every()
        behind = ", ".join(f"{g.name} {g.behind}" for g in groups if g.behind)
        for s in every:
            log(f"{source}: {s.frames} frames in" + (f" at {s}" if len(every) > 1 else "")
                + (f", {s.dropped} let go by the painter and filled" if s.dropped else "")
                + (f", {s.jumps} jumps" if s.jumps else ""))
            s.frames = s.dropped = s.jumps = 0
        if behind:
            log(f"encoders behind by {behind} frames")
        for g in groups:
            g.behind = 0
            if isinstance(g, PullGroup):
                log(f"{g.name}: {g.wire / SUMMARY / 1e3:.0f} kB/s over ssh")
                g.wire = 0
        for d in made:
            log(d.summary())

    log("stopping")
    m.feed.end()
    with m.lock:
        groups, made = list(m.groups.values()), list(m.made)
    for g in groups:
        g.end()
    for d in made:
        d.close()
    log("stopped")
    return 0

if __name__ == "__main__":
    sys.exit(main())
