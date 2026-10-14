#!/usr/bin/env python3
"""HDMI in: what a capture device sees, on a wall.

    senders/hdmi/main.py pi.local:1
    senders/hdmi/main.py --rotate 180 --fps 30 pi.local:1

A USB HDMI capture dongle is a webcam to Linux, and one with the MS2130
gives 1080p as MJPEG at up to 60 frames a second, or raw at 30. Whatever is
plugged into it -- a laptop, an iPad, a Pi's own screen -- goes on the
wall as JPEGs on a video layer, each scaled by the wall to fit its glass.

ffmpeg reads the dongle's MJPEG and has one job: encode every frame small,
for motion. Beside that it copies the dongle's own JPEGs out untouched,
which costs nothing. One encoded output and a copy is all it is given,
because a second encoded output makes it hand its frames over in bunches,
six at once five times a second, and a video that moves in steps. Nor is
it asked to drop frames that did not change: its filters for that hold
the newest frame back until a different one comes, so a slide that stands
still never leaves it and the wall stays a slide behind.

So what changed is decided here. Each quick frame is looked at as it
arrives, decoded at an eighth of its size in grey (a few milliseconds:
JPEG can be read that small directly), and sent only where it differs from
the picture on the wall. A still slide costs nothing on the wire, a change
goes the moment it is captured, and a video goes at the rate it plays.

Text wants a fine JPEG, motion a small one, and they are never wanted at
once. Once the picture has stood still for SETTLE, the dongle's own JPEG
of it is sharpened (`--sharpen`: a capture dongle softens text a little on
its way through), encoded with its colour whole, since coloured text on
colour is what halving the colour smears, and sent to stay. That is a
fifth of a second of work, done once a change in a thread of its own, so
nothing waits for it.

Each stream read from ffmpeg is kept one deep: a wall that falls behind
gets the newest picture when it has room, never a queue of old ones, so
the delay stays a frame or two whatever the wall can take. The picture on
the wall is sent again whenever the wall has lost it.

The catalogue entry, `catalogue.json` here, belongs only on the host the
dongle is plugged into, as `~/.bitwall/senders/hdmi.json`, not in the
catalogue every host shares: a host without one would offer it and fail.

The layer is the base unless a controller says otherwise in
`BITWALL_PLACE`, so the input takes the wall as a television's would.
Nothing is captured while the wall says nobody is watching.

Whether anything is plugged in is said on the layer's session as meta,
`signal yes` or `signal no`, said when the judgement changes and again
whenever the layer opens, so a controller knows from the wall's `report`
(`meta set signal no` for the base, `meta <session> set signal no` for a
layer) or by following `said`, without taking pictures. It is judged on
the same eighth-size grey glance as a change is: with nothing plugged in
the MS2130 sends one flat near-black frame, so a picture with no
variation (DARK_SPREAD) and no brightness (DARK_MEAN) is no signal. A
laptop showing black looks the same, so a signal once seen lasts until
the picture has been flat for `--signal-hold` seconds on end (300), while
a picture seen is a signal at once. While nothing is captured, because
nobody is watching, the last judgement stands.
"""
import argparse
import glob
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from io import BytesIO

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import bitwall                                                     # noqa: E402

#: How the picture is turned: as ffmpeg filters for the quick frames, and
#: as a PIL transpose for the still one, made from the dongle's JPEG.
TURNS = {0: ([], None), 90: (["transpose=clock"], Image.Transpose.ROTATE_270),
         180: (["hflip", "vflip"], Image.Transpose.ROTATE_180),
         270: (["transpose=cclock"], Image.Transpose.ROTATE_90)}

#: Where ffmpeg writes the copy of the dongle's JPEGs, made a pipe at start.
COPY_PIPE = "pipe:copy"

#: How long the picture stands still before its fine frame is sent.
SETTLE = 0.25

#: A frame is new where this many pixels of its eighth-size grey picture
#: differ from the one on the wall by more than DIFF levels. The capture's
#: own noise is one or two; a pointer moved is several.
DIFF, PIXELS = 16, 3

#: How often the held picture is sent again, in seconds, so a wall that
#: came back without saying so is never left blank for long.
RESEND = 10

#: A glance whose grey levels spread less than this, and average less than
#: DARK_MEAN, is the flat near-black frame a dongle sends with nothing in it.
DARK_SPREAD, DARK_MEAN = 2.0, 20

#: How long leaving may take before the sender simply goes.
LEAVING = 4


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def find_device():
    """The first capture device on USB, as its stable by-id path."""
    found = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
    return found[0] if found else None


class Stream:
    """One of ffmpeg's outputs, cut into JPEGs, the newest kept."""

    def __init__(self, cond):
        self.cond = cond
        self.latest = None
        self.count = 0
        self.at = 0.0               # when the newest arrived, monotonic
        self.gaps = []              # ms between arrivals, for --debug

    def read(self, pipe):
        """Cut a stream into JPEGs at each end-of-image marker. Neither
        ffmpeg's encoder nor the dongle writes thumbnails, and
        entropy-coded data never holds the marker, so each FFD9 ends a
        picture. The search starts where the last one left off."""
        buf = bytearray()
        looked = 0
        while True:
            chunk = pipe.read(1 << 16)
            if not chunk:
                break
            buf += chunk
            while True:
                end = buf.find(b"\xff\xd9", max(0, looked - 1))
                if end < 0:
                    looked = len(buf)
                    break
                start = buf.find(b"\xff\xd8")
                jpeg = bytes(buf[start:end + 2]) if 0 <= start < end else None
                del buf[:end + 2]
                looked = 0
                if jpeg:
                    with self.cond:
                        now = time.monotonic()
                        if self.at:
                            self.gaps.append(1000 * (now - self.at))
                        self.latest, self.at = jpeg, now
                        self.count += 1
                        self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()


class Capture:
    """ffmpeg reading the device: the quick frames on its stdout, and a
    copy of the dongle's own JPEGs on a pipe of its own."""

    def __init__(self, argv):
        self.argv = argv
        self.cond = threading.Condition()
        self.quick = self.copy = None
        self.proc = None
        self.tail = []

    def start(self):
        self.quick, self.copy = Stream(self.cond), Stream(self.cond)
        self.tail = []
        r, w = os.pipe()
        argv = [f"pipe:{w}" if x == COPY_PIPE else x for x in self.argv]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, pass_fds=(w,))
        os.close(w)
        threading.Thread(target=self.quick.read, args=(self.proc.stdout,), daemon=True).start()
        threading.Thread(target=self.copy.read, args=(os.fdopen(r, "rb", buffering=0),),
                         daemon=True).start()
        threading.Thread(target=self._errors, args=(self.proc,), daemon=True).start()

    def _errors(self, proc):
        for line in proc.stderr:
            self.tail = (self.tail + [line.decode("utf-8", "replace").rstrip()])[-8:]

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


class Fine:
    """The still picture made fine, in a thread of its own: the dongle's
    JPEG decoded, turned, sharpened and encoded with its colour whole."""

    def __init__(self, turn, sharpen, quality):
        self.turn, self.sharpen, self.quality = turn, sharpen, quality
        self.lock = threading.Lock()
        self.busy = False
        self.made = None            # (the change it is of, its JPEG)

    def ask(self, change, jpeg):
        """Start making the fine frame of `change` from `jpeg`, unless one
        is being made already."""
        with self.lock:
            if self.busy:
                return
            self.busy = True
        threading.Thread(target=self._make, args=(change, jpeg), daemon=True).start()

    def take(self, change):
        """The fine frame of `change`, once it is made; None before."""
        with self.lock:
            if self.made and self.made[0] == change:
                made, self.made = self.made[1], None
                return made
            return None

    def _make(self, change, jpeg):
        out = None
        try:
            im = Image.open(BytesIO(jpeg)).convert("RGB")
            if self.turn is not None:
                im = im.transpose(self.turn)
            if self.sharpen:
                im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=int(100 * self.sharpen),
                                                       threshold=0))
            b = BytesIO()
            im.save(b, "JPEG", quality=self.quality, subsampling=0)
            out = b.getvalue()
        except (OSError, ValueError) as e:
            log(f"the still frame could not be made: {e}")
        with self.lock:
            self.busy = False
            if out:
                self.made = (change, out)


def ffmpeg_argv(a):
    w, h = a.size.split("x")
    turn = TURNS[a.rotate][0]
    # The quick frames `--motion` pixels high where given, their shape
    # kept: the wall decodes each one before it shows it, and a 720 line
    # one in under half the time of a 1080 line one.
    scale = [f"scale=-2:{a.motion}"] if a.motion else []
    quick = ["-map", "0:v", "-vf", ",".join(turn + scale + ["format=yuvj420p"]),
             "-c:v", "mjpeg", "-q:v", str(a.quality)]
    copy = ["-map", "0:v", "-c:v", "copy"]
    out = []
    for stream, pipe in ((quick, "pipe:1"), (copy, COPY_PIPE)):
        # Each frame goes the moment it is made, and the pipe is not left
        # to fill to 32 KB first.
        out += stream + ["-fps_mode", "passthrough", "-flush_packets", "1",
                         "-f", "image2pipe", pipe]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", f"{w}x{h}",
            "-framerate", str(a.fps), "-thread_queue_size", "16", "-i", a.device] + out


def glance(jpeg):
    """The picture at an eighth of its size in grey, read straight from
    the JPEG's coefficients, for telling one frame from another."""
    im = Image.open(BytesIO(jpeg))
    im.draft("L", (im.width // 8, im.height // 8))
    return np.asarray(im.convert("L"), np.int16)


def differs(a, b):
    """How many pixels of two glances differ by more than DIFF, or a
    large number where they are not the same size."""
    if a is None or b is None or a.shape != b.shape:
        return 1 << 30
    return int(np.count_nonzero(np.abs(a - b) > DIFF))


def lit(look):
    """Whether a glance is a picture: anything but flat and dark."""
    return bool(look.std() > DARK_SPREAD or look.mean() > DARK_MEAN)


class Signal:
    """Whether a source is plugged in, judged glance by glance: a picture
    is a signal at once, and a signal lasts until the picture has been
    flat and dark for `hold` seconds on end, since a laptop showing black
    is the same flat frame as no laptop at all."""

    def __init__(self, hold):
        self.hold = hold
        self.state = None           # True, False, or None before a judgement
        self.flat_since = None

    def judge(self, look, now):
        """Take a glance at `now` (monotonic); True where the judgement
        changed."""
        was = self.state
        if lit(look):
            self.flat_since, self.state = None, True
        elif self.state:
            self.flat_since = self.flat_since if self.flat_since is not None else now
            if now - self.flat_since >= self.hold:
                self.state = False
        else:
            self.state = False
        return self.state != was

    def pause(self):
        """Nothing is being looked at: the judgement stands, and a run of
        flat frames starts again from the next one seen."""
        self.flat_since = None


def fit(src, glass, align="centre"):
    """Where a `src` (w, h) picture goes on `glass`: as large as it fits,
    its shape kept, centred, or against the top or the bottom where the
    glass is taller than the picture's shape. Returns (at, size)."""
    sw, sh = src
    gw, gh = glass
    k = min(gw / sw, gh / sh)
    w, h = max(1, round(sw * k)), max(1, round(sh * k))
    y = 0 if align == "top" else gh - h if align == "bottom" else (gh - h) // 2
    return ((gw - w) // 2, y), (w, h)


def leave(cap, layer):
    """Stop capturing and take the picture off the wall, but never hang
    doing it: a wall half gone can leave a session waiting for good, and
    a sender told to stop must stop."""
    def tidy():
        cap.stop()
        log(f"stopping: {layer.frames_sent} frames sent, {layer.frames_dropped} waited for room")
        layer.clear()
        layer.close()
    t = threading.Thread(target=tidy, daemon=True)
    t.start()
    t.join(LEAVING)
    if t.is_alive():
        log(f"leaving took over {LEAVING} s; going without it")
        if cap.proc is not None:
            cap.proc.kill()
        os._exit(0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", help="the capture device (default: the first on USB)")
    ap.add_argument("--size", default="1920x1080", help="what to capture, WxH (default 1920x1080)")
    ap.add_argument("--fps", type=int, default=30, help="frames a second to capture (default 30)")
    ap.add_argument("--rotate", type=int, choices=sorted(TURNS), default=0,
                    help="turn the picture, for a source that is itself turned")
    ap.add_argument("--quality", type=int, default=8,
                    help="JPEG quality while the picture moves, 2 best to 31 worst (default 8)")
    ap.add_argument("--motion", default="full",
                    help="how many pixels high the picture is sent while it moves, so the wall "
                         "decodes it sooner, or full (default) for as captured")
    ap.add_argument("--align", choices=("top", "centre", "bottom"), default="centre",
                    help="where the picture sits on a glass taller than its shape "
                         "(default centre)")
    ap.add_argument("--still", type=int, default=90,
                    help="JPEG quality of the still picture, 1 to 95 (default 90)")
    ap.add_argument("--sharpen", type=float, default=1.0,
                    help="how much to sharpen the still picture, 0 for none (default 1)")
    ap.add_argument("--signal-hold", type=float, default=300,
                    help="seconds a seen signal lasts while the picture is flat and dark, "
                         "since a source showing black looks like none (default 300)")
    ap.add_argument("--debug", action="store_true",
                    help="say each second what came in, what went, and where the time went")
    ap.add_argument("target", help="the wall, host:wall")
    a = ap.parse_args()

    a.motion = None if a.motion == "full" else int(a.motion)
    a.device = a.device or find_device()
    if not a.device or not os.path.exists(a.device):
        log(f"no capture device{' at ' + a.device if a.device else ' on USB'}; stopping")
        return 1
    w, h = (int(v) for v in a.size.split("x"))
    src = (h, w) if a.rotate in (90, 270) else (w, h)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    place = os.environ.get("BITWALL_PLACE") or "base"
    if place == "base":
        layer = bitwall.layer(a.target, place=0, take=True)
    else:
        layer = bitwall.layer(a.target, place=place)
    host = socket.gethostname()
    layer.meta(name="HDMI in", description=f"the HDMI capture on {host}, "
               f"{a.size} at {a.fps}fps" + (f", turned {a.rotate}" if a.rotate else ""))
    log(f"{a.device} {a.size} at {a.fps}fps, turned {a.rotate}, quality {a.quality} moving"
        + (f" at {a.motion} lines" if a.motion else "") + " "
        f"and {a.still} still, sharpened {a.sharpen:g}, to {a.target} layer {layer.z}")

    cap = Capture(ffmpeg_argv(a))
    fine = Fine(TURNS[a.rotate][1], a.sharpen, a.still)
    signal_ = Signal(a.signal_hold)
    shown = None                    # the picture on the wall, to send again
    sent_at = 0.0
    seen = 0                        # the quick stream's count when last looked
    ref = None                      # the glance of the picture on the wall
    change = 0                      # which change of picture is on the wall
    changed = 0.0                   # when it went up
    finished = True                 # whether its fine frame is up
    glass = None
    failures = 0
    tally = dict(arrived=0, looked=0, repeat=0, near=0, quick=0, fine=0, declined=0,
                 look_ms=0.0, write_ms=0.0)
    tally_at, tally_count = time.monotonic(), 0

    while not stop.is_set():
        if a.debug and time.monotonic() - tally_at >= 1:
            count = cap.quick.count if cap.quick else 0
            gaps = cap.quick.gaps if cap.quick else []
            if cap.quick:
                cap.quick.gaps = []
            tally["arrived"] = count - tally_count
            log("debug: " + ", ".join(f"{k} {v:.0f}" for k, v in tally.items())
                + "; gaps " + " ".join(f"{label}:{sum(lo <= g < hi for g in gaps)}" for label, lo, hi
                                       in (("<10", 0, 10), ("10-25", 10, 25), ("25-45", 25, 45),
                                           ("45-80", 45, 80), (">80", 80, 1e9))))
            tally = dict.fromkeys(tally, 0)
            tally_at, tally_count = time.monotonic(), count
        if layer.stopped:
            log(f"the wall let the layer go: {layer.stopped}")
            break
        if layer.watching() == 0:
            if cap.running():
                cap.stop()
                signal_.pause()
                log("nobody watching; capture stopped")
            stop.wait(1)
            continue
        if not cap.running():
            if cap.proc is not None:
                failures += 1
                why = "; ".join(cap.tail[-3:]) or f"exit {cap.proc.returncode}"
                log(f"ffmpeg ended: {why}")
                cap.proc = None
                if stop.wait(min(30, 2 * failures)):
                    break
            cap.start()
            log("capturing")
            shown, seen, ref, finished = None, 0, None, True
        elif failures and cap.quick.count > 5:
            failures = 0

        if layer.pixels() != glass and layer.pixels():
            glass = layer.pixels()
            log(f"glass {glass[0]}x{glass[1]}")
            sent_at = 0

        with cap.cond:
            if cap.quick.count == seen:
                cap.cond.wait(0.05)
            count = cap.quick.count
            newest = cap.quick.latest if count != seen else None
            copy, copy_at = cap.copy.latest, cap.copy.at
        if layer.repaint_asked():
            sent_at = 0
        if glass is None:
            continue
        now = time.monotonic()
        picture = kind = None
        if newest is not None:
            seen = count
            looking = time.monotonic()
            look = glance(newest)
            tally["looked"] += 1
            tally["look_ms"] += 1000 * (time.monotonic() - looking)
            if signal_.judge(look, now):
                said = "yes" if signal_.state else "no"
                layer.meta(signal=said)
                log(f"signal {said}: mean {look.mean():.1f}, spread {look.std():.1f}")
            n = differs(look, ref)
            if n >= PIXELS:
                picture, kind = newest, "quick"
            else:
                tally["near" if n else "repeat"] += 1
        if picture is None and not finished and now - changed >= SETTLE:
            made = fine.take(change)
            if made is not None:
                picture, kind = made, "fine"
            elif copy is not None and copy_at >= changed:
                fine.ask(change, copy)
        if picture is None and shown is not None and now - sent_at >= RESEND:
            picture, kind = shown, "again"
        if picture is None:
            continue
        at, size = fit(src, glass, a.align)
        began = time.monotonic()
        done = layer.frame(picture, at=at, size=size, hold="still")
        tally["write_ms"] += 1000 * (time.monotonic() - began)
        if done:
            if kind in tally:
                tally[kind] += 1
            shown, sent_at = picture, now
            if kind == "quick":
                ref, changed, finished = look, now, False
                change += 1
            elif kind == "fine":
                finished = True
        elif kind == "quick":
            tally["declined"] += 1
            seen -= 1               # the pipe is full: the newest goes when it has room
            stop.wait(0.005)

    leave(cap, layer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
