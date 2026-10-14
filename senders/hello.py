#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = ["pyte", "rich"]
# ///
"""The smallest useful sender: one file, drawn with rich.

    senders/hello.py pi.local

A sender is a normal program. It imports bitwall, attaches to a receiver,
asks for a font size, and hands over grids. That is all.

This one builds its grid by rendering `rich` into a headless terminal
emulator -- the same trick bitwall's tmux source uses, applied here. So any
library that can write to a terminal can drive a wall, and neither the
stream nor the receiver learns anything about it.

The panel border is rich's. bitwall places cells and nothing else, so how
the content looks is entirely this sender's business.
"""
import io
import os
import signal
import sys
import time

# bitwall lives one directory up; a sender is a normal program that imports it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bitwall                                                # noqa: E402


def render(cols, rows):
    """The whole sender: an area in, a grid of cells out.

    bitwall calls this whenever it wants a frame. Everything here is this
    sender's own business: rich lays the panel out, and pyte turns what rich
    emits into cells. Each is imported where it is used rather than at the
    top of the file, so it is plain that bitwall needs neither.
    """
    from rich.align import Align
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.text import Text

    def to_cells(ansi):
        """Parse a terminal byte stream into a grid, using an emulator.

        Three things bite on this boundary, all about cursor position:

        \033[?7l  autowrap off. A line exactly as wide as the screen leaves
                  the cursor past the last column, and with wrap on that
                  spills onto the next row: every line costs two and the
                  content scrolls off the top.
        \r\n      not a bare \n, which moves down a row but keeps the
                  column -- so with wrap off, every line after the first
                  starts wherever the previous one ended.
        rstrip    the newline after a full-height render scrolls the screen
                  and takes the top row with it.
        """
        import pyte

        class Screen(pyte.Screen):
            # pyte raises on the private DSR some emitters send.
            def report_device_status(self, *a, **k):
                pass

            def define_charset(self, *a, **k):
                pass

        screen = Screen(cols, rows)
        body = ansi.rstrip("\n").replace("\n", "\r\n")
        pyte.Stream(screen).feed("\033[?7l" + body)
        buf = screen.buffer
        return [[(c.data, c.fg, c.bg, c.bold, c.italics,
                  c.underscore, c.strikethrough, c.reverse)
                 for c in (buf[y][x] for x in range(cols))]
                for y in range(rows)]

    out = io.StringIO()
    # force_terminal, because this is not a tty and rich would drop colour.
    console = Console(file=out, width=cols, height=rows, force_terminal=True,
                      color_system="256", legacy_windows=False)
    body = Group(
        Text("hello, wall", style="bold cyan"),
        Text(""),
        Text(time.strftime("%H:%M:%S"), style="grey74"),
        Text(""),
        Text(f"drawn by rich into {cols} x {rows}", style="grey50"),
    )
    console.print(
        Panel(Align.left(body), title="[bold]bitwall[/bold]",
              subtitle=f"[grey50]{time.strftime('%a %d %b')}[/grey50]",
              border_style="cyan", height=rows, padding=(1, 3)),
        end="")
    return to_cells(out.getvalue())


def main():
    if len(sys.argv) != 2:
        print("usage: hello.py <host>      e.g. hello.py pi.local", file=sys.stderr)
        return 2
    host = sys.argv[1]                 # anything you can ssh to

    # Opens the ssh transport and starts bitwall's background threads: one
    # renders frames, one watches for the receiver's area changing.
    wall = bitwall.attach(host)
    # The receiver relaunches its terminal at this size and reports back the
    # grid that produced. Blocks until it has.
    wall.set_font_size(22)
    # Whatever the receiver says it can show. Not what we asked for: a Mac
    # window that gets dragged reports a new area without the size changing.
    cols, rows = wall.area()
    print(f"hello -> {host}  {cols}x{rows}  (ctrl-c to stop)", flush=True)

    # From here on, cells come from render(). bitwall calls it on its own
    # thread whenever it needs a frame -- there is no draw loop to write.
    wall.set_source(render)

    # Nothing to do here but stay alive: the work is on bitwall's threads.
    # This loop exists so ctrl-c can reach detach(), which stops them.
    stopping = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.update(now=True))
    try:
        while not stopping["now"]:
            time.sleep(0.3)
    finally:
        wall.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
