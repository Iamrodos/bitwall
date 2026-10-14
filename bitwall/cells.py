"""What a cell is.

The unit both sides of bitwall agree on: a sender produces a grid of these,
`bitwall.stream` turns the grid into bytes, and a receiver paints them.

    (text, fg, bg, bold, italics, underscore, strikethrough, reverse[, faint])

`text` is a single character, or "" for the second half of a double-width
one. Colours are pyte's names or a hex string, so a grid decoded from a
terminal and one computed in python carry attributes the same way.

`faint` is a ninth field, there only when it is set, so every grid built
of eight-field cells is still a grid of cells. On a braille character it
asks a receiver to show the pattern's unlit dots too, in the cell's ground
colour, with the wall's black between them: a dot-matrix display whose
unlit dots glow faintly. Anywhere else it means nothing.
"""

BLANK = (" ", "default", "default", False, False, False, False, False)


def cell(ch, fg="default", bg="default", bold=False, italics=False,
         underscore=False, strikethrough=False, reverse=False, faint=False):
    if faint:
        return (ch, fg, bg, bold, italics, underscore, strikethrough, reverse,
                True)
    return (ch, fg, bg, bold, italics, underscore, strikethrough, reverse)
