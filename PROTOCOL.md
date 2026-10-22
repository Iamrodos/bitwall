# The bitwall protocol

A wall is a door and the sessions opened through it. A program that wants
to draw on a wall, or to talk to it, asks at the door for a session of its
own: a pipe to write its frames and commands into, and a pipe the wall
answers on. Nothing else is in the contract. There are no state files.

## Where walls live

### The run directory

A host keeps its walls in its run directory, and says where: a
non-interactive shell on it finds it as
`${XDG_RUNTIME_DIR:-$HOME/.bitwall/run}`, so a sender or a controller asks
rather than knowing what kind of machine it is talking to. On a Pi that is
`/run/user/<uid>`; a Mac usually has no `XDG_RUNTIME_DIR`, so there it is
in the home directory. A Windows host's is `//./pipe/bitwall-<user>`,
and every fifo below is a named pipe at the same path, reached through
`bitwall/relay.py` from another host (WINDOWS.md). A receiver must put its walls where that shell
finds them, whatever its own environment says, or nothing will find
them. `BITWALL_RUN` overrides the host's run directory from the sending
end, for every host, and `BITWALL_LOCAL_RUN` for the sender's own machine
alone; a receiver may be told to put its walls elsewhere. An override
at either end is for tests and special cases: walls placed or sought
elsewhere are invisible to a controller, which only looks where the
host's shell says.

### Every wall is a directory under `bitwall/`

    <run>/bitwall/
    ├── 0/                     a screen
    │   ├── door.in            fifo: open sessions here; it never answers
    │   └── s/<name>/          one per session, named by its caller
    │       ├── in             fifo: this session's frames and commands
    │       └── out            fifo: what the wall says to this session
    ├── 1/                     another screen
    └── eink/                  a wall made by naming it

Everything under `bitwall/` is a wall: a directory counts as a wall only
if its name is a wall name and it holds a fifo named `door.in`, so a
directory a receiver is still building under a `.` name is never found. A
controller finds a host's walls with one listing of `<run>/bitwall/`.

Any number of receiver programs may run on a host, and each makes the
wall directories it owns: one painter for two screens, a painter per
screen, an e-ink receiver beside them. Nothing remote knows or cares
which program owns a wall; it opens `<wall>/door.in`. A receiver's own
files, such as a pipe for making and removing walls or a note of when it
last saw a sender, live outside `bitwall/` and are not part of the
protocol.

### Wall names

A wall's name is a token of one to thirty-two lower case letters, digits,
`_` and `-`, starting with a letter or a digit, and is only ever compared
as written: `001` is `001`, not `1`. Lower case only, because a Mac's
filesystem takes two names differing in case for one directory. Screens
are numbered: a machine with several screens has one wall per screen,
numbered `0`, `1`, `2` without leading zeros. Other walls are named, and
a name is never all digits, so a named wall can never take a screen's
place.

A sender names a wall as `host:<wall>`, and `host` alone is wall `0`.
`localhost` is this machine, asked the same way without ssh; a bare IPv6
address can only name wall `0`, so use a host name for any other.

### Claiming a wall

A receiver builds a wall's directory under a temporary name beginning
with `.`, which is never a wall's name, and renames it into place, so
nothing ever finds half a wall. If the name is taken, it takes the wall
over only when `door.in` has no reader: that is a wall left by a receiver
that was killed, and the receiver removes it and renames its own into
place. A wall whose door is read belongs to a live receiver, and the
claim is refused. A receiver that stops in good order removes the walls
it made.

A caller can tell a live wall from a dead one without waiting: opening
`door.in` for writing without blocking fails at once when nothing reads
it. A caller does that first, so a dead door is known before any request
is made and not after a wait.

## The door

The door only takes requests. Answers come back on the caller's own
session, whose path it already knows, so two callers at the door at once
never read each other's replies. Each request is one line, ending in a
newline, in one write of at most `PIPE_BUF` bytes, so requests from
different callers never mix.

    open <name> <role> [key=value ...]

`<name>` is the caller's, a token of one to thirty-two of
`[A-Za-z0-9_-]`, and names the session's directory, `s/<name>/`. The
caller keeps it unique: a library names its sessions so that two callers
never choose the same one, `<name>-<pid>` style. A name whose directory
already exists is in use, however the filesystem compares names. `glass`
is not a name: it is what `picture` and `mirror` call the glass.

`<role>` is `layer` or `controller`.

The options are all optional:

| key | meaning | in v1 |
|---|---|---|
| `z=<place>` | where in the stack: `0` the base, `above`, `top` or a number, as *Places* below; `0` when left out | all of them |
| `type=<t>` | what the layer's stream carries; `cells` when left out | `cells` or `video` at 0, `video` above it, and `cells` where `above=` says it |
| `take=1` | displace whoever holds the base | taken by every wall, for the base alone |
| `wait=1` | a base in waiting | reserved |
| `input=all` | all input, drawn or not | reserved |
| `data=1` | a video layer's stream on a fifo of its own, *The data pipe* | on a wall whose `caps` says `data` |
| `codec=<c>` | what that stream is: `hevc`, `h264` or `qoi` | with `data=1` |
| `fps=<n>` | the stream's frames a second, played out by the wall | with `data=1` |
| `start=<ms>` | the Unix time in ms its first frame is shown at | with `data=1` and `fps` |
| `instance=<id>` | the id a controller gave the sender it started, kept for the session's life for `sessions` to say | all of them |

`z`, `type`, `take`, `data`, `codec`, `fps` and `start` are for a layer
only. `instance` is a layer's or a controller's, the one key a
controller's open takes: a sender that holds no place on a wall, only a
controller session there (a mirror reading its glass), is found on it by
that session's `instance`, as any other sender is by its layer's. An
`instance` is one to sixty-four lower case letters, digits and `-`,
starting with a letter or a digit. A key the wall does not
take, or a value it does not take for that key, is refused, never
ignored, so a caller learns at once what a wall cannot do. On a v1 wall
`wait=1` and `input=all` are refused with `no-such-key`, as are `data=1`
on a wall that does not say `data` or on a cells layer, `codec`, `fps`
and `start` without `data=1`, `start` without `fps`, and
`take=1` for a layer above the base, which is never taken from its
holder, any `z` that is not a number, `above` or `top`, an `fps` that is
not a number above 0 and at most 240 (`23.976` is one), a `start`
that is not whole ms, and an `instance` of any other form. `full` is for
a wall that takes the request and has no place free.

### `opened`, or `error open`

The receiver makes `s/<name>/` with `in` and `out`, making `out` last, so
a session whose `out` exists is whole. On `out` it says:

    hello v1 line
    opened <name> z=<n>

`z` is the layer the wall gave, a controller's `opened` saying none.
A layer opened with `data=1` is told `opened <name> z=<n> data`, and
`s/<name>/data` is there before it is said. Or the wall refuses:

    hello v1 line
    error open <name> <reason>

Every `out` starts with `hello v1 line`. The reasons are fixed words:

    busy          the base is held, and the request has no take=1
    full          there is no such place: z names a layer the wall has
                  not got, no slot above the base is free, or the wall
                  has all the controllers it takes
    no-such-type  a type the wall does not take at that place
    no-such-codec a `codec` the wall does not take, or none, with data=1
    no-such-role  a role other than layer or controller, or none
    no-such-key   a key, or a key's value, the wall does not take for
                  this role

A malformed request, one named `glass`, or one whose name is in use, is
dropped: nothing is
made and nothing is said, and the session holding that name hears nothing
of it. `bad-name` and `name-in-use` name those two reasons a request is
dropped; they are not words a wall says.

The caller opens `s/<name>/out` for reading once it exists, and waits 5
seconds for it to appear before treating the request as dropped. What the
wall says before the caller opens it is held for it, up to what the pipe
holds, and at least 8 KB.

A refused session's directory is removed once its `out` has been read, or
10 seconds after the refusal, whichever is first, and until then the name
is in use. A caller that asks again under the same name at once may be
dropped for it; it waits for the directory to go, or picks another name.

## Sessions

A **layer** draws. A **controller** does not: it sends commands and hears
answers. A sender is a layer; the control page, a presence feeder and
`bitwall-ctl` are controllers.

**Frames and commands share a session's `in`.** The stream in it is only
ever its writer's own. `cat > in` is a sender.

**Each session hears only what concerns it**, on its own `out`: its own
answers, and the lines listed in *What each session hears*.

### The life of a session

The receiver reads each `in` without blocking and without holding it
open for writing, so it sees its writer go.

- A session is **connected** once the first byte arrives on its `in`. A
  library's first write is `report`.
- It **ends** at end of file on `in` after that; when it says `close` or
  `detach`; or when a `ping` goes unanswered, which is what tells a
  sender whose ssh connection has died, still holding `in` open, from a
  live one. A layer is held to a `ping` only once it has answered one: a
  layer that has never said `pong` is judged by its `in` alone, and its
  silence never ends it. A controller is held to every `ping`.
- A session nobody writes to within 10 seconds of `opened` is removed.
- When the receiver ends a session it says, as the last line on its
  `out`:

      closed <name> <reason>

  with `eof`, `asked` (for `close` and `detach`), `timeout` (an
  unanswered ping, or nothing written within 10 seconds) or `displaced`.
  It then stops reading `in`, closes both fifos and removes the session's
  directory, and the name is free again. A writer still writing to `in`
  finds nobody reading it. A `closed` nobody has yet read is kept 10
  seconds for a reader before the directory goes.

A session's `out` comes to end of file only after `closed`, or when its
receiver has gone. A reader that reaches end of file with no `closed`
before it has lost the wall, not been sent away.

A session's life is its `in`'s. When the reader of its `out` has gone
while `in` stays open, the session goes on, and what it is told goes
nowhere.

### Silence is not absence

A wall showing something static is sent nothing, for as long as nothing
changes, so bytes arriving are no measure of whether the writer is still
there. The wall asks instead. It says `ping` on a session's `out`, and
the session has 2 seconds to answer `pong` on its `in`. While the wall is
watched, a session is asked after 5 seconds of quiet. While it is not, a
session that has answered is asked once a minute, and one that has never
answered is asked once only. The question is put whether or not anybody
is watching, so a sender that dies behind a dark wall is noticed.

A layer that has answered once is held to it: the next `ping` it fails
to answer ends it, `closed <name> timeout`. A layer that has never
answered is not; the question is forgotten and its life is its `in`'s. A
session is never ended merely because it had nothing to say.

A controller answers every `ping`, and one that does not answer within
the 2 seconds is ended, `closed <name> timeout`, whether or not it has
ever answered before.

Nothing about this is an acknowledgement. `pong` answers a question about
the session, not about any frame or request; a wall with no doubt about a
session never asks.

A session may ask the same of the wall: it says `ping` on its `in`, and
the wall answers `pong` on its `out`.

### `detach` and `close`

    ESC _ bitwall detach ESC \
    ESC _ bitwall close ESC \

A session says either as the last thing it writes, after its last frame,
and the receiver ends that session at once, as it would one that failed
to answer a ping, saying `closed <name> asked`. A sender says `detach`;
the two are the same. When the base goes, its metadata goes with it, the
wall shows that it has no sender, and the base is free for the next
`open`. A sender that is killed, or crashes, says nothing, and its
session ends at end of file or by `ping`.

## Requests are advisory; announcements are the truth

A sender asks for a font size; what is true is whatever area the receiver
then announces. A receiver may honour a request, ignore it, or do
something partial, and the sender learns the outcome by being told the
new state rather than by being acknowledged.

So there are no acknowledgements and no waiting. A command that is
dropped is not a silent failure: the state simply does not change, and
the sender can see that.

The receiver announces; it is never polled. It says what has changed as
it changes, and everything on request (`report`).

## Commands

Commands are **APC strings**: `ESC _ bitwall ... ESC \`.

A terminal ignores an APC string it does not recognise; that is what the
sequence is for. So a layer's stream stays paintable: piping it straight
at a terminal shows the wall, and the commands pass through invisibly.

That keeps the property that a receiver is anything that can read a pipe.
A receiver that wants to act on commands can `tee` the stream and pull
them out with `awk`, sending the paintable half one way and the commands
the other. The demultiplexer can be a pipeline; it does not have to be a
program.

    ESC _ bitwall report ESC \
    ESC _ bitwall sessions ESC \
    ESC _ bitwall font 14 ESC \
    ESC _ bitwall grid 240x60 ESC \
    ESC _ bitwall glass 1280x720 ESC \
    ESC _ bitwall blank 1 ESC \
    ESC _ bitwall want touch=1 keyboard=0 ESC \
    ESC _ bitwall meta set name Snake ESC \
    ESC _ bitwall pong ESC \
    ESC _ bitwall detach ESC \
    ESC _ bitwall stats 1 at=ne show=fps,in ESC \
    ESC _ bitwall follow in ESC \
    ESC _ bitwall picture glass size=480x270 ESC \
    ESC _ bitwall mirror sysinfo-4821 every=200 ESC \

A command is at most 4096 bytes between `ESC _` and `ESC \`; a longer one
is dropped. Commands are not paint: bytes that were only commands do not
wake a dozing wall or end its status page. The base's first paint does.

### Who may say what

| command | the base | other layers | controllers |
|---|---|---|---|
| `font`, `grid` | a cells base | a cells layer, for its own grid | yes |
| `blank` | yes | no | yes |
| `want` | yes | yes | no |
| `frame` | a video base | a video layer | no |
| `clear` | a video base | a video or cells layer | no |
| `align`, `box` | a video base | a video layer | no |
| `presence`, `stats`, `follow`, `measures`, `glass` | no | no | yes |
| `report`, `sessions`, `ping`, `pong`, `picture`, `mirror` | yes | yes | yes |
| `meta`, `detach`, `close` | its own session | its own session | its own session |

A command from a role the table does not allow is not run, and is said
to be, whether or not it carried an id:

    error <command> not-for-role

### `id` says which answer is which

Any command may carry `id=<token>`, one to thirty-two of `[A-Za-z0-9_-]`.
Every line the command causes to be said ends with the same word:

    ESC _ bitwall font 16 id=k3 ESC \
    area 120x33 16 id=k3

For `meta`, whose value is the rest of the line, the id goes straight
after `meta`: `meta id=k3 set name Snake`.

A command that is refused, or has an argument that is wrong, says
`error`, with the command's id if it had one:

    error <command> [<argument>:] <reason...> [id=<token>]

    error stats at=zz: unknown point id=7
    error frob unknown command id=q
    error font id: not a token
    error blank not-for-role

Two refusals are said only to a command that carried an id: a command
the receiver does not know, and `font` with no size in it. Without an id
nobody asked to be told, and a sender sending a newer command to an older
receiver is ordinary, so a line of error for each would be noise; a
controller always sends an id, so it always hears. Every other refusal is
said either way. A command whose id is not a token is not run, and its
error has no id.

`pong` causes no lines, with an id or without, and `blank` none of its
own: what it changes is said as `display`.

## What the wall says is lines of text

Line-delimited, space-separated, `key=value` where a value is needed.
Chosen so that `grep` and `awk` are sufficient to read it, for the same
reason the frames stay paintable.

A value, or an argument, that holds what a word cannot -- whitespace, a
control, `=` or `%` -- is percent-encoded: each of those characters as
`%XX` for each of its UTF-8 bytes, and nothing else. `label Back%20room`
is the value `Back room`, and one word to every reader. A value without
those characters is on the wire as it always was, and a reader that does
not decode shows the escapes, which is legible rather than wrong. A
reader decodes only whole `%XX` runs, so a `%` that is no escape stays a
`%`. A command's name and an option's key are vocabulary and never
encoded. `meta set` is the exception in the other direction: its value
is the rest of the line, as written. In both directions; `protocol.word`
and `protocol.unword` are the two halves in Python.

    hello v1 line
    opened sysinfo-4821
    caps area font grid meta blank want presence repaint ping detach close screen watching picture mirror stats sessions follow measures frame clear layers=8 types=cells,video codecs=qoi controllers=8
    accepts touch keyboard
    area 174x42 14
    watching 1
    input key key=Return mods=ctrl
    input tap cell=3,4 area=174,42
    repaint
    ping
    screen colour=true rate=30 pixels=1920x1080 cell=12x28 glyphs=U+2500-259F,U+2800-28FF
    measures wall grid fps in out frame draw swap atlas inbox sender pong display power cpu render temp
    stats 1 at=ne size=14 show=fps,in
    session opened control-pi-1 controller - -
    error stats at=zz: unknown point id=7
    dropped 3
    closed sysinfo-4821 displaced

### What each session hears

| line | said to |
|---|---|
| `hello`, `opened`, `error open`, `closed` | the session concerned |
| a command's answer and its errors | the session that sent it |
| `area`, `repaint` | every session but a video layer, which hears `screen` and `repaint` only when the glass's pixels change |
| `ping` | each session, on its own clock |
| `watching`, `accepts` | every layer, as they change |
| `input` | the layer it is routed to: in v1, the base |
| `display`, `power`, `presence`, `stats`, `session opened`, `session closed`, `session moved` | every controller, as they change |

A session that has just connected is told `repaint`, and so is every
session whenever the wall needs a frame that stands on its own. A
controller hears more if it asks: see *What a controller hears*.

### Backpressure

Backpressure is per session. A full `in` slows only its own writer, and
there is no ordering between sessions, only within one.

A session's `out` is written without blocking, so no reader can hold up
the wall. When it backs up, the lines that say a current value (`area`,
`watching`, `accepts`, `display`, `power`, `presence`, `screen`) are merged into the latest
value and never dropped; every other line is dropped and counted, and the
reader is told `dropped <n>` before the next line that fits. `opened`,
`error open`, `closed` and the `hello` that opens an `out` are never
dropped; a report's `hello` is dropped with the rest of the report. A
picture or a mirror frame is bigger than a pipe, so it waits in the
session's queue until the reader has taken it, up to 8 MB, and lines said
meanwhile wait behind it rather than overtaking it or being lost. A
mirror frame is not queued behind another: it is dropped.

### `hello` is the one thing that can never change

    hello v<version> <grammar>

It tells you how to read everything after it, so it cannot live inside
the thing it describes: if the framing ever changed, you would have to
parse the capabilities to discover you cannot parse the capabilities.

So `hello` is the first line on every `out` and the first line of every
report, and its own encoding is frozen for good. Everything after it may
evolve. It reads `hello v1 line`.

### `caps` is what it speaks; `accepts` is what a viewer may give it

Two different questions, kept apart. `caps` is the protocol vocabulary:
which commands this receiver understands, and how much it takes.
`accepts` is whether anybody can tap the screen or type at it.
Conflating them would make a wall with no touchscreen look like an old
one.

**`caps` is a set, not a point on a line.** A version is ordered and
total. Capabilities are not like that: a wall with no touchscreen will
never do touch, and a Pi will never want a heartbeat, and neither is
older than the other. So what a receiver understands is said as a set of
names, and a sender sends only what the receiver speaks. Dropping the
unknown stays as the safety net for when the two disagree, rather than
as the mechanism.

The words are the commands and announcements it knows: `area`, `font`,
`grid`, `meta`, `blank`, `want`, `input`, `presence`, `repaint`,
`watching`, `ping`, `detach`, `close`, `screen`, `picture`, `mirror`,
`stats`, `sessions`, `follow`, `measures`, `frame`, `clear`, and `data`,
`align` and `box` where it takes a data pipe; `kitty` where its grid carries kitty
graphics (*A grid may carry kitty graphics*); `stream` where it
streams its glass raw (*The glass as a raw stream*); and `glass` where a
controller may ask its glass for a size (*`glass` asks for pixels*). A wall may take commands
of its own beyond these, and says them here the same way: a pane of
`senders/panes` says `split unsplit share layout` (PANES.md). A reader
that does not know a word passes over it. Four keys say how much it
takes:

    layers=<n>          how many layers, the base included
    types=<t>[,<t>...]  the layer types it takes
    above=<t>[,<t>...]  of those, the ones a layer above the base may be;
                        `video` alone where it is not said
    codecs=<c>[,<c>...] what a video layer's stream on `data` may be, said
                        by a wall that takes `data`
    hw=<c>[,<c>...]     of those, the ones the device decodes in hardware;
                        left out where none is
    images=<c>[,<c>...] what a `frame` on a video layer's `in` may be:
                        `qoi`, `png`, `jpeg`; a wall that takes `video`
                        and says no `images` takes `qoi` alone
    controllers=<n>     how many controllers at once
    streams=<n>         how many raw glass streams at once, every
                        session's together, said by a wall that says
                        `stream` and runs more than one; a wall that says
                        `stream` alone runs one

Every wall takes `cells`; a Pi 5 says `layers=8 types=cells,video data
codecs=qoi,hevc,h264`, the base and seven above it, and a Pi 3
`codecs=qoi,h264`. What `codecs` lists is found when the wall starts,
from the decoders it has; the web receiver says `qoi` and `mjpeg`, and
`h264` and `hevc` as its deciding tab's browser says it decodes them.
`hw=` is there for a sender choosing what to send a wall, since a
codec decoded in hardware costs the wall little and one in software may
cost it the picture: a Pi 5 says `hw=hevc`, a Pi 3 `hw=h264`, and the
web receiver those its tab's browser says it would decode in hardware,
which is the browser's word rather than a measure. A codec in `codecs`
and not in `hw` is decoded in software, or nobody knows. The painter says `images=qoi,png,jpeg` where
it was built with libjpeg and `images=qoi,png` where not; the web
receiver, whose browser decodes, says all three. The limits are there against a program that
leaks sessions; an `open` past one is `full`.

### `report` is what the wall is

    ESC _ bitwall report ESC \

Any session may ask, and the answer comes on its own `out`:

    hello v1 line
    receiver ...
    caps ...
    measures ...
    pictures qoi jpeg grid
    screen ...
    reach ...
    accepts ...
    meta ...            the base's, one line each, with ages
    meta <session> ...  each layer's above it, the same, tagged
    stats ...
    presence ...
    threads ...
    display ...
    power ...
    watching ...
    area ...

Two places are fixed: `hello` first, because it says how to read the
rest, and `area` last, so the one asking knows it has all of it. The
lines between come in any order. Lines a receiver has no use for are
left out (`pictures` from one that takes no pictures, `power` from one
that drives no monitor, `reach` from one whose glass is not reached from
elsewhere, `meta` when no layer has said any, `threads` from one that
keeps no threads of its own to speak of). A report
with an id carries it on every line, and the area line with your id is
the end of it.

`threads` is how the painter shares out its work, what its knobs came
to (README.md, *The painter*):

    threads cores=4 workers=2 decoder=1 software=2 nice=5

    cores     the machine's
    workers   the threads decoding a video layer's pictures and encoding
              pictures and mirrors of the glass, off the thread that draws;
              0 where that thread does it
    decoder   ffmpeg's -threads for a stream on `data` that a hardware
              decoder or hwaccel decodes
    software  ffmpeg's -threads for one it decodes on the CPU; a painter
              that does not say it runs `decoder` for both
    nice      how far below the painter ffmpeg runs, 0 to 19

### `sessions` is who is on it

    ESC _ bitwall sessions ESC \

    session sysinfo-4821 layer 0 cells instance=mac-7f3a2c
    session fireflies-8812 layer 2 video instance=mac-9c21f0
    session control-pi-1 controller - -
    session mirror-6031 controller - - instance=mac-2b7e44
    session ctl-5120 controller - -
    sessions 5

One line for each session the wall holds, the asker's own included, in
the order they were opened: its name, its role, its place and its type,
`-` where a role has none, and `instance=` where the session was opened
with one, so a controller finds what it started without reading every
session's meta. It is the `instance` on the `open`, whatever the session
says in its meta later, and a wall that never reads a stream says it too. The last line is `sessions <n>`, the count. A
refused session whose directory has not yet gone is not a session the
wall holds, and is not listed.

## What a controller hears

A controller is a session like any other: commands on its `in`, answers
on its `out`, and nothing copied to it that it did not ask for. By
default it hears its own answers and the wall's state: `area`,
`display`, `power`, `presence` and `stats` as they change,
whoever changed them -- another controller, or a viewer's key on a web
wall -- and sessions coming and going:

    session opened sysinfo-4821 layer 0 cells instance=mac-7f3a2c
    session closed sysinfo-4821 displaced

A `session opened` line is the session as `sessions` would list it, its
`instance=` from the open included where it had one, so a controller
keeping its own list from these lines has what a listing would give it.

It is not told of its own session. It may ask for more:

    ESC _ bitwall follow said ESC \
    ESC _ bitwall follow in ESC \
    ESC _ bitwall follow off ESC \
    ESC _ bitwall measures 1000 ESC \
    ESC _ bitwall measures 0 ESC \

    follow said   also every line the wall says to any other session
    follow in     that, and every command arriving on any other session
    follow off    back to the default
    measures <ms> a measure line that often, 200 to 60000, held to the
                  nearer end; 0 stops

A followed line is tagged with the direction and the session's name, `>`
said and `<` arriving, and is otherwise as it was:

    > sysinfo-4821 area 160x38 20
    < sysinfo-4821 meta set name sysinfo
    < control-pi-1 font 20 id=c7

Frame bytes never reach a controller, only commands. A video layer's
`frame` line is part of its frame and is not followed either, like the
bytes after it; its `clear` is a command and is. Pictures, mirror
frames and measure lines go only to the session that asked for them, and
are never followed; nor is `dropped`, nor a line that is itself tagged
`> ` or `< `, so what one follower is told is not followed to another.
A `follow` or `measures` with no value, or one it
does not know, is an error like any other argument that is wrong:

    error follow no value
    error follow both: unknown option
    error measures soon: not a number

A controller that changes something hears it as any session does: a font
changed from the control page is an `area` on every session's `out`, and
the base resizes to it as though it had asked.

`controllers/bitwall-ctl` is a controller, and does all of this for you.
It opens a short-lived session, `ctl-<pid>`, sends the command, prints
the answer and closes; `--follow` keeps it open:

    controllers/bitwall-ctl pi.local:1                     # the report
    controllers/bitwall-ctl pi.local:1 stats 1 show=all    # a command, and its answer
    controllers/bitwall-ctl --follow pi.local follow in    # everything, both ways
    controllers/bitwall-ctl --follow pi.local measures 500 # its numbers, twice a second

## The base

A wall is a stack of layers, and **layer 0 is the base**: the sender. It
covers the whole wall, with nothing transparent beneath it; as the lowest
cells layer, its font request decides the area; and it is held by one
session at a time.

    open sysinfo-4821 layer z=0 type=cells take=1
    opened sysinfo-4821 z=0

`take=1` displaces whoever holds the base: that session is told

    closed <name> displaced

and ends, and the new one is then opened. A library opens the base with
`take=1`, and a sender displaced stays out rather than fight back.
Without `take=1`, an `open` for a base that is held is `error open <name>
busy`.

A wall has a sender once its base has painted. Until then it shows its
status page, and after a while dozes, dark for want of a sender.

**A video base.** `open <name> layer z=0 type=video take=1` holds the
base as any base is held, and is a video layer (*A video layer*) at 0:
the glass under it is black, and its first frame is the wall having a
sender. It sets no font and has no grid, so `font` and `grid` from it are
`not-for-role`, and it is told `screen`, never `area`. Video layers go
over it as over any base. `sessions` says `layer 0 video`.

## `display` is what the wall shows; `power` is what the monitor draws

    display lit
    display blank
    display away
    display doze

    power on
    power standby

`display` is whether the wall is showing its picture and, if it is dark,
why: `lit` is showing it; `blank` is dark because a controller or the
base asked; `away` is dark because nobody is there to see, as `presence`
says;
`doze` is dark for want of a sender. A wall both blanked and away says
`blank`, the one somebody asked for. Every receiver says it the same way.

`power` is the monitor itself: `on`, drawing its full power whatever it
shows, or put into `standby`. A dark wall is black at full power at
first, and a Pi puts its monitor into standby a minute later; a screen
left on all night at full power costs something a screen asleep does
not, and the two are only told apart here. Only a receiver that drives a
monitor says it: a web receiver has none it can know, and leaves `power`
out, as it leaves out any measure it cannot take.

Each is said in every report, before `area`, and to every controller
whenever it changes, whatever changed it.

`watching` follows the dark a blank or presence makes, not the wall's
own doze: a wall blanked or away says `watching 0`, and a sender stops
composing frames for it and repaints in full when it lights. When it is
not dark, a Pi says `watching 1`, dozing or not, so that a sender
arriving is drawn for; a web receiver says how many tabs are in view,
which can be 0. It is said to every layer whenever it changes.

## `presence` is whether anybody is there

    ESC _ bitwall presence 0 from=presence-lounge ESC \
    ESC _ bitwall presence 1 from=presence-lounge ESC \

Whatever watches the room (a light going off, a Mac locked, a sensor on a
Pi's pins) opens a controller session and tells the wall whether anybody
is there to see it. With nobody there the wall goes dark and says
`display away`, whatever is sending to it: no sender needs to know about
presence, and one built on `bitwall.Wall` stops composing because
`watching` has gone to 0. With somebody back it lights, and the sender
repaints in full.

Presence is kept apart from `blank`. A blank is somebody asking for the
dark; presence is the room. The wall is dark if either says so, so a
screen turned off by hand stays off when somebody walks in, and lifting a
blank does not light a wall nobody is in front of.

A wall follows one presence, named when it is started (`--presence
1=lounge` to the painter, `make kitchen presence=kitchen` to the web
receiver), and says it, with who last said what and how long ago:

    presence 0 name=lounge from=presence-lounge age=40
    presence 1 name=lounge

A wall nothing has fed is present, and says no `from`. Every `presence`
command carries a `from`, a token as an id is, so a wall always knows who
is feeding it; one feeder per wall, since two would undo each other, and
signals are combined before they reach the wall. `age` is whole seconds
since that feeder last spoke, not since the value last changed: a feeder
saying the same thing again resets it, and one that has gone quiet shows
as an age that keeps climbing. The line is said in every report, after
every `presence` command, and to every controller whenever it changes.

Presence lasts as long as the receiver does: across senders coming and
going, but not across the receiver restarting, which starts present,
with no `from`. That is the safe way round, since a restarted wall lights
rather than staying dark by mistake, and it puts the remembering on the
feeder, which says presence again whenever a report shows a wall with no
`from`, and every minute besides.

    error presence no value
    error presence no from
    error presence 2: not 0 or 1
    error presence from=a.b: not a token
    error presence soon: unknown option

## `screen` is what the wall can actually show

    screen colour=true rate=30 pixels=1920x1080 cell=12x28 glyphs=U+2500-259F,U+2800-28FF

Area says how many cells there are. It says nothing about what a cell can
be, and `screen` does.

    colour   mono | grey | 16 | 256 | true
    rate     frames a second this screen wants, at most
    pixels   the glass the wall paints on, width by height
    cell     one cell's size in pixels at the current font
    glyphs   code points it can draw, spelled as CSS `unicode-range`

**Pixels and cell are the glass.** How many pixels a wall has is the
receiver's to choose, for its hardware and for where it hangs: a wall
seen across a room runs a low resolution, one in front of somebody a high
one, and an e-ink panel its own. So is which way up it hangs: a screen on
its side is a glass taller than wide, said so here, and a sender composes
for that shape rather than for the monitor's. It is set when the receiver
starts, and said here, and said again whenever it changes: a window
resized, or a glass with no screen given another size (*`glass` asks for
pixels*). A web wall's glass is the box of the tab that decides it, in
device pixels, said once a resize has settled; a web wall no tab has yet
shown has no glass, and says neither `pixels` nor `cell`. A sender of cells needs neither to compose, since the font
decides how many cells there are; it uses `cell` to judge how fine a
stroke is in real pixels, and a sender of pixels sizes what it sends by
`pixels`. `cell` changes with the font, so `screen` is said again after
every `area` that changes it. A receiver that cannot know either leaves
it out.

**Colour is a class, not a palette.** A receiver listing the four
thousand colours it owns tells a sender nothing it can act on; a sender
wants to know whether a gradient will survive, and that is a single word.
What the sender does with it is quantise on the way out, so a two-colour
screen is sent two colours rather than sent a gradient and left to
flatten it.

**Rate is the one a receiver cannot fix for itself.** Colour it must
quantise anyway to paint at all, and doing that locally costs it nothing.
Frames are different: by the time twenty a second reach a panel that can
show one, the sender has composed them, the wire has carried them, and on
electronic paper the panel has worn itself out obeying. Nobody downstream
can refuse a frame as cheaply as the sender can decline to draw it.

So `rate` is honoured by the bitwall library rather than by each sender:
a sender asking for 60fps on a screen that says 1 gets 1, and does not
have to know why. It is a ceiling and not a promise: a wall with nothing
happening is still sent nothing.

**Glyphs are ranges because that is what the evidence is.** A font
either has U+2588 or it does not; `fc-list :charset=2588` answers that
and nothing else. A vocabulary of categories (plain, box, blocks,
braille) would be laid over code points, ordered as though fonts gained
things in that order, and a font with braille and no box drawing would
make a liar of it. `unicode-range` is the spelling CSS uses for exactly
this question, so it is the spelling here.

This is the one part of `screen` a sender has to act on itself. A colour
is reduced on the way out and a frame nobody can show is a frame not
drawn, but there is no general way to turn a braille sparkline into an
ASCII one: only whoever composed it knows what it was a picture of. So
`w.can(...)` is a question, and the answer is the sender's to use.

**Saying nothing is the useful answer for most screens.** A browser falls
back through every font on the machine, so there is no code point a tab
is confident it cannot draw; claiming a range there would make senders
plainer than they needed to be. An absent claim is not a claim of ASCII.

A receiver that says nothing of a key gets truecolour and whatever the
sender felt like.

## `receiver` is the program keeping the wall

    receiver bitwall-paint version=f9d78ff5ce96 started=1790636220 booted=1790077234

In every report, and nowhere else: which program keeps the wall, and
which build of it. The first word is the program as its file is named
(`bitwall-paint`, `bitwall-web`, `bitwall-mac`, `bitwall-shell`).

    version   the first twelve hex digits of the SHA-256 of the program's
              source files one after another, or - where it was not told
    started   when the program started, in Unix seconds
    booted    when its machine last started, in Unix seconds, where known

**A version is its source, not a commit.** A Pi builds the painter from
files copied to it, with no repository behind them, so a commit is not
always there to name; the files always are. Which files make which
program is `bitwall/receivers/__init__.py` (`SOURCES`), and the same
hash is worked out there, by the painter's build (README.md), and by the
shell receiver of itself. A controller holding the same files works out
the same version, so a wall still running yesterday's build is seen
rather than discovered.

**Times, not ages.** Both are moments, so the line is the same every
time it is said, and a painter started a minute ago, or a machine that
rebooted in the night, is plain beside a wall's other facts.

## `reach` is how the glass is reached from elsewhere

    reach http port=8901 bound=192.0.2.10,100.64.0.1 path=/r/kitchen

    reach https port=8902 name=host.example.ts.net path=/r/kitchen

In every report of a wall whose glass can be opened from another
machine, and left out of any other: a web wall's glass is a tab, and
this is where a tab finds it. The first word is how, one line a way in:
`http`, and `https` where something with a certificate stands in front
of it, `tailscale serve` say; a receiver reached some other way, a VNC
server say, would name its own.

    port    the port it listens on
    bound   the addresses it listens on, comma separated, or * for all
    name    the name its certificate is for, which a browser must use
    path    where on it this wall is

A browser decodes a film only on a secure page, `https` or `localhost`,
so a tab opened over plain `http` from elsewhere shows no film.

**The facts, not a URL.** A receiver listening on two addresses has two
URLs, and one listening on all of them has as many as its machine has,
some reachable from nowhere else. Which works depends on where the one
looking is, which only the one looking knows: the control page builds
the link for the network it is being seen from.

## `font` and `grid` ask for cells

    ESC _ bitwall font 14 ESC \
    ESC _ bitwall grid 240x60 ESC \

    area 240x60 12        honoured: the font is whatever makes it fit
    area 174x42 14        not honoured: this is what there is

`font` asks for a size in points. A game with a fixed board wants a
number of cells, not a font size, and which font produces them is the
receiver's business, since it knows its glass. So `grid` is the same
request as `font` from the other end, and the answer to both is the area,
said to every session whether or not anything changed. `repaint` follows
it only when the size actually changed. A receiver that cannot change its
cell says the area it has, and the sender composes for that.

The two are one argument, so the last one wins: a `grid` supersedes a
`font` request and a `font` supersedes a `grid`. A sender may ask again
whenever its board changes, which is what a carousel of games does
between one and the next.

## `glass` asks for pixels

    ESC _ bitwall glass 1280x720 id=g ESC \

    area 160x40 14 id=g   the glass is 1280x720 now, and this is its area

A wall whose glass is nobody's monitor -- a painter with no screen, or
in a window -- takes its size in pixels from a controller, as `font`
takes a cell's. With no screen the glass becomes that size; in a window
the window is made it, in real pixels, as near as the desktop allows. A
wall that offers it says `glass` in `caps`; a monitor's glass is its
mode, set when the receiver starts, and a wall on one says
`error glass: not offered`. The size is from 64x64 to as large as the
wall can draw, else `error glass <w>x<h>: not between 64x64 and
<n>x<n>`.

A glass changing size, however it changes -- asked for, a window
dragged, a monitor's mode changed -- is taken once it has held for about
300 ms, so a drag is one change, at the size it stops at. Then it is a
new glass, as when a web wall's tab is resized: each wall's area is
worked out from its font again and every session is told `area` and
`screen`, and `repaint` where the area changed; every video layer is
taken down and told `screen` and `repaint`, never `area`; a cells layer
with a font or grid of its own keeps it and is told its own area; and a
raw stream keeps its size and fits the new glass into it, black round it
where the shapes differ. The one who asked hears the area that follows
with its id on it, or, where the glass stays as it was, the area as it
stands. As with `font`, the answer is what there is.

## Input

A receiver says what a viewer may give it, and what it was given, and
bitwall carries both without interpreting either.

    ESC _ bitwall want touch=1 keyboard=0 buttons=1 ESC \

    accepts touch keyboard       what this receiver will pass on, when asked
    input tap cell=3,4 area=174,42
    input key key=Return mods=ctrl
    input button n=2

A layer asks for what it wants with `want`, each of `touch`, `keyboard`
and `buttons` 0 or 1. `accepts` is said in every report and to every
layer when it changes; a receiver with nothing to offer says a bare
`accepts`. Each press is said once, as it happens, to the layer it is
routed to, and only of a kind that layer asked for.

By convention, not by rule: `touch` reports `tap` with a cell and the
area it was taken against; `keyboard` reports `key`, with `mods` comma
separated where there are any; `buttons` reports `button` with a number.
The area travels with a tap because a press that crosses a resize would
otherwise land on a grid that no longer exists.

Whether an input is enabled is the receiver's page or hardware to decide,
not the sender's.

## `stats` is the receiver's own numbers, on its own glass

    ESC _ bitwall stats 1 [at=<point>] [size=<points>] [show=<names>] ESC \
    ESC _ bitwall stats 0 ESC \

    at     n ne e se s sw w nw c: an edge, a corner, or the centre    ne
    size   the overlay's font in points, 8 to 40, clamped             14
    show   measures, comma separated, or all                          fps

An option left out keeps its value. The values start as above, and last
through senders coming and going until the receiver restarts. A wrong
option keeps its old value and says `error`, and the options beside it
still apply. Names in `show` that the receiver does not know are dropped;
a `show` with none it knows is wrong. A first word other than 0 or 1
refuses the whole command.

The answer, after any errors, to every `stats` command and in every
report, and to every other controller whenever it changes:

    stats 1 at=ne size=14 show=fps,in

and what it can show, after `caps` in every report, only what this
receiver can know, so a measure it has no way to take is not offered:

    measures wall grid fps in out frame draw swap atlas inbox sender pong display power cpu render temp

The overlay is the receiver's, never the sender's: drawn over the grid in
a box inset 8 pixels from the edge, at a font of its own, so the area,
the font and every cell stay exactly what the sender composed against.
Under the wall's measures it has a row for each session, layers top to
bottom as they were opened and controllers alike: its name, its place
and type, frames a second, bytes in a second and lines dropped since it
opened.

    fireflies-8812        2 video     24fps  590.0KB/s 0 dropped

It is not drawn while the wall is dark, and while it is on the wall
presents twice a second whether or not frames arrive. Rates are over the
last second; a value with nothing to go on yet is `-`.

| name     | what                                                         | shown as                  |
|----------|--------------------------------------------------------------|---------------------------|
| `wall`   | the wall's name and its connector                            | `1 HDMI-A-2`              |
| `grid`   | cells, and the font that makes them                          | `174x42 @14`              |
| `fps`    | times the glass was redrawn, by any layer, not counting the overlay's own refreshes | a second |
| `in`     | bytes read from every session, commands included             | `6.9MB/s`                 |
| `out`    | bytes written to every session, pictures and mirror frames included | `210KB/s`          |
| `frame`  | time taken to read each of the base's frames' escape codes and set the cells they change | ms |
| `draw`   | time taken to hand the GPU the whole grid to draw, each time the glass is updated | ms |
| `swap`   | time taken to put the finished picture on the screen, each time the glass is updated: on the GPU often mostly waiting for it to finish drawing, in software pushing the changed rectangles | ms |
| `atlas`  | slots used of all (software: glyphs cached of the table), and times it filled, once it has | `812/4096`, `812/4096, 3 restarts` |
| `inbox`  | bytes read and not yet parsed                                | `12.3KB`                  |
| `sender` | whether the wall has a sender, a base that has painted, and time since its last byte | `yes 2s`     |
| `pong`   | the last ping's round trip to the base                       | ms, or `-`                |
| `display`| `lit`, `blank`, `away` or `doze`                             | the word                  |
| `power`  | the monitor: `on` or `standby`, where the receiver drives one | the word                 |
| `cpu`    | the receiver's own CPU time                                  | %, or `-` without `/proc` |
| `render` | how the wall is drawn, and on the GPU the process's share of its time, every engine, all walls together | `gpu 19%`, `gpu` where the driver keeps no count, `software` |
| `temp`   | the board's temperature, from `thermal_zone0`                | °C, or `-`                |

## `measure` is the same numbers, for a controller

A controller that has said `measures <ms>` is sent lines on its own
clock, whether or not the overlay is up, and only that controller hears
them: one for the glass and the painter, and after it one for each
session, layers and controllers alike:

    measure wall=0 connector=HDMI-A-1 grid=240x54 font=14 fps=24.0 in=592000 out=2210 frame=1.50 draw=0.30 swap=0.70 atlas=78 atlas_of=9636 restarts=0 display=lit power=on cpu=6 render=gpu gpu=5 temp=55.0
    measure wall=0 session=sysinfo-4821 role=layer z=0 type=cells fps=1.0 in=1843 out=205 dropped=0 queue=0 quiet=0.4 pong=10
    measure wall=0 session=fireflies-8812 role=layer z=2 type=video fps=24.0 in=590000 out=102 dropped=0 queue=0 quiet=0.0 pong=20 decode=1.10 upload=0.40
    measure wall=0 session=control-pi-0 role=controller fps=- in=102 out=41000 dropped=3 queue=12288 quiet=0.3 pong=10

The wall's line is every measure the receiver has of the glass and of
itself, as numbers in fixed units: rates and times since that
controller's last line, `fps` the times the glass was redrawn by any
layer, `in` and `out` bytes a second read from every session and written
to every session, `frame`, `draw` and `swap` in ms, `cpu` and `gpu`
in percent, `temp` in °C. A session's line is its place and its own
numbers, its rates over the last whole second: `fps` the frames it put
up (`-` for a controller, which draws none), `in` and `out` bytes a
second read from it and written to it, `dropped` the lines let go since
it opened, and for a stream on `data` the frames let go as late, `queue` the bytes waiting on its `out`, `quiet` seconds since
its last byte, `pong` its last ping's round trip in ms, for a video
layer `decode`, the ms each frame took to decode and put up, and of that
`upload`, the ms it took the thread that draws, where the decoding is
done elsewhere (the painter's workers, or ffmpeg), and for the
session the glass is streamed to `streamed` and `stream_dropped`, the
frames written whole and let go since it began, every stream of the
session's together. Whether a
wall has a sender is its `sessions`, not a measure. A measure the
overlay shows as two things is two keys here (`wall` and `connector`,
`grid` and `font`, `atlas`, `atlas_of` and `restarts`, `render` and
`gpu`). A measure the receiver has but
nothing to go on yet is `-`; one it cannot know at all (a web receiver
has no swap and no board to take the temperature of) is left out of the
line, not sent as `-`, so a controller shows what a wall has and nothing
it never will. Choosing and formatting are the controller's business.

## `picture` is the wall as it stands

    ESC _ bitwall picture glass [size=<w>x<h>] [format=qoi|jpeg] [quality=<1-100>] ESC \
    ESC _ bitwall picture <session> ESC \

Nobody asking can see the screen, and none has reason to reach a sender
to learn what is on it, so they ask the wall. A picture is of a
*target*, and the target decides what comes back:

| target | what it is |
|---|---|
| `glass` | the glass as it shows, every layer composited, the receiver's own overlay left out: a QOI image, or a JPEG where `format=jpeg` asks, the glass's full pixels unless `size` asks for fewer |
| a cells layer | that layer's own cells, `grid`: what the wall's grid holds, as a sender would write it |
| a video layer | the frame it shows, the bytes it was sent in the codec it was sent in, with where it is and the box it was drawn to |

A session is named exactly as `sessions` lists it. `size` is for the
glass alone, and is the most it may be: the glass is scaled to fit
inside it with its shape kept, and never made larger than it is, so
`size=480x480` of a 1920x1080 glass is 480x270. The answer says the size
it is. The wall draws the glass at that size itself, so a small picture
costs a small readback.

`format` is for the glass alone too: `qoi`, which is what comes without
it, or `jpeg`, where the wall's `pictures` says it makes one, at
`quality`, 1 to 100, 85 where it is not said. `quality` is for a JPEG
alone. A JPEG is baseline and opaque, the same pixels at the same size
the QOI would have been, a quarter of its bytes or less for a busy
glass, and what a browser or a photograph viewer shows as it comes. The
painter makes it with libjpeg on its pool, as it makes the QOI; the web
receiver's tab with its browser's own encoder.

The glass is the layers composited, without the receiver's own overlay,
and it is what would show whether or not anything shows: a wall that is
`blank`, `away` or `doze`, or whose monitor is in standby, is pictured
as though it were lit. Whether the glass is dark is `display`'s to say.

Any session may ask, and the picture comes on the asker's own `out`, and
only there: a line saying what follows, carrying the command's id if it
had one, and then exactly as many bytes as it says, which are not lines.
The count is always the last thing before any id:

    picture glass qoi 480x270 41233 id=k3
    picture glass jpeg 1280x720 390043 id=k7
    picture fireflies-8812-z1 qoi 195x288 at=812,764 24710 id=k4
    picture picture-5120-l0 jpeg 843x560 at=914,8 size=677x450 61022 id=k6
    picture sysinfo-4821 grid 240x60 184320 id=k5

The size is said with the picture because a reader cannot be sure it
heard the `area` or the `screen` before it: any session can lose a line
to `dropped`.

| format | what it is |
|---|---|
| `qoi` | a QOI image, RGBA, alpha kept: the glass is opaque, a video frame is what its sender made |
| `jpeg` | the glass, where `format=jpeg` asked; or a video layer's frame, as `png` is |
| `png` | a video layer's frame as its sender sent it; `size` is the box it was drawn to, where it was scaled |
| `grid` | a full repaint, each row placed with a cursor move, colour set as truecolour only where it changes and SGR 2 on faint braille where that changes, UTF-8, ending with the bare `ESC[0m`. Anything that paints a stream can show it, and one pasted into a session makes the same wall. |

A receiver that can take pictures says `picture` in `caps`, and which
formats after `measures` in every report, a word each:

    pictures qoi jpeg grid

`jpeg` is there where the glass may be asked for as one: the web
receiver says it, and the painter where it was built with libjpeg, which
is where its `images=` says `jpeg` too. A reader looks for the word, so
one that knows only `qoi` and `grid` reads the line as it always did.

Asking what cannot be answered is an error like any other argument that
is wrong:

    error picture no target
    error picture sysinfo-9: no such session
    error picture ctl-5120: not a layer
    error picture fireflies-8812-z1: nothing shown
    error picture glass: not offered
    error picture sysinfo-4821: not offered
    error picture sysinfo-4821 size=480x270: only for the glass
    error picture size=big: bad size
    error picture format=jpeg: not offered
    error picture quality=0: bad quality
    error picture quality=80: only for jpeg
    error picture sysinfo-4821 format=jpeg: only for the glass
    error picture fast: unknown option

`not offered` is a target a receiver cannot read: the glass on one that
cannot see its own pixels, a grid on one that keeps no cells; or a
format it does not make, `jpeg` on a wall whose `pictures` does not say
it.

A picture is an answer, and is never dropped: it waits in the session's
queue, as *Backpressure* describes. A reader so far behind that it will
not fit is told `error picture <target>: too far behind`.

## `mirror` is the wall as it changes

    ESC _ bitwall mirror glass [every=<ms>] [size=<w>x<h>] [format=qoi|jpeg] [quality=<1-100>] ESC \
    ESC _ bitwall mirror <session> [every=<ms>] ESC \
    ESC _ bitwall mirror glass stream=i420 size=<w>x<h> fps=<n> [name=<n>] [overlay=1] ESC \
    ESC _ bitwall mirror <target> off ESC \
    ESC _ bitwall mirror glass off name=<n> ESC \

A picture is a still. To watch a wall work, a session asks for a mirror
of a target, and the wall sends frames on the asker's `out`, and only
there. Each is a line saying what follows and how many bytes, and then
exactly that many:

    mirror glass qoi 480x270 39880
    mirror glass jpeg 480x270 9310
    mirror sysinfo-4821 grid 240x60 184320
    mirror sysinfo-4821 diff 2210
    mirror fireflies-8812-z1 qoi 195x288 at=812,764 hold=1000 24710
    mirror picture-5120-l0 jpeg 843x560 at=914,8 size=677x450 hold=still 61022

A mirror's frames carry no id: they are not answers to the command but
the wall as it goes on changing, as `measure` lines are. An id on
`mirror` marks only its errors.

**The glass** is looked at on a clock of the mirror's own, every 100 ms
unless `every` says otherwise, from 50 to 60000; a value outside that is
held to the nearer end. Each look is a whole QOI image at the size asked,
or a JPEG where `format=jpeg` asks, at `quality`, as `picture` makes
them, sent when it differs from the last one sent, and not at all when
nothing has changed. Its bytes and its size are what a video layer's
`frame` takes, so a glass mirror can be passed on to another wall's
video layer with only its line rewritten, a JPEG to a wall whose
`images=` takes one.

**A cells layer** is looked at on the same clock. The first frame is
`grid`, the whole of its cells, the same bytes a picture of it would be.
Each after it is `diff`: only the cells that differ from the frame
before, each placed with a cursor move where it does not follow the
last, written the same way and ending with the same bare `ESC[0m`.
Painted in order onto the first, they make the layer. A whole frame says
the size of the grid; a diff does not, since it paints onto the grid the
reader already has. When the grid changes size, the next frame is whole
again.

**A video layer** has no clock of the wall's: each frame it is sent and
takes is passed on as it came, its bytes not decoded again, with where
it goes, the box it is drawn to where it was scaled and how long it
holds, no oftener than `every` when that is said.
When the layer is taken down, by `clear`, by its hold running out, by a
new glass or by its session ending, the mirror is told so in a line, and
shows nothing until the next frame:

    mirror fireflies-8812-z1 clear

That line is never dropped.

The frames are the wall, not the sender. What changed and changed back
between two looks is not in either. A look is taken between reads, so a
sender frame bigger than one read can be caught half painted; the next
look finishes it. The sender is not told it is being watched.

**Mirror frames are dropped, answers never.** A session whose queue
still holds a frame is not sent the next. A cells diff after that is
worked out against what was last sent, which is all bound to reach the
reader in order, so a slow reader sees fewer frames but never falls out
of step with the wall, and never holds it up.

A receiver that can mirror says `mirror` in `caps`. Asking what it
cannot answer is an error, with the words `picture` uses, and:

    error mirror every=soon: not a number
    error mirror glass: 4 mirrors already

A wall keeps at most four mirrors at once, two for any one session: each
costs it a look at the glass or its cells each frame. Asking again for a
target the session already mirrors starts it again, with a whole frame
and the new rate. `mirror <target> off` stops it; so does the asking
session ending, and a mirrored session ending stops its mirrors.

A mirror shows what the wall holds, whatever its `display`. A wall that
is `blank`, `away` or `doze`, or whose monitor is in standby, goes on
taking and painting what its layers send, so its mirror moves while they
do, and a glass mirror shows the glass as though it were lit; whether
the glass is dark is `display`'s to say, not the mirror's.

### The glass as a raw stream

    ESC _ bitwall mirror glass stream=i420 size=1280x720 fps=25 ESC \

A glass mirror is QOI stills, sent when the glass changes, on `out`
among the lines. To show one wall on another as video, the glass is
wanted as moving pictures for an encoder to take, at a steady rate and
in its format, so it comes raw on a fifo of its own,
`s/<name>/glass`, beside the session's `in` and `out`: raw frames out of
the wall, as `data` carries frames in. The wall encodes nothing; what
reads the fifo -- ffmpeg, as a rule -- does.

**Each frame** is a header of 32 bytes, little-endian, then the frame:

| bytes | what |
|---|---|
| 0-3 | `BWG1` |
| 4-7 | width, the `size` asked |
| 8-11 | height |
| 12-19 | the frame's number, from 0 for the first the wall drew |
| 20-27 | when it was drawn, in microseconds of the wall's clock, Unix time |
| 28-31 | the bytes after the header: `w*h*3/2` |

and the frame is I420: the Y plane, `w` by `h`, then U and V, each `w/2`
by `h/2`, top row first, BT.709 at studio range, as a film is encoded and
a wall decodes one. It is exactly `size`: the glass scaled to fit with
its shape kept, in the middle, and black round it where the shapes
differ, so an encoder is given one size for the life of the stream
whatever the glass does. It is the glass as a picture of it is, every
layer and not the overlay, and as though lit.

**Cadence.** A frame is drawn every 1/fps whether or not the glass has
changed, since an encoder wants a steady clock and a sender a picture
that keeps arriving. A frame the reader is not ready for -- a full pipe,
or nobody reading -- is let go, the newest kept, never queued: so a slow
reader sees fewer frames but always the latest, and never holds up the
wall. Numbers go on rising through what was let go, so a gap in them is
frames dropped. The fifo is opened by the wall without waiting, and a
reader that comes is given whole frames from its first byte; one that
goes takes the rest of its frame with it, and the next is given whole
frames again.

**Who and how many.** A stream is a controller's: any other session is
told `error mirror not-for-role`. Each costs the wall a pass over the
glass and a read of it every frame, so a wall says in `caps` how many it
runs at once, every session's together: `streams=<n>`, or `stream` alone
for one at a time, where a second
request is `error mirror busy` whoever asks. Past `n` a request is
`error mirror glass: <n> streams already`. Nothing is said when one
starts: the fifo is there, and the frames begin.

**Several streams at once.** Every stream is the one glass, composed
once: they differ in size, rate and the overlay, never in what is on
them. A session names each stream it holds beside the first with
`name=`, letters, digits, `-` and `_`, up to 24; a named stream's fifo
is `s/<session>/glass-<name>`, and the one asked without a name is on
`glass` as before. The same name asked again by the same session is
`error mirror busy`; another session's names are its own. Each stream
keeps its own clock, a frame every 1/fps from when it was asked, so
streams at different rates go on side by side, and its frame numbers
are its own.

    ESC _ bitwall mirror glass stream=i420 size=1920x1080 fps=25 name=full ESC \
    ESC _ bitwall mirror glass stream=i420 size=1280x720 fps=15 name=preview ESC \

    s/mirror-6031/glass-full
    s/mirror-6031/glass-preview

A frame smaller than the glass is the glass shrunk by area, each pixel
the light that falls on it: exactly at a half or a quarter of the
glass's size, and near it between, about as near as a `picture glass`
of that size is.

**The overlay.** A stream is the glass as a picture of it is, without
the receiver's own overlay, unless it is asked with `overlay=1`: then,
while the overlay is up (`stats`), it is drawn over the glass in that
stream as it is on the screen, upright, at the glass's size before the
stream's is made of it. Other streams, and every `picture glass`, go on
without it.

**Ending.** `mirror glass off name=<n>` ends that stream of the
session's and nothing else, and a name it does not hold is
`error mirror name=<n>: no such stream`. `mirror glass off` ends every
stream the session holds, and its glass mirror too; so does the session
ending. Each fifo goes with its stream.

`size` is even on both sides, as 4:2:0 wants, 16 to the most the wall's
GPU draws; `fps` 1 to 60. Asking what cannot be done is an error like
any other:

    error mirror stream=nv12: not i420
    error mirror size=853x480: bad size
    error mirror fps=0: not 1 to 60
    error mirror stream=i420: no size
    error mirror stream=i420: no fps
    error mirror sysinfo-4821 stream=i420: only for the glass
    error mirror glass stream=i420: not offered
    error mirror name=a/b: bad name
    error mirror overlay=2: not 0 or 1

A wall that streams says `stream` in `caps`, and `streams=8` where it
runs several: the painter on its GPU, which while it streams draws the
glass once, into a picture of its own, copies that to the screen, and
for each stream's frame shrinks it to the stream's size where that is
smaller and turns it to the frame's planes, so a frame costs it a pass or
two and a read of a byte and a half a pixel;
where it paints in software it does not offer it. The session's measure line counts what it
has drawn: `streamed`, the frames written whole, and `stream_dropped`,
those let go, since the stream began.

## `meta` is what a session says about itself

A receiver never learns what it is showing. It paints cells, and cells
are not a name. So a session says who it is, and the receiver keeps that
and hands it to whoever asks: a controller listing walls, a page naming
one, a person at a shell wondering what that screen is.

    ESC _ bitwall meta set <key> <value...> ESC \
    ESC _ bitwall meta hide <key> ESC \
    ESC _ bitwall meta show <key> ESC \
    ESC _ bitwall meta clear [<key>] ESC \
    ESC _ bitwall meta report ESC \

Meta is per session: each command sets or asks about the session's own.
The base's describes the wall as it stands; a layer above it carries its
own (`name notifications`). Every `report` carries them all: the base's
as it is, and each layer's above it tagged with its session, the name
straight after `meta`, so a controller lists every sender on the wall
from one report.

The value is the rest of the line, so a name with spaces in it needs no
quoting and no escaping: `meta set name The Incredibles (2004)` is the
whole of it. The key is the one word before that, which is why a key
cannot contain a space.

What comes back are the same statements, so a reader that understands
the command understands the answer, and a report is the commands that
would rebuild the state:

    meta set name Snake
    meta set description a snake that plays itself, 240x60
    meta set pid 4821
    meta hide pid
    meta age name 42

`meta report` says all of the session's own to it. A report says the
base's, then each layer's above it in the order they were opened, all
before `area`, a layer's tagged:

    meta set name sysinfo
    meta age name 3600
    meta fireflies-8812 set name Fireflies
    meta fireflies-8812 age name 42

Each change is answered to the session that made it, and a controller
following `said` sees every layer's as they happen, tagged with the
session as every followed line is: `> fireflies-8812 meta set name
Fireflies`. When a layer ends its meta goes with it, and nothing is said
of the meta: the controller hears `session closed fireflies-8812 ...`,
which it hears of every session that ends, and drops what it held for
that session; a `sessions` it asks afterwards no longer lists it.

    key      one to thirty-two of [a-z0-9._-], lower case
    value    printable UTF-8, at most 256 bytes, the rest of the line
    how many at most 32 keys, and 4 KB of them in all

Over any of those is an error and nothing is stored, as is a `set` with
no value, a `hide` or `show` of a key that is not set, and a verb it does
not know:

    error meta set name: too long
    error meta set Name: not a key
    error meta set name: no value
    error meta hide pid: not set
    error meta frob: unknown verb

### What the keys mean is convention, not protocol

A receiver stores what it is given and never interprets it. Three keys
are agreed, so that a controller written today can read a sender written
tomorrow: `name`, what to call it (`Snake`, `Fireflies`), short
enough for a list; `description`, a line about it for a person; and
`instance`, the id a controller gave the sender it started, one to
sixty-four lower case letters, digits and `-`, starting with a letter or
a digit, set hidden from `BITWALL_INSTANCE` by `bitwall.Wall` for a base
and by `bitwall.layer()` for a layer above it -- a Wall's own `layer()`
says the Wall's, so a sender's base and layers say one -- so that no sender has to,
as each also says it on its `open`; a value of any other form is not set.
A controller session has no meta, and says its instance on its `open`
alone: `bitwall.Controller` takes it from `BITWALL_INSTANCE` in a sender
placed `controller`, the first Controller only. Everything else is
between whoever sets it and whoever reads it.

`instance` joins a wall to a controller's record of what it started, and
`sessions` says it on each session's line, from the `open`. Whether a wall has a sender is
its `sessions`, and whether the sender is running is the record's own
status.

### `hide` is about the glass, not about secrecy

A hidden key is one a receiver does not paint where a wall shows its
metadata, as it shows its stats. It is still said in reports and in
`meta report`, because a controller needs it. A process id on the wall
of a room is noise; in a controller it is how you stop the thing.

**Nothing here is private.** Anyone who can open a session on the wall
can read it, hidden or not. A key that would matter if a stranger read
it does not belong in metadata at all.

### It goes with its session, and its age is always said

A session's metadata lasts as long as the session: when the session
ends, its metadata goes. A session starts with none, and a sender
library says what it was told again whenever it opens a session, so a
sender sets its name once and a receiver that restarts is told again
without the sender knowing.

Every key carries how long ago it was set, in seconds, said as `meta age
<key> <seconds>`. Seconds ago rather than a time, so a receiver needs a
timer and not a clock, and nothing depends on two machines agreeing what
the time is. A reader decides for itself what is too old to trust:
nothing here expires on its own.

### A receiver never acts on it

It stores, announces and paints. Whatever a key says it is (a command to
run, a way to stop the sender) is words to a receiver. A controller may
act on them, with somebody behind it. Reserved for that: `tell`, a
controller's words carried to a layer on its `out` and validated there,
which is knocking on the door rather than reading the noticeboard.

## Layers and types

A wall draws its layers from 0 upwards, then its own overlay, the top one
winning wherever it has drawn and every layer showing through where the
ones above are transparent. How many layers a wall takes, and of which
types, it says in `caps`: `layers=8` on a Pi, the base and seven layers
above it, each a video layer (*A video layer* below) or, where the wall
says so, a cells layer. What this section names beyond that is room held
for more, and a v1 wall refuses it.

**A layer is not a pane.** A pane splits the wall and changes what the
base is given; a layer sits over it, and the base's area, cells and
frames are exactly what they would be without it. The receiver stacks
surfaces without knowing what any of them shows.

**Places.** A layer asks for a place, not a number, and the wall gives
it one and says which on `opened`. The layers above the base are two
stacks with no gaps: one built up from the base, slot 1 upwards, and one
down from the highest slot; the slot a layer has is its place in its stack,
and the order it is drawn in.

| `z=` on `open` | what it gets | refused when |
|---|---|---|
| `0` (or left out) | the base | the base is held, without `take` (`busy`) |
| `above` | the slot on top of the stack from below: over every layer placed from below, in the order they were started | the wall is full (`full`) |
| `top` | the slot under the stack from the top: the first started stays highest | the wall is full (`full`) |
| `<n>` | slot `n` in the stack from below, the layers from `n` up moving up one to make room; or on top of that stack where it is not yet `n` high | the wall is full (`full`) |

So layers started one after another stack in that order, a layer that
goes leaves no gap -- every layer of its stack beyond it moves one slot
toward it -- and the two stacks meet in the middle, the wall `full` when no
slot is left between them. A layer is never taken from its holder: what
must stay over everything is started early with `top` and left running.

**A layer that moves** keeps its name, its session and its pipes; only its
slot changes, and the wall tells it, and every controller:

    moved ticker z=2                  on the layer's `out`
    session moved ticker 2            on every controller's

A layer that loses its wall -- restarted, or reopened -- asks again for the
slot it had, `z=<n>`, so that the stack comes back in its order whoever
reconnects first: each inserts at its old place, or on top of what has
come back so far, and the stack is sorted again once all are back. A layer
placed `top` asks `top` again.

When the area changes, every session but a video layer is
told `repaint`; a video layer is pixels over pixels, plays on through a
new font, and is cleared only when the glass's pixels change. When the base leaves,
layers above it stay, over the status page.

**A base in waiting.** `wait=1` asks for a base that takes the screen
only once it has sent a whole frame, so there is never a status page
between senders.

**Types.** `type=` names what a layer's stream carries. `cells` is the
first: cells as terminal bytes with APC commands, as in *Frames*.
A new type defines its own stream and nothing else in the protocol
changes; `caps` lists the types a wall takes. `cells` is on every wall,
and `video` is the second, above the base and, as *A video base*, at 0.

**A `cells` layer above the base**, on a wall whose `caps` say `cells` in
`above=`, uses the wall's grid, the same size and place as the base's,
and sends only the cells it draws, as any frame is a diff; every cell it
has not drawn is transparent. It is placed as any layer is, and stacks
with video layers in one order, so a ticker may sit over a film and under
a sprite, or over both:

    open ticker layer z=above type=cells
    opened ticker z=2

To put a dot in the top right corner of a 160x38 wall it writes to its
own `in` the bytes any sender would:

    ESC[1;160H ESC[38;2;255;255;255m ● ESC[0m

a cursor move, a colour, the character, the end of the frame.

**Its grid is the base's, or its own.** A cells layer that has asked no
font has the base's grid, cell for cell, and is told `area` and `repaint`
whenever the base's changes. `font <n>` on its own `in` gives it a grid of
its own at that font, and `grid <cols>x<rows>` a grid of exactly those
cells in the font that holds them, each centred on the glass by its own
cell; either is answered with its own `area`, with the request's id, and
`repaint`, and from then on the base's font changes leave it alone. `font
base` (or `grid base`) gives that up and follows the base's grid again.
What a layer asked is kept through a change of glass, as the base's is: a
font stays its font and its cells follow the glass, a grid stays its cells
and its font follows. Where there is no cells base -- a film at 0 -- a
layer that has asked nothing has the grid the wall's font makes as it
stands. Its `report` says its own `area` and `screen`, the cell its own. `clear` takes every cell it has drawn away, and its
session ending takes the layer down. A picture of it, `picture <session>`,
is its own cells as it would have sent them, at its own grid's size, every
cell it has not drawn left out; a mirror of it is not offered yet.

**Opacity is in the colour.** On a cells layer above the base a colour
carries an alpha, for ink and ground alike: `38;6;r;g;b;a` and
`48;6;r;g;b;a` beside truecolour's `38;2` and `48;2`, `a` from 0, nothing,
to 255, opaque; a library writes it from the hex `rrggbbaa`. Every other
colour is opaque, and a ground left unset, or set back by `49` or a reset,
is none, so a cell in the default colours shows what is beneath, and a
space in them takes a cell away. The receiver blends each cell over what
is beneath, pixel by pixel, without knowing what either shows. The base
reads no alpha: nothing is beneath it.

**Input routing.** Input goes to the topmost layer that has drawn in the
cell touched, or the base where nothing has; keys to the one layer that
asked for them. A layer may ask for all input with `input=all`, one at a
time. On a v1 wall the base hears all input.

## A video layer

    open fireflies layer z=above type=video
    opened fireflies z=2

A video layer is pictures in pixels over the layers beneath, each pixel
blended over what is under it by its alpha. The wall answers as for any
layer, and says `video` in `types`, the pictures a frame may be in
`images`, and the streams it decodes on `data` in `codecs`. The layer
starts transparent. Every layer above the base is a
video layer or a cells layer, one session each, placed as *Places* says;
they are blended bottom to top, and a picture or a mirror of the glass has
them all. A wall that takes no video layer refuses `z=1`, `above` and
`top` with `full`, and `type=video` with `no-such-type`; `cells` above the
base is `no-such-type` on a wall whose `above=` does not say `cells`.

**Frames go on `in`**, each a command saying what follows and then
exactly that many bytes, which are not a command:

    ESC _ bitwall frame <codec> <w>x<h> at=<x>,<y> [size=<w>x<h>] hold=<ms>|still <bytes> ESC \
    <bytes of payload>

The codec is one the wall's `images` lists:

- `qoi`: a QOI image, alpha kept. Every wall that takes video draws it.
- `png`: any PNG a common decoder reads -- grey, palette or colour, with
  or without alpha, 8 or 16 bits, taken to 8 -- its alpha blended as a
  QOI's is. Gamma and colour profiles are not applied: pixels are sRGB.
- `jpeg`: baseline or progressive, grey or colour, opaque: it hides what
  is under it. Drawn as stored, so an EXIF orientation is the sender's
  to apply; a CMYK JPEG does not decode.

A photograph goes as the JPEG it came as, rather than made into a QOI
five times its size. `<w>x<h>` is the image's own size, which its
header must match, at most 4096 each way. `at` is where its top left
corner goes, in the glass's pixels as `screen pixels=` counts them, each
a whole number in ASCII digits with `-` before one left of or above the
glass, at most 65535 either way, 0,0 when left out; what falls off the
glass is not drawn. `size` is the box
it is drawn to there, at most 4096 each way, scaled smoothly by the wall
on its GPU or in the browser, its own size when left out; the wall
stretches to the box, so the picture's shape is the sender's to keep.
`hold` is how long it stays up, 1000 ms when left out, or `still`: until
the next frame, a `clear` or the session's end, never timed out, for a
photograph that is not a stalled sender. The count is the last word
before any id, at most 16 MB. Commands go between frames.

A still put back after the glass changed (below) is the sender's to
send again, its box worked out afresh against the new glass. A picture
that belongs to the cells, and should move with them when the font
changes, is kitty graphics on the base instead (*A grid may carry kitty
graphics*).

Each frame replaces the one before: the layer is the latest frame and
nothing else. The wall shows it as it arrives, with no clock of its own,
so the sender paces, and a pipe that is full is the sender's sign to drop
a frame rather than queue it. The layer's backpressure is its own and
never holds up the base or any other session.

A frame the wall does not take is dropped, its bytes skipped, and it
says why, with the id if there was one:

    error frame vp9: not offered            a codec the wall's images= does not list
    error frame 5000x20: bad size
    error frame no size
    error frame hold=x: bad value
    error frame size=0x10: bad value
    error frame pts=40: unknown option
    error frame 99999999: too big
    error frame qoi: does not decode        its header says another size, or it is not whole
    error frame jpeg: does not decode       as qoi, or a CMYK JPEG

A `frame` with no count to go on says `error frame no count`, and the
wall reads on from the next command. `pts=<ms>`, a time for the wall to
show the frame at, is reserved for a wall that plays frames out on a
clock of its own; no wall offers it.

**Every ending leaves the layer transparent:**

- `ESC _ bitwall clear ESC \`: the layer goes transparent, and the
  session stays open;
- `close` or `detach`: the layer is cleared and the session ends,
  `closed <name> asked`;
- end of file on `in`, or an unanswered `ping`: the same, `closed <name>
  eof` or `timeout`;
- no frame within `hold`, unless it was `still`: the layer is cleared
  and the session stays open, so a stalled sender still holding its pipe never leaves a frozen
  picture on the wall;
- the glass's pixels change, a new mode or a web wall's deciding tab
  settling on a new box: the layer is cleared and told
  the new `screen` and `repaint`. A font change moves the cells, not the
  pixels, so a video layer hears nothing of it and plays on untouched.

### The data pipe

    open film layer z=0 type=video take=1 data=1 codec=hevc fps=24 start=1790000000000
    opened film z=0 data

`data=1` makes `s/<name>/data` beside `in` and `out`, and the layer's
stream goes on it, as the bytes a decoder reads, with nothing framing
them: commands stay on `in`, and each fifo has its own backpressure, so a
command never waits behind a megabyte of film. `codec` says what the
stream is:

| `codec=` | the stream |
|---|---|
| `hevc` | HEVC, Annex B; decoded in hardware on a Pi 5 |
| `h264` | H.264, Annex B; in hardware on a Pi 3, in software on a Pi 5 |
| `qoi` | QOI images back to back, alpha kept |
| `mjpeg` | JPEGs back to back, each ended by its end-of-image marker; taken by the painter only where the board decodes it, by the web receiver always |

A wall that cannot take a codec refuses it, `error open <name>
no-such-codec`, and `caps` lists those it takes. A stream starts when a
writer opens `data` and ends when the last writer closes it: what is
decoded by then is played out, and the layer is cleared. The session
stays open, and the next writer starts the next stream, so the next clip
needs no new knock at the door.

**Each frame fills its box**, the whole glass unless the layer says
another: it is scaled to the largest size that fits the box with its
shape kept, and centred. A sender wanting pixel for pixel sends frames
the box's size, the glass's as `screen pixels=` says it where there is
no box.

**The box** is the layer's to say, on its `in`:

    ESC _ bitwall box at=<x>,<y> size=<w>x<h> ESC \
    ESC _ bitwall box ESC \

`at` is where its top left corner goes, in the glass's pixels, each a
whole number in ASCII digits with `-` before one left of or above the
glass, at most 65535 either way, 0,0 when left out; `size` is its width
and height, at most 4096 each way; `box`
alone is the whole glass again. So a film plays as an inset over
whatever is beneath, the layer transparent outside its frame. It holds
from the next frame, mid-stream or between streams, for the life of the
session, so a layer that opens again says it again. The box is kept
through a change of glass, a new mode or a web wall's deciding tab
settling on a new box, as the pixels it says; what then falls off the
glass is not drawn, and the layer, cleared and told `screen` and
`repaint`, may say another. A box it cannot take is said, with the id if
there was one:

    error box no size                       `at` alone
    error box size=0x10: bad value
    error box over=1: unknown option

A wall whose `caps` do not say `box` fills the glass with every frame;
it drops the command as it drops any it does not know, and a sender
that needs the box sends its frames on `in` instead, each with `at` and
`size`.

**Where the box left over goes** is the layer's to say too:

    ESC _ bitwall align top|centre|bottom ESC \

`top` puts the frame against the top of a box taller than its shape, the
spare all below it; `bottom` against the bottom; `centre`, the default,
in the middle. A box wider than the frame's shape has its spare at the
sides, centred, whatever `align` says, so on a box of the frame's own
shape it changes nothing. It holds from the next frame, for the life of
the session, so a layer that opens again says it again. Any other word
is `error align <word>: not top, centre or bottom`. A wall whose `caps`
do not say `align` centres every frame; it drops the command as it drops
any it does not know.

**Timed playback.** With `fps`, the wall plays the stream out rather than
the sender pacing it: frame i is shown when the wall's clock reaches
start + i/fps, where start is `start`, the Unix time in ms, or else the
moment the first frame was decoded. A frame early waits; one whose
successor is already due is late, and is dropped and counted in the
session's `dropped`. Walls whose clocks agree show the same frame at the
same refresh, which is how several walls play one film. `start` is the
first stream's; a later stream starts when its first frame is decoded.
Without `fps` each frame is shown as it is decoded, and the pipe paces
it.

A data frame has no `hold`: it stays up until the next frame, `clear`,
the end of its stream or of its session. A stream that does not decode
is ended and said once:

    error data hevc: does not decode

A `picture` of the layer is its frame on the glass as it was decoded, as
QOI at its own size, or a JPEG as it came where the wall shows it
without decoding it, `at` where its top left went; a mirror of it sends
only frames that came on `in`.

A session's measure line counts a data stream as it counts frames: `fps`
the frames shown, `in` the bytes on `in` and `data` together, `decode`
the ms each took to put up.

## Trust

The receiver's side is the untrusted one. Wall names, session names,
`sessions` answers and meta all come from it: a controller escapes them
before showing them, and a library checks every name is a token before it
goes into a path or a shell command. Access to a wall is access to its
host by ssh; there is nothing else to it.

## Extending it

Add commands. Never redefine one. A receiver drops what it does not
understand, and a sender consults `caps` rather than relying on being
dropped.

If a meaning has to change, that is a new name, not a new version: a
version number that must be checked at every call site has failed at the
one job it had, which was to be checked once.

## Frames

The receiver contract is three things: accept a font size request,
publish the area it can offer in cells, paint the stream.

A frame is absolutely positioned runs of SGR and text, with a bare
`ESC[0m`, no other parameter, delimiting a frame: a sender emits one to
close a frame and one to open a full repaint, and nowhere else, so a
receiver can paint between frames rather than partway through one.

### Some cells are drawn as shapes

A receiver draws these as geometry rather than type, so that neighbours
join with no seam where a glyph would stop short of the cell's edge: box
drawing, the block elements, braille as an even grid of dots, and the four
diagonal halves ◢ ◣ ◤ ◥ (U+25E2-25E5), filled out to the cell's corners.

**Faint braille shows its unlit dots.** A braille cell (U+2800-28FF) with
SGR 2 set is drawn the way a dot-matrix display looks: the lit dots in the
ink, every unlit dot in the cell's ground colour, and the wall's default
ground, black, between the dots. Ink and ground are what they are for any
cell, reverse video included. So the unlit dots show only on a cell given
a background of its own; on the default ground they are black on black,
and faint braille looks like plain braille. `⠀` (U+2800) with SGR 2 is a
cell of unlit dots. SGR 22 ends it, as it ends bold. On any other
character SGR 2 means nothing: a sender that wants dimmer ink sends a
dimmer colour.

A receiver that does not know this paints faint braille as plain braille,
which is still the lit dots.

### A grid may carry kitty graphics

`kitty` in `caps` says the wall's grid draws kitty graphics: pictures sent
as APC strings starting `G` (`ESC _G <keys> ; <payload> ESC \`), placed in
the grid's cells, as the kitty terminal protocol has them
(<https://sw.kovidgoyal.net/kitty/graphics-protocol/>, the reference for
every key named here). It is a thing a
grid can do, as `glyphs` is, not a layer or a type: the pictures sit in the
cells of the stream they came in, among its characters, and move with the
grid when its font or area changes.

The shell receiver says it when the terminal it draws on answers kitty's
own question, asked once as it starts: an image asked about with `a=q`,
then the device attributes, `ESC [c`, which every terminal answers, so the
reply is known without a timeout. It passes the strings through to that
terminal untouched, as it passes everything, and the terminal draws them.
A wall that does not say `kitty` ignores them, as it ignores any APC string
it does not know, so a sender sends them only where the wall says it.

A sender sends each with `q=2`, which asks the terminal to answer nothing:
an answer would reach the terminal's input as typing. The strings go
between frames, never inside one, and never contain the bare `ESC[0m` that
delimits frames.

**What a wall that draws them itself draws.** The shell receiver's
terminal does the whole kitty protocol, since the strings reach it as they
were sent. A receiver that draws them itself -- the painter, the web
receiver -- draws this much of it, the same on each, and says `kitty` only
once it does:

- **Actions:** `a=t` defines an image under its id, `a=T` defines it and
  places it at the cursor, `a=p` places one already defined, `a=d`
  deletes: `d=i` an image's placements, `d=I` those and the image, `d=p`
  with `i` and `p` one placement (kitty's `d=i` with `p` too; kitty's own
  `d=p`, the placements at a cell, is not drawn), `d=a` and `d=A` all of
  the session's. `a=q` is answered
  on the session's `out`, `kitty i=<id> OK` or its refusal (below).
- **Formats:** `f=100`, PNG, and raw `f=24` RGB and `f=32` RGBA with `s=`
  and `v=` their width and height; data sent in the string itself, `t=d`,
  base64, in chunks with `m=1` on all but the last.
- **Keys:** `i` the image's id, `p` a placement's, so one image can be
  placed more than once; `c` and `r` the cells it covers, as kitty has
  them: both given, it is stretched to fill them, its shape not kept; one
  given, the other is worked out from the picture's own shape, so it is
  not distorted; neither, it is its own size in pixels, covering the cells
  that takes; `z`,
  below the text where negative and over it otherwise; `C=1`, the cursor
  left where it was; `q`, taken and ignored, since a wall drawing them
  itself answers only on the session's `out` (below).
- **Placed in cells.** A placement is at the cell the cursor was on, and
  moves with the grid when its font or area changes. Placements at one `z`
  are drawn in the order they were placed, later over earlier.
- **Ids.** An id sent again replaces the picture, where it is placed, and
  is not a new image. A picture sent with no id is given one of the
  wall's own, which no sender knows, so it goes only with `d=a` or `d=A`.
- **Other keys are ignored,** not refused: cropping (`x`, `y`, `w`, `h`),
  offsets within a cell (`X`, `Y`), `I`, `P`, `S`, `O`.
- **Not drawn, and refused:** data read from a file, a temporary file or
  shared memory (`t=f`, `t=t`, `t=s`), which would have the wall read what
  a sender names; animation (`a=f`, `a=a`, `a=c`); Unicode placeholders
  (`U=1`); compressed data (`o=z`).

**Each session's are its own.** An id names an image within the session
that sent it, so no sender can place, change or delete another's. Placing
an id the session never defined is refused; deleting one is nothing. When a
session ends, for whatever reason, its images and placements go with it,
as every ending leaves a layer transparent.

**Limits.** An image is at most 16 MB decoded and 4096 pixels each way,
as a video frame is, and one string at most 512 KB: a picture bigger than
that is sent in chunks, as kitty senders do at 4 KB a string. A session holds at most 32 images and 64 placements,
and a wall at most 64 MB of decoded images across all of them. An image
past a limit is refused; nothing already there is dropped to make room.
An image whose chunks stop coming is dropped when the next string starts
another, or when the session ends.

**Against kitty's own.** kitty asks a terminal for a quota that holds "at
least a few full screen images", 320 MB a buffer in kitty itself, and
when a new image would pass it deletes older ones to make room, those
with no placements first. A wall's is a fifth of that, since a Pi 3 has
1 GB for everything: seven 1920x1080 pictures, two at 4K. And a wall
refuses the new picture rather than delete an old one, because an old
one gone is a picture missing from the wall with its sender told
nothing, where a refusal is said. kitty's chunks are at most 4096 bytes
of base64, so its senders' strings are far inside a wall's 512 KB; its
ids and placement ids run from 1 to 4294967295, as a wall's do.

**What is refused is said.** A sender sends with `q=2`, and a wall drawing
them itself has no terminal to answer on, so a refusal is said on the
session's `out`, whatever `q` was:

    error kitty i=7: too big            past a limit: its size, a string, the wall's total
    error kitty i=7: too many           past the session's images or placements
    error kitty i=7: does not decode    not PNG, or the raw size is wrong
    error kitty i=7: no such image      placed, but never defined by this session
    error kitty i=7: not offered        a medium, key or action above not drawn

Where the protocol leaves a choice -- how a picture is scaled, the order
of placements at one `z` -- the painter's is the reference, and the web
receiver does what it does.

## The least a receiver can be

A receiver in the shell stays small: make `bitwall/<wall>/` and its
`door.in`; read `open` lines from the door, make the session's fifos,
write `hello` and `opened` to its `out`, then `caps area` and `area
<cols>x<rows> <font>`, the size from `stty size`, once and unprompted,
and `cat` the base's `in` to the terminal; refuse every role but the
base, and displace the base on `take=1`; tidy up when the base's writer
goes, so the next `open` can take the wall. It offers one layer, and
ignores commands as a terminal already does, since APC strings pass
through unseen. Inside a session the stream is a paintable stream, so it
paints on a plain terminal.

`bitwall/receivers/shell/bitwall-shell` is that, and a little more: up
to four controllers beside the base, which may ask `sessions`, `report`
and `ping`, and a new `area` and `repaint` to the base when the terminal
is resized.

## Nothing else

A wall is its door and its sessions. A sender learns everything from
what the receiver says on its session's `out`, and asks by commands on
its `in`. What it asks before the receiver has said `hello` is held and
sent once it has. A receiver that never answers cannot be driven.
