#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["gstreamer-bundle"]
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
or the folder `--dir` names, in turn, or with `--shuffle` in a random
order.

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

`--file` may be an address ffmpeg reads, a stream on the web, played as
it arrives. `--sound FILE` takes the sound from a file of its own rather
than the film's, and `--speed` plays the picture that much faster, its
frames' times divided by it; the sound is then taken to be at that speed
already, so `--speed` with `--audio` needs `--sound`. `--title` is what
the film is called in the log and on the wall where its file name says
nothing. `--fps` sends every film at one frame rate, so that a caller
playing one film after another on the same layers (`main`'s `held`) has
them all at the rate the layers were opened with.

`--subs FILE` shows a film's subtitles, an `.srt`, or with `--subs auto`
each film's own, the `.srt` of the same name beside it. They are a cells
layer of their own over the film, on one wall: the middle one of the
bottom row of the layout, or of the top row with `--subs-at top`. Each
line goes up when the walls' clock reaches the second of the film it is
for, by the same clock as the picture, and comes down at its end;
`--subs-delay` moves them all later, or with a negative number sooner.
`--subs-at bar`, the default, puts them in the middle of the black bar
under a letterboxed film, and where there is no bar to put them in, at
the bottom of the picture. `--subs-style` is `plain`, white on nothing,
the default in the bar; `boxed`, white on a dark translucent ground, the
default over the picture; or `yellow`. `--subs-font` gives the lines a
font size of their own; otherwise they are in the wall's font as it
stands.
"""
import argparse
import fractions
import json
import os
import random
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
import bitwall.sound                                             # noqa: E402

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
#: Seconds every wall must have said nobody is watching before the film
#: pauses, so a passing blink of presence does not stop it.
AWAY = 10


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} film {msg}", flush=True)


def slug(path):
    """A film's file name as one word, as the film sender has it:
    `The Grand Budapest Hotel (2014).mp4` is `the-grand-budapest-hotel`."""
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"\(\d{4}\)", "", name).lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


def films(folder=MEDIA):
    """The films in a folder, the media directory unless told, in name
    order."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
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


def graph(rows, glass, bezel, n_fps, fit="cover", speed=1.0):
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
    fast = f"setpts=PTS/{speed:.6g}," if speed != 1 else ""
    parts = [f"[0:v]{fast}fps={n_fps},{shape},split={n}" + "".join(f"[s{k}]" for k in range(n))]
    k = 0
    for r in range(nrows):
        for c in range(ncols):
            parts.append(f"[s{k}]crop={gw}:{gh}:{c * (gw + bezel)}:{r * (gh + bezel)},"
                         f"format=nv12[o{k}]")
            k += 1
    return ";".join(parts), (cw, ch)


def band(rows, target, glass, bezel, film, fit):
    """The film's rows of glass on the wall `target`, (top, bottom) in its
    pixels, or None where the film covers it."""
    if fit == "cover":
        return None
    gw, gh = glass
    nrows, ncols = len(rows), len(rows[0])
    cw, ch = ncols * gw + (ncols - 1) * bezel, nrows * gh + (nrows - 1) * bezel
    fh = film[1] * min(cw / film[0], ch / film[1])
    y0 = 0 if fit == "top" else (ch - fh) / 2
    r = next(i for i, row in enumerate(rows) if target in row)
    off = r * (gh + bezel)
    return max(0.0, y0 - off), min(float(gh), y0 + fh - off)


def play(path, rows, walls, args, stop, seek=None, held=None, subs=None):
    """One film on the layout, from a fresh start, `seek` seconds in where
    given: ('ended', 'stopped', 'lost', 'resized' or 'away', the seconds
    into the film it had got to).

    With nobody watching any of its walls for `AWAY` seconds, the film
    stops being made: the encoder ends, the layers stay open, and play
    returns 'away' once somebody is watching again, at the second the walls
    went dark, for the film to go on from there.

    `held`, where given, is the walls' layers kept from one film to the
    next, {target: (layer, codec, fps)}: a layer still open at the same
    codec and rate plays this film as its next stream, its start said with
    `stream_start`, and is left open at the end, so the wall is never let
    go between films. The caller closes them.

    `subs`, where given, is the subtitles' layer and its `Subs` source,
    (layer, subs, target), handed this film's cues where it has them."""
    if seek is not None:
        args = argparse.Namespace(**dict(vars(args), seek=seek))
    w, h, rate, secs = probe(path)
    secs /= args.speed
    if args.fps:
        rate = fractions.Fraction(args.fps).limit_denominator(1001)
    fps = round(float(rate), 6)
    name = args.title or os.path.basename(path)
    glass = walls[rows[0][0]]["pixels"]
    flat = [t for row in rows for t in row]
    filt, canvas = graph(rows, glass, args.bezel, rate, args.fit, args.speed)
    # Measured before the start is chosen, since it takes a moment.
    cues = []
    if subs:
        own = subs_for(path, args.subs)
        try:
            cues = read_srt(own) if own else []
        except OSError as e:
            log(f"subtitles {own}: {e}")
        log(f"subtitles: {len(cues)} lines from {own}" if cues else "subtitles: none for this film")
    ahead = walls_clock(flat) if args.audio or cues else 0.0
    start = int(time.time() * 1000 + args.ahead * 1000)
    log(f"{name}: {w}x{h} at {fps}fps, {secs:.0f}s"
        f"{f' at {args.speed:g}x' if args.speed != 1 else ''}, as a "
        f"{canvas[0]}x{canvas[1]} canvas of {len(rows[0])}x{len(rows)} walls of "
        f"{glass[0]}x{glass[1]}, bezel {args.bezel}; starting at {start}")
    layers, fresh = {}, []
    for t in flat:
        kept = (held or {}).pop(t, None)
        if kept and kept[1:] == (walls[t]["codec"], fps) and kept[0].open_now \
                and kept[0].has_data:
            layers[t] = kept[0]
            layers[t].stream_start(start)
            continue
        if kept:
            kept[0].close()
        layers[t] = bitwall.video.Layer(t, place=0, take=True, data=True,
                                        codec=walls[t]["codec"], fps=fps, start=start)
        fresh.append(t)
    threads = [threading.Thread(target=layers[t].open) for t in fresh]
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
    if held is not None:
        # Kept there while it plays too, for the caller to see who watches.
        held.update({t: (layers[t], walls[t]["codec"], fps) for t in flat})
    if cues:
        subs[1].play(cues, start, args.seek or 0, ahead, args.speed,
                     band(rows, subs[2], glass, args.bezel, (w, h), args.fit))
    for t in flat:
        what = args.title or slug(path)
        layers[t].meta(name="Film", description=f"{what}, a piece of it on each of "
                       f"{len(flat)} walls" if len(flat) > 1 else f"{what}, decoded on the wall")
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
    if "://" in path:
        # A stream on the web that stalls or drops is asked for again.
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if args.seek:
        # The seek is in the film as played; the file is at its own speed.
        cmd += ["-ss", str(args.seek * args.speed)]
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
    resized, since, dark = None, None, None
    while enc.poll() is None and not stop.is_set() and not lost.is_set():
        time.sleep(0.2)
        if all(layers[t].watching() == 0 for t in flat):
            dark = dark or time.time()
            if time.time() - dark >= AWAY:
                break
        else:
            dark = None
        now = {t: layers[t].pixels() for t in flat}
        changed = {t: p for t, p in now.items() if p and p != walls[t]["pixels"]}
        if changed != resized:
            resized, since = changed, time.time()
        elif changed and time.time() - since >= 1.0:
            break
    away = dark is not None and time.time() - dark >= AWAY
    how = "stopped" if stop.is_set() else "lost" if lost.is_set() else \
        "away" if away and enc.poll() is None else \
        "resized" if resized and enc.poll() is None else "ended"
    into = (args.seek or 0) + max(0.0, (dark if how == "away" else time.time()) - start / 1000)
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
    if how == "away":
        log(f"nobody watching; paused at {into:.0f}s")
        while not stop.is_set() and all(
                layers[t].open_now and layers[t].watching() == 0 for t in flat):
            stop.wait(1)
        if not stop.is_set():
            log(f"watched again; on from {into:.0f}s")
    if enc.returncode not in (0, None) and how == "ended":
        log(f"ffmpeg ended with {enc.returncode}: {' / '.join(err[-3:]) or 'nothing said'}")
    log(f"{how}: " + ", ".join(f"{t} {sent[t] / 1e6:.1f} MB" for t in flat))
    if how == "ended" and enc.returncode == 0:
        # The walls hold a few seconds of it still: wait for the last frame.
        end = start / 1000 + secs - (args.seek or 0) + 1
        stop.wait(max(0.0, end - time.time()))
    done.set()
    hush(heard)
    if subs:
        subs[1].idle()
    if held is not None and how != "lost":
        held.update({t: (layers[t], walls[t]["codec"], fps) for t in flat})
    else:
        for t, layer in layers.items():
            layer.close()
            if held is not None:
                held.pop(t, None)
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
    """The film's sound here, second t of it when the walls show second t,
    by `bitwall.sound`: on this machine's realtime clock, each sample at
    its own time, so it holds to the picture for the length of the film
    and comes back into step by itself after a stall.

    With `--audio-tune`, the delay is read from that file while the film
    plays, and the sound started again at a new one when it changes, so
    it can be moved under a running picture by a viewer watching it.
    """
    by = args.audio_delay
    while True:
        # The walls show second t at start + t on their clock, which is
        # `ahead` of this one: here that is `ahead` sooner.
        at = start / 1000 - ahead + by
        try:
            player = bitwall.sound.play(args.sound or path, args.seek or 0, at)
        except bitwall.sound.SoundError as e:
            log(f"no sound: {e}")
            return
        heard[:] = [player]
        log(f"sound from {(args.seek or 0) + max(0.0, time.time() - at):.2f}s, "
            f"{by:+.3f}s against the picture")
        want = None
        while not done.wait(0.25):
            why = player.failed()
            if why:
                log(f"sound stopped: {why}")
                return
            want = tuned(args.audio_tune)
            if want is not None and abs(want - by) > 1e-4:
                break
        player.stop()
        if done.is_set():
            return
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


def hush(players):
    """Stop the sound."""
    for player in players:
        player.stop()


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_TAG = re.compile(r"<[^>]*>|\{[^}]*\}")


def read_srt(path):
    """A SubRip file's cues, [(from, to, [(line, italic)])], in seconds
    of the film, in order."""
    raw = open(path, "rb").read()
    text = raw.decode("utf-8-sig", "replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.strip().splitlines()
        at = next((i for i, l in enumerate(lines) if _SRT_TIME.search(l)), None)
        if at is None:
            continue
        g = [int(x) for x in _SRT_TIME.search(lines[at]).groups()]
        t0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        t1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        said = [(_TAG.sub("", l).strip(), "<i>" in l.lower()) for l in lines[at + 1:]]
        said = [s for s in said if s[0]]
        if said and t1 > t0:
            cues.append((t0, t1, said))
    cues.sort(key=lambda c: c[0])
    return cues


def subs_for(path, given):
    """The subtitle file for a film: the one given, or with `auto` the
    film's own `.srt` beside it; None where there is none."""
    if not given:
        return None
    if given != "auto":
        return given
    if "://" in path:
        return None
    own = os.path.splitext(path)[0] + ".srt"
    return own if os.path.exists(own) else None


class Subs:
    """The subtitles, as a cells layer's source: `render(cols, rows)` is
    the grid, transparent but for the line showing now.

    `play` hands it a film's cues and when the film started on the walls'
    clock; `idle` takes them away between films."""

    STYLES = {"plain": ("ffffff", "default"), "boxed": ("ffffff", "101418c0"),
              "yellow": ("ffd84a", "default")}

    def __init__(self, at, style, delay, glass):
        self.at, self.style, self.delay = at, style, delay
        self.glass = glass
        self.lock = threading.Lock()
        self.cues, self.film = [], None
        self.k = 0

    def play(self, cues, start, seek, ahead, speed, band):
        """A film's cues, from `start` (ms, walls' clock), `seek` seconds
        in; `band` is the film's rows of glass on this wall, (top, bottom)
        in pixels, or None where it fills the wall."""
        with self.lock:
            self.cues, self.k = cues, 0
            self.film = (start / 1000 - ahead, seek, speed, band)

    def idle(self):
        with self.lock:
            self.cues, self.film = [], None

    def now(self):
        """The lines to show now, or []."""
        with self.lock:
            cues, film = self.cues, self.film
            if not cues or film is None:
                return []
            t0, seek, speed, _ = film
            t = (seek + (time.time() - t0)) * speed - self.delay
            k = self.k
            if k >= len(cues) or cues[k][0] > t:
                k = 0
            while k + 1 < len(cues) and cues[k + 1][0] <= t:
                k += 1
            self.k = k
        c = cues[k]
        return c[2] if c[0] <= t < c[1] else []

    def where(self, rows):
        """Where the lines go: the rows they may use, (first, last), how
        they sit in them (`top`, `bottom` or `middle`), and the style."""
        with self.lock:
            band = self.film[3] if self.film else None
        if self.at == "bar" and band:
            # The first row wholly under the picture, to the wall's foot.
            below = -(-int(band[1]) * rows // self.glass[1])
            if below < rows:
                return (below, rows - 1), "middle", self.style or "plain"
        if self.at == "top":
            return (1, rows - 2), "top", self.style or "boxed"
        return (1, rows - 2), "bottom", self.style or "boxed"

    def render(self, cols, rows):
        clear = bitwall.cell(" ")
        grid = [[clear] * cols for _ in range(rows)]
        said = self.now()
        if not said or cols < 8 or rows < 3:
            return grid
        (first, last), sit, style = self.where(rows)
        fg, bg = self.STYLES[style]
        room = cols - 4
        lines = []
        for text, italic in said:
            while len(text) > room:
                cut = text.rfind(" ", 0, room + 1)
                cut = cut if cut > 0 else room
                lines.append((text[:cut].rstrip(), italic))
                text = text[cut:].lstrip()
            lines.append((text, italic))
        space = last - first + 1
        lines = lines[-space:]
        top = first if sit == "top" else last - len(lines) + 1 if sit == "bottom" \
            else first + (space - len(lines)) // 2
        pad = 1 if bg != "default" else 0
        for i, (text, italic) in enumerate(lines):
            text = " " * pad + text + " " * pad
            left = max(0, (cols - len(text)) // 2)
            row = grid[top + i]
            for j, ch in enumerate(text[:cols - left]):
                row[left + j] = bitwall.cell(ch, fg, bg, bold=True, italics=italic)
        return grid


def main(argv=None, stop=None, held=None):
    """The sender, on `argv` (the command line's where None). A caller
    playing films one after another passes its own `stop`, which this then
    leaves to it rather than taking the signals, and may pass `held`, a
    dict it keeps, to keep the walls from one call to the next (`play`);
    it closes the layers in it when it is done."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--film", default="all",
                    help="a film from media/ by its slug, as tears-of-steel; all plays them in turn")
    ap.add_argument("--dir", default=MEDIA,
                    help="the folder the films are in (default the repo's media/)")
    ap.add_argument("--shuffle", action="store_true",
                    help="play them in a random order, shuffled again each time round")
    ap.add_argument("--file", help="a film file, or an address ffmpeg reads, to play instead")
    ap.add_argument("--sound", help="the sound from this file instead, at the speed played")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="play the picture this much faster (default 1)")
    ap.add_argument("--fps", type=float,
                    help="send every film at this frame rate rather than its own")
    ap.add_argument("--title", help="what the film is called, for the log and the wall")
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
    ap.add_argument("--subs", help="an .srt to show, or auto for each film's own beside it")
    ap.add_argument("--subs-at", choices=("bar", "bottom", "top"), default="bar",
                    help="in the black bar under the picture, else at its bottom; at the "
                         "bottom; or at the top (default bar)")
    ap.add_argument("--subs-style", choices=tuple(Subs.STYLES),
                    help="plain, boxed or yellow (default plain in the bar, boxed over the picture)")
    ap.add_argument("--subs-font", type=int, help="the subtitles' font size (default the wall's)")
    ap.add_argument("--subs-delay", type=float, default=0.0,
                    help="seconds to show them later; negative sooner")
    ap.add_argument("target", nargs="?", help="the wall, host:wall, when there is no layout")
    args = ap.parse_args(argv)
    if args.speed <= 0:
        ap.error("--speed is more than 0")
    if args.audio and args.speed != 1 and not args.sound:
        ap.error("--speed with --audio needs --sound, the sound at that speed")
    if args.audio:
        bitwall.sound.warm(log)

    if stop is None:
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
        queue = [f for f in films(args.dir) if args.film == "all" or slug(f) == args.film]
        if not queue:
            log(f"no film {args.film!r} in {args.dir}")
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

    subs = None
    if args.subs:
        row = rows[0] if args.subs_at == "top" else rows[-1]
        where = row[len(row) // 2]
        source = Subs(args.subs_at, args.subs_style, args.subs_delay, walls[where]["pixels"])
        layer = bitwall.CellsLayer(where, place="above", min_frame=0.1).attach()
        if layer.z is None:
            log(f"{where}: no cells layer for the subtitles: "
                f"{getattr(layer, '_refused', None) or 'the wall did not open one'}")
            layer.detach()
        else:
            layer.meta(name="Subtitles", description="the film's subtitles")
            if args.subs_font:
                layer.set_font_size(args.subs_font)
            layer.set_source(source.render)
            cols, nrows = layer.area()
            log(f"subtitles -> {where} layer {layer.z}, {cols}x{nrows}, at {args.subs_at}")
            subs = (layer, source, where)

    k, seek = 0, None
    while not stop.is_set():
        if args.shuffle and k % len(queue) == 0 and seek is None:
            random.shuffle(queue)
        path = queue[k % len(queue)]
        how, into = play(path, rows, walls, args, stop, seek, held, subs)
        seek = None
        if how in ("resized", "away"):
            seek = into
            continue
        if how == "lost":
            log("a wall was lost; starting again when it is back")
            stop.wait(5)
            continue
        k += 1
        if args.once and k >= len(queue):
            break
    if subs:
        subs[0].detach()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
