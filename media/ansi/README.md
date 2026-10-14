# ansi

Art packs for `senders/ansi`. None of it is in git -- it is not ours -- but
all of it is a download away.

## Where it comes from

[16colo.rs](https://16colo.rs) is the scene's archive: five and a half
thousand packs going back to the early nineties, still taking new ones.
Everything is a zip of `.ans` files, and every file has a rendered PNG
beside it made by the tool the scene agrees is right.

## Getting a pack

    mkdir -p media/ansi/pack && cd media/ansi
    curl -sLO https://16colo.rs/archive/2020/blocktronics-20th-century-blocks.zip
    unzip -q -o blocktronics-20th-century-blocks.zip -d pack

Any pack works. To find one:

    curl -s "https://api.16colo.rs/v1/pack/?filter=blocktronics&pagesize=10"
    curl -s "https://api.16colo.rs/v1/year/1996?pagesize=10"

The download path is `/archive/<year>/<pack>.zip`, and the year comes back
in that listing.

## Showing it

    senders/ansi/main.py media/ansi/pack localhost

## Checking it

`senders/ansi/verify.py` compares what the renderer produces against the
published render of the same file, row by row. It fetches those PNGs into
`media/ansi/png` the first time and uses the copies after.

    senders/ansi/verify.py media/ansi/pack blocktronics-20th-century-blocks

The second argument is the pack name, because that is what the image URLs
are keyed on. Every piece should say `ok`; a piece that does not is a
piece being read wrongly, and the row it names is where to look.
