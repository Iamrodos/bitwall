"""A wall's local end: its door and its sessions' pipes, on the machine it runs on.

On a Mac or a Pi each is a fifo at its path; on Windows each is a named
pipe at the same path, under a run directory of `//./pipe/bitwall-<user>`
(WINDOWS.md). Both sides of a wall use this, so neither asks which it is:

    the wall's side                      a caller's side
    make_door(path)    claim and read    knock(door, request)
    make_in(path)      a pipe it reads   taken(session_dir)
    make_out(path)     a pipe it writes  open_read(path, wait)  -> fd
    read(end, n)                         open_write(path)       -> fd
    write(end, data)   never blocks      ready(fd, timeout)
                                         run_dir()
    select(ends, t)    which can be read walls(run)
    drained(end)       its reader has it all
    gone(end)          its reader came and went
    close(end)

A fifo's end is its descriptor, and every call on it is the call the
receivers made before there was a Windows: `os.read`, `os.write` and
`select` on it. A pipe's end is an object holding the pipe's handle and a
thread that reads it, overlapped, a megabyte at a time: one piece in hand,
and the next not read until that one is taken, so a wall that stops
reading fills the pipe and the writer waits, as a fifo's does.

The standard library only, and nothing from the rest of bitwall, since
`relay.py` carries it to a host that has none of bitwall, in one
`python3 -c`.
"""
import errno
import os
import posixpath
import select as _select
import sys
import threading
import time

PIPES = os.name == "nt"

if PIPES:
    import _winapi
    import msvcrt

#: The buffer a wall gives each pipe it reads, which is how much a writer
#: may get ahead of it: what `F_SETPIPE_SZ` gives a fifo on a Pi.
IN_BUFFER = 1 << 20
#: The buffer of a pipe the wall writes, `out`: a fifo's on a Mac.
OUT_BUFFER = 1 << 16
#: How many callers may be at a Windows door at once; a fifth waits for
#: one of them, a millisecond or less.
DOOR_INSTANCES = 4
#: How much of a door request is kept: a request is one line.
DOOR_MAX = 4096

# Windows' own numbers, which `_winapi` does not all name.
_INBOUND, _OUTBOUND = 0x1, 0x2
_OVERLAPPED = 0x40000000
_FIRST = 0x00080000              # FILE_FLAG_FIRST_PIPE_INSTANCE
_NOWAIT = 0x1
_LOCAL_ONLY = 0x8                # PIPE_REJECT_REMOTE_CLIENTS
_NOT_FOUND, _DENIED, _BROKEN, _BUSY, _NO_DATA = 2, 5, 109, 231, 232
_CONNECTED, _LISTENING, _ABORTED, _PENDING = 535, 536, 995, 997
_TIMEOUT = 258


class NoDoor(OSError):
    """Nothing reads the door: no wall is there."""


class Claimed(OSError):
    """The wall's name is held by a live receiver."""


def native(path):
    """A path as Windows' calls take it: `//./pipe/a/b` is `\\\\.\\pipe\\a\\b`."""
    return path.replace("/", "\\") if PIPES else path


def run_dir():
    """Where this machine keeps its walls, as PROTOCOL.md says a host's
    shell finds them; on Windows the pipe namespace, the user in the name
    since the machine's users share it."""
    if PIPES:
        user = (os.environ.get("USERNAME") or "user").lower()
        return f"//./pipe/bitwall-{user}"
    return os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".bitwall", "run")


def _pipes():
    """Every named pipe on this machine, as Windows lists them: the name
    after `\\\\.\\pipe\\`. A listing, never a look at one by its path:
    that takes the waiting instance, and the next caller is refused."""
    try:
        return os.listdir("\\\\.\\pipe\\")
    except OSError:
        return []


def _pipe_name(path):
    """A pipe's name in the listing: its path after `//./pipe/`."""
    return native(path)[len("\\\\.\\pipe\\"):].lower()


# ------------------------------------------------------------ a caller's side

def knock(door, request):
    """Put one request at a wall's door, in one write. NoDoor when nothing
    reads it, found at once rather than after a wait."""
    data = (request + "\n").encode()
    if not PIPES:
        try:
            fd = os.open(door, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            raise NoDoor(e.errno, f"no wall reads {door}") from e
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return
    h = _connect(door, _winapi.GENERIC_WRITE, wait=0)
    try:
        _winapi.WriteFile(h, data)
    finally:
        _winapi.CloseHandle(h)


def _connect(path, access, wait):
    """A handle on the pipe at `path`: tried until `wait` seconds have
    gone while it is not there, and for as long as a busy door takes to
    free an instance. NoDoor when it never came."""
    name = native(path)
    until = time.monotonic() + wait
    busy_until = time.monotonic() + 5.0
    while True:
        try:
            return _winapi.CreateFile(name, access, 0, _winapi.NULL,
                                      _winapi.OPEN_EXISTING, 0, _winapi.NULL)
        except OSError as e:
            code = getattr(e, "winerror", None)
            if code == _BUSY and time.monotonic() < busy_until:
                try:
                    _winapi.WaitNamedPipe(name, 1000)
                except OSError:
                    time.sleep(0.001)
                continue
            if code in (_NOT_FOUND, _BUSY) and time.monotonic() < until:
                time.sleep(0.02)
                continue
            raise NoDoor(errno.ENOENT, f"nothing at {path}") from e


def taken(session_dir):
    """Whether a session of that name is on the wall already."""
    if not PIPES:
        return os.path.lexists(session_dir)
    mine = _pipe_name(session_dir) + "\\"
    return any(p.lower().startswith(mine) for p in _pipes())


def open_read(path, wait):
    """A descriptor reading the pipe at `path` once it is there, waiting
    up to `wait` seconds for it; NoDoor if it never came. It reads to end
    of file when the wall closes its end."""
    if not PIPES:
        until = time.monotonic() + wait
        while not os.path.exists(path):
            if time.monotonic() >= until:
                raise NoDoor(errno.ENOENT, f"nothing at {path}")
            time.sleep(0.02)
        return os.open(path, os.O_RDONLY)
    return _fd(_connect(path, _winapi.GENERIC_READ, wait), os.O_RDONLY)


def open_write(path, wait=0.0):
    """A descriptor writing the pipe at `path`, which blocks while the
    wall's end is full: the backpressure a sender is paced by."""
    if not PIPES:
        return os.open(path, os.O_WRONLY)
    return _fd(_connect(path, _winapi.GENERIC_WRITE, wait), 0)


def _fd(handle, flags):
    fd = msvcrt.open_osfhandle(handle, flags)
    msvcrt.setmode(fd, os.O_BINARY)
    return fd


def ready(fd, timeout):
    """Whether a descriptor from `open_read` has something to read, or has
    reached its end, waiting up to `timeout` seconds: `select` on a fifo,
    and on a pipe a look at how much is waiting in it, so a caller that
    hears nothing in time can let the pipe go rather than be held in a
    read that may never return."""
    if not PIPES:
        return bool(_select.select([fd], [], [], timeout)[0])
    h = msvcrt.get_osfhandle(fd)
    until = time.monotonic() + timeout
    while True:
        try:
            if _winapi.PeekNamedPipe(h, 0)[0]:
                return True
        except OSError:
            return True                 # broken: the read says end of file
        if time.monotonic() >= until:
            return False
        time.sleep(0.01)


def walls(run):
    """The walls under a run directory with a live door, by name."""
    if PIPES:
        head = _pipe_name(posixpath.join(run, "bitwall")) + "\\"
        found = []
        for p in _pipes():
            low = p.lower()
            if low.startswith(head) and low.endswith("\\door.in"):
                wall = low[len(head):-len("\\door.in")]
                if wall and "\\" not in wall:
                    found.append(wall)
        return sorted(set(found))
    top = os.path.join(run, "bitwall")
    try:
        names = os.listdir(top)
    except OSError:
        return []
    return sorted(n for n in names if not n.startswith(".")
                  and door_live(os.path.join(top, n, "door.in")))


def door_live(path):
    """Whether something reads the door at `path`: a live wall's."""
    if PIPES:
        return _pipe_name(path) in (p.lower() for p in _pipes())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT, errno.ENOTDIR):
            return False
        raise
    os.close(fd)
    return True


# --------------------------------------------------------------- a wall's side

def make_door(path):
    """The door a wall reads, claimed: Claimed when a live receiver
    holds the name. On POSIX the fifo is made and opened here and the
    claim is the receiver's, by renaming its directory into place."""
    if not PIPES:
        os.mkfifo(path, 0o600)
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    return _Door(path)


def make_in(path, size=None):
    """A pipe the wall reads, `in` or `data`: at end of file when its
    writer goes, and ready for the next writer after that."""
    if not PIPES:
        os.mkfifo(path, 0o600)
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    return _In(path, size or IN_BUFFER)


def make_out(path):
    """A pipe the wall writes, `out`: what is written before a reader
    comes is held for it."""
    if not PIPES:
        os.mkfifo(path, 0o600)
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)
    return _Out(path)


def read(end, n):
    """What has arrived, up to `n` bytes; b"" at end of file, when the
    writer has gone; BlockingIOError when nothing has."""
    if not PIPES:
        return os.read(end, n)
    return end.read(n)


def write(end, data):
    """As much of `data` as the pipe takes now, never waiting:
    BlockingIOError when it takes nothing, BrokenPipeError when its
    reader has gone."""
    if not PIPES:
        return os.write(end, data)
    return end.write(data)


def close(end):
    if end is None:
        return
    if not PIPES:
        try:
            os.close(end)
        except OSError:
            pass
        return
    end.close()


def select(ends, timeout):
    """The ends among `ends` that have something to read, or have reached
    end of file, waiting up to `timeout` seconds for one."""
    if not PIPES:
        return _select.select(ends, [], [], timeout)[0]
    until = time.monotonic() + timeout
    with _bell:
        while True:
            ready = [e for e in ends if e.ready()]
            left = until - time.monotonic()
            if ready or left <= 0:
                return ready
            _bell.wait(left)


def drained(end):
    """Whether everything written to `out` has been read, or will be by
    a reader already there: a pipe's bytes outlive the wall's handle."""
    if not PIPES:
        return not _select.select([end], [], [], 0)[0]
    return end.connected


def gone(end):
    """Whether the reader of a pipe the wall writes has come and gone, so
    it will take nothing more and the next reader wants a pipe made anew.
    Never on a fifo, which the wall holds open for reading itself."""
    if not PIPES:
        return False
    return end.gone()


#: Rung by every pipe's reader when something arrives, for `select`.
_bell = threading.Condition()

_ZERO = 0


class _Reader:
    """A pipe the wall reads, with a thread reading it overlapped: one
    piece in hand at a time, end of file said once per writer."""

    def __init__(self, path, size, first=False, whole=False, instances=2):
        self.path = path
        self.name = native(path)
        self.size = size
        self.instances = instances
        self.whole = whole
        self._lock = threading.Condition()
        self._pieces = []
        self._eofs = 0
        self._closed = False
        self._ov = None
        self._h = self._make(first)
        threading.Thread(target=self._run, daemon=True,
                         name=f"bitwall-pipe-{os.path.basename(path)}").start()

    def _make(self, first=False):
        try:
            return _winapi.CreateNamedPipe(
                self.name, _INBOUND | _OVERLAPPED | (_FIRST if first else 0),
                _LOCAL_ONLY, self.instances, 4096, self.size, 0, _winapi.NULL)
        except OSError as e:
            if first and getattr(e, "winerror", None) == _DENIED:
                raise Claimed(errno.EEXIST, f"{self.path}: a live wall holds it") from e
            raise

    def ready(self):
        with self._lock:
            return bool(self._pieces) or self._eofs > 0

    def read(self, n):
        with self._lock:
            if self._pieces:
                piece = self._pieces[0]
                if len(piece) <= n:
                    self._pieces.pop(0)
                else:
                    self._pieces[0] = piece[n:]
                    piece = piece[:n]
                self._lock.notify_all()
                return piece
            if self._eofs:
                self._eofs -= 1
                self._lock.notify_all()
                return b""
        raise BlockingIOError(errno.EAGAIN, "nothing yet")

    def write(self, data):
        raise OSError(errno.EBADF, "a pipe the wall reads")

    def close(self):
        with self._lock:
            self._closed = True
            self._lock.notify_all()
        ov = self._ov
        if ov is not None:
            try:
                ov.cancel()
            except OSError:
                pass

    def _wait(self, ov):
        """An overlapped call's result, given up when the end is closed."""
        self._ov = ov
        try:
            while True:
                if self._closed:
                    try:
                        ov.cancel()
                    except OSError:
                        pass
                got = _winapi.WaitForMultipleObjects([ov.event], False, 200)
                if got != _TIMEOUT:
                    break
            return ov.GetOverlappedResult(True)
        finally:
            self._ov = None

    def _push(self, data, eof=False):
        with self._lock:
            # One piece in hand at a time: the next is not read until the
            # wall has taken this one, so a wall that stops reading fills
            # the pipe and its writer waits.
            while self._pieces and not self._closed:
                self._lock.wait(0.5)
            if data:
                self._pieces.append(data)
            if eof:
                self._eofs += 1
        with _bell:
            _bell.notify_all()

    def _run(self):
        h = self._h
        try:
            while not self._closed:
                try:
                    ov = _winapi.ConnectNamedPipe(h, overlapped=True)
                    self._wait(ov)
                except OSError as e:
                    if getattr(e, "winerror", None) not in (_CONNECTED, _NO_DATA):
                        if self._closed:
                            break
                        raise
                # The next writer's instance is there before this one goes,
                # so a writer that comes now is never told there is no pipe.
                nxt = self._make() if not self._closed else None
                held = []
                while not self._closed:
                    try:
                        ov, err = _winapi.ReadFile(h, 1 << 20, overlapped=True)
                        n, err = self._wait(ov)
                        # A read's result says end of file and a cancel
                        # rather than raising them.
                        if err in (_BROKEN, _ABORTED):
                            break
                        data = bytes(ov.getbuffer())
                    except OSError as e:
                        if getattr(e, "winerror", None) in (_BROKEN, _ABORTED, _NO_DATA):
                            break
                        raise
                    if not data:
                        continue
                    if self.whole:
                        if sum(map(len, held)) < DOOR_MAX:
                            held.append(data)
                    else:
                        self._push(data)
                if self.whole:
                    if held and not self._closed:
                        self._push(b"".join(held))
                elif not self._closed:
                    self._push(b"", eof=True)
                _winapi.CloseHandle(h)
                if nxt is None:
                    return
                h = self._h = nxt
        except OSError:
            pass
        finally:
            try:
                _winapi.CloseHandle(self._h)
            except OSError:
                pass


class _In(_Reader):
    """`in` or `data`: one writer at a time, end of file when it goes."""

    def __init__(self, path, size):
        super().__init__(path, size, instances=2)


class _Door:
    """A Windows door: several instances, so callers at once each have
    one, and each caller's request handed on whole when it closes, so two
    requests never mix whatever their length. The first instance is made
    first-of-its-name, which is the claim."""

    def __init__(self, path):
        self.path = path
        self._ends = [_Reader(path, 4096, first=(i == 0), whole=True,
                              instances=DOOR_INSTANCES * 2)
                      for i in range(DOOR_INSTANCES)]

    def ready(self):
        return any(e.ready() for e in self._ends)

    def read(self, n):
        out = []
        for e in self._ends:
            try:
                out.append(e.read(n))
            except BlockingIOError:
                pass
        data = b"".join(out)
        if not data:
            raise BlockingIOError(errno.EAGAIN, "nothing yet")
        return data

    def write(self, data):
        raise OSError(errno.EBADF, "the door only takes requests")

    def close(self):
        for e in self._ends:
            e.close()


class _Out:
    """`out`: written without waiting (PIPE_NOWAIT), a write that does not
    fit taking nothing; written before its reader comes, nothing is taken
    and the wall's queue holds it, as a fifo the wall holds open for
    reading would."""

    def __init__(self, path):
        self.path = path
        self.connected = False
        self._h = _winapi.CreateNamedPipe(
            native(path), _OUTBOUND, _NOWAIT | _LOCAL_ONLY, 1,
            OUT_BUFFER, 4096, 0, _winapi.NULL)

    def ready(self):
        return False

    def read(self, n):
        raise BlockingIOError(errno.EAGAIN, "a pipe the wall writes")

    def write(self, data):
        if self._h is None:
            raise BrokenPipeError(errno.EPIPE, "closed")
        try:
            n, err = _winapi.WriteFile(self._h, data)
        except OSError as e:
            code = getattr(e, "winerror", None)
            if code == _LISTENING:
                raise BlockingIOError(errno.EAGAIN, "no reader yet") from e
            if code in (_NO_DATA, _BROKEN):
                raise BrokenPipeError(errno.EPIPE, "its reader has gone") from e
            raise
        self.connected = True
        if n == 0 and data:
            raise BlockingIOError(errno.EAGAIN, "full")
        return n

    def gone(self):
        # ConnectNamedPipe on a PIPE_NOWAIT pipe only says its state:
        # ERROR_NO_DATA once a reader has closed and nothing has reset it.
        if self._h is None:
            return True
        try:
            _winapi.ConnectNamedPipe(self._h)
        except OSError as e:
            return getattr(e, "winerror", None) in (_NO_DATA, _BROKEN)
        return False

    def close(self):
        h, self._h = self._h, None
        if h is not None:
            try:
                _winapi.CloseHandle(h)
            except OSError:
                pass


def binary_stdio():
    """stdin and stdout as bytes, untranslated, where the C library would
    otherwise turn each newline into two."""
    if PIPES:
        for fd in (0, 1):
            try:
                msvcrt.setmode(fd, os.O_BINARY)
            except OSError:
                pass


def copy(src, dst):
    """Everything from one descriptor to another until end of file or a
    broken pipe at either end; how many bytes went."""
    total = 0
    try:
        while True:
            data = os.read(src, 1 << 16)
            if not data:
                break
            total += len(data)
            while data:
                data = data[os.write(dst, data):]
    except (BrokenPipeError, OSError):
        pass
    return total


if __name__ == "__main__":
    sys.exit("bitwall/local.py is a module; relay.py is the program")
