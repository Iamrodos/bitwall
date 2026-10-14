# bitwall on Windows

A Windows machine runs the painter, a web wall, a wall in a console, a
sender, a mirror, the control page and the senders' controller, and a
sender anywhere reaches its walls as it reaches a Pi's. What follows
is how its walls are made, how to run each part there and keep it
running, and what it measured on Windows 11 (build 26100), with its own
OpenSSH 9.5, PowerShell 5.1 as the ssh shell and the Store's Python 3.12.

Nothing in the protocol changes, and nothing on a Pi or a Mac: a sender
reaching a Pi runs byte for byte the commands it ran before. What
changes is what a fifo is on a host that has none.

## A wall on Windows

Every fifo of a wall is a **named pipe at the same path**. A Windows
host's run directory is `//./pipe/bitwall-<user>`, so wall `kitchen`'s
door is

    //./pipe/bitwall-<user>/bitwall/kitchen/door.in

which Windows takes as the pipe named
`bitwall-<user>\bitwall\kitchen\door.in`. The pipe namespace keeps the
rest of a path as the name, so `wall_run`, `session_dir` and
`posixpath.join` work unchanged and every path PROTOCOL.md names is still
a path. The user is in the name because the namespace is the machine's,
shared by every user on it.

The wall makes each pipe and reads it (`door.in`, `in`, `data`) or writes
it (`out`, `glass`); a caller opens it as a file. Each pipe is made
`PIPE_REJECT_REMOTE_CLIENTS`, so only this machine reaches it, and with
Windows' default security, so only its user and the administrators do.

| a fifo's property the protocol uses | as a named pipe |
|---|---|
| a dead door is known at once | opening it fails at once, file not found (0.0 ms) |
| a known amount of room before a writer waits | exactly the inbound buffer the wall asks for: 1 MB, as `F_SETPIPE_SZ` on a Pi |
| end of file when the writer goes | `ERROR_BROKEN_PIPE`, in both directions |
| requests from callers at once never mix | each caller has its own instance |
| a session's `out` exists, so the session is whole | `out` is made last |
| a wall's own files are found by listing | `\\.\pipe\` is listed |

`bitwall/local.py` is that mapping for Python, one interface over both:
a fifo's end is its descriptor and every call on it is the one the
receivers made before; a pipe's end holds the handle and a thread that
reads it overlapped.

### The door

A Windows door keeps four instances waiting, so four callers can knock
at once, each on an instance of its own; a fifth waits for one to come
free (`WaitNamedPipe`). A request is handed on whole when its caller
closes, so requests never mix, whatever their length. Measured: 8
callers making 400 knocks between them, 400 requests each arriving whole,
0.08 ms a knock; 0.04 ms with nobody else at the door.

### Claiming a wall

The first door instance is made `FILE_FLAG_FIRST_PIPE_INSTANCE`, and
while a live receiver holds the name that fails: that is the refused
claim. A receiver that dies takes its pipes with it, so there is never a
dead wall to take over, and no `.`-name to build under and rename.

### A session

`s/<name>/in` and `out`, and `data` where asked for. `in` and `data` are
read by the wall; when the writer goes the wall reads end of file, and
for `data` the next writer starts the next stream, as on a fifo. `out` is
written by the wall without waiting (`PIPE_NOWAIT`): a write that does
not fit takes nothing, and the session's queue keeps it, with the rules
it already has (state lines merged, others dropped and counted). A pipe
cannot hold bytes for a reader that has not come, so what the wall says
before then waits in the queue and goes when the reader connects; a
caller sees what it sees on a fifo. What the wall has written to `out`
outlives its handle, so a refused session's `error open` and a session's
last `closed` still reach a reader already there.

**Never look at a pipe by its path.** `os.path.exists` on a pipe says
False, and takes the waiting instance with it, so the next caller to open
it is refused. Whether a session's name is taken, and which walls a host
has, are answered by listing `\\.\pipe\`.

## The relay: what replaces `cat` and the opener

A caller reaches a wall on another host through ssh, and on a Mac or a
Pi the host's shell does the rest: `cat > in`, the opener (a few lines
of Python that knock and copy `out`), and `printf` for the run
directory. A Windows host's shell cannot open a named pipe, so there the
same jobs are done by `bitwall/relay.py`:

    relay run                          the host's run directory
    relay walls <run>                  its walls with a live door, a line each
    relay find <run>                   the same, and a web receiver's control
                                       pair, as `bitwall-control` lists them
    relay web <run> <command>          a command to that web receiver, and its answer
    relay open <wall> <name> <request> <wait>
                                       knock, wait for `out`, copy it to stdout
    relay write <path>                 copy stdin into `in` or `data`
    relay read <path>                  copy a pipe, `glass`, to stdout
    relay clock                        the host's Unix time, and again for
                                       each line on stdin: for a mirror

`open` exits 3 when nothing reads the door, 4 when no `out` came within
the wait and 5 when the name was a session's already: the opener's codes,
so `Session` reads a relay exactly as it reads the opener.

The relay is carried, never installed. `relay.command()` makes it one
`python3 -c` with `local.py` and `relay.py` inside, compressed, their
docstrings and comments left out, about 6 KB:

    python3 -c "import base64,zlib,sys;exec(zlib.decompress(base64.b64decode(sys.argv[1])))" <program> <arguments>

The program and the arguments (a JSON list) are base64, which has no
quote, no `$` and no space, so sh, PowerShell and cmd all hand them on
untouched; PowerShell 5.1 mangles a quoted argument it passes to a
program, so nothing else is quoted but the few words of `-c`. The
program goes as an argument rather than in the `-c`: PowerShell scanned
a 6 KB `-c` for six seconds before running it, and passes the same bytes
as an argument in no time at all. No host keeps a copy that can go
stale, as `catalogue.py` is carried to a sender host.

A sender on Windows drawing on a wall on the same machine runs neither
ssh nor the relay: it opens the door and its session's pipes in its own
process, through `local.py` (`Link.here`; `_HereOpener` and `_HereWriter`
in `session.py`, which `Session` holds as it holds the processes). A
sender on a Mac or a Pi drawing on a wall on its own host still runs `sh
-c` with `cat` and the opener, a process for each end of each session, as
it always has; the same in-process ends work there too
(`tests/windows-pipes --here`).

## How a sender finds out

A `Link` asks the host where it keeps its walls, the first time it is
needed, as it always has: `printf %s "${XDG_RUNTIME_DIR:-…}"` through
ssh. A Pi or a Mac answers with a path. PowerShell answers with nothing,
so the link asks again through the relay, `relay run`, and a Windows host
answers `//./pipe/bitwall-<user>`. That path is how the link knows: a run
directory under `//./pipe/` is reached through the relay, and any other
through the shell, exactly as before. `BITWALL_RUN` set to a `//./pipe/`
path says the same from the sending end. A Windows machine running two
painters gives the second its own pipe, `--run //./pipe/<name>`, and a
sender there reaches it with `BITWALL_LOCAL_RUN=//./pipe/<name>`, which
moves this machine's walls only: the Pis it also sends to are still
found where they keep theirs.

What a sender does with a wall then goes through the link:
`Session.open_argv` and `writer_argv`, and a video layer's `data_argv`.

## Backpressure and end of file

- **A full pipe still tells a sender to slow down.** `in` and `data` are
  made with a 1 MB inbound buffer, and the wall reads a piece at a time,
  the next not read until it has taken the last, so a wall that stops
  reading a `data` it has no room for fills the pipe, the relay's write
  waits, ssh's window fills, and the sender's write waits, as on a Pi.
  Measured: a Mac writing through ssh into
  a wall reading 2 MB/s settled at 2.05 MB/s with about 1.2 MB in
  flight, nothing dropped and nothing run ahead.
- **End of file is `ERROR_BROKEN_PIPE`**, both ways: the wall reads it
  when the writer of `in` goes, and the relay's `open` when the wall
  closes `out`. There is no false end of file before a writer arrives,
  and nothing like the Mac's select-against-poll quirk.
- **Two writers on one pipe** are merged on a fifo; on a Windows pipe
  the second waits for its own instance. Nothing in bitwall does it.

## Why named pipes

Named pipes keep every fifo property above, and Python reaches them with
`_winapi`, CPython's own module under multiprocessing and asyncio, so
bitwall needs no dependency for them; the painter calls the same API in
C. Loopback TCP would take 2 s to find a dead door (Windows retries a
refused connect), set its own buffer, be open to every user on the
machine and need a file saying the port; Python on Windows has no
AF_UNIX. There is no broker: the relay is a command run once a session,
not a daemon. Only a Windows host is reached through the relay; a Pi or
a Mac keeps `cat` and the opener. The relay runs on `python3`, which is
on the path in an ssh session and needs nothing set.

One ssh command to Windows costs about 0.6-0.8 s from a Mac, 0.3 s of it
PowerShell starting, and Windows' ssh server takes no ControlMaster.
`cmd` as OpenSSH's `DefaultShell` would save the 0.3 s; bitwall works
with either.

## What it costs

`tests/windows-pipes` (the receiver core on named pipes, the sender
in-process) checks the claim, `sessions`, a name in use dropped, a
missing wall absent, end of file, `closed asked` reaching the reader
after the wall let go of `out`, and `data` holding its writer at 1.3 MB
while the wall did not read it and letting it go when it did. Against a
wall on an Intel laptop (`tests/hold-wall`):

| a sender on | to the wall | |
|---|---|---|
| the same machine, in-process | a controller open and its report: 31 ms; a sender talking: 47-63 ms | 162 frames in 8 s, its 20 fps cap |
| a Mac, through ssh and the relay | the host's kind learnt: 1.1 s, once a link; a controller open and report: 1.9-2.2 s; a sender talking: 1.5-1.6 s | 213 frames in 10 s, its 20 fps cap |

From a Mac, learning the run directory is two ssh round trips, the
shell's empty answer and then `relay run`, about 0.55 s each; an open is
another for the opener and one for the writer.

## The painter on Windows

`bitwall-paint.exe` is the painter built in
MSYS2's UCRT64 (README.md, "The painter on Windows"), a wall in a window
or with no screen, whose door and sessions are the named pipes above.
A sender cannot tell it from a Pi's painter or the receiver core, here or
through the relay.

Its pipes are a seam of their own (`platform.h`, "pipes"), so the
compositor calls no fifo API: `ipc-posix.c` is the fifo code a Pi and a
Mac ran before, moved and not changed, and `ipc-win.c` is the same
mapping as `local.py`, in C.

| the wall's pipe | `ipc-posix.c` | `ipc-win.c` |
|---|---|---|
| the door | a fifo held O_RDWR, under a `.` name renamed into place, a lock round the claim | four instances, the first made first of its name, which is the claim; a request handed on whole when its caller closes |
| `in`, `data` | a fifo read from a thread waiting in `select` with its waker | a pipe read overlapped, a megabyte at most at a time and the next read not asked for until the last is taken; a thread waits on its event and its waker's at once |
| `out` | a fifo held O_RDWR, 1 MB on Linux | `PIPE_NOWAIT`, 1 MB, written 64 KB at a time; what comes before the reader stays in the session's queue |
| a glass stream | a fifo opened once a reader has it, written without waiting | a pipe written overlapped, its reader waited for with the stream's waker; disconnected for the next reader when one goes |
| a waker | a pipe | a manual-reset event |
| an end | the descriptor | an index into a table of the painter's pipes |

An end is an `int` either way, so the compositor's sessions, inboxes and
streams are as they were. A pipe is listed by the painter's own table, so
a name in use is a session's pipe there, never a look at a path. The
painter's notes, each wall's last-seen, go in the user's temporary folder,
`%TEMP%\bitwall`, since the run directory holds only pipes.

The other seams: GL 2.1 through SDL's WGL context (`gpu-win.c`, every
call past GL 1.1 through the driver's pointer, since `opengl32.dll` exports
no more), the Mac's `display-sdl.c` as it is, a child started with
CreateProcess in a job of its own that inherits its three handles and no
other (`child-win.c`), ffmpeg on D3D11VA, else Quick Sync (`probe-win.c`;
`BITWALL_HWACCEL=d3d11va|qsv|none` chooses), and the process's own times
(`machine-win.c`). `compat-win.h` is what little else of POSIX it lacks:
`setenv`, and `getifaddrs` for the status page's address.

### What it measured

An Intel Iris Xe laptop, Windows 11; GL 4.6 from Intel's driver, used as
2.1; Chocolatey's ffmpeg. Everything run from an ssh session.

- `tests/painter-pixels` (BITWALL_PAINT_EXE naming it), no screen: all
  seven, as on the Mac. One scene composed three times identical; the I420
  stream the glass within 0.51 levels; five streams at once at their own
  sizes, the same figures as the Mac's to the hundredth; the overlay only
  where asked; `align`; QOI, PNG and JPEG blended within 0.5; and cells and
  drawn characters against a Pi painter's `picture glass`, text one level
  out in 0.20% of its pixels and every drawn character exact.
- A sender on the same machine, in-process (`uv run --no-project python
  senders/machine/main.py localhost:0`): drawing 2 s after it started.
- A sender on a Mac, `pc:0` through the relay (`pc` a `Host` alias in the
  Mac's `~/.ssh/config`, with its address as `HostName`): attached with its area
  in 2.2 s; `picture glass` in 0.09 s with all 14400 cells exactly their
  colour; 10 fps, its cap, 445 KB/s.
- A JPEG of 843x1262 on a video layer, at 721x1080: within 1.0 of PIL's
  bilinear scaling on average, 8 at the 99th percentile.
- A 1080p24 film on `data`, 20 s: 24.0 fps on D3D11VA in HEVC and in
  H.264, and on Quick Sync; decode 1.1-1.5 ms a frame; the painter 1-12% of
  a core, ffmpeg 6-26% (65% decoding HEVC on the CPU). In 3 of 6 runs one
  or two seconds fell to 12-15 fps, their decode 27-44 ms.
- The glass streamed raw while that film played, read on the machine: 1920x1080
  at 25, 499-500 frames in 20 s (24.97-24.99 fps), none missing, the
  painter 13-15% of a core; 1280x720 at 25, 24.99 fps, none missing, 11%.
- The glass resized live with no screen (`glass WxH`): the base
  told its new area in 390-590 ms and `picture glass` the new size; the
  resize stress at 40 sizes passes, every picture right, its streams at
  21.4/25 and 12.8/15 fps, private bytes 209 MB before and 165 MB after.
- Idle, a wall and nobody on it: 0.6% of a core.
- A second painter on the same wall is refused; a video layer's `data` takes
  a stream, ends, and takes the next, each on the glass within 270 ms.

Every CPU figure leaves out one thread of Intel's GL driver, which in an
ssh session (session 0) spins a whole core from the first thing drawn to
the painter's end, in a window or with none; a 30-line SDL program that
clears a hidden window once does the same. The painter's own `cpu=` in
`measures` counts it, and says about 100.

The figures are the process's CPU time less that of every thread that
took more than half a core over a second while the wall was idle. The
same over ten seconds, in PowerShell on the machine:

```powershell
$p = Get-Process bitwall-paint; $s = 10; $a = @{}; $p.Threads | % { $a[$_.Id] = $_.TotalProcessorTime.TotalSeconds }; $t0 = $p.TotalProcessorTime.TotalSeconds; Start-Sleep $s; $p.Refresh(); $spin = 0; $p.Threads | % { $d = $_.TotalProcessorTime.TotalSeconds - $a[$_.Id]; if ($d -gt $s / 2) { $spin += $d } }; "painter {0:N1}% of a core, its spinning GL thread left out ({1:N0}%)" -f ((($p.TotalProcessorTime.TotalSeconds - $t0) - $spin) / $s * 100), ($spin / $s * 100)
```

An idle wall said `painter 0.3% of a core, its spinning GL thread left
out (99%)`. A thread busy for real with more than half a core, the
painter drawing a 1080p film, would be left out too; read the figure
against `measures` then.

### What `measures` says on Windows

    measure wall=0 connector=none grid=75x20 font=28 fps=1.0 ... display=lit power=on cpu=98 render=gpu

- `cpu` is the whole process, the spinning thread with it, so about 100
  in an ssh session whatever the wall does.
- `render=gpu` with no share: Windows keeps no count of a process's GPU
  time the painter reads, so there is no `gpu=`.
- No `temp`: there is no `thermal_zone0` to read, so it is left out.
- `power` and `display` are the state the wall was asked for, said back.
  With no screen nothing is switched; in a window, nothing either, since
  the painter does not drive the monitor.
- The rest (`fps`, `in`, `out`, a layer's `decode` and `dropped`) mean
  what they mean on a Pi.

## The mirror on Windows

`senders/mirror` runs on Windows with a painter there as its source,
and sends to one there as a destination (MIRROR.md, *A Windows host*,
with what was measured). It reads a glass stream's pipe in its own
process through `local.py`; from another host it reads it with `relay
read`; and it asks a Windows host's clock with `relay clock`, which
answers once a line so each answer is timed on a relay already
running. Asked with `date` over a connection of its own (where Git's
`date` is on the path), the time is read late in a round trip of half
a second, and came out 167 ms wrong from a Mac to Windows and 106 ms
from Windows to a Pi, against each host's own NTP offset. On Windows the mirror asks every host that
way, since Windows' ssh shares no connection and each `date` is a new
one.

### Windows' ssh, run from a program in an ssh session

Windows' own `ssh.exe`, started by a program in an ssh session on
Windows with its stdout a pipe, does its whole job and then never
exits: its debug log stops at `chan_shutdown_write` on that pipe. A
`Link` asking a host where it keeps its walls waits for that exit, and
says it could not ask. With stdout a file or inherited it exits at once;
the same from PowerShell piping `ssh` into anything hangs too; and Git
for Windows' `ssh` (`C:\Program Files\Git\usr\bin`) does not hang.
A program run in the console's own session does not show it. To run a
sender on Windows from an ssh session, put Git's first on the path:

```powershell
$env:PATH = "C:\Program Files\Git\usr\bin;" + $env:PATH
```

## A web wall on Windows

`bitwall-web` keeps its walls with the receiver core, on the pipes
above, so a web wall on Windows is a wall beside the painter's in the
same run directory: wall `0` the painter's and `kitchen` the
web receiver's, both under `//./pipe/bitwall-<user>`, each claimed by its
door's first instance, each listed by `\\.\pipe\`. A sender on the
machine reaches it as `localhost:<name>` in its own process, and one
anywhere else as `<host>:<name>` through the relay, as it reaches the
painter. Its control pair, `bitwall-web.in` and `.out`, are pipes at
those paths too; `.out` has one reader at a time and is made anew once
it has gone, so a caller opens it, then writes its command to `.in`.

In an ssh session it runs on uv's Python with its one dependency said by
hand (*Traps*, `uv run --script`), and outlives the session started
through WMI:

```powershell
$d = "C:\Users\<user>\Documents\bitwall"
Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{ CommandLine = 'cmd /c set "BITWALL_WEB_COMPRESS=0" && uv run --no-project --with aiohttp python bitwall\receivers\web\bitwall-web --host 127.0.0.1 --port 8082 kitchen > ..\web.log 2>&1'; CurrentDirectory = $d }
```

`--run` is `//./pipe/bitwall-<user>` unless given. Ctrl+C stops it in a
console; started so, `taskkill /T /F` on the `cmd`, and its walls go
with it.

## A wall in a console

Two receivers put a wall on the console they are run in, each making its
door and sessions as the named pipes above, under
`//./pipe/bitwall-<user>`, so a sender reaches them as it reaches the
painter, `localhost:<wall>` on the machine and `<host>:<wall>` through the
relay from anywhere else.

- **`bitwall-ps`**, `bitwall/receivers/shell/bitwall-ps.ps1`: the shell
  receiver in Windows PowerShell 5.1, the least a receiver can be. It
  says its area from the console's size and copies the base's stream to
  the console unread; one layer, the base, and up to four controllers,
  which may ask `sessions`, `report` and `ping`. Its pipes are the calls
  `bitwall/local.py` makes, written in C# inside the script, since
  PowerShell makes no named pipe with the flags a wall needs.

      powershell -File bitwall\receivers\shell\bitwall-ps.ps1 [wall]

- **`bitwall-term`**, `bitwall/receivers/term`: one Rust program, the same
  sources on Windows, a Mac and Linux, with the pipes behind one interface
  (`src/pipe/windows.rs` here, `unix.rs` there). It reads the stream into
  cells, stacks cells layers over the base, draws the stats overlay,
  answers `picture` and `mirror` of its cells as `grid`, and passes the
  console's keys and clicks to a base that asks for them. A terminal has
  no pixels the wall can see, so it offers no video layer and no picture
  of its glass (its README.md, *What it does not do, and why*).

      cargo build --release
      target\release\bitwall-term.exe [--font <n>] [wall]

Each makes its pipes for its user, the administrators and the system, so
a wall made in an elevated console is reached from one that is not, and
from ssh.

## The control page on Windows

`controllers/bitwall-control` runs on Windows and shows the machine's
own walls beside every other device's. This machine's are found and
opened in its own process: `local.py` lists `\\.\pipe\` (`relay.find`),
and each controller session is a `Link.here`. A web receiver's control
pair is asked the same way (`relay.web`), opening `bitwall-web.out`
before it writes `bitwall-web.in`, one caller at a time. Every other
device is reached through ssh as from a Mac, the page server putting
Git's `ssh` first on the path itself where Git is installed. A Windows
host seen from anywhere else, whose PowerShell answers the shell's
listing with nothing but complaint, is asked `relay run` once, and then
`relay find` and `relay web`.

Its devices are the machine's own `~\.bitwall\devices.json`, laid out
as `controllers/devices.example.json`: on Windows, this machine as
`localhost` and each Pi by an address, since Windows looks up no
`.local`. `--devices FILE` names another file.

```powershell
uv run --script controllers\bitwall-control                              # in a console
uv run --no-project --with aiohttp python controllers\bitwall-control     # over ssh
```

It listens on 127.0.0.1:8910 and the machine's Tailscale address, and
Ctrl+C in the console closes every session. To outlive an ssh session
it is started through WMI, as anything else here, and stopped with
`taskkill /T /F`:

```powershell
$d = "C:\Users\<user>\Documents\bitwall"
Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{ CommandLine = 'cmd /c uv run --no-project --with aiohttp python controllers\bitwall-control > ..\control.log 2>&1'; CurrentDirectory = $d }
```

The page's start, stop and move go through `bitwall-senders`, run by the
page server's own Python, so they work here on a Windows machine that
is a sender host (*A sender host*).

## Keeping it running

**Whatever an ssh session starts ends with it**, `Start-Process` and all:
Windows' sshd keeps the session's processes in a job and closes it when
the connection goes. A painter started with `Start-Process -WindowStyle
Hidden` was gone three seconds after ssh exited. A process made by WMI
is outside that job and stays, in session 0, its stderr in a file by way
of `cmd`:

```powershell
$d = "C:\Users\<user>\Documents\paint"
Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{ CommandLine = "cmd /c bitwall-paint.exe --run //./pipe/bitwall-<user> --glass 1920x1080 > paint.log 2>&1"; CurrentDirectory = $d }
```

It answers `ReturnValue 0` and the pid of the `cmd`. A sender is started
the same way from the repo's folder, `cmd /c uv run --no-project python
senders\machine\main.py localhost:0 > ..\machine.log 2>&1`. One that
reaches a wall on another host puts Git's `ssh` first inside that `cmd`,
as this mirror does:

```powershell
$d = "C:\Users\<user>\Documents\bitwall"
Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{ CommandLine = 'cmd /c set "PATH=C:\Program Files\Git\usr\bin;%PATH%" && python3 senders\mirror\main.py localhost:0 --to 192.0.2.10:1,192.0.2.11:0 > ..\mirror.log 2>&1'; CurrentDirectory = $d }
```

The painter, a sender and that mirror all keep running after ssh has
exited, the mirror at 25 fps on a Pi and a Mac.

**It is stopped by force, and only so.** In session 0 there is no window
to close and no console to send Ctrl+C to, so `taskkill` without `/F`
refuses, and a sender started by hand cannot catch the end and say
`detach` (one `bitwall-senders` started is asked first, *A sender host*):

```powershell
taskkill /T /F /PID <the cmd's pid>        # the cmd, uv and the sender under it
taskkill /F /IM bitwall-paint.exe          # every painter on the machine it may stop
```

The wall finds out at the end of the pipe and frees the base at once, as
on any wall whose sender died. A painter so killed takes its walls with
it. Nothing starts either at boot or brings it back (*Limits*).

## A sender host

A Windows machine runs `controllers/bitwall-senders` for itself and for
others, and is a host it starts senders on, from its own console or with
`--on <host>` from another machine (CONTROL.md):

```powershell
uv run --script controllers\bitwall-senders list
uv run --script controllers\bitwall-senders start bitwall/mirror localhost:0 --to 192.0.2.10:1
uv run --script controllers\bitwall-senders status localhost 192.0.2.10
uv run --script controllers\bitwall-senders start bitwall/snake 192.0.2.10:1 --on 192.0.2.10
```

**Its catalogue is directory junctions**, since a symlink there wants
Developer Mode or an administrator and a junction wants neither;
`catalogue.py` follows one as it does a link:

```powershell
mkdir $HOME\.bitwall\senders
New-Item -ItemType Junction -Path $HOME\.bitwall\senders\bitwall -Target $HOME\Documents\bitwall\senders\catalogue
```

An entry's `start` is found from where its `.json` really is, so
`../games/cells/main.py` is the repo's.

**A sender is started by WMI**, so it outlives the console or ssh session
that asked (*Keeping it running*), in session 0. `instance.py` writes a
launcher beside the record, `~\.bitwall\running\<id>.launch.py`, holding
the sender's argv, its environment and its log, and has
`Win32_Process.Create` run it with the Python that ran `instance.py`; the
launcher starts the sender and waits for it, and its pid is the one the
record keeps. A `.py`, or a script whose `#!` runs Python, is run as `uv
run --no-project --with <each dependency> python <script>`, its
dependencies and Python read from its PEP 723 header, which works in an
ssh session where `uv run --script` does not; a `.exe`, `.cmd` or `.bat`
is run as it is, and anything else is refused. The environment is the
asker's, `BITWALL_LOCAL_RUN` with it, and Git's `ssh` goes first on its
path where Git is installed.

**Stop and move are written into the record**, as `told`, `{"n": <count>,
"what": "stop" | "move"}`, since Windows has no SIGTERM to send and no
SIGUSR1. A thread of `bitwall.moving`, in every process that imports
bitwall with `BITWALL_RECORD` set, watches the file twice a second. On
`stop` it raises the signal the sender stops on -- its own SIGINT
handler where it has one, since only SIGINT wakes a main thread waiting
on Windows, else its own SIGTERM handler, else SIGINT, Python's
KeyboardInterrupt -- so the sender detaches and leaves as it does
anywhere; five seconds on, `taskkill /T /F` ends the launcher and what is
under it. On `move` it moves the sender as SIGUSR1 does on a Mac or a Pi.

**Its requests arrive as base64**, a word PowerShell 5.1 passes as it is:
it takes the double quotes out of JSON it hands a program, which
`instance.py` answered with `bad request: Expecting property name
enclosed in double quotes`. Run on Windows, `bitwall-senders` puts Git's
`ssh` first on its own path, for a host it asks over ssh and for the
walls it looks at (*Windows' ssh, run from a program in an ssh session*).

## Traps

- **`uv run --script` fails in an ssh session** with uv's own Python: a
  script's environment names uv's `cpython-3.14` junction as its home,
  which Windows will not follow there, so it fails with `uv trampoline
  failed to spawn Python child process`, `UV_PYTHON` set or not. What
  works over ssh: `uv run --python (Get-Command python3).Source --script
  <script>`, the Store's 3.12, which warns and runs a script asking for
  3.14; or `uv run --no-project --with <each dependency> python
  <script>`, uv's 3.14 with the dependencies said by hand (README.md,
  *On Windows*). In a console `uv run --script` works.
- **Windows' own `ssh` never exits** when a program in an ssh session
  starts it with its stdout a pipe: put Git's first on the path (*Windows'
  ssh, run from a program in an ssh session*).
- **Never look at a pipe by its path** (*A session*).
- **`git pull` belongs in a console.** Over ssh, Git's credential
  manager cannot reach its store (`Unable to persist credentials with the
  'wincredman' credential store`). A tar piped to `tar` there arrives byte
  for byte, so `git archive` from another machine updates the copy over
  ssh (README.md, *The painter on Windows*).
- **What an ssh session starts dies with it** (*Keeping it running*).

## Limits

- A painter started over ssh or by WMI runs in session 0, with no window
  anyone sees; nothing starts it at boot or brings it back, as a service
  or a scheduled task would. Whether Intel's GL driver thread spins in
  the console's session too is not known.
- `tests/bitwall-contracts` does not run against a wall on Windows: its
  callers read and write fifos with `os`, and it imports `fcntl` and
  `termios`. `tests/painter-pixels` and `tests/windows-pipes` reach a
  Windows wall, through `local.py`.
- The film sender asks a host's clock with `date +%s.%N`, which a
  Windows host answers only where Git's `date` is on its path, and then
  at the end of a fresh connection, a quarter second out; the mirror's
  `relay clock` does it right.
