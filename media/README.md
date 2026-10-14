# media

Films for the film senders to play, so a demo has something to show. Nothing
here is in git: films are large, and most are not ours to give away.

`senders/film` (decoded on the wall) and `senders/games/cellfilm` (drawn in
cells) play every film in this directory in turn when given none, and
`--film <slug>` picks one by its slug: the title in lower case, words
joined by `-`, the year left off. `senders/film` takes `.mp4`, `.mov` and
`.mkv`; `cellfilm` takes `.mp4` from here, and any file named on its
command line. Files named `sync-*` are test patterns and are skipped.

    senders/film/main.py --film tears-of-steel pi.local
    senders/games/cellfilm/main.py --mode fine "media/Tears of Steel (2012).mov" pi.local

A film free to use is Tears of Steel (2012), the Blender Foundation's,
CC BY 3.0, in 4K from
http://ftp.nluug.nl/pub/graphics/blender/demo/movies/ToS/tearsofsteel_4k.mov.zip
-- unzipped and named `Tears of Steel (2012).mov`.
