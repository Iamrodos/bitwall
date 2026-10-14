"""The painter built for this machine and run on its own GPU, for the tests.

`build()` is `make PLATFORM=<this machine>` in bitwall/receivers/pi, kept
in ~/.cache/bitwall-tests under a hash of the sources and the Makefile, so
every run and every worker of one starts at once. `Painter` runs it with no
screen, its glass `--glass WxH`, in a run directory of its own, and
`Session` is a caller at its door: `open` knocked, `out` read, `in`
written, as PROTOCOL.md has it, with no library between.

There is a GPU painter here where the Makefile has a platform for a
desktop, which is a Mac; a Pi's GPU, KMS and V4L2 are looked at on a Pi.
On Windows the painter is built in MSYS2 (README.md, "On Windows") and
BITWALL_PAINT_EXE names it: its walls are named pipes (WINDOWS.md), which
`there`, `Reader` and `writer` reach as the fifos are reached elsewhere. Where there is none `why_not()` says why, and the
tests that need one skip and say so.
"""
import contextlib
import errno
import hashlib
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from bitwall import local                                      # noqa: E402

WINDOWS = os.name == "nt"
PI_DIR = os.path.join(ROOT, "bitwall", "receivers", "pi")
FONT_FILE = os.path.join(PI_DIR, "MesloLGSNerdFontMono-Regular.ttf")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "bitwall-tests")

APC, ST = "\x1b_", "\x1b\\"


def platform():
    """The Makefile's PLATFORM for this machine's own GPU, or None."""
    return "mac" if sys.platform == "darwin" else "windows" if WINDOWS else None


def why_not():
    """Why there is no GPU painter here, or None when there can be one."""
    if os.environ.get("BITWALL_PAINT_EXE"):
        return None
    if WINDOWS:
        return "on Windows the painter is built in MSYS2 and BITWALL_PAINT_EXE names it"
    if platform() is None:
        return f"no GPU painter builds on {sys.platform}: the Makefile has no platform for it"
    if not shutil.which("make") or not shutil.which("pkg-config"):
        return "make and pkg-config are needed to build the GPU painter"
    return None


def build():
    """The painter's path, built if it is not in the cache; RuntimeError,
    saying why, where it does not build."""
    why = why_not()
    if why:
        raise RuntimeError(why)
    if os.environ.get("BITWALL_PAINT_EXE"):
        return os.environ["BITWALL_PAINT_EXE"]
    h = hashlib.sha256(platform().encode())
    for name in sorted(os.listdir(PI_DIR)):
        if name == "Makefile" or name.endswith((".c", ".h")):
            with open(os.path.join(PI_DIR, name), "rb") as fh:
                h.update(name.encode() + b"\0" + fh.read())
    exe = os.path.join(CACHE, f"paint-gpu-{h.hexdigest()[:16]}", "bitwall-paint")
    if os.access(exe, os.X_OK):
        return exe
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    building = f"{exe}.{os.getpid()}"
    built = subprocess.run(["make", "-s", "-B", "-C", PI_DIR, f"PLATFORM={platform()}",
                            f"OUT={building}"], capture_output=True, text=True)
    if built.returncode != 0:
        raise RuntimeError("the GPU painter did not build:\n"
                           + "\n".join(built.stderr.splitlines()[:8]))
    os.replace(building, exe)
    return exe


def wait_for(test, timeout, step=0.01):
    """Poll `test` until it is true or the time is up; its last answer."""
    until = time.monotonic() + timeout
    while True:
        got = test()
        if got or time.monotonic() >= until:
            return got
        time.sleep(step)


class Painter:
    """One painter with no screen and one wall, `0`, on a glass `w` by `h`
    at font `font`, in a run directory of its own that goes with it."""

    def __init__(self, glass, font=14, env=None):
        self.tmp = tempfile.mkdtemp(prefix="bwp-")
        # On Windows the walls are pipes, under a run directory of the
        # painter's own in the pipe namespace.
        self.run = (local.run_dir() + "-" + os.path.basename(self.tmp).lower()
                    if WINDOWS else self.tmp)
        self.wall = posixpath.join(self.run, "bitwall", "0")
        self.err = open(os.path.join(self.tmp, "err"), "w+")
        argv = [build(), "--font", str(font), "--font-file", FONT_FILE,
                "--glass", f"{glass[0]}x{glass[1]}", "--run", self.run, "--walls", "1"]
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=self.err,
                                     env=dict(os.environ, **(env or {})))
        door = posixpath.join(self.wall, "door.in")
        if not wait_for(lambda: self._door_open(door), 10):
            self.stop()
            raise RuntimeError(f"the painter never opened its door: {self.said()}")

    @staticmethod
    def _door_open(door):
        if WINDOWS:
            return local.door_live(door)
        try:
            os.close(os.open(door, os.O_WRONLY | os.O_NONBLOCK))
            return True
        except OSError:
            return False

    def said(self):
        self.err.flush()
        self.err.seek(0)
        return self.err.read()

    def session(self, name, role="layer", **opts):
        return Session(self, name, role, **opts)

    def stop(self):
        if self.proc.poll() is None:
            if WINDOWS:
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.err.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


_BLOB = re.compile(rb"^(picture|mirror) ")


def there(path):
    """Whether the fifo or pipe at `path` is there. A pipe is looked for
    in the listing, never by its path, which would take its instance."""
    if WINDOWS:
        return local._pipe_name(path) in (p.lower() for p in local._pipes())
    return os.path.exists(path)


def writer(path):
    """A descriptor writing the fifo or pipe at `path`, which waits while
    it is full."""
    if WINDOWS:
        return local.open_write(path, 2.0)
    return os.open(path, os.O_WRONLY)


class Reader:
    """The fifo or pipe at `path`, read without waiting: `read` is what has
    come, BlockingIOError when nothing has, and b"" at its end. A pipe's
    descriptor blocks, so on Windows a thread reads it into a queue."""

    def __init__(self, path, wait=5.0):
        self._t = None
        if not WINDOWS:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            return
        self.fd = local.open_read(path, wait)
        self._got, self._end, self._lock = [], False, threading.Lock()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            while True:
                data = os.read(self.fd, 1 << 22)
                if not data:
                    break
                with self._lock:
                    self._got.append(data)
        except OSError:
            pass
        finally:
            self._end = True
            with contextlib.suppress(OSError):
                os.close(self.fd)

    def read(self, n):
        if self._t is None:
            return os.read(self.fd, n)
        with self._lock:
            if self._got:
                data = b"".join(self._got)
                self._got = [data[n:]] if len(data) > n else []
                return data[:n]
        if self._end:
            return b""
        raise BlockingIOError(errno.EAGAIN, "nothing yet")

    def close(self):
        if self._t is None:
            os.close(self.fd)


class Session:
    """A session as its caller has it. `heard` is every line `out` said; a
    picture is taken off the stream into `pictures`, its words and bytes.
    The wall's `ping` is answered as it is read."""

    def __init__(self, painter, name, role, **opts):
        self.dir = posixpath.join(painter.wall, "s", name)
        self.heard, self.pictures, self.buf, self.writer = [], [], b"", None
        words = " ".join(["open", name, role] + [f"{k}={v}" for k, v in opts.items()])
        local.knock(posixpath.join(painter.wall, "door.in"), words)
        out = posixpath.join(self.dir, "out")
        if not wait_for(lambda: there(out), 5):
            raise RuntimeError(f"{name}: no out")
        self.fd = Reader(out)
        if not self.wait(lambda h: any(x.startswith(f"opened {name}") for x in h), 5):
            raise RuntimeError(f"{name}: not opened: {self.heard}")

    def write(self, data):
        if self.writer is None:
            self.writer = writer(posixpath.join(self.dir, "in"))
        data = data.encode() if isinstance(data, str) else data
        while data:
            try:
                data = data[os.write(self.writer, data):]
            except BlockingIOError:
                time.sleep(0.005)

    def command(self, text):
        self.write(APC + "bitwall " + text + ST)

    def pump(self):
        while True:
            try:
                data = self.fd.read(1 << 20)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    break
                raise
            if not data:
                break
            self.buf += data
        while b"\n" in self.buf:
            line, rest = self.buf.split(b"\n", 1)
            words = line.decode("utf-8", "replace").split()
            size = next((int(w) for w in reversed(words) if w.isdigit()), None)
            if _BLOB.match(line) and size is not None:
                if len(rest) < size:
                    break
                self.pictures.append((words, rest[:size]))
                self.buf = rest[size:]
                continue
            self.buf = rest
            self.heard.append(" ".join(words))
            if words == ["ping"]:
                with contextlib.suppress(OSError):
                    self.command("pong")

    def wait(self, test, timeout=3.0):
        return wait_for(lambda: (self.pump(), test(self.heard))[1], timeout)

    def picture(self, target="glass", timeout=5.0):
        """A picture of `target`, its words and its bytes."""
        self.pictures = []
        self.command(f"picture {target}")
        if not wait_for(lambda: (self.pump(), self.pictures)[1], timeout):
            raise AssertionError(f"no picture of {target}; heard {self.heard[-4:]}")
        return self.pictures[0]

    def close(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
        if self.fd is not None:
            self.fd.close()
            self.fd = None
