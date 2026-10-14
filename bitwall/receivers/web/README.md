# The web receiver

Walls whose glass is a browser tab. Each is a wall like a screen of a Pi:
`<run>/bitwall/<name>/`, its door and its sessions as PROTOCOL.md has them,
a base and seven layers above it, and the Pi painter's behaviour wherever
the protocol leaves a choice.

A film on a layer's `data`, or as a video base, is decoded by the
browser (WebCodecs): the server cuts the stream into frames
(`../stream.py`), hands each to every tab half a second before it is due,
and each tab plays it out on the wall's clock, frame i at start + i/fps.
`caps` says `codecs=qoi,mjpeg`, and `h264` and `hevc` as the deciding
tab's browser says it decodes them, so a wall with no tab, or one whose
tab cannot decode, takes no film rather than one it would show black;
`hw=` names those of them the browser says it decodes in hardware. QOI
and MJPEG are pictures, not films: the server cuts them and each goes to
the tabs as a frame on `in` does, drawn by the browser. A film
damaged on the way is picked up again at its next key. A tab arriving mid-film is given
the frames since the last key; with no tab open a film's frames are let
go at their time and counted as dropped. WebCodecs needs a secure page,
so a tab on `http://localhost` or over HTTPS decodes and one on a bare
network address does not, and says it decodes no film.

    bitwall/receivers/web/bitwall-web kitchen            # port 8901, the host's run directory
    open http://localhost:8901/r/kitchen

    senders/sysinfo/main.py localhost:kitchen            # the base
    senders/fireflies/main.py localhost:kitchen          # a video layer over it

`bitwall-web` is the server, the receiver core (`../core.py`) the wall it keeps for each name,
`bitwall-screen.js` the element that stacks and paints a wall in a tab, and
`page.html` the page that holds one. `--presence kitchen=kitchen` names the
presence a wall made at startup follows; `--font` the font its cells are
drawn at until one is asked for.

## Making and removing walls

A page can only open a wall that exists. Walls are made by whoever runs
the receiver: the names given when it starts, and after that through its
own pair of fifos beside the walls and outside `bitwall/`,
`bitwall-web.in` for commands and `bitwall-web.out` for the answers:

    list                            wall <name> for each, then walls <n>
    make <name> [presence=<name>]   made <name>
    remove <name>                   removed <name>

    ssh mac.local 'printf "make film presence=lounge id=1\n" > ~/.bitwall/run/bitwall-web.in'

Each command may carry `id=<token>`, which comes back on every line it
causes. A wall's name is never all digits, so it never takes a screen's
place. The server takes a wall left by one that was killed, as the painter
does, and removes its walls when it stops.

## Where the work happens

The server keeps each wall's door and sessions: the command table,
backpressure, pings and reaping, the stack and its places, meta, measures,
pictures and mirrors. It reads every `in` the moment it is written, takes
the commands out, keeps the base's cells as a sender would read them back,
and hands the rest to every tab on the wall over a websocket: the base's
bytes as they came, and each video frame as a line and its picture, QOI,
PNG or JPEG.

The tab paints. The base's cells go on one canvas, each layer above it on
a canvas of its own, and the receiver's overlay on one over them all; the
browser stacks them, so a layer's frame is up as soon as it is decoded,
never waiting for the base. A dark wall is a black cover over the stack,
which goes on painting beneath it, so a picture of a dark wall is what
would show.

A tab joining late is given the wall as it stands, the base's cells as a
full repaint and each layer's frame, and a tab that falls 8 MB behind is
given it again instead of what it missed.

**Kitty graphics** on the base are the server's too, up to what a tab
draws (`../kitty.py`, PROTOCOL.md "A grid may carry kitty graphics"): it
takes the strings out of the base's bytes, assembles their chunks, keeps
the ids, the limits and the refusals, and a session's pictures go with
it. A tab is sent three things: `kimg`, a picture under a key, followed
by its bytes as the sender sent them, PNG or raw; `kgone`, a picture
dropped; and `kplaces`, every placement in the order they are drawn. It
decodes each with the browser and draws it over its cells, under the
text where `z` is negative. A tab joining late is sent the pictures and
placements after the cells. Pictures do not count toward falling behind:
one of 16 MB would otherwise send a tab back to the start again and
again.

## The tab that decides

A wall has one glass, and one tab is it: the page marked `primary`, else
the one in view most lately brought into view, else the first to say its
box. That tab says its box in device pixels, and a cell's size at every
font from 4 to 400 in whole device pixels, an even number across and a
multiple of four down. The wall works out its area from them as the
painter does from its font -- `font` gives the cells that fit, `grid` the
largest font whose cells cover it -- and the tab lays out with the same
numbers, so the two agree. Other tabs fit the same grid into their own box.

A resize is said once it has settled, 300 ms after the last change, so a
dragged corner costs one new area and one `repaint`. A new box is a new
glass: video layers are cleared and told the new `screen` and `repaint`,
as a new mode is on a Pi. A font change moves the cells, not the pixels,
and leaves them playing.

The deciding tab counts how often the glass is put up, by any layer, and
how long each layer's frames take to decode, and sends them each second:
the wall's `fps`, `frame` and `draw`, and each video layer's `decode`, as
the painter measures its own. `cpu` is the server's.

`picture glass` and `mirror glass` ask the deciding tab for a look at its
canvases, drawn at the size asked and handed back as QOI; a picture waits
in the asker's queue for it, so nothing said meanwhile overtakes it. With
no tab there is no glass: `pixels` is not said, and a picture of the glass
is `not offered`.

`watching` is how many tabs are in view, 0 while the wall is dark.

## Frames, and the seam in the middle of one

A pipe holds 64 KB and a wall's frame is often larger, so a repaint
arrives in pieces. The page paints on frame boundaries, the bare `ESC[0m`,
not on arrival, with a 250 ms guard in case a sender stops mid-frame, so a
repaint never sweeps down the wall.

## Drawing

Block elements are rectangles and box characters lines, because a glyph
stops short of the cell edge and leaves a seam at every boundary; the
table in AGENTS.md is what is drawn as geometry. A row is walked twice,
once for grounds and once for ink, each pass drawing the longest stretch
that shares a colour.

Drop a `wall.woff2` beside this file and it is served at `/font.woff2` and
used ahead of whatever the device has, which is how a tab gets the glyph
coverage the Pi has.
`icons.woff2` beside it, the Nerd Font icon sets, is served at
`/icons.woff2` and named by the page for their ranges alone, so a tab
fetches it only once a wall draws an icon. `make-font` cuts both from the
Meslo the Pi paints with, and the wall claims in `glyphs=` the ranges of
each file that is there.

## Zoom

The viewer's zoom is a multiplier on the font the wall is asked for, said
with the box; the wall works out the area and says it, the same path a
resize takes.

## Other pages

The Mac receiver, `bitwall/receivers/mac`, is this page in a window of its
own, and its wall is `localhost:mac` in the host's run directory. The
carousel and the stack in `demos/` embed this server through
`bitwall.receivers.web.server().build()` and add one route each, so a wall
never learns what page it is on.

## Tests

`tests/bitwall-contracts` runs every receiver clause against this one, its
glass a tab in a headless Chrome of the checks' own, driven over the
DevTools protocol; a resize there is the tab's viewport changed.

## Security

Writing to a wall needs ssh, but opening a page needs only the network,
and a tab drives its wall: its glass, its presence, its input. So
`bitwall-web` listens on 127.0.0.1 unless given `--host ADDR` (repeated
for several, `0.0.0.0` for every address), and another machine reaches
it through something that decides who may, such as `tailscale serve`:

    tailscale serve --bg --https=8902 http://127.0.0.1:8901
    bitwall/receivers/web/bitwall-web --https host.example.ts.net:8902 kitchen

A websocket whose Origin names another host or port than the request
went to is refused, so a page elsewhere cannot drive a wall through a
browser on this machine.
