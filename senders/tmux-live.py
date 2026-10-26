#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = []
# ///
"""A tmux session on the wall, with the keyboard wired back to it.

    senders/tmux-live.py --session 0 localhost

The session is hosted read-only as the scene, so what the wall shows is
whatever the session is doing. Keystrokes travel the other way: the
receiver reports what was pressed, this turns the name back into something
tmux understands, and `send-keys` puts it into the session.

Nothing about the wall is special to tmux. A receiver reports a key by
name and has no idea what will be done with it; deciding that a `Return`
means `Enter` in a terminal is this sender's business and nobody else's.
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bitwall                                               # noqa: E402
from bitwall.stream import TmuxSource                        # noqa: E402

#: Key names as the receivers say them, in the spelling tmux wants.
NAMED = {"Return": "Enter", "space": "Space", "BackSpace": "BSpace",
         "Tab": "Tab", "Escape": "Escape", "Delete": "DC",
         "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
         "Home": "Home", "End": "End", "PageUp": "PPage",
         "PageDown": "NPage"}


def as_tmux(payload):
    """One press, as arguments to send-keys, or None if it means nothing."""
    key = payload.get("key") or ""
    mods = payload.get("mods") or []
    if len(key) == 1:
        if "ctrl" in mods:
            return ["C-" + key.lower()]
        if "alt" in mods:
            return ["M-" + key.lower()]
        return ["-l", "--", key]      # literal, so a lone "-" is not a flag
    name = NAMED.get(key)
    return [name] if name else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("--session", default="0")
    ap.add_argument("--socket", default=os.environ.get("BITWALL_SOCK", ""))
    ap.add_argument("--font-size", type=int, default=15)
    args = ap.parse_args()

    sock = args.socket or subprocess.run(
        ["tmux", "display-message", "-p", "#{socket_path}"],
        capture_output=True, text=True).stdout.strip()

    wall = bitwall.attach(args.host, min_frame=1 / 20)
    wall.unblank()
    # Ask for the keyboard. The receiver decides whether it is willing,
    # and says in `accepts` what is actually on.
    wall.want(keyboard=True)
    wall.meta(name=f"tmux {args.session}")

    with wall.transition():
        wall.set_font_size(args.font_size)
        cols, rows = wall.area()
        # Writable: tmux will not accept send-keys while the client the
        # command resolves to is read-only, and this sender's whole point
        # is carrying keystrokes back.
        wall.set_source(TmuxSource(args.session, sock, readonly=False))

    caps = wall.capabilities()
    print(f"tmux {args.session} -> {args.host}  {cols}x{rows} at font "
          f"{args.font_size}  keyboard={caps.get('keyboard')}  "
          f"(ctrl-c to stop)", flush=True)

    send = ["tmux", "-S", sock, "send-keys", "-t", args.session]
    try:
        while True:
            got = wall.input()
            if got and got.get("kind") == "key":
                arg = as_tmux(got)
                if arg:
                    subprocess.run(send + arg, capture_output=True)
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        wall.want(keyboard=False)
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
