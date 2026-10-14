#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = ["pyte"]
# ///
"""Classic terminal ASCII animations on a wall, as a playlist.

    senders/ascii/main.py [--secs 30] [--font 22] [--only CMD] <host>
    senders/ascii/main.py --name tank --only asciiquarium --grid 140x40 \
        --font 9 localhost
    senders/ascii/main.py [--name NAME] --stop

`--size` is the same as `--font`. `--place above|top|<n>` puts it on a
cells layer at that place instead of the base, so the animation's text
lies over whatever the wall shows, its blank cells transparent. `--name` is what lets several of these
run at once, each feeding a wall of its own -- a carousel shows one
program per wall, at the font size that program wants. Everything private
to a run is named after it: the tmux session, its socket, and the pid file
`--stop` reads. One a controller starts, with no `--name`, is named after
its instance (`BITWALL_INSTANCE`), so two on two walls keep apart.

The tmux server is a daemon of its own session, which a signal to this
process's group does not reach, so it is killed here on the way out.

`--grid` is the shape this looks right in, which is not the same as the
area any one screen happens to have. A page showing several walls at once
uses it to give a small box the same grid as a large one at a smaller
font, so a preview is the picture made small rather than a smaller
picture. Without it a preview gets whatever cells fit, which for a tank
of fish is a few dozen and no tank.

The animations run in a tmux pane and reach the wall through a read-only
TmuxSource. Most of them come off the internet (parrot.live, ascii.live),
and pyte decodes their output into cells before anything is serialised, so
no escape sequence they send can become a bitwall command.
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))   # repo root

import bitwall                                                # noqa: E402

RUNDIR = os.path.join(tempfile.gettempdir(), "bitwall-ascii")
CONF = os.path.join(_HERE, "tmux.conf")


def say(*parts, **kw):
    """A line of the log: the local time, then what print would have said."""
    print(time.strftime("%H:%M:%S"), *parts, **kw)


def names(name):
    """The session, socket and pid file belonging to one run."""
    return (f"bitwall-{name}",
            os.path.join(RUNDIR, f"{name}.sock"),
            os.path.join(RUNDIR, f"{name}.pid"))


def stop_existing(name):
    _, sock, pidfile = names(name)
    try:
        with open(pidfile) as fh:
            os.kill(int(fh.read().strip()), signal.SIGTERM)
        say(f"{name} stopped", flush=True)
    except Exception:
        pass
    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)


def build_session(name, secs, only=None):
    """One borderless pane running the playlist."""
    session, sock, _ = names(name)
    t = ["tmux", "-f", CONF, "-S", sock]
    subprocess.run(t + ["kill-session", "-t", session], capture_output=True)
    subprocess.run(t + ["new-session", "-d", "-s", session, "-x", "120",
                        "-y", "40", os.path.join(_HERE, "pane-playlist"),
                        str(secs)] + ([only] if only else []), check=True)
    subprocess.run(t + ["set-option", "-t", session, "window-size", "manual"])
    subprocess.run(t + ["set-option", "-t", session, "pane-border-status",
                        "off"])
    # TmuxSource attaches as xterm-256color. Without RGB, tmux squashes
    # 24-bit colour (mpv --vo=tct) down to the 256 palette.
    subprocess.run(t + ["set-option", "-as", "terminal-features",
                        ",xterm-256color:RGB"])


def main():
    secs, size, only, name, stopping, place = 30, 22, None, None, False, None
    grid = (120, 36)
    args = sys.argv[1:]
    while args and args[0].startswith("--"):
        a = args.pop(0)
        if a == "--secs":
            secs = int(args.pop(0))
        elif a == "--only":
            only = args.pop(0)
        elif a in ("--size", "--font"):
            size = int(args.pop(0))
        elif a == "--name":
            name = args.pop(0)
        elif a == "--grid":
            c, _, r = args.pop(0).partition("x")
            grid = (int(c), int(r))
        elif a == "--place":
            place = args.pop(0)
            if place not in ("above", "top") and not place.isdigit():
                say(f"ascii: --place {place!r}: above, top or a number",
                    file=sys.stderr, flush=True)
                return 2
        elif a == "--stop":
            stopping = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            say(f"ascii: unknown option {a}", file=sys.stderr, flush=True)
            return 2
    run = name or ("ascii-" + os.environ["BITWALL_INSTANCE"]
                   if os.environ.get("BITWALL_INSTANCE") else "ascii")
    name = name or "ascii"
    if stopping:
        stop_existing(run)
        return 0
    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    # Caught before tmux is started, so a stop that arrives while it is
    # starting still ends with the server killed.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    session, sock, pidfile = names(run)
    os.makedirs(RUNDIR, exist_ok=True)
    build_session(run, secs, only)
    with open(pidfile, "w") as fh:
        fh.write(str(os.getpid()))

    if place:
        w = bitwall.CellsLayer(args[0], place=int(place) if place.isdigit() else place).attach()
        if w.z is None:
            say(f"{args[0]}: no cells layer {place}", flush=True)
            w.detach()
            subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
            return 1
    else:
        w = bitwall.attach(args[0])
    # A receiver remembers what the last sender wanted. This one takes no
    # input, so hand the page its own keys back (s, ?).
    w.want(keyboard=False, touch=False)
    try:
        with w.transition():
            w.set_font_size(size)
            w.set_source(bitwall.TmuxSource(session, sock, readonly=True))
        # What this is and the grid it wants. A page laying out several
        # walls has only this to shape a box with, and the area a receiver
        # happens to have is not the shape of what is in it.
        w.meta(name=name,
               description="Classic terminal animations, one after another"
               if not only else f"A terminal animation: {only}")
        c, r = w.area()
        say(f"{name} -> {args[0]}   {c}x{r} at {size}px   "
            f"grid {grid[0]}x{grid[1]}   {only or f'playlist, {secs}s each'}",
            flush=True)
        while not stop.wait(0.5):
            if w.area() != (c, r):
                c, r = w.area()
                say(f"{name}: resized to {c}x{r}", flush=True)
    finally:
        # The wall is let go of before the server is killed: a tmux client
        # whose server goes says so on its screen, and that would be the
        # last frame the wall kept.
        w.detach()
        subprocess.run(["tmux", "-S", sock, "kill-server"],
                       capture_output=True)
        say(f"{name}: stopped", flush=True)
        for path in (pidfile, sock):
            try:
                os.remove(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
