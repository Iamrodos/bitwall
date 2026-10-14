# Panes: several senders on one wall

`senders/panes` cuts one wall into panes, each a wall of its own that
any sender attaches to and none can tell from a whole one. Neither the
real wall's receiver nor the senders change: the real wall sees one
sender, and each sender sees a wall.

## Using it

Start it on a wall like any sender. The whole wall is then one pane, a
wall named `<wall>-<name>` on the host it runs on:

    bitwall-senders start bitwall/panes localhost:herdr        # pane `main`
    senders/panes/main.py localhost:herdr --name ticker        # or named

Cut it by asking a pane, one request at a time, as tmux's `split-window`
targets a pane, and start a sender on each pane as on any wall:

    bitwall-ctl localhost:herdr-ticker split down fluid        # ticker over fluid
    bitwall-ctl localhost:herdr-ticker share 30                # ticker 30%, fluid 70%
    bitwall-ctl localhost:herdr-fluid split down snake 30      # snake under fluid
    bitwall-ctl localhost:herdr-snake split right maze         # snake and maze
    bitwall-senders start bitwall/ticker localhost:herdr-ticker
    bitwall-senders start bitwall/fluid localhost:herdr-fluid
    bitwall-senders start bitwall/snake localhost:herdr-snake
    bitwall-senders start bitwall/maze localhost:herdr-maze

    ┌─────────────────────────────────┐
    │             ticker              │
    ├─────────────────────────────────┤
    │                                 │
    │              fluid              │
    │                                 │
    ├────────────────┬────────────────┤
    │     snake      │      maze      │
    └────────────────┴────────────────┘

The commands, taken by a pane on a controller session only, so a sender
cannot rearrange the wall it draws on:

    split right|down|left|up <name> [share]   a new pane beside this one, `share` per cent of the space
    unsplit                                   this pane goes, its space to its sibling
    share <percent>                           this pane's share of its cut
    layout                                    every pane, a line each, then `panes <n>`

Each is answered as any command is (`split fluid down-of=ticker share=50`,
`error split fluid: name in use`), and a pane says them in its `caps`,
which is how a controller knows a wall is a pane. `bitwall-ctl` sends
any command, so it drives them.

A pane's name is its own for life, whatever is cut around it. A pane
whose size changes is told its `area` and `repaint`, as on a resized
screen; its sender is never restarted. The tree is kept in
`~/.bitwall/panes/<host>_<wall>.json`, so `panes` restarted brings back the
same panes under the same names and their senders attach again on their
own. Stopping it takes its panes down; their senders wait for their
walls as any sender does.

To a controller, the real wall's base is `panes`, its meta naming the
senders in it (`ticker + fluid + snake + maze`), and each pane is a wall with its
own sessions, sender, meta and stop.

## How it is built

**A proxy wall.** One process per real wall, a sender outward
(`bitwall.attach`) and receiver cores inward. Each pane is a
`bitwall.receivers.core.Wall`, the receiver side of PROTOCOL.md that the
web receiver also runs on -- door, sessions, commands, backpressure,
meta, the base's cells kept as a grid -- with a rectangle of the real
wall for its glass. So it runs on any host that has the library, and
its panes are that host's walls: run it beside the real wall and the
names sit together.

**A tree.** Leaves are panes; a cut is a direction and its first side's
share. Splitting a pane replaces its leaf with a cut holding it and the
new one; unsplitting replaces its cut with the sibling. A split of a
split is one more node, never a second process stacked on the first. A
split that would leave any pane under 4x2 is refused.

**Composing.** Each frame the tree is laid over the real wall's area in
whole cells and each pane's grid is copied into the frame at its
rectangle; bitwall's diffing sends what changed. A pane with no sender
shows its wall's name, dim.

**What a pane says of its glass is the real wall's**: glyphs, depth,
cell size, kitty, touch. Its box is fixed at its grid, with a cell's
size at every font scaled from the real cell, so a font asked of a pane
is answered with the area it has and the font said in `area` is the real
wall's. The font is `--font`.

**Watching.** A pane is watched as the real wall is, from the moment it
is made, through the core's `set_visible`: dark when the wall is dark,
so a sender saves its work there too.

**Kitty pictures.** A pane offers kitty where the real wall does. Its
core reads each picture and placement out of its sender's stream; `panes`
keeps the pictures and sends each placement to the real wall whole
(`a=T`, since herdr draws that and drops an upload placed after with
`a=p`), at the pane's offset, cropped to the pane with kitty's source
rectangle, under an id in the pane's own range so two senders' `i=1`
never meet. A pane that moves has its pictures taken down and put up
again; the real wall coming back (`Wall.when_lost`) has all of them put
up again; leaving deletes every one.

**Picture of the glass.** A pane's `picture glass` (and so its `mirror
glass`) is the real wall's own picture of its glass, asked over a
controller session `panes` keeps on it, with the pane's rectangle cut
out -- the grid centred on the glass as the painter and the page lay it
-- and scaled to the size asked. Exact, kitty pictures and video layers
included, where the real wall takes pictures; not offered where it does
not, as on a shell wall.

**Input.** A tap on the real wall goes to the pane it falls in, its cell
and area the pane's. Keyboard and buttons go to no pane: a wall has no
focus to give them.

## Limits

- Panes are drawn edge to edge, with no border.
- A pane offers the base alone (`layers=1 types=cells`): no video layers
  or films.
- The control page lists panes as walls beside the real one, not drawn
  inside its card.
- The first pane is `main` when started from the catalogue, which has no
  free-text option; `--name` names it when started by hand.
- A sender that draws its own kitty pictures sends them again after its
  wall comes back only if it forgets them on `Wall.when_lost`, as
  `letters` does; one that does not keeps a blank where its picture was
  until it next changes.
