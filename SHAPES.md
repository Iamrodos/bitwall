# Shapes a wall draws

A receiver draws some code points as geometry instead of asking the font:
blocks, box lines, the diagonal halves, the rounded corners, braille, the
sextants and octants (docs/writing-a-sender.md, *What the receivers draw as
geometry*).
The sender composes cells; the receiver draws what each cell names, at its
own cell size, and knows nothing about what the cells add up to. This file
is the design for taking that further: new shapes in the receivers, and
helpers that draw bands, panels and boxes with them. Nothing in *New
shapes* or *Helpers* is built yet.

## The rules

**Every cell draws only itself.** A glow, a shadow or a curve that spans
cells is made of pieces the sender places, one per cell, each drawn by the
receiver from its code point alone. A receiver effect that paints across
cells paints something no cell asked for, breaks the painter's
damage-only redraw, and stops the grid of characters being the whole
picture.

**A shape is ink over ground.** `fg` is the shape, `bg` is what shows
around it, as with every other cell. A corner of a panel is the panel's
colour over the page's.

**The receiver owns the measurements.** The gap between tiles and the
radius of a one-cell corner are fixed fractions of a cell in the
receiver's table, so every wall draws the same shape for the same code
point and a sender never sends a pixel.

**A sender asks before it uses anything new.** `w.can(chars)`, and a
fallback when the answer is no. The helpers do the asking so a sender
does not.

## What is drawn now

Every standard character in the table in docs/writing-a-sender.md, so every pattern of a
cell cut 2x4 is one character, exact at every cell size and seamless
across cells, and meaning the same in a terminal that draws them as
geometry (Ghostty, kitty, WezTerm, foot). The smooth mosaic slopes,
U+1FB3C-1FB6F, are not drawn: they are polygons, where everything the
receivers draw is rectangles, triangles and arcs. They are the only
standard slope shallower than `◢`, which on a 1:2 cell is about 63
degrees.

TILE, in `letters`, is the 3x5 bitmap of BLOCK as a panel of lit tiles
with a hairline gap and rounded outer corners, drawn as one kitty picture
over the cells the text takes. It needs no new shapes, since the picture
is exact at whatever size the cells are; its gap and radius are fractions
of a tile's side (`TILE_GAP`, `TILE_RADIUS`).

## New shapes

Private code points, drawn only by bitwall's receivers: shapes a grid of
characters otherwise cannot draw, as a Nerd Font's are, but worked out at
the wall's own cell size, so they are exact at any size, and a sender
knows whether a wall has them.

**Cell shapes.** One cell, with any of its four sides inset by the gap and
any of its four corners shaped, round or cut; a corner can be shaped only
where both its sides are inset. That is 47 shapes with round corners only,
78 with one style a cell, 136 with the styles mixed. They make a panel's
edges and a band's straight sides; a panel's inner cells are `█`.

**Corner slices.** A corner rounder than one cell is shared across a block
of cells, each drawing its slice of the arc, named by kind (filled, for a
band or panel, or line, for an outline), corner, radius in half rows from
one to eight, and the cell's position in the block. A radius of r rows is
2r cells wide on a 1:2 cell, so a block is at most 8x4. A filled slice is
inset by the gap where the shape's edge is, so it meets the edge cells
either side; a line slice is as thick as a light box line and leaves the
cell through its middle, so it meets `─` and `│`. The receiver computes a
slice when it fills its atlas, as it does for blocks, rather than storing
it.

A band five rows tall with round ends has three clear rows inside: room
for HALF text beside three lines of small text. An outline gives its top
and bottom rows to the line.

**Code points.** Powerline's where it has the exact shape: the arrows
U+E0B0 and U+E0B2 (a band's pointed end in one cell) and the half circles
U+E0B4 and U+E0B6 (a one-row pill's ends), so a terminal with a Nerd Font
looks close to the wall. Everything else in Supplementary Private Use
Area-B, which Nerd Fonts does not use:

- U+100000 + (style << 8) + (sides << 4) + corners: the cell shapes,
  style 0 round and 1 cut; sides top, right, bottom, left and corners
  top-left, top-right, bottom-right, bottom-left, four bits each.
- U+101000 + (kind << 10) + (corner << 8) + ((halfrows - 1) << 5) +
  (row << 3) + column: the slices, U+101000-1017FF.

Both advertised as ranges in `screen glyphs=`, so `w.can()` answers for
them.

Open: which radii are offered; one corner style a cell or mixed; the gap
and radius, judged on the glass (about 0.14 and 0.7 of a cell's width
look right); heavy and double line slices, or light only.

## Helpers

Two functions in bitwall, beside `letters` and for the same reason: every
sender wants them, drawn the same whoever draws them.

    band(grid, x, y, cols, rows, fg, bg, ends="round", wall=w)
        ends: square, point, chamfer, round, pill, tab
    box(grid, x, y, cols, rows, fg, bg, fill=True, radius=1, corners="round", wall=w)

Both return where text fits inside, as (x, y, cols, rows): the rows the
shape fully covers, past the curve. Each draws the best the wall can:

1. the new shapes;
2. the characters there are, by best match: each cell of a filled curve
   the drawn character with the fewest pixels wrong, sampled at 8x16, and
   each cell an outline crosses the box piece joining the sides it
   crosses, `─ │ ╭ ╮ ╰ ╯`; a pointed end `◢◥`/`◣◤` stacked, as the sysinfo
   header draws it;
3. `█` and `┌─┐`.

The second level needs nothing new from a receiver, so the helpers are
useful before the new shapes exist.

The same idea goes further: lines at any angle snapped to eighths of a
cell's edge (sparklines, clock hands, gauges); circles and rings from
slices (dials, round LED emitters); gradients across cells for shadows
and glows; heavy, dashed and double outlines.
