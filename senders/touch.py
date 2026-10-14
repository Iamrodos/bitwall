#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = []
# ///
"""Watch the conversation between a sender and a receiver.

    senders/touch.py localhost
    senders/touch.py pi.local:1

Light up whatever cell you press, and show every message that crosses the
channel while you do it. Press one of the buttons along the top and the
request goes out, the receiver answers, and both appear in the log.

It exists because the protocol succeeds silently. Nothing acknowledges
anything: a request is advisory and what the receiver announces afterwards
is the truth, so a command that went and a command that was dropped look
identical until the next announcement. The only way to see that working is
to watch both halves at once.

The receiver is told nothing about any of this. It knows it was pressed at
a cell; it does not know that pressing lights it up, and it has no idea it
is being watched.
"""
import os
import signal
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import bitwall                                                  # noqa: E402

FONT = int(os.environ.get("BITWALL_FONT", "16"))
HUE = [39, 43, 48, 84, 120, 156, 192, 198, 204, 210, 216, 222]
LABEL, VALUE, DIM = 244, 250, 240
OUT, IN, BTN, BTN_ON = 215, 79, 252, 226


class Button:
    """A rectangle of cells that does something when pressed.

    The receiver reports a cell and has no idea anything is there. What a
    press means is decided here, which is the whole point of the demo.
    """

    def __init__(self, label, action):
        self.label = label
        self.action = action
        self.x = self.y = self.w = 0
        self.lit = 0.0

    def holds(self, x, y):
        return self.y == y and self.x <= x < self.x + self.w


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    lit = {}                      # cell -> when it was pressed
    last = {"press": None, "n": 0, "at": 0.0}
    keys = {"last": None, "n": 0, "at": 0.0, "recent": []}
    state = {"font": FONT, "blank": False, "touch": True, "keyboard": True}

    w = bitwall.attach(host)
    w.set_font_size(FONT)
    # Inputs are off until a sender asks. The receiver may still refuse:
    # whether a window or a page gives up its clicks is its own business.
    w.want(touch=True, keyboard=True)
    time.sleep(0.8)

    def ask_report():
        w.ask()

    def font_by(step):
        def go():
            state["font"] = max(6, min(40, state["font"] + step))
            w.set_font_size(state["font"], wait=False)
        return go

    def toggle_blank():
        state["blank"] = not state["blank"]
        w.blank() if state["blank"] else w.unblank()

    def toggle_keys():
        """Only the keyboard, and touch is never offered as a toggle.

        Turning touch off would make the buttons unreachable, and the only
        way back would be a restart -- a demo that can lock the viewer out
        of itself is a bad demo.
        """
        state["keyboard"] = not state["keyboard"]
        w.want(touch=True, keyboard=state["keyboard"])

    buttons = [Button("report", ask_report),
               Button("font +", font_by(1)),
               Button("font -", font_by(-1)),
               Button("blank", toggle_blank),
               Button("keys", toggle_keys)]

    def layout(cols):
        """Place the buttons for a given width.

        Not done inside render, because a press can arrive before the
        first frame -- a wall nobody is watching is not drawn at all, and
        the first thing that happens when somebody opens it may well be a
        press. Buttons that only exist once drawn would miss it.
        """
        at, row = 2, 2
        for b in buttons:
            b.w = len(b.label) + 4
            if at + b.w > max(20, cols) - 1:      # wrap, for a phone
                at, row = 2, row + 1
            b.x, b.y = at, row
            at += b.w + 1

    layout(w.area()[0])
    # Negotiation is not instant: the receiver only speaks when asked, and
    # the first ask is retried for a few seconds in case it was restarting.
    for _ in range(30):
        if w.talking:
            break
        time.sleep(0.2)
    print(f"touch -> {host}  {w.area()}  "
          f"{'talking' if w.talking else 'polling files'}", flush=True)

    def render(cols, rows):
        now = time.monotonic()
        g = [[bitwall.BLANK] * cols for _ in range(rows)]

        def put(x, y, text, fg=VALUE, bg="default", bold=False):
            for i, ch in enumerate(text):
                if 0 <= x + i < cols and 0 <= y < rows:
                    g[y][x + i] = bitwall.cell(ch, fg, bg, bold=bold)

        # -- the buttons, laid out for whatever width we have today -----
        layout(cols)
        for b in buttons:
            on = now - b.lit < 0.6
            put(b.x, b.y, f"[ {b.label} ]", BTN_ON if on else BTN,
                "default", bold=on)

        # -- what you pressed -------------------------------------------
        for (cx, cy), when in list(lit.items()):
            age = now - when
            if age > 12:
                lit.pop((cx, cy), None)
                continue
            if 0 <= cx < cols and 0 <= cy < rows:
                g[cy][cx] = bitwall.cell("█", HUE[min(11, int(age))],
                                         "default", bold=True)

        put(2, 0, "bitwall channel", LABEL, bold=True)
        put(20, 0, f"{cols}x{rows} at font {state['font']}"
                   f"   {'talking' if w.talking else 'polling files'}", DIM)

        p, k = last["press"], keys["last"]
        line = []
        if p:
            line.append(f"press {p['cell'][0]},{p['cell'][1]}"
                        f" of {p['area'][0]}x{p['area'][1]}  x{last['n']}")
        if k:
            mods = "+".join(k.get("mods", []))
            line.append(f"key {mods + '+' if mods else ''}{k['key']}"
                        f"  x{keys['n']}")
        put(2, 4, "   ".join(line) or "press anywhere, or type", DIM)

        # -- the channel ------------------------------------------------
        put(2, 6, "channel, newest first", LABEL)
        top = 9
        room = max(1, rows - top - 1)
        # Newest first: on a phone the buttons are at the top, so the
        # answer to what you just pressed belongs directly under your
        # thumb rather than at the far end of a list.
        for i, (when, way, text) in enumerate(
                reversed(w.traffic()[-room:])):
            y = top + i
            ago = time.time() - when
            put(2, y, f"{ago:4.0f}s", DIM)
            put(8, y, "out" if way == "out" else " in",
                OUT if way == "out" else IN, "default", bold=True)
            put(12, y, text[:max(0, cols - 14)],
                VALUE if way == "in" else OUT)
        if not w.traffic():
            put(2, top, "nothing yet -- press a button", DIM)
        return g

    w.set_source(render)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.wait(0.1):
            got = w.input()
            if not got or not isinstance(got, dict):
                continue
            kind = got.get("kind")
            if kind == "tap" and "cell" in got:
                x, y = got["cell"]
                layout(got.get("area", [80])[0])
                hit = next((b for b in buttons if b.holds(x, y)), None)
                if hit:
                    hit.lit = time.monotonic()
                    print(f"  button {hit.label}", flush=True)
                    try:
                        hit.action()
                    except Exception as exc:               # noqa: BLE001
                        print(f"  {hit.label} failed: {exc}", flush=True)
                else:
                    lit[(x, y)] = time.monotonic()
                    print(f"  tap {x},{y}", flush=True)
                last.update(press=got, n=last["n"] + 1, at=time.monotonic())
            elif kind == "key" and got.get("key"):
                mods = "+".join(got.get("mods", []))
                shown = f"{mods}+{got['key']}" if mods else got["key"]
                keys["recent"].append(shown)
                del keys["recent"][:-12]
                keys.update(last=got, n=keys["n"] + 1, at=time.monotonic())
                print(f"  key {shown}", flush=True)
    finally:
        w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
