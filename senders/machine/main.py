#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""How this machine is, and whether everything is up, on a wall.

    BITWALL_RUN=$TMPDIR/bitwall-mac ./senders/machine/main.py localhost
    ./senders/machine/main.py --theme amber --fps 2 pi.local
    ./senders/machine/main.py --service 'web=curl -sf localhost:8080/' \\
                              --service "sshd=pgrep -x sshd >/dev/null" \\
                              --disk root=/ --disk usb=/mnt/usb pi.local

A status board, not an animation: CPU, memory, load, network and disk as
instrument meters and scope sweeps; the host's name, uptime, addresses and
tailnet; and a row of services that are up or not. Calm by design -- most
of the grid sits still between readings, and the graphs advance a column
at a time rather than redrawing.

Services are the operator's business, so they are `--service label=command`
and up means the command exited zero; `pgrep -f '[b]itwall-web'` and a
real health check both fit. With none the board gives them no room. The
disk shown is the one the system is on unless --disk names others.

Reading the machine happens on its own threads and its own clocks, never in
render(): render is called at the frame rate whether anything changed or
not, and `tailscale status` or a service check would stall the wall.

Every argument falls back rather than failing. A wall is a screen in a
room with nobody at the keyboard, and a typo in a flag should cost a line
on stderr, not a dark screen.

Importable by the systems rotator: `render`, `collect`, `FONT`, `SECS` and
`FPS` are module level, and `collect` takes the services and disks to read.
"""
import argparse
import os
import signal
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BITWALL = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, BITWALL)

import bitwall                                          # noqa: E402
import panel                                            # noqa: E402
import readings                                         # noqa: E402

# How this wall wants to be shown when something else is driving it. Large
# type: a few meters and one row of services, read from across a room.
FONT = 18
SECS = 60
FPS = 1.0

# Services are the operator's to name, so there are none unless asked for.
DEFAULT_SERVICES = []


def _system_disk():
    """The volume the system is on: the system drive on Windows, and on a
    Mac the data volume, since `/` there is a sealed snapshot whose use says
    nothing about how full the disk is."""
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    if os.path.isdir("/System/Volumes/Data"):
        return "/System/Volumes/Data"
    return "/"


DEFAULT_DISKS = [("disk", _system_disk())]

_theme = panel.DEFAULT_THEME
_machine = None
_wall = None


def collect(stop, services=None, disks=None):
    """Start every clock; the rotator's hook. `services` and `disks` are
    [(label, command)] and [(label, path)], the defaults where not given."""
    global _machine
    if _machine is None:
        _machine = readings.Machine(
            DEFAULT_SERVICES if services is None else services,
            DEFAULT_DISKS if disks is None else disks, gate=_blanked)
    _machine.start(stop)
    stop.wait()


def _blanked():
    return _wall is not None and _wall.blanked


def render(cols, rows):
    """Lay out the latest readings at today's area.

    cols and rows are the current truth, not startup's. Nothing is cached
    against a size, so a resize is correct by construction.
    """
    if _machine is None:
        snap = None
    else:
        snap = _machine.snapshot()
    if snap is None:
        grid = [[bitwall.cell(" ") for _ in range(cols)] for _ in range(rows)]
        msg = "machine: waiting for readings"
        for i, ch in enumerate(msg[:cols]):
            grid[0][i] = bitwall.cell(ch, "888888")
        return grid
    return [[bitwall.cell(g.ch, g.fg, g.bg, bold=g.bold) for g in row]
            for row in panel.frame(cols, rows, snap, _theme)]


def _parse_pairs(items, what, ap_default):
    """label=value pairs from the command line, bad ones reported and skipped."""
    out = []
    for item in items or []:
        label, sep, value = item.partition("=")
        if not sep or not label.strip() or not value.strip():
            print(f"machine: ignoring --{what} {item!r}: want label=value",
                  file=sys.stderr, flush=True)
            continue
        out.append((label.strip(), value.strip()))
    return out or ap_default


def _clamp(value, lo, hi, name):
    if value < lo or value > hi:
        fixed = min(hi, max(lo, value))
        print(f"machine: --{name} {value} out of range, using {fixed}",
              file=sys.stderr, flush=True)
        return fixed
    return value


def main():
    global _theme, _machine, _wall
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default="localhost",
                    help="the receiver to attach to")
    ap.add_argument("--theme", default=panel.DEFAULT_THEME,
                    help="one of " + ", ".join(panel.THEMES)
                         + f" (default {panel.DEFAULT_THEME})")
    ap.add_argument("--fps", type=float, default=FPS,
                    help=f"frames a second, 0.2 to 10 (default {FPS:g})")
    ap.add_argument("--font", type=int, default=FONT,
                    help=f"font size to ask the receiver for (default {FONT})")
    ap.add_argument("--service", action="append", metavar="LABEL=COMMAND",
                    help="a service, up when COMMAND exits zero; repeatable")
    ap.add_argument("--disk", action="append", metavar="LABEL=PATH",
                    help="a volume to show; repeatable, in place of the "
                         "system's own")
    ap.add_argument("--tailscale", metavar="PATH",
                    help="the tailscale binary (default: look for one)")
    args = ap.parse_args()

    if args.theme not in panel.THEMES:
        print(f"machine: no theme {args.theme!r}, using {panel.DEFAULT_THEME}",
              file=sys.stderr, flush=True)
        args.theme = panel.DEFAULT_THEME
    _theme = args.theme
    fps = _clamp(args.fps if args.fps == args.fps else FPS, 0.2, 10.0, "fps")
    font = _clamp(args.font, 6, 60, "font")
    services = _parse_pairs(args.service, "service", DEFAULT_SERVICES)
    disks = _parse_pairs(args.disk, "disk", DEFAULT_DISKS)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    _machine = readings.Machine(services, disks, tailscale=args.tailscale,
                                gate=_blanked)
    _machine.start(stop)

    w = bitwall.attach(args.target, min_frame=1 / fps)
    _wall = w
    with w.transition():
        w.set_font_size(font)
        w.set_source(render)
    # A hint for a gallery page, not a layout assumption: the engine reads
    # the real area every frame. 140x40 is the full layout with room for
    # three-row graphs, which is what this looks right in.
    w.meta(name="machine")
    cols, rows = w.area()
    ts = readings.find_tailscale(args.tailscale)
    print(f"machine -> {args.target}  {cols}x{rows}  "
          f"{panel.layout_for(cols, rows)}  theme={_theme}  {fps:g}fps  "
          f"services={len(services)}  tailscale={ts or 'not found'}",
          file=sys.stderr, flush=True)
    try:
        stop.wait()
    finally:
        stop.set()
        w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
