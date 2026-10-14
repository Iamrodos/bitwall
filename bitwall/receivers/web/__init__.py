"""Puts a wall in a browser tab, and does the painting in the browser.

The same three things every receiver does -- take a font size request,
publish the area it can offer in cells, paint the stream -- with the one
thing a terminal cannot do: a browser really can change its font, so a size
request is honoured, the cell grows, and the area is recomputed from the box
the page gave it.

How it fits together
--------------------
`bitwall-web` is the server: one process, any number of walls, each
`<run>/bitwall/<name>/` with its door and sessions as PROTOCOL.md has them
(the receiver core, `../core.py`), so a sender cannot tell it from a screen of a Pi. It carries
what the layers draw to every tab showing the wall, and the tabs stack
the layers and paint them: `bitwall-screen.js` is the element that does,
and `page.html` the page that holds one.

What the page has to understand is small. A frame is absolutely positioned
runs -- `ESC[y;xH`, an SGR, some text -- so the parser is a cursor, a colour
and an attribute set, with no scrolling, no wrapping and no margins. A
video layer's frame is QOI, decoded in the tab and put up on a canvas of
its own over the base.

Area
----
One tab decides the wall: the one whose page says it is primary, else the
one most lately brought into view. Its box in device pixels is the wall's
glass, and it says a cell's size at every font, so the wall works out its
area from the font asked for as the painter does. A resize is said once it
has settled, so a dragged corner costs one repaint.

Zoom
----
The viewer's zoom is a **multiplier on the font the wall is asked for**,
never an absolute size, which the sender's next request would stomp.
Magnifying fits fewer cells, so the area changes and is said again, which
is the same path a window resize takes.

Many tabs, one wall
-------------------
Every tab on a wall paints the same bytes; the ones that do not decide it
fit its grid into their own box. A wall has one area, and a sender
composes for one grid.

Security
--------
Writing to a wall needs a way into the host's run directory, but opening
a page needs only the network, and a tab on a wall decides its glass, its
presence and its input. So the server listens on 127.0.0.1 unless told
`--host`, and is reached from elsewhere through something that decides who
may, as `tailscale serve` does; and a websocket opened by a page from
another origin is refused, so a page elsewhere cannot use a browser on the
machine to drive a wall.
"""
import os

#: Where the demos that embed this receiver keep their walls, reached by a
#: sender only through `BITWALL_RUN`.
RUN = os.environ.get(
    "BITWALL_WEB_RUN",
    os.path.join(os.environ.get("TMPDIR", "/tmp"), "bitwall-web"))

#: This host's run directory, as a sender's `host:<name>` finds it from
#: outside: XDG_RUNTIME_DIR where there is one, else ~/.bitwall/run. The
#: wall named `kitchen` is HOST_RUN/bitwall/kitchen.
#: On Windows it is the pipe namespace, `//./pipe/bitwall-<user>`
#: (WINDOWS.md).
HOST_RUN = os.environ.get("BITWALL_WEB_RUN") or (
    "//./pipe/bitwall-" + (os.environ.get("USERNAME") or "user").lower()
    if os.name == "nt" else os.environ.get("XDG_RUNTIME_DIR")
    or os.path.join(os.path.expanduser("~"), ".bitwall", "run"))

PORT = int(os.environ.get("BITWALL_WEB_PORT", "8901"))

#: Where the server listens unless told `--host`: this machine only.
HOST = "127.0.0.1"

#: What `--host` says, the same for the server and the demos embedding it.
HOST_HELP = ("an address to listen on, 127.0.0.1 unless given; repeat it for "
             "more, 0.0.0.0 for every one. Whoever reaches it sees every wall "
             "and drives one as a tab: its glass, its presence, its input")

#: The font stack the page renders with. Box, block and braille coverage is
#: what matters; the first family a device actually has wins, and the woff2
#: served from here is ahead of all of them.
FONT = os.environ.get(
    "BITWALL_WEB_FONT",
    '"bitwall wall", "MesloLGS Nerd Font Mono", Menlo, ui-monospace, '
    'Monaco, "DejaVu Sans Mono", monospace')

HERE = os.path.dirname(os.path.abspath(__file__))

#: A font file dropped here is served at /font.woff2 and used ahead of
#: whatever the device has, so every tab has the coverage the pi has.
FONT_FILE = os.path.join(HERE, "wall.woff2")

#: The Nerd Font icons, cut by `make-font` to a file of their own and
#: served at /icons.woff2, which the page fetches only once an icon is drawn.
ICONS_FILE = os.path.join(HERE, "icons.woff2")


def server():
    """The server module, whose file is named for the command it is rather
    than for an import: anything building on this receiver -- a window
    that hosts it, a page with several walls on it -- wants its `build()`."""
    import importlib.machinery
    import importlib.util
    path = os.path.join(HERE, "bitwall-web")
    loader = importlib.machinery.SourceFileLoader("bitwall_web_server", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


#: What a browser canvas can show, as the wall's `screen` says it. The rate
#: is what a page can paint rather than what a panel can take; both are
#: settable, since what a wall is, is decided when it is stood up.
COLOUR = os.environ.get("BITWALL_WEB_COLOUR", "true")
RATE = int(os.environ.get("BITWALL_WEB_RATE", "30"))

# What the served font covers, claimed only while the font is here; kept
# beside `make-font`, which cuts the font to these same ranges, the ones
# that meet joined and in the fewest digits: a value on the wire is at
# most 256 bytes (PROTOCOL.md), and the whole claim is near it.
SERVED = ("U+20-7E,U+A0-17F,U+2000-206F,U+2190-23FF,U+2500-27BF,U+2800-28FF,"
          "U+FF61-FF9F,U+E0A0-E0A3,U+E0B0-E0D7")
# The icon sets in the icons file, claimed only while it is here; kept
# beside `make-font`'s ICONS and the page's unicode-range for them.
ICONS = "U+E300-E3E3,U+EA60-EC1E,U+F000-F2E0,U+F400-F533,U+F0001-F1AF0"
# Drawn as geometry by the page, so there whatever the font has.
DRAWN = ("U+1CD00-1CDE5,U+1CEA0-1CEAF,U+1FB00-1FB3B,U+1FB70-1FB8B,"
         "U+1FBE4-1FBE7")
GLYPHS = os.environ.get("BITWALL_WEB_GLYPHS", ",".join(
    [SERVED] * os.path.exists(FONT_FILE) + [ICONS] * os.path.exists(ICONS_FILE) + [DRAWN]))
