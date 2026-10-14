#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""One film across several walls, each showing its piece, decoded on the wall.

    senders/film/main.py --film tears-of-steel pi.local
    senders/film/main.py --file clip.mp4 \\
        --layout 'pi-a.local:0,pi-a.local:1 / pi-b.local:0,pi-b.local:1' --bezel 60

A layout is the walls as they hang, a row at a time: commas between the
walls of a row, a slash between rows. One wall is a layout of one, so the
same sender plays a film on a single wall. The walls are taken to be the
same size, the first one's glass.

The film is decoded once here, scaled to cover the whole of the layout,
and cut into a piece a wall by ffmpeg in one pass (`split` and `crop`),
less a `--bezel` gap between neighbours, the glass's pixels that the
frames of the screens hide. Each piece is encoded on this machine's own
hardware where it has an encoder ffmpeg can use, and in software where it
does not, HEVC for a wall that decodes it and otherwise H.264, as the
wall's `caps` says, and
goes to the wall's base on its data pipe (PROTOCOL.md, "The data pipe").
The walls play their pieces out themselves: every wall is opened with the
same `fps` and the same `start`, a few seconds ahead, so frame i goes up
on each when its clock reaches start + i/fps. How closely they agree is
how closely their clocks do.

The sender does not pace anything. ffmpeg runs as fast as the walls take
the pieces, and a wall that is full holds up the rest, which is what
keeps them together. A wall lost part way through the film starts the
whole of it again, since a stream picked up in the middle has no start of
its own; every film begins with a fresh one, all the walls at once.

A wall whose glass changes size -- a web wall's window resized -- has the
film made again for the new glass, from where it had got to: the frames
are cut to the glass's shape, so a film made for one shape is small on
another. The new size has to hold for a second first, so a window being
dragged costs one restart, not one a step.

With nothing named it plays the films in the repo's `media/` directory,
in turn.

`--audio` plays the film's sound on the machine this runs on, on the
walls' clock: second t of the film is heard when the walls' clock reaches
start + t, which is when they show it. Clocks set by NTP still differ by
a few hundred milliseconds between a Mac and a Pi, so how far the walls'
clock is from this one is measured first, over the ssh connection bitwall
already holds to each host: its time asked several times, the quickest
answer kept and half its round trip allowed for. `--audio-delay` holds
the sound back, or with a negative number brings it forward, for what is
left: what the sound takes to come out of the speakers, and the screens
to show a frame. `--audio-tune FILE` moves it while the film plays.
"""
import argparse
import fractions
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import bitwall                                                   # noqa: E402

MEDIA = os.path.join(ROOT, "media")
#: What counts as a film there: ffmpeg reads any of them, so a film is
#: played in the container it came in.
FILMS = (".mp4", ".mov", ".mkv")
#: What each codec a wall may take may be encoded with here, best first:
#: the machine's own hardware (a Mac's, an Nvidia, Intel or AMD card's),
#: then software. The stream is written raw, Annex B.
ENCODERS = {
    "hevc": ("hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"),
    "h264": ("h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf", "libx264"),
}
#: The codecs in the order they are preferred, the smaller first.
PREFER = ("hevc", "h264")
#: What a piece of the film is read in.
CHUNK = 1 << 16


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} film {msg}", flush=True)


def slug(path):
    """A film's file name as one word, as the film sender has it:
    `The Grand Budapest Hotel (2014).mp4` is `the-grand-budapest-hotel`."""
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"\(\d{4}\)", "", name).lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


def films():
    """The films in the media directory, in name order."""
    try:
        names = sorted(os.listdir(MEDIA))
    except OSError:
        return []
    return [os.path.join(MEDIA, n) for n in names
            if n.lower().endswith(FILMS) and not n.startswith((".", "sync-"))]


def layout_of(text):
    """The walls as rows, from `a:0,a:1 / b:0,b:1`."""
    rows = [[w.strip() for w in row.split(",") if w.strip()] for row in text.split("/")]
    rows = [r for r in rows if r]
    if not rows or len({len(r) for r in rows}) != 1:
        raise ValueError(f"a layout is rows of the same length: {text!r}")
    return rows


_encoders = {}


def encoder(codec):
    """The first of the codec's encoders that encodes on this machine, or
    None. Tried rather than read from `ffmpeg -encoders`, which lists a
    hardware encoder whether or not there is hardware for it."""
    if codec not in _encoders:
        _encoders[codec] = None
        for name in ENCODERS[codec]:
            try:
                ok = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24", "-frames:v", "2",
                     "-c:v", name, "-f", "null", "-"],
                    capture_output=True, timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            if ok:
                _encoders[codec] = name
                log(f"encoding {codec} with {name}")
                break
    return _encoders[codec]


def probe(path):
    """The film's size, frame rate and length, asked of the file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate:format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=20)
    got = json.loads(out.stdout or "{}")
    st, fmt = got["streams"][0], got.get("format", {})
    rate = fractions.Fraction(st["avg_frame_rate"])
    return int(st["width"]), int(st["height"]), rate, float(fmt.get("duration") or 0)


def ask(target):
    """What a wall takes and how big its glass is, asked on a controller
    session: (codecs, (width, height))."""
    c = bitwall.Controller(target).open()
    try:
        codecs, pixels = [], None
        for said in c.report:
            if getattr(said, "name", None) == "caps":
                codecs = (said.opts.get("codecs") or "").split(",")
                if "data" not in said.args:
                    codecs = []
            elif getattr(said, "name", None) == "screen":
                w, _, h = (said.opts.get("pixels") or "").partition("x")
                if w.isdigit() and h.isdigit():
                    pixels = (int(w), int(h))
        return codecs, pixels
    finally:
        c.close()


def graph(rows, glass, bezel, n_fps, fit="cover"):
    """The filter that makes one piece a wall from the film: scaled to
    the whole layout with the gaps -- to cover it and cropped, or to fit in
    it and letterboxed in black, in the middle or against the top with all
    the black below -- split, and each piece cut out."""
    gw, gh = glass
    nrows, ncols = len(rows), len(rows[0])
    cw, ch = ncols * gw + (ncols - 1) * bezel, nrows * gh + (nrows - 1) * bezel
    n = nrows * ncols
    shape = (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}"
             if fit == "cover" else
             f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
             f"pad={cw}:{ch}:(ow-iw)/2:{'0' if fit == 'top' else '(oh-ih)/2'}:black")
    parts = [f"[0:v]fps={n_fps},{shape},split={n}" + "".join(f"[s{k}]" for k in range(n))]
    k = 0
    for r in range(nrows):
        for c in range(ncols):
            parts.append(f"[s{k}]crop={gw}:{gh}:{c * (gw + bezel)}:{r * (gh + bezel)},"
                         f"format=nv12[o{k}]")
            k += 1
    return ";".join(parts), (cw, ch)


def play(path, rows, walls, args, stop, seek=None):
    """One film on the layout, from a fresh start, `seek` seconds in where
    given: ('ended', 'stopped', 'lost' or 'resized', the seconds into the
    film it had got to)."""
    if seek is not None:
        args = argparse.Namespace(**dict(vars(args), seek=seek))
    w, h, rate, secs = probe(path)
    fps = round(float(rate), 6)
    glass = walls[rows[0][0]]["pixels"]
    flat = [t for row in rows for t in row]
    filt, canvas = graph(rows, glass, args.bezel, rate, args.fit)
    # Measured before the start is chosen, since it takes a moment.
    ahead = walls_clock(flat) if args.audio else 0.0
    start = int(time.time() * 1000 + args.ahead * 1000)
    log(f"{os.path.basename(path)}: {w}x{h} at {fps}fps, {secs:.0f}s, as a "
        f"{canvas[0]}x{canvas[1]} canvas of {len(rows[0])}x{len(rows)} walls of "
        f"{glass[0]}x{glass[1]}, bezel {args.bezel}; starting at {start}")
    layers = {t: bitwall.video.Layer(t, place=0, take=True, data=True,
                                     codec=walls[t]["codec"], fps=fps, start=start)
              for t in flat}
    threads = [threading.Thread(target=layers[t].open) for t in flat]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    shut = [t for t in flat if not (layers[t].open_now and layers[t].has_data)]
    if shut:
        for t in shut:
            log(f"{t}: no data pipe: {layers[t].stopped or 'the wall did not open one'}")
        for layer in layers.values():
            layer.close()
        return "lost", args.seek or 0
    for t in flat:
        layers[t].meta(name="Film", description=f"{slug(path)}, a piece of it on each of "
                       f"{len(flat)} walls" if len(flat) > 1 else f"{slug(path)}, decoded on the wall")
    # Each piece comes back on a socket of its own on the loopback address,
    # which ffmpeg connects to: a descriptor handed to a child is Unix's,
    # and a socket is the same on every machine.
    socks = []
    for _ in flat:
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)
        ls.settimeout(0.5)
        socks.append(ls)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if args.seek:
        cmd += ["-ss", str(args.seek)]
    cmd += ["-i", path, "-an", "-filter_complex", filt]
    for k, t in enumerate(flat):
        codec = walls[t]["codec"]
        cmd += ["-map", f"[o{k}]", "-c:v", encoder(codec), "-b:v", f"{args.bitrate}M",
                "-g", str(max(1, round(float(rate) * 2))), "-f", codec,
                "tcp://127.0.0.1:%d" % socks[k].getsockname()[1]]
    enc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    lost = threading.Event()
    sent = {t: 0 for t in flat}

    def carry(t, ls):
        src = None
        with ls:
            while src is None and enc.poll() is None and not stop.is_set() \
                    and not lost.is_set():
                try:
                    src, _ = ls.accept()
                except socket.timeout:
                    pass
        if src is not None:
            with src:
                src.settimeout(None)
                while not stop.is_set() and not lost.is_set():
                    chunk = src.recv(CHUNK)
                    if not chunk:
                        break
                    if layers[t].data_write(chunk) is None:
                        log(f"{t}: lost its data pipe after {sent[t] / 1e6:.1f} MB")
                        lost.set()
                        break
                    sent[t] += len(chunk)
        layers[t].data_end()

    carriers = [threading.Thread(target=carry, args=(t, socks[k]), daemon=True)
                for k, t in enumerate(flat)]
    for th in carriers:
        th.start()
    done = threading.Event()
    heard = []
    if args.audio:
        threading.Thread(target=sound, args=(path, start, args, done, heard, ahead),
                         daemon=True).start()
    err = []
    threading.Thread(target=lambda: err.extend(enc.stderr.read().decode(
        "utf-8", "replace").splitlines()), daemon=True).start()
    resized, since = None, None
    while enc.poll() is None and not stop.is_set() and not lost.is_set():
        time.sleep(0.2)
        now = {t: layers[t].pixels() for t in flat}
        changed = {t: p for t, p in now.items() if p and p != walls[t]["pixels"]}
        if changed != resized:
            resized, since = changed, time.time()
        elif changed and time.time() - since >= 1.0:
            break
    how = "stopped" if stop.is_set() else "lost" if lost.is_set() else \
        "resized" if resized and enc.poll() is None else "ended"
    into = (args.seek or 0) + max(0.0, time.time() - start / 1000)
    if how == "resized":
        for t, p in resized.items():
            log(f"{t}: its glass is now {p[0]}x{p[1]}; the film again from {into:.0f}s")
            walls[t]["pixels"] = p
    if enc.poll() is None:
        enc.terminate()
        try:
            enc.wait(5)
        except subprocess.TimeoutExpired:
            enc.kill()
    for th in carriers:
        th.join(timeout=10)
    if how != "ended":
        done.set()
    if enc.returncode not in (0, None) and how == "ended":
        log(f"ffmpeg ended with {enc.returncode}: {' / '.join(err[-3:]) or 'nothing said'}")
    log(f"{how}: " + ", ".join(f"{t} {sent[t] / 1e6:.1f} MB" for t in flat))
    if how == "ended":
        # The walls hold a few seconds of it still: wait for the last frame.
        end = start / 1000 + secs - (args.seek or 0) + 1
        stop.wait(max(0.0, end - time.time()))
    done.set()
    hush(heard)
    for layer in layers.values():
        layer.close()
    return how, into


def clock_offset(target, tries=7):
    """How far the clock of the host `target` is on is ahead of this one,
    in seconds, and the round trip it was measured with; (0, 0) for a wall
    on this machine, None where the host does not answer.

    The quickest of several answers is the one kept: its round trip is the
    least the answer can have waited, and the host's time is taken to be
    from half way through it.
    """
    link = bitwall.session.Link(target)
    if link.local:
        return 0.0, 0.0
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
    return best


def walls_clock(flat):
    """How far the walls' clock is ahead of this one: each host's measured,
    and the middle one taken, since the sound can follow only one."""
    offsets = []
    for host in sorted({bitwall.session.address(t)[0] for t in flat}):
        got = clock_offset(next(t for t in flat if bitwall.session.address(t)[0] == host))
        if got is None:
            log(f"{host}: did not say its time; its clock taken to be this one's")
            continue
        off, rtt = got
        log(f"{host}: clock {off * 1000:+.0f} ms against this one "
            f"(asked in {rtt * 1000:.0f} ms)")
        offsets.append(off)
    return sorted(offsets)[len(offsets) // 2] if offsets else 0.0


def sound(path, start, args, done, heard, ahead=0.0):
    """The film's sound here, second t of it when the walls show second t.

    Started when the walls start, by their clock, plus the delay; one
    started late is started that much further in, so it is never behind
    the picture by the time it took to begin. ffmpeg cuts the sound from
    where it is asked, to the sample. On a Mac it plays it itself; elsewhere
    ffplay does, as wav, whose header says the rate and channels ffplay
    has no options for.

    With `--audio-tune`, the delay is read from that file while the film
    plays, and the sound started again at a new one when it changes, so
    it can be moved under a running picture by a viewer watching it.
    """
    by = args.audio_delay
    while True:
        # The walls show second t at start + t on their clock, which is
        # `ahead` of this one: here that is `ahead` sooner.
        at = start / 1000 - ahead + by
        if done.wait(max(0.0, at - time.time())):
            return
        into = (args.seek or 0) + max(0.0, time.time() - at)
        procs = speak(path, into)
        if procs is None:
            return
        heard[:] = procs
        log(f"sound from {into:.2f}s, {by:+.3f}s against the picture")
        want = None
        while not done.wait(0.25):
            want = tuned(args.audio_tune)
            if want is not None and abs(want - by) > 1e-4:
                break
        if done.is_set():
            hush(procs)
            return
        # Moved: this sound is no longer wanted a moment longer, and one
        # asked to finish politely plays on for most of a second.
        for proc in procs:
            proc.kill()
            proc.wait()
        by = want


def tuned(path):
    """The delay a tuning file says, in seconds, or None."""
    if not path:
        return None
    try:
        with open(path) as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return None


def speak(path, into):
    """Processes playing the film's sound from `into` seconds, or None."""
    cut = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{into:.3f}", "-i", path, "-vn"]
    try:
        if sys.platform == "darwin":
            # Straight to the speakers: it keeps to time from the first
            # quarter second, a few milliseconds ahead of them, where
            # ffplay behind a pipe takes its own while to start and to fill.
            # At 48 kHz, the rate the Mac's output runs at: a 44.1 kHz film
            # handed over as it is falls behind the picture as it plays.
            return [subprocess.Popen(cut + ["-ar", "48000", "-f", "audiotoolbox", "-"],
                                     stdin=subprocess.DEVNULL)]
        first = subprocess.Popen(cut + ["-f", "wav", "-"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE)
        procs = [first, subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-i", "-"],
            stdin=first.stdout)]
        first.stdout.close()
        return procs
    except OSError as e:
        log(f"no sound: {e}")
        return None


def hush(procs):
    """Stop the sound: ffmpeg before ffplay, so it is not left writing to a
    pipe with nobody reading and saying so."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--film", default="all",
                    help="a film from media/ by its slug, as tears-of-steel; all plays them in turn")
    ap.add_argument("--file", help="a film file to play instead")
    ap.add_argument("--layout", help="the walls as rows: 'a:0,a:1 / b:0,b:1' (default: the target)")
    ap.add_argument("--bezel", type=int, default=0,
                    help="the glass pixels hidden between neighbouring walls (default 0)")
    ap.add_argument("--ahead", type=float, default=2.5,
                    help="seconds between opening the walls and the first frame")
    ap.add_argument("--bitrate", type=float, default=6, help="Mbit/s a wall (default 6)")
    ap.add_argument("--seek", type=float, default=0, help="start this many seconds in")
    ap.add_argument("--once", action="store_true", help="play once and stop")
    ap.add_argument("--fit", choices=("cover", "letterbox", "top"), default="cover",
                    help="cover the layout, cropping the film; letterbox it whole; or letterbox "
                         "it against the top, all the black below (default cover)")
    ap.add_argument("--audio", action="store_true",
                    help="play the sound on this machine, on the walls' clock")
    ap.add_argument("--audio-delay", type=float, default=0.0,
                    help="seconds to hold the sound back; negative brings it forward")
    ap.add_argument("--audio-tune", default=os.environ.get("BITWALL_AUDIO_TUNE"),
                    help="a file holding the delay in seconds, followed while the film plays")
    ap.add_argument("target", nargs="?", help="the wall, host:wall, when there is no layout")
    args = ap.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    text = args.layout or args.target
    if not text:
        ap.error("a target wall or a --layout")
    rows = layout_of(text)
    flat = [t for row in rows for t in row]
    if args.target and args.target in flat:
        # The wall a controller started this on opens first, so it is the
        # one that says the controller's instance.
        flat.remove(args.target)
        flat.insert(0, args.target)
    if args.file:
        queue = [args.file]
    else:
        queue = [f for f in films() if args.film == "all" or slug(f) == args.film]
        if not queue:
            log(f"no film {args.film!r} in {MEDIA}")
            return 1

    walls = {}
    for t in flat:
        try:
            codecs, pixels = ask(t)
        except Exception as e:                                  # noqa: BLE001
            log(f"{t}: cannot ask it what it takes: {e}")
            return 1
        codec = next((c for c in PREFER if c in codecs), None)
        if codec is None or pixels is None:
            log(f"{t}: takes no film on a data pipe (codecs {','.join(codecs) or 'none'})")
            return 1
        walls[t] = {"codec": codec, "pixels": pixels}
        log(f"{t}: {pixels[0]}x{pixels[1]}, takes {','.join(codecs)}; sending {codec}")
    first = walls[flat[0]]["pixels"]
    for t in flat:
        if walls[t]["pixels"] != first:
            log(f"{t}: its glass is {walls[t]['pixels']}, not {first}; each piece is {first}")

    k, seek = 0, None
    while not stop.is_set():
        path = queue[k % len(queue)]
        how, into = play(path, rows, walls, args, stop, seek)
        seek = None
        if how == "resized":
            seek = into
            continue
        if how == "lost":
            log("a wall was lost; starting again when it is back")
            stop.wait(5)
            continue
        k += 1
        if args.once and k >= len(queue):
            break
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
