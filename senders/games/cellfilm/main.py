#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = ["numpy"]
# ///
"""Video on a wall.

    senders/games/cellfilm/main.py pi.local
    senders/games/cellfilm/main.py --film tears-of-steel --mode fine pi.local
    senders/games/cellfilm/main.py --mode fine --font 14 FILE pi.local
    senders/games/cellfilm/main.py --shuffle DIRECTORY pi.local

The bitwall front end on `player.py`. bitwall calls render() whenever it
wants a frame and tells it the receiver's area, which is the contract the
player already has. The player's frame is arrays already, and bitwall
takes those whole, so a film never becomes a grid of cells on its way out;
only a sentence in place of the picture is converted, Glyph to cell. The
host is the last argument; everything before it is a file or a directory
to play.

With nothing named it plays the films in the repo's `media/` directory, in
turn, and `--film` picks one of them by its slug: the title in lower case,
words joined by `-`, the year left off, so `Mars Attacks! (1996).mp4` is
`mars-attacks`. The slug is matched against what is in the directory when
it starts, so a controller can offer a fixed list of words and never a path.
The `sync-*` files there are test patterns, not films.

The area is asked for rather than assumed: the font size we request is a
request, and the grid it produces is the receiver's to report.

The wall's minimum frame interval is set to the player's frame rate. Every
cell of a film differs from the last frame, so the receiver's damage
tracking saves nothing and each frame is a full repaint on the wire; asking
for them faster than the file plays would only re-send an unchanged picture.
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))

import bitwall                                                  # noqa: E402
from player import OPTIONS, Player, parse_args                  # noqa: E402

DEFAULT_FONT = 14        # small enough that a column of pixels is a column
MEDIA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))),
                     "media")
USAGE = ("usage: main.py [options] [FILE|DIRECTORY ...] HOST\n" + OPTIONS +
         "\n  --film SLUG    one film from media/, as tears-of-steel; all plays"
         "\n                 them in turn (the default with no FILE)"
         "\n  --font N       receiver font size (default %d)"
         "\n  --at MINUTES   start each film this far in"
         "\n  --audio        play the sound on this machine as well"
         "\n  --audio-delay S  hold the sound back this many seconds, to"
         "\n                 meet a picture that arrives late"
         "\n  --depth D      send mono|grey|16|256|true instead of what"
         "\n                 the screen says, for a link that is the limit"
         % DEFAULT_FONT)


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, flush=True, **kw)


def slug(path):
    """A film's file name as one word: lower case, `-` between words, no
    year. `The Grand Budapest Hotel (2014).mp4` is
    `the-grand-budapest-hotel`."""
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
            if n.lower().endswith(".mp4") and not n.startswith((".", "sync-"))]


def title(path):
    return os.path.splitext(os.path.basename(path))[0]


def aspect(path, fallback=16 / 9):
    """The film's own proportions, for a page deciding what box to give it.

    Asked of the file rather than assumed: a wall showing a film in a box
    the wrong shape pads it to fit, and the padding is what a viewer sees.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             path], capture_output=True, text=True, timeout=10).stdout
        w, h = (int(v) for v in out.strip().split("x")[:2])
        return w / h if w and h else fallback
    except (OSError, ValueError, subprocess.SubprocessError):
        return fallback


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.split("\n\n")[0] + "\n\n" + USAGE)
        return 0
    font = DEFAULT_FONT
    film = "all"
    if "--film" in args:
        i = args.index("--film")
        film = args[i + 1] if i + 1 < len(args) else "all"
        del args[i:i + 2]
    if "--font" in args:
        i = args.index("--font")
        font = int(args[i + 1])
        del args[i:i + 2]
    audio = "--audio" in args
    if audio:
        args.remove("--audio")
    lag = 0.0
    if "--audio-delay" in args:
        i = args.index("--audio-delay")
        lag = float(args[i + 1])
        del args[i:i + 2]
    depth = None
    if "--depth" in args:
        i = args.index("--depth")
        depth = args[i + 1]
        del args[i:i + 2]
    start = 0.0
    if "--at" in args:
        i = args.index("--at")
        start = float(args[i + 1]) * 60.0        # minutes in
        del args[i:i + 2]
    #: The cell's shape, as given; else taken from what the wall says of
    #: its cells once it has, since the player draws as though its
    #: framebuffer pixels were square and a 6x16 cell taken for 2:1
    #: draws the film a quarter too narrow.
    cell_said = "--cell" in args
    rest, opts = parse_args(args, USAGE)
    host = rest.pop()
    if not rest:
        rest = films()
        if film != "all":
            one = [f for f in rest if slug(f) == film]
            if not one:
                say(f"video: no film {film!r} in {MEDIA}, playing them all")
            rest = one or rest
        if not rest:
            say(f"video: no films in {MEDIA}")
            return 1

    player = Player(rest, start=start, **opts)
    #: Taken by a frame and by closing, so a frame asked for after the
    #: player is closed cannot start its decoder again.
    playing = threading.Lock()
    closed = {"now": False}

    #: Set when a frame has been built, which is the first moment there
    #: is a picture to be in time with.
    showing = threading.Event()

    def render(cols, rows):
        """The film as arrays, which the wall diffs and sends whole; or,
        when the player has a sentence to show instead, as cells."""
        with playing:
            if closed["now"]:
                return [[bitwall.cell(" ")] * cols for _ in range(rows)]
            got = None if cell_said else wall.screen().get("cell")
            if got and got[0] > 0:
                ratio = max(0.5, min(8.0, got[1] / got[0]))
                if abs(ratio - player.cell_ratio) > 0.01:
                    say(f"video: cell {ratio:.2f}:1, as the wall says")
                    player.cell_ratio = ratio
                    # Decoded again at the new shape from where it had got
                    # to, as a resize is.
                    player.cols = -1
            got = player.arrays(cols, rows)
        if isinstance(got, list):
            got = [[bitwall.cell(g.ch,
                                 "default" if g.fg is None else g.fg,
                                 "default" if g.bg is None else g.bg)
                    for g in row]
                   for row in got]
        showing.set()
        return got

    held = {"proc": None}
    #: A file to read the delay from while the film plays. Comparing two
    #: settings across separate runs compares their startups as much as
    #: themselves; moved underneath a running picture, the setting is
    #: the only thing that changed.
    tune = os.environ.get("BITWALL_AUDIO_TUNE")
    if audio:
        # The picture is paced by the clock rather than by frames -- a
        # receiver too slow for the rate drops frames rather than
        # playing slowly -- so sound started beside it stays with it,
        # and neither waits on the other. Best effort by design:
        # nothing here re-syncs them afterwards.
        def sound(by):
            """Play the film's sound, `by` seconds behind the picture.

            ffmpeg does the seeking and ffplay plays what comes out of
            it. ffplay's own seek does not land where it is asked: with
            video in the stream it goes to the nearest key picture,
            which at ordinary encoder settings is ten seconds wide, so a
            delay smaller than that does nothing until it crosses a
            boundary and the sound jumps whole seconds. ffmpeg decodes
            and discards to the sample asked for.

            Placed against where the picture has actually reached rather
            than where the film was asked to begin: the decoder is
            started by the first frame wanted rather than at launch, and
            a long seek takes a while, during which the film has run on.
            At the very beginning there is nothing behind to seek to, so
            whatever the delay cannot take by seeking it takes by
            waiting -- seeking alone would clamp at zero and the delay
            would quietly vanish.
            """
            hold = max(0.0, by - player.position)
            if hold > 0:
                time.sleep(hold)
            at = player.position
            cut = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-ss", "%.3f" % max(0.0, at - by), "-i", player.path,
                 "-vn", "-f", "wav", "-"],
                stdout=subprocess.PIPE)
            play = subprocess.Popen(
                # Carried as wav rather than bare samples: the header
                # says the rate and the channels, which this ffplay has
                # no options to be told.
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                 "-i", "-"],
                stdin=cut.stdout)
            cut.stdout.close()
            say(f"video: picture at {at:.2f}s, sound {by:.2f}s behind it")
            return cut, play

        def begin():
            """Start the sound, and follow the tuning file if there is one."""
            showing.wait()
            by = lag
            while True:
                try:
                    held["proc"] = sound(by)
                except OSError as e:
                    say(f"video: no sound: {e}")
                    return
                if not tune:
                    return
                while True:
                    time.sleep(0.25)
                    try:
                        with open(tune) as fh:
                            want = float(fh.read().strip())
                    except Exception:
                        continue
                    if abs(want - by) > 1e-3:
                        break
                by = want
                for one in held["proc"]:
                    one.terminate()

        threading.Thread(target=begin, daemon=True).start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    wall = bitwall.attach(host, min_frame=1.0 / opts["fps"])
    if depth:
        # A film changes every cell of every frame, so the colour each
        # one costs is the whole of the wire. Worth spending only where
        # the link can carry it.
        wall.use_depth(depth)
    # A film takes no input, and a receiver remembers what the last
    # sender wanted: without saying so, a page goes on forwarding its
    # own keys to us and the viewer loses the stats readout.
    wall.want(keyboard=False, touch=False)
    with wall.transition():
        wall.set_font_size(font)
        cols, rows = wall.area()
        wall.set_source(bitwall.FunctionSource(arrays=render))
    tw, th = player.tile
    # Which film this is, for whoever is looking at a list of walls rather
    # than at the wall.
    about = f"A film drawn in cells, {opts['mode']} mode"
    wall.meta(name=title(player.path), description=about)
    say(f"video -> {host}  {len(player.files)} file(s), first "
        f"{os.path.basename(player.path)}  mode {opts['mode']}  "
        f"{cols}x{rows} at font {font}  framebuffer "
        f"{cols * tw}x{rows * th} at {opts['fps']}fps")

    t0 = time.monotonic()
    now = (player.index, player.loops)
    error = None
    try:
        while not stop.wait(0.5):
            if (player.index, player.loops) != now:
                now = (player.index, player.loops)
                wall.meta(name=title(player.path), description=about)
                say(f"video: next {os.path.basename(player.path)}")
            if player.error and player.error != error:
                say(f"video: ffmpeg: {player.error}")
            error = player.error
            if wall.area() != (cols, rows):
                cols, rows = wall.area()
                say(f"video: resized to {cols}x{rows}")
    finally:
        dt = max(1e-6, time.monotonic() - t0)
        say(f"video: stopping  {wall.frames_sent} frames, "
            f"{wall.bytes_sent} bytes, "
            f"{wall.bytes_sent / dt / 1024:.0f} KiB/s pre-compression")
        # The decoder and the sound are child processes, and they go
        # before the wall is told we are leaving.
        with playing:
            closed["now"] = True
            player.close()
        if held["proc"] is not None:
            for one in held["proc"]:
                one.terminate()
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
