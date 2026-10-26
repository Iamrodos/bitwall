"""Sessions on a wall: reaching one, opening a session at its door, holding it.

A wall is a directory under its host's run directory, `<run>/bitwall/<wall>/`,
holding a door and the sessions opened through it (PROTOCOL.md). A caller
writes one `open` line at the door, reads the session's `out` once it
appears, and writes frames and commands into its `in`. On another host it
does the same through ssh: `in` is written through one connection and
`out` read through another. `localhost` is this machine, reached the same
way with no ssh.

    link = Link("pi.local:1")                 # which wall, and how to get there
    s = Session(link, "sysinfo-4821", "layer", {"z": 0, "type": "cells", "take": 1},
                heard=..., ended=...)
    s.open()                                  # knock, then listen on out

`Wall` builds the base on a `Session`; `Controller` is a session for a
program that talks to a wall rather than draws on it.
"""
import collections
import hashlib
import io
import itertools
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from . import local as _local, portable, protocol, relay
from .receivers import pi as _pi

#: Targets that mean this machine. A wall here is reached by running the
#: command rather than sending it somewhere.
LOCAL = ("localhost", "127.0.0.1", "::1", "local", "")

#: Where a host keeps its walls, as its own shell finds it. Asked of the
#: host rather than known here, so a caller never has to know what kind
#: of machine it is talking to: a Pi's session has XDG_RUNTIME_DIR, and a
#: Mac, which has none, keeps them in the home directory.
RUN_HERE = "${XDG_RUNTIME_DIR:-$HOME/.bitwall/run}"

#: What a Windows host's run directory begins with: its walls are named
#: pipes (WINDOWS.md), reached through the relay rather than a shell.
PIPES = "//./pipe/"

#: What may follow the colon: a screen's number, or a name. Lower case
#: only, since a Mac's filesystem would take two names differing in case
#: for one directory; the one rule for every sender, receiver and
#: controller.
WALL_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")

#: A controller's instance id: lower case letters, digits and `-`.
INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

#: A session's name, or an id: one to thirty-two of these.
NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")

#: How long a caller waits for its `out` to appear before the request is
#: taken as dropped: PROTOCOL.md's five seconds.
OPEN_WAIT = 5.0

#: How the far end says why it stopped before the session said anything.
NO_WALL = 3           # no door, or nobody reading it: no wall is there
DROPPED = 4           # no `out` within the wait: the request was dropped
IN_USE = 5            # `s/<name>/` was there before the knock: the name is taken

#: The far end of an open: run on the wall's host. It fails at once when
#: nothing reads the door, so a dead wall is known before any request is
#: made, and when the name is already a session's, so it never reads an
#: older session's `out` as its own; writes the request in one write;
#: waits for the session's `out`; then copies everything the wall says to
#: stdout until `out` ends. Python rather than shell because only it can
#: open a fifo for writing without waiting on a reader that may never
#: come.
OPENER = """
import os, sys, time
wall, name, request = sys.argv[1:4]
out = os.path.join(wall, "s", name, "out")
if os.path.lexists(os.path.dirname(out)):
    sys.exit(%d)
try:
    fd = os.open(os.path.join(wall, "door.in"), os.O_WRONLY | os.O_NONBLOCK)
except OSError:
    sys.exit(%d)
os.write(fd, (request + "\\n").encode())
os.close(fd)
until = time.monotonic() + %s
while not os.path.exists(out):
    if time.monotonic() >= until:
        sys.exit(%d)
    time.sleep(0.02)
fd = os.open(out, os.O_RDONLY)
try:
    while True:
        data = os.read(fd, 65536)
        if not data:
            break
        while data:
            data = data[os.write(1, data):]
except BrokenPipeError:
    pass
""" % (IN_USE, NO_WALL, OPEN_WAIT, DROPPED)


def address(target):
    """The (host, wall) a target names.

    `host:<wall>` is that wall on that host, a screen's number or a name
    for a wall made by naming it, and `host` alone is wall 0. The wall
    comes back as it was written, so `001` is `001` and not `1`. A bare
    IPv6 address is all colons, so it is taken whole.
    """
    if target in LOCAL or target.count(":") != 1:
        return target, "0"
    host, _, wall = target.partition(":")
    if not (wall.isascii() and WALL_TOKEN.match(wall)):
        raise ValueError(f"bitwall: {target!r}: the wall after ':' must be "
                         f"a number, or a name of lower case letters, digits, "
                         f"_ and -, starting with a letter or digit")
    return host, wall


def wall_run(run, wall):
    """The directory of wall `wall`, given its host's run directory:
    `<run>/bitwall/<wall>`, every wall a peer of every other. A path on
    a wall's host is a POSIX one, whatever machine the caller is on."""
    return posixpath.join(run, "bitwall", wall)


def session_name(base=None, tag=None):
    """A session name that cannot collide with another caller's:
    `<base>-<pid>`, with `-<tag>` after it when given.

    The base is the program's name unless given: its script's, or its
    directory's where the script is `main.py`, so `senders/sysinfo/main.py`
    opens `sysinfo-4821`. Cut to fit the thirty-two characters a name has.
    """
    if base is None:
        path = sys.argv[0] if sys.argv and sys.argv[0] else ""
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem in ("", "-", "-c", "-m", "main", "__main__") and path:
            stem = os.path.basename(os.path.dirname(os.path.abspath(path)))
        base = stem
    base = re.sub(r"[^A-Za-z0-9_-]", "-", str(base)).strip("-") or "sender"
    tail = f"-{os.getpid()}" + (f"-{tag}" if tag else "")
    return base[:32 - len(tail)] + tail


def with_id(text, id_):
    """A command carrying an id where it will be read as one.

    A metadata value is the rest of the line, so an id after it would be
    part of what was set. `meta` takes it in front of the verb instead.
    """
    verb, space, rest = text.partition(" ")
    if verb == "meta":
        return f"meta id={id_}{space}{rest}"
    return f"{text} id={id_}"


class Link:
    """How a wall is reached: its host, the wall's directory there, and
    the command lines that run things on it.

    A wall on this machine is a process, not a host, so a command for it
    runs here instead of being carried anywhere. Only the host is ever
    handed to ssh; the wall is a directory on it.
    """

    def __init__(self, target, user=None, run=None):
        #: What the wall was asked for by, `host` or `host:N`.
        self.target = target
        self.host, self.wall = address(target)
        self.local = self.host in LOCAL
        # Connection defaults belong to the receiver that defines them,
        # not to a global config file.
        self.user = user if user is not None else _pi.USER
        # The wall's directory is the one thing a caller must know about
        # it, so it can be said from outside rather than in code. `run`
        # names the wall's directory; BITWALL_RUN, like the default, names
        # the host's run directory, and the wall is found inside it.
        # BITWALL_LOCAL_RUN names it for this machine's walls alone, so a
        # sender with walls of its own here still finds every other host's
        # where that host keeps them.
        base = os.environ.get("BITWALL_RUN") or (
            os.environ.get("BITWALL_LOCAL_RUN") if self.local else None)
        self._run = (run if run is not None
                     else wall_run(base, self.wall) if base else None)
        # One ssh connection per wall, so a film on one cannot queue
        # another's frames behind it, and losing one loses only its wall.
        self.cm = os.path.join(
            tempfile.gettempdir(), f"bitwall-cm-{self.host}"
            + (f"-{self.wall}" if self.wall != "0" else ""))
        # A Unix socket's path is 104 bytes at most on a Mac, whose
        # TMPDIR is long, and ssh adds `-raw` and a random 17 while it
        # makes the socket: a host and a long wall name go as a digest.
        if len(self.cm) + 21 > 100:
            self.cm = os.path.join(tempfile.gettempdir(), "bitwall-cm-" + hashlib.sha1(
                f"{self.host}:{self.wall}".encode()).hexdigest()[:12])
        self.sshopts = self._opts(True)
        #: Whether the wall's pipes are Windows' named pipes, reached
        #: through the relay: known once the run directory is.
        self._pipes = None
        #: Whether a session's ends are opened in this process rather than
        #: by a process run for each: a wall on this Windows machine.
        self.here = self.local and portable.WINDOWS

    def _opts(self, compress):
        """ssh's options for a connection compressed or not. A cell stream
        is escape sequences and repeated attributes, which compress about
        tenfold for a little CPU at each end; pictures and video are
        compressed already, and squeezing them again costs a core for
        nothing. Every session shares its master's compression, so the
        two kinds share two masters, never one."""
        opts = (["-C"] if compress else []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if portable.SSH_SHARES:
            opts += ["-o", f"ControlPath={self.cm}" + ("" if compress else "-raw"),
                     "-o", "ControlMaster=auto", "-o", "ControlPersist=60"]
        return opts

    def argv(self, cmd, extra=(), compress=True):
        """How to run a shell command on the wall's host, through a
        compressed connection unless told otherwise."""
        if self.local:
            return ["sh", "-c", cmd]
        return ([portable.SSH] + self._opts(compress) + list(extra)
                + [f"{self.user}@{self.host}", cmd])

    def relay_argv(self, verb, *args, extra=(), compress=True):
        """How to run the relay on the wall's host (`relay.py`): this
        Python where the wall is here, the host's `python3` through ssh
        where it is not."""
        if self.local:
            return relay.command(verb, *args, python=sys.executable)
        # A host asked `run` through the relay is one whose shell did not
        # answer with a path: Windows.
        windows = self._pipes if self._pipes is not None else verb == "run"
        return ([portable.SSH] + self._opts(compress) + list(extra)
                + [f"{self.user}@{self.host}", relay.line(
                    verb, *args, python=relay.WINDOWS_PYTHON if windows else "python3")])

    #: What keeps a writer's ssh honest: a connection that has died is
    #: found within a minute rather than held for ever.
    ALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")

    def writer_argv(self, path, compress=True):
        """The command that copies its stdin into the pipe at `path`, `in`
        or `data`: `cat` on a Mac or a Pi, the relay on Windows."""
        if self.pipes:
            return self.relay_argv("write", path, extra=self.ALIVE, compress=compress)
        return self.argv(f"exec cat > {shlex.quote(path)}", extra=self.ALIVE,
                         compress=compress)

    def writer(self, path, compress=True):
        """Something that copies what is written to its `stdin` into the
        pipe at `path`, a process or, for a wall here, the pipe itself:
        either way `stdin`, `wait`, `poll` and `kill`."""
        if self.here:
            return _HereWriter(path)
        return subprocess.Popen(self.writer_argv(path, compress), stdin=subprocess.PIPE,
                                **portable.detached())

    @property
    def pipes(self):
        """Whether the wall's host keeps its walls as Windows' named pipes."""
        if self._pipes is None:
            self._pipes = self.run.startswith(PIPES)
        return self._pipes

    def shell(self, cmd, timeout=15):
        """Run a command on the wall's host; what it printed."""
        try:
            r = subprocess.run(self.argv(cmd), capture_output=True,
                               stdin=subprocess.DEVNULL, text=True, timeout=timeout)
            return r.stdout.strip()
        except Exception:
            return ""

    @property
    def run(self):
        """The wall's directory, the host asked where it keeps its walls
        the first time it is needed. There is no default: a host that
        cannot say, or says something that is not a path, is an error
        rather than a guess that waits on a wall that is not there."""
        if self._run is None:
            if self.local and portable.WINDOWS:
                base = _local.run_dir()
            else:
                base = self.shell(f'printf %s "{RUN_HERE}"')
            if not base.startswith("/") and not self.local:
                # Not a POSIX shell's answer: a Windows host, whose shell
                # is PowerShell, says where through the relay.
                try:
                    r = subprocess.run(self.relay_argv("run"), capture_output=True,
                                       stdin=subprocess.DEVNULL, text=True, timeout=15)
                    said = r.stdout.strip()
                except Exception:
                    said = ""
                base = said if said.startswith(PIPES) else base
            if not base.startswith("/"):
                raise RuntimeError(
                    f"bitwall: could not ask {self.host} where it keeps its walls"
                    + (f": it said {base[:80]!r}" if base else ": no answer"))
            self._run = wall_run(base, self.wall)
        return self._run

    @property
    def q(self):
        """The wall's directory, safe to put in a command line.

        Quoted rather than trusted: it arrives from the environment, and a
        path is a string somebody else may have chosen.
        """
        return shlex.quote(self.run)

    def session_dir(self, name):
        return posixpath.join(self.run, "s", name)


class Blob:
    """A picture or a mirror frame as a session hears it: the line that
    announced it, and exactly the bytes it said would follow."""

    __slots__ = ("said", "data")

    def __init__(self, said, data):
        self.said, self.data = said, data

    def __repr__(self):
        return f"Blob({self.said!r}, {len(self.data)} bytes)"


_BLOB = re.compile(rb"^(picture|mirror) ")


class Session:
    """One session on a wall as its caller holds it.

    `open()` runs the far end of an open on the wall's host and reads its
    stdout, which is the session's `out`, on a thread of its own; each
    line, parsed, goes to `heard(session, said)`, and a picture or a
    mirror frame goes whole as a `Blob`. The writer, `cat` into the
    session's `in`, starts when the wall says `opened`, so nothing is ever
    written to a session the wall refused. When the session is over, by
    the wall's doing or by `close()` or `kill()`, `ended(session)` is
    called once, and `state` says how it went: `open`, `closed` (the wall
    said `closed`, the reason in `closed`), `refused` (`error open`, the
    reason in `refused`), `dropped` (no `out` within the wait, or the
    name already a session's), `absent` (nothing reads the door) or
    `lost` (`out` ended with no `closed`: the wall has gone).
    """

    def __init__(self, link, name, role, options=None, heard=None, ended=None,
                 compress=None):
        if not NAME.match(name):
            raise ValueError(f"bitwall: {name!r} is not a session name")
        self.link = link
        self.name = name
        #: Whether the session's ssh compresses: a cells layer's stream,
        #: unless told otherwise; not a video layer's pictures, nor a
        #: controller's, whose `out` carries pictures and mirrors.
        self.compress = (compress if compress is not None else
                         role == "layer" and (options or {}).get("type", "cells") != "video")
        self.role = role
        self.options = dict(options or {})
        self.request = protocol.render("open", name, role, **self.options)
        self.heard = heard or (lambda s, said: None)
        self.ended = ended or (lambda s: None)
        self.state = "opening"
        self.closed = None
        self.refused = None
        self.listener = None
        self.writer = None
        #: The writer's stdin, written by a thread of its own.
        self.pipe = None
        #: Set once `out` has ended, and once `ended` has been called.
        self.eof = threading.Event()
        self.done = threading.Event()
        self._buf = b""
        self._lock = threading.Lock()
        #: Held around every whole write, so two writers' commands never
        #: interleave on the pipe.
        self._wlock = threading.Lock()
        self._stopping = False

    def __repr__(self):
        return f"Session({self.name!r}, {self.role!r}, {self.state!r})"

    # ------------------------------------------------------------ opening

    def open_argv(self):
        """The command that knocks and then listens, as it runs on the host:
        OPENER through the host's shell, or on Windows the relay's `open`,
        which is the same thing for a named pipe."""
        if self.link.pipes:
            return self.link.relay_argv("open", self.link.run, self.name, self.request,
                                        OPEN_WAIT, compress=self.compress)
        cmd = (f"exec python3 -c {shlex.quote(OPENER)} {self.link.q} "
               f"{shlex.quote(self.name)} {shlex.quote(self.request)}")
        return self.link.argv(cmd, compress=self.compress)

    def writer_argv(self):
        """The command that carries this session's `in`."""
        return self.link.writer_argv(
            posixpath.join(self.link.session_dir(self.name), "in"), compress=self.compress)

    def open(self):
        """Knock at the door and listen on the session that answers."""
        # In a process group of its own, as the writer is: a Ctrl+C or a
        # controller's signal is for the caller, which closes the session
        # in good order, and an ssh it reached would end first, exiting as
        # it does for a wall that has gone. A caller that dies without
        # closing still ends it: its writer's input ends, the wall lets
        # the session go, and `out` ends with it.
        if self.link.here:
            self.listener = _HereOpener(self.link.run, self.name, self.request)
        else:
            self.listener = subprocess.Popen(self.open_argv(), stdout=subprocess.PIPE,
                                             stdin=subprocess.DEVNULL, **portable.detached())
        threading.Thread(target=self._listen, args=(self.listener,),
                         daemon=True, name=f"bitwall-out-{self.name}").start()
        return self

    def _start_writer(self):
        # In a session of its own, so a signal to the caller's process
        # group, a controller stopping it, leaves the writer to carry the
        # caller's last words, and it ends when its input does.
        proc = self.link.writer(posixpath.join(self.link.session_dir(self.name), "in"),
                                compress=self.compress)
        self.writer = proc
        self.pipe = portable.Pipe(proc, name=f"bitwall-in-{self.name}")
        threading.Thread(target=self._watch_writer, args=(proc,),
                         daemon=True).start()

    def _watch_writer(self, proc):
        """A writer that ends on its own is noticed, not left until the
        next write: a quiet caller writes nothing, and would otherwise go
        on listening to a wall that no longer reads it.

        The wall's own word comes first: a writer ends because the wall
        ended the session, and the `closed` that says why is on its way
        down `out`, so `out` is given a moment to end before the session
        is judged by the writer alone.
        """
        proc.wait()
        with self._lock:
            mine = proc is self.writer and not self._stopping
        if not mine or self.eof.wait(3.0):
            return
        with self._lock:
            if self.state == "open" and not self._stopping:
                self.state = "lost"
        self._over()

    def _over(self):
        """Say once that the session has come to an end."""
        with self._lock:
            done = self.done.is_set()
            self.done.set()
        if not done:
            self.ended(self)

    # ---------------------------------------------------------- listening

    def _listen(self, proc):
        try:
            while True:
                # read1, not read: a buffered read blocks until it has the
                # whole size asked for, and a wall says eighty bytes and
                # then waits for something to happen.
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                for said in self._feed(chunk):
                    if isinstance(said, protocol.Message):
                        self._note(said)
                    try:
                        self.heard(self, said)
                    except Exception:
                        pass              # one bad message is not a wall down
        except Exception:
            pass
        code = proc.wait()
        with self._lock:
            if self.state == "opening" and not self._stopping:
                self.state = ("absent" if code == NO_WALL else
                              "dropped" if code in (DROPPED, IN_USE) else "lost")
            elif self.state == "open" and not self._stopping:
                self.state = "lost"
            self.eof.set()
        self._over()

    def _note(self, said):
        """What a line means for the session itself."""
        if said.name == "opened" and said.args[:1] == (self.name,):
            with self._lock:
                if self.state == "opening" and not self._stopping:
                    # The writer before the state, so a caller that sees
                    # the session open finds something to write to.
                    self._start_writer()
                    self.state = "open"
        elif said.name == "error" and said.args[:2] == ("open", self.name):
            with self._lock:
                self.state = "refused"
                self.refused = said.args[2] if len(said.args) > 2 else "?"
        elif said.name == "closed" and said.args[:1] == (self.name,):
            with self._lock:
                self.state = "closed"
                self.closed = said.args[1] if len(said.args) > 1 else "?"

    def _feed(self, chunk):
        """Lines and blobs out of what has arrived so far."""
        self._buf += chunk
        out = []
        while True:
            cut = self._buf.find(b"\n")
            if cut < 0:
                if len(self._buf) > protocol.MAX_MESSAGE * 16:
                    self._buf = self._buf[-protocol.MAX_MESSAGE:]
                break
            line, rest = self._buf[:cut], self._buf[cut + 1:]
            text = line.decode("utf-8", "replace")
            said = protocol.parse(text)
            if said is None:
                self._buf = rest
                continue
            said.raw = " ".join(text.split())
            size = next((protocol.number(w) for w in reversed(said.args)
                         if protocol.number(w) is not None), None)
            # A picture or a mirror frame says how many bytes follow; a line
            # that says none, `mirror <session> clear`, is only a line.
            if _BLOB.match(line) and size is not None:
                if len(rest) < size:
                    break             # the bytes are still coming
                out.append(Blob(said, rest[:size]))
                self._buf = rest[size:]
                continue
            self._buf = rest
            out.append(said)
        return out

    # ------------------------------------------------------------ writing

    @property
    def writable(self):
        pipe = self.pipe
        return self.writer is not None and pipe is not None and not pipe.closed

    @property
    def ready(self):
        """Whether `in` has taken everything written to it, so a frame
        written now goes at once rather than waiting behind another."""
        pipe = self.pipe
        return self.writer is not None and pipe is not None and pipe.idle

    def write(self, data, stop=None, timeout=None):
        """Put `data` whole on the session's `in`; how many bytes went, or
        None if the pipe is gone, or it was not taken in time.

        A wall that has stopped reading holds a write only until `stop` is
        set or `timeout` seconds have passed, not for ever.
        """
        pipe = self.pipe
        if self.writer is None or pipe is None:
            return None
        with self._wlock:
            sent = pipe.write(data, stop, timeout)
        return sent if sent == len(data) else None

    # ------------------------------------------------------------- ending

    def close(self, last=None, linger=2.0):
        """End the session in good order: `last`, an APC such as `detach`
        or `close`, goes after everything else, the writer ends when its
        input does, `out` is given `linger` seconds to end with the wall's
        `closed`, and the listener is let go. Bounded throughout, at about
        twice `linger`, so a caller stopping is not held up by a wall
        that is not answering."""
        with self._lock:
            self._stopping = True
            writer, listener, pipe = self.writer, self.listener, self.pipe
            self.writer = self.listener = self.pipe = None
        if writer is not None and pipe is not None:
            with self._wlock:
                if last:
                    pipe.write(last, timeout=linger / 4)
                pipe.close()
            # The writer ends when its input does, once what it holds has
            # reached the wall; it is only killed when it will not.
            try:
                writer.wait(timeout=linger / 2)
            except Exception:
                _end(writer)
        self.eof.wait(linger)
        _end(listener)
        self._over()

    def kill(self):
        """Let go of both ends at once, as when the wall has gone."""
        with self._lock:
            self._stopping = True
            writer, listener, pipe = self.writer, self.listener, self.pipe
            self.writer = self.listener = self.pipe = None
        if pipe is not None:
            pipe.close()
        for proc in (writer, listener):
            _end(proc)
        self._over()


class _Here:
    """One end of a session on a wall on this machine, opened in this
    process: what the opener or a writer is as a process, `wait`, `poll`,
    `terminate` and `kill` included, so `Session` holds either alike."""

    def __init__(self):
        self.returncode = None
        self._done = threading.Event()

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
        self._done.set()

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("bitwall-here", timeout)
        return self.returncode

    def terminate(self):
        self._finish(-15)

    kill = terminate


class _HereOpener(_Here):
    """The opener in this process: knocks, waits for `out`, and is its
    own `stdout`, read until the wall ends it. Exits as the opener does,
    NO_WALL, DROPPED or IN_USE, before `out` was there."""

    def __init__(self, run, name, request):
        super().__init__()
        self.stdout = self
        self._fd = None
        self._opened = threading.Event()
        threading.Thread(target=self._open, args=(run, name, request), daemon=True,
                         name=f"bitwall-open-{name}").start()

    def _open(self, run, name, request):
        sdir = posixpath.join(run, "s", name)
        try:
            if _local.taken(sdir):
                return self._finish(IN_USE)
            try:
                _local.knock(posixpath.join(run, "door.in"), request)
            except _local.NoDoor:
                return self._finish(NO_WALL)
            try:
                self._fd = _local.open_read(posixpath.join(sdir, "out"), OPEN_WAIT)
            except _local.NoDoor:
                return self._finish(DROPPED)
        except OSError:
            return self._finish(NO_WALL)
        finally:
            self._opened.set()

    def read1(self, n=65536):
        """What `out` has said so far; b"" once it has ended."""
        self._opened.wait()
        if self._fd is None or self._done.is_set():
            return b""
        try:
            data = os.read(self._fd, n)
        except OSError:
            data = b""
        if not data:
            # Closed by the reader, here, never by another thread: a pipe
            # on Windows cannot be closed under a read.
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._finish(0)
        return data


class _Stdin(io.FileIO):
    """A writer's end of a pipe, which says when it has been closed."""

    def close(self):
        try:
            super().close()
        finally:
            self.closed_by.set()


class _HereWriter(_Here):
    """A writer in this process: `stdin` is the pipe itself, and the writer
    has ended once that is closed. Where the pipe is not there, `stdin`
    is one nobody reads, and the first write finds it broken."""

    def __init__(self, path):
        super().__init__()
        try:
            fd = _local.open_write(path, wait=2.0)
        except OSError:
            r, fd = os.pipe()
            os.close(r)
        self.stdin = _Stdin(fd, "wb", closefd=True)
        self.stdin.closed_by = self._done
        self.returncode = 0


def _end(proc):
    """Stop a process for certain, and soon: asked first, then killed."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=0.3)
        except Exception:
            pass


class Controller:
    """A controller session on a wall, for a program that talks to a wall
    rather than draws on it: `bitwall-ctl`, the control page, a presence feeder.

        c = Controller("pi.local:1").open()
        for said in c.ask("stats 1 show=all"):
            print(said.raw)
        c.send("follow in")
        while (said := c.read(timeout=5)) is not None:
            ...
        c.close()

    `open()` waits for the wall's answer and raises where there is no wall
    (`NoWall`) or it refused (`Refused`); a request the door drops is made
    again under `<name>-r<k>`, a few times, since the name is most often
    a session of this controller's own that the wall has not yet let go
    of. Once open it asks for the wall's report, which connects the
    session, and keeps the answer as `report`. `ask()` sends a command
    with an id and returns every line that answered it; `read()` is the
    next line the wall said that nobody has taken, a `Blob` for a picture
    or a mirror frame. A `ping` from the wall is answered here and never
    surfaced. The session's name is `ctl-<pid>` unless given, the second
    and later controllers of one process `ctl-<pid>-2` and on.

    `instance` is the id a controller started this under, said on the
    open so that the wall's `sessions` lists it, as it lists a base's or
    a layer's: that is how a sender that holds no place on a wall, only
    a controller session there, is found on it. Left out, it is
    `BITWALL_INSTANCE`, taken from the environment by the first
    Controller of a sender a controller placed `controller` (told in
    `BITWALL_PLACE`), so the controllers it opens after, on other walls,
    do not claim it; a sender placed anywhere else has its instance
    carried by its Wall or layer, and its controllers leave it alone.
    """

    #: How many times a dropped request is made again, and the wait
    #: between, which is about what a wall takes to let a name go.
    TRIES = 3
    RETRY = 0.5
    #: How long the report asked for as the session opens is waited for.
    #: It ends at its `area`, so this is only ever waited out on a wall
    #: that does not answer; a wall over a slow ssh answers in more than
    #: the few seconds an ordinary command is given.
    REPORT_WAIT = 15.0

    def __init__(self, target, name=None, user=None, run=None, wait=OPEN_WAIT + 15,
                 compress=False, instance=None):
        self.link = Link(target, user, run)
        if instance is None and os.environ.get("BITWALL_PLACE") == "controller":
            instance = os.environ.pop("BITWALL_INSTANCE", None)
        #: The instance said on the open, or None: one of any other form is
        #: not said, as a Wall's and a layer's is not.
        self.instance = instance if instance and INSTANCE.match(instance) else None
        #: A controller hears pictures and mirrors, compressed already.
        self.compress = compress
        self._seq = next(_controllers)
        self.name = name or session_name(
            "ctl", tag=str(self._seq) if self._seq > 1 else None)
        self.wait = wait
        #: What the wall said of itself as the session opened.
        self.report = []
        self._heard = collections.deque()
        self._ready = threading.Condition()
        self._ended = False
        self._n = 0
        #: What every id this controller sends begins with, so that no
        #: two controllers on one wall, or two runs of one, share an id.
        self._ids = f"c{os.getpid()}-{self._seq}-"
        self.session = self._make(self.name)

    def _make(self, name):
        return Session(self.link, name, "controller",
                       {"instance": self.instance} if self.instance else {},
                       heard=self._on_line, ended=self._on_end, compress=self.compress)

    @property
    def state(self):
        return self.session.state

    @property
    def closed(self):
        """Why the wall ended the session, once it has."""
        return self.session.closed

    def open(self):
        asked = self.name
        for attempt in range(self.TRIES):
            if attempt:
                tag = f"-r{attempt}"
                self.session = self._make(asked[:32 - len(tag)] + tag)
                self._ended = False
            self.session.open()
            until = time.monotonic() + self.wait
            with self._ready:
                while self.session.state == "opening" and not self._ended:
                    left = until - time.monotonic()
                    if left <= 0:
                        break
                    self._ready.wait(left)
            state = self.session.state
            if state == "open":
                self.name = self.session.name
                # The first write connects the session, and a wall removes
                # one nobody writes to; the report is what a controller
                # wants to know first in any case, so it is asked for here
                # and kept.
                self.report = self.ask("report", timeout=self.REPORT_WAIT)
                return self
            self.session.kill()
            if state != "dropped":
                break
            time.sleep(self.RETRY)
        if state == "absent":
            raise NoWall(f"bitwall: {self.link.target}: no wall is there")
        if state == "refused":
            raise Refused(f"bitwall: {self.link.target}: refused: {self.session.refused}")
        if state == "dropped":
            raise Refused(f"bitwall: {self.link.target}: the door did not answer")
        raise NoWall(f"bitwall: {self.link.target}: could not open a session")

    def _on_line(self, session, said):
        if session is not self.session:
            return
        keep = True
        if isinstance(said, protocol.Message):
            if said.name == "ping":
                self.send("pong")
                keep = False
            # The greeting at the door is the session's own business; a
            # `hello` heard once the session is open begins a report.
            elif said.name == "opened" and said.args[:1] == (self.name,):
                keep = False
            elif said.name == "hello" and session.state == "opening":
                keep = False
        with self._ready:
            if keep:
                self._heard.append(said)
            self._ready.notify_all()

    def _on_end(self, session):
        if session is not self.session:
            return
        with self._ready:
            self._ended = True
            self._ready.notify_all()

    #: How long a command may wait on a full pipe before it is given up:
    #: a wall that has stopped reading is not one to hang a controller on.
    SEND_WAIT = 5.0

    def send(self, text):
        """One command, as PROTOCOL.md writes it: `font 20`, `presence 1
        from=lounge`. True if it went."""
        data = (protocol.APC + protocol.MARK + " " + text + protocol.ST).encode()
        return self.session.write(data, timeout=self.SEND_WAIT) is not None

    def say(self, name, *args, **opts):
        """A command built of words, checked to be words."""
        return self.send(protocol.render(name, *args, **opts))

    def read(self, timeout=None):
        """The next thing the wall said, or None when nothing came in time
        or the session has ended."""
        until = None if timeout is None else time.monotonic() + timeout
        with self._ready:
            while not self._heard:
                if self._ended:
                    return None
                left = None if until is None else until - time.monotonic()
                if left is not None and left <= 0:
                    return None
                self._ready.wait(left)
            return self._heard.popleft()

    def ask(self, text, timeout=3.0, quiet=0.4):
        """Send `text` with an id and collect what answered it.

        A report ends at its `area` and `sessions` at its count; any other
        answer is taken to be over once nothing more has come for `quiet`
        seconds, or at `timeout`. Lines heard meanwhile that were not
        answers, a followed line among them, are kept for `read()`.
        """
        self._n += 1
        id_ = self._ids + str(self._n)
        verb = text.split()[0] if text.split() else ""
        last = {"report": "area", "sessions": "sessions", "ping": "pong"}.get(verb)
        if not self.send(with_id(text, id_)):
            return []
        got, others = [], []
        deadline = time.monotonic() + timeout
        settle = None
        while True:
            until = deadline if settle is None else min(deadline, settle)
            said = self.read(max(0.0, until - time.monotonic()))
            if said is None:
                if time.monotonic() >= until or self._ended:
                    break
                continue
            line = said if isinstance(said, protocol.Message) else said.said
            if line.get("id") != id_ or line.name in (">", "<"):
                others.append(said)
                continue
            got.append(said)
            if last is not None and line.name in (last, "error"):
                break
            settle = time.monotonic() + quiet
        if others:
            with self._ready:
                self._heard.extendleft(reversed(others))
        return got

    def close(self):
        """Say `close`, and let the session go."""
        self.session.close(last=(protocol.APC + protocol.MARK + " close"
                                 + protocol.ST).encode())


#: Which Controller of this process each is, for its default name and
#: its ids.
_controllers = itertools.count(1)


class NoWall(RuntimeError):
    """No wall is there: no door, or nobody reading it."""


class Refused(RuntimeError):
    """The wall would not open the session, or dropped the request."""
