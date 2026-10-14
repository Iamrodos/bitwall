# Controlling senders

How a controller finds senders, starts and stops them, and knows which
wall each is on -- the same controller that finds walls and talks to them.

## Principles

- **No daemon on a sender host.** Everything a controller needs is files
  and scripts on the host, found over ssh the way walls are found. Any
  number of controllers can run at once, and one that restarts loses
  nothing.
- **A sender is opaque.** It may be one process, several (the LED sender
  and its effects), or something else entirely. The controller only calls
  the scripts the sender lists and never assumes what it looks like.
- **The wall is the truth.** Which wall a sender is on is confirmed by what
  the wall says, not by what the sender or its record claims.

## Hosts

The controller has a list of sender hosts beside its list of receiver
hosts. They may be different machines, and one machine may be both: a
Mac may run senders and a web receiver.

Everything below lives on disk, under `~/.bitwall/` on the sender host,
never in the run directory: a Pi's `/run/user` is in RAM and gone at
every boot, and what is running has to be known after one.

    ~/.bitwall/senders/       the catalogue
    ~/.bitwall/catalogue.json what this host hides of it
    ~/.bitwall/running/       one record per instance

Nothing is installed there beyond those. What the controller runs on a
host -- walking the catalogue, the standard `stop` and `status` -- it
sends over ssh each time, so there is no copy on any host to go stale.

## The catalogue: what can run

`~/.bitwall/senders/` on each sender host, holding `*.json` files, one per
sender.
Discovery walks it and follows symlinks, so publishing a sender is a link:
to one file, or to a directory of them from another repo (demos, say).
What a host's catalogue holds is links -- a whole catalogue directory, or
single entries -- so an entry is the repo's and changes with it; a plain
`.json` file works too, and is the host's own copy, which nothing updates.

- Symlink loops are detected, not followed forever; depth is limited.
- Script paths resolve from where the `.json` really lives, not from the
  link, so a sender in another repo can say `"start": "./main.py"`.
- A sender's identity is its path under the catalogue (`demos/video`),
  since two trees may both have a `video.json`.
- A file that is wrong -- not JSON, a script missing, an option that
  does not check -- is reported and left out, whole, and the rest listed.

bitwall's own senders are described in `senders/catalogue/`, linked in on
a sender host as `~/.bitwall/senders/bitwall`, so they are `bitwall/picture`
and the like:

    mkdir -p ~/.bitwall/senders
    ln -s "$PWD/senders/catalogue" ~/.bitwall/senders/bitwall

A machine's own senders sit beside that link, as files or
links of their own; nothing is put inside it, since that is the repo.
A Pi, which runs only the senders deployed to it, links each entry on
its own, `~/.bitwall/senders/hdmi.json` to
`~/bitwall-src/senders/catalogue/hdmi.json`.

### Whether it can run here

An entry may name an `available` script, found as `start` is, that says
whether the sender can run on this host: HDMI in needs a USB capture
dongle, and says so where there is none.

```json
{ "label": "HDMI in", "start": "../hdmi/main.py",
  "available": "../hdmi/available", ... }
```

The walk runs every `available` script at once, each with two seconds
to answer. Exit 0 is available; anything else is not, and the first line
it printed is the reason; one that takes longer is not available, "took
too long to say". A sender that is not available is not offered: the
control page shows it greyed with its reason and no card, `list` prints
it under `unavailable:`, and a start refuses it:

    bitwall-senders: bitwall/hdmi is unavailable here: no USB capture device

On Windows a `.py` script, or one whose `#!` runs Python, is run by the
walker's Python, so it keeps to the standard library, and a `.exe`, `.cmd`, `.bat` or `.com` as it is. A shell
script cannot be run there, so a sender whose `available` is one is
unavailable on Windows, and says that is why.

### What a host hides

`~/.bitwall/catalogue.json`, beside the catalogue, hides what this host
should not offer although a linked catalogue holds it:

```json
{"hide": ["bitwall/hdmi", "bitwall/games/*"]}
```

Names are as `list` shows them, with fnmatch's `*`, `?` and `[...]`; a
`*` matches across `/` too. A hidden sender is left out of the walk
whole -- not listed, not offered, its file not even read -- and a start
does not know it. `list` says how many, `3 hidden (~/.bitwall/catalogue.json)`.
No file hides nothing; one that is not JSON, or whose `hide` is not a
list of names, hides nothing and is reported as a problem.

```json
{
  "label": "Films",
  "description": "A film from the media directory, drawn in cells",
  "start": "./main.py",
  "options": {
    "font": {"type": "font", "label": "Font size",
             "description": "Cell size on the wall", "default": 10,
             "min": 6, "max": 40},
    "mode": {"type": "choice", "label": "Picture mode",
             "description": "How pixels map to cells: half is colour, fine is detail",
             "choices": ["half", "quad", "fine"], "default": "fine"},
    "shuffle": {"type": "bool", "label": "Shuffle",
                "description": "Play the directory in random order", "default": false}
  }
}
```

### Where an entry goes

An entry says what it sends and where it goes; left out, it is a cells
base:

```json
{ "label": "Fireflies", "start": "../fireflies/main.py",
  "type": "video", "place": "above", "options": { ... } }
```

- `type`: `cells` (the default) or `video`.
- `place`: `base` (the default), `above` or `top`, as PROTOCOL.md's
  *Places* has them. The sender is told its place in `BITWALL_PLACE`, and
  `bitwall.layer()` asks for it.
- `place: controller` is a sender that holds only a controller session
  on the wall it is started on, as `mirror` does to read its glass and
  show it on the walls of its `walls` option. Nothing is asked to make
  room; the controller session carries its id on its `open`, as a layer
  does, so the start is done when the wall's `sessions` says it, and
  `status` shows the sender on that wall as a controller, with no layer.
- A `font` option on a video layer's entry makes the entry wrong, since a
  video layer has no grid: it is reported and left out when the catalogue
  is read. A cells layer's (`"type": "cells"` above the base) is the font
  of its own grid.
- `moves`: `true` for a sender a controller may move to another wall
  while it runs (*Moving a sender*, below); false when left out. Say it
  only of a sender built on `bitwall.Wall` or `bitwall.layer()` that has
  been moved and looked at, since the signal that moves it ends a
  process that does not catch it.

## Verbs

`start` is a script named in the `.json`. `stop` and `status` may be
too; left out, the controller uses its standard ones, which act on the
pid, and most senders need nothing else. A sender that is more than one
process (the LED sender and its effects) names its own.

The controller runs `start` so that it outlives the ssh session that ran
it: detached (`setsid`, no terminal, stdin from nowhere), with the pid
written into the instance's record before it returns, so `stop` and
`status` have something to act on and a controller that went away took
nothing with it.

| verb     | what                                                    |
|----------|---------------------------------------------------------|
| `start`  | run it: options, then the wall last as `host:wall`      |
| `stop`   | stop it; optional, the standard one ends the pid        |
| `status` | is it up; optional, the standard one checks the pid     |
| `move`   | not a script: for an entry that says `moves`, the record and SIGUSR1, and the library moves it. Any other is moved by `stop` then `start` |

## Options

Every option is a launch argument, `--<name> [value]`, with the
wall last. Types:

- `bool`: the flag present when on
- `choice`: a fixed list, `--mode half`
- `font`: a known type the controller knows how to render; a whole
  number, within `min` and `max` if given, and always within 1..400
- `walls`: other walls a sender is told of, for one that shows a wall on
  others; a comma-separated list, `--to pi-a.local:0,pi-b.local:1`, each
  address written as a target is (`host`, or `host:wall`) and checked by
  the same rules, none twice. An address may end in what that wall is
  sent, `@WxH`, `@WxH/fps` or `@/fps` (`--to pi-a.local:0@1920x1080/15`),
  which the sender reads and the controller passes on. An empty list is the option not given, so
  the flag is left out; a list that is given names from `min` (default
  1) to `max` (default 64) walls. A `default` is a list as well, empty
  when left out. It is passed on as written, so the sender reads its
  walls as it would read its own target. The page offers it as a text
  field and checks it the same way before a start is asked for.

Every option, and the sender itself, has a `label` and a `description`.
The controller checks each value against its type and choices, passes them
as an argv and never as a shell string, and only ever starts catalogue
entries.

## What is running

A controller starting a sender writes `~/.bitwall/running/<id>.json`
first, with an exclusive create. It holds the sender, the wall, the
options, the pid and when it started, and the sender's output goes to
`<id>.log` beside it; the sender may keep its own state there too, or
anywhere it likes. A record whose `status` says the sender is gone is a
dead instance, shown as one until `stop` clears it.

The id is the sender host's name and a random part (`mac-7f3a2c`). The
record is named by it, not by the wall, and keeps the place its entry
asked for. Whether a place is free is asked of the wall's `sessions`,
not of a measure: a base start refuses a wall whose base is held,
whoever started it -- by hand, from another host -- and names what is
there; a layer start refuses a wall that takes no layers, or has every one held.
While it asks and starts it holds a lock on the sender host, keyed on
the wall, so two starts from one host cannot both find the place free.
Two hosts starting onto one wall in the same second are not guarded
against.

    $ bitwall-senders start bitwall/fireflies pi.local:0
    started mac-9c21f0 on pi.local:0 above, layer 2
    $ bitwall-senders start bitwall/fluid pi.local:0
    bitwall-senders: pi.local:0's base has sysinfo (mac-7f3a2c); stop it first, or --replace
    $ bitwall-senders start --replace bitwall/fluid pi.local:0
    stopped mac-7f3a2c (sysinfo); started mac-2b90e1 on pi.local:0 base
    $ bitwall-senders status
    localhost
      mac-2b90e1  bitwall/fluid      pi.local:0 base     up, on its wall
      mac-9c21f0  bitwall/fireflies  pi.local:0 above 2  up, on its wall
    $ bitwall-senders move mac-2b90e1 pi.local:1
    moved mac-2b90e1 from pi.local:0 to pi.local:1 base

`--replace` stops the base's sender through its record where this host
has one, and otherwise starts the new one anyway, since a base opens with
`take` and displaces what is there.

`start` is given the instance id in the environment, `BITWALL_INSTANCE`,
and any place but the base, `BITWALL_PLACE`. The library puts the id in the
session's meta as a hidden key, `instance` -- off the glass, still in
every report -- for a base and a layer alike, so no sender has to, and
the wall says it on the session's line in `sessions`. A sender placed
`controller` says it on its first `bitwall.Controller`'s open, which has
no meta; the controllers it opens after, and those of a sender placed
anywhere else, say none, since its Wall or layer says it there. A sender with a base
and layers of its own opens them with its Wall's `layer()`, and all of
them say the one id: they are one sender, and a controller shows and
stops them as one. A start is done
when a session on the wall says the record's `instance`; `status` shows
the layer it is on. A move is done when the new wall's `sessions` says
the instance and the old wall's no longer does.

**A sender survives its receiver restarting.** A receiver that restarts,
or a web wall made again, breaks the sender's writer; a sender left
writing into nothing would be up to `status` with no wall having its id,
and the wall being the truth would show it often. So a sender whose
writer dies attaches again on its own, says who it is and repaints, and a
receiver restart is a blip rather than an orphan.

Several copies of one sender run at once, each on its own wall, each with
its own id.

## Moving a sender

`bitwall-senders move <id> <wall>` moves a running sender to another
wall without a restart: the same process, the same instance id and
record, and everything it holds. It asks the new wall for room as a
start does (`--replace` onto a held base), then the sender's host
(`instance.py repoint`) writes the new wall into the record -- `wall` as
the controller names it, `target` as the sender does, `localhost:<n>` on
its own machine -- and sends the sender's pid SIGUSR1. The pid alone,
never its group: only the library catches it, and a child of the
sender's would end.

The library does the rest (`bitwall/moving.py`). The first Wall or Layer
that takes `BITWALL_INSTANCE` from the environment sets the handler, on
the main thread and only where nothing else has claimed SIGUSR1 (Windows
has no SIGUSR1: there `repoint` writes `told` into the record and a
thread of the library's watches it, WINDOWS.md, *A sender host*); on the
signal it reads `target` from `BITWALL_RECORD` and calls `retarget` on
every Wall and Layer of the process that says the instance:

- `Wall.retarget(target)` takes what `before_leaving` drew off the old
  wall (letters' pictures), tells it `detach`, forgets what it said of
  itself (area, screen, caps, watching), and attaches to the new wall as
  to one that came back: meta and instance, font or grid, `want` and
  blank said again, `when_lost` called so pictures are sent again, and
  the source painted whole at the new area.
- `Layer.retarget(target)` opens the layer on the new wall at the place
  first asked for, lets the old one go, and sets `repaint_asked()` so the
  sender sends a frame that stands alone. A stream on `data` is ended,
  not carried: its `start` is the old wall's clock.

A sender may call `retarget` itself. What the library cannot move is
what a sender worked out once from the old wall and kept -- a glass size,
a choice made from `capabilities()` at start, a mirror of a session by
name -- and a sender tied to several walls (`film` with a layout,
`panes`); those leave `moves` out and are moved by stop and start.

## Presence

A controller's command, sent on its session's `in` like any other:

    ESC _ bitwall presence 0 from=presence-lounge ESC \

- **The wall does it, not the sender.** A wall goes dark on presence with
  no help from whatever is sending to it: no sender needs any support for
  it, and every wall behaves alike whatever is on it. A sender built on
  `bitwall.Wall` hears `watching 0` and stops composing, so it saves its
  work for free; one that ignores it goes on sending frames the dark wall
  does not show, which costs only the sending. A dark wall still holds its
  picture, so a controller's capture and live view go on working.
- **Separate from `blank`.** `blank` is somebody asking for the dark;
  presence is the environment. Two owners of one switch undo each other,
  so a hand-turned-off screen would light when somebody walked in.
- **The receiver combines them.** Dark is `blank` or no presence, and is
  what `display` says, with a word of its own for presence: `away`, beside
  `lit`, `blank` and `doze`, so a controller can tell somebody
  turned it off from nobody being in the room. `watching` is not dark and
  somebody able to see: always on a Pi, the visible tabs on the web.
  Senders are unchanged; they already stop at `watching 0`.
- **`from` is a free token, said back** with an age, in every report and on
  change: `presence 0 from=presence-lounge age=40`. The controller card
  shows where a wall's presence comes from, or that nothing feeds it.
- **One source per wall.** Two feeding one wall flip it; combine signals
  before they reach it.
- **The wall holds the name of what it follows**, an opaque token given at
  launch (`--presence 1=lounge` to the painter; `make kitchen
  presence=kitchen` through a web receiver's control fifo) and said in its
  report. The feeder defines what each name means (a room's motion
  sensor, a screen unlocked, anyone home, a time of day) and tells the
  walls following the names it knows. Moving a screen is one line on the
  device.
- **Anything can feed it.** A Pi with a sensor on its pins and a script
  opening a controller session on its own wall needs no network.
- Best as its own process, so presence works with no controller open and
  several controllers never each feed it. `controllers/presence-example`
  is a skeleton of one: a room's name, its walls, and a check command to
  replace with whatever really knows.

A sender never unblanks a wall on attach, or a restarted sender would
light a dark one; and because the wall holds presence rather than a
sender, a sender that restarts keeps a dark the wall was told about. A
receiver that restarts starts present; the feeder says presence again when a report
shows no `from`, and every minute besides.

## Security

The controller runs programs on the sender hosts, so it only ever starts
catalogue entries, with options checked and passed as argv. The page,
`controllers/bitwall-control`, listens on 127.0.0.1 and on the machine's
Tailscale address, found with `tailscale ip -4`, and nowhere else unless
given `--host`: a phone or tablet on the tailnet reaches it, and the rest
of the local network does not. `tailscale serve` is left to whatever else
the machine serves.

## The control page

`controllers/bitwall-control` does everything above from a page. Its
devices come from `--devices FILE`, `--device NAME=HOST`,
`$BITWALL_DEVICES` or `~/.bitwall/devices.json`
(`controllers/devices.example.json`).

- **The hat.** Each wall's sender sits on its display as a hat of its own
  colour: its name and description, and, when a controller started it, its
  record -- source, options, state, instance, pid, log -- joined to the
  wall by the instance id the wall carries. More shows with each detail
  level; a dead one is red. With controls at `min` or `all` the hat has
  stop, restart and log, the log in a modal; `off` is the walls and what
  is on them alone, and `all` adds each wall's own controls and traffic.
  The page remembers which.
- **The catalogue.** With the catalogue on, each sender host's senders are
  cards above its walls: a name, then the description, then the raw name
  and a control for each option as the detail grows. More than five are
  one row with a filter and a step at each end. A sender the host says
  it cannot run is a grey line with its reason under the cards, and no
  card to drag.
- **The stack.** Under each wall's display are its layers from its
  `sessions`, top first: the place (`base`, or the record's `above` or
  `top` and the layer it got), what it is, its instance, its frames a
  second and lines dropped from its measure line, and stop, through its
  record; a layer started by hand has no record to stop it through and
  says `hand`. The controllers fold to one line.

  ```
  top 7    notify     mac-1d40a2   2fps  0 dropped [stop]
  above 1  fireflies  mac-9c21f0  20fps  0 dropped [stop]
  base     sysinfo    mac-7f3a2c   1fps  0 dropped [stop]
           control-pi-0, presence-lounge       (controllers)
  start [Fireflies ▾] places above  fps [20 ▾]  [start]
  ```
- **Starting.** The start row under the stack, or a card dropped on a
  wall's hat or display, starts that sender there with the values chosen,
  at the place its entry gives. A layer goes straight on; a base onto a
  held base asks, then starts with `--replace`. The card carries a drag
  type of its own, so nothing else can be dropped, and a layer's card
  cannot be dropped on a wall whose `caps` says `layers=1`.
- **Moving.** A sender with a record, in a hat or on no wall, is dragged
  onto another wall and goes there: the page server asks the new wall's
  `caps` and `sessions` whether it has the place, stops the sender, and
  starts it there with the same options, `--replace` asked first onto a
  held base. A move the wall cannot take is refused with why before
  anything stops.
- **Web walls.** A device whose run directory has a web receiver's pipe,
  `bitwall-web.in`, has a name, a presence and make beside its name, and
  each wall the receiver lists a remove, which asks first, stops what a
  controller started on it, and then removes it (`make` and `remove` on
  the pipe). Shown at `min` and `all`.
- **Which receiver.** Each wall says the program keeping it and the
  version of its source (PROTOCOL.md, `receiver`), shown from `more`;
  one that is not the source the page has is red at every level, so a
  wall still to be updated is seen. At `all`, when it started and when
  its machine did.
- **A link to the glass.** A wall that says how its glass is reached
  (`reach`) has an open icon on its heading, built for the network the
  page is being seen from, and faint where none of its addresses is on
  that network.
- **Sender hosts.** A sender runs on its own host's walls and on those of
  any host its host can reach without a password, asked of the host
  itself. A Windows machine is a sender host as well (WINDOWS.md, *A
  sender host*).
- **Quick hand-overs.** A sender says `detach` as it leaves (PROTOCOL.md),
  so a wall is free for the next at once, and a replace or restart takes
  a second.
- **Every request answered.** A start, stop or restart is taken or refused
  with why; one with no answer in five seconds says so, a quiet
  connection is made again, and a page that reconnects to new code
  reloads. The page's log keeps each request and how it went.
- **On no wall.** A record no wall shows -- a sender that died after
  its wall moved on to something else, or nothing -- is a hat as a wall's
  sender is, named from its catalogue entry and the wall it was last on,
  in a row of its own between its host's catalogue and walls, and can be
  dragged onto a wall as any sender can.

## Limits

- The page moves a sender by stop and start, even one whose entry says
  `moves`; `bitwall-senders move` is the one that keeps it running.
- An option's choices are fixed in its entry, so a film list is the
  entry's rather than the host's.
- A sender's log is not capped or rolled.
- Senders are not started again from their records after a reboot.
