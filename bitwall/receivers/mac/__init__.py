"""Puts a wall in a window on a Mac.

The sender asks for a font size, reads back the area the window can offer in
cells, and streams what to paint. Everything shown is decided by the sender,
so this side holds no credentials and no content.

    bitwall/receivers/mac/bitwall-mac

It runs itself: the script header names its Python and its dependencies,
and uv fetches both on first use.

How it fits together
--------------------
One process, one command. The window is a `WKWebView` showing the web
receiver's page, served from a socket the process opens on the loopback
address, so the parser and the painter are the files a browser already
uses and a fix to either lands in both. The wall, its door and its sessions
come from the web receiver's server, imported rather than written a
second time.

Nothing listens on the network: the socket is bound to 127.0.0.1 on a port
the kernel picks, and the port is never published.

Area
----
Cells are `window pixels / cell pixels`, republished whenever either
changes. Dragging the window edge changes the area without the font size
changing, which is the case a fixed screen never produces.

The page measures the cell and the window knows its pixels, and neither
can work out the other's business alone -- so `--size` is satisfied by the
page asking the window to resize to the grid it was told to open at. The
viewer's zoom takes the same path, scaling window and font together so the
grid survives it.

What a viewer may give back
---------------------------
`--touch` and `--keyboard` say what this window is willing to pass on; a
sender asks for what it wants with `want`, and only where the two
agree is anything reported. Presses go out as what was pressed -- a cell,
or a key by name -- never as bytes, because turning either into something
a program understands is the sender's business.
"""
import os

#: The run directory the window's wall is kept in, `RUN/bitwall/<wall>/`:
#: the host's own, where a sender and a controller look for it, so
#: `localhost:mac` reaches it as `localhost:kitchen` reaches a web wall.
RUN = os.environ.get("BITWALL_MAC_RUN") or os.environ.get("XDG_RUNTIME_DIR") \
    or os.path.join(os.path.expanduser("~"), ".bitwall", "run")

#: The wall's name.
WALL = os.environ.get("BITWALL_MAC_WALL", "mac")

#: Monospaced font. MesloLGS matches the pi receiver's glyphs, including
#: Nerd Font icons; Menlo ships with macOS.
FONT = os.environ.get("BITWALL_MAC_FONT", "MesloLGS Nerd Font Mono")
FALLBACK_FONT = "Menlo"

HERE = os.path.dirname(os.path.abspath(__file__))
