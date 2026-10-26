"""bitwall — the sender's API.

A sender attaches to a wall, tells it what font size to render at, and
hands it grids. That is the whole surface:

    import bitwall

    w = bitwall.attach("display.local")     # any ssh-reachable wall
    w.set_font_size(18)
    w.set_source(bitwall.FunctionSource(my_render))  # or CommandSource("btop")
    w.blank(); w.unblank()
    w.detach()

**One session, many sources.** attach() opens the wall's base at its door
and keeps that session for the life of the Wall, opening it again on its
own if the wall goes away and comes back. set_source() swaps what is
feeding it without the wall noticing: no reconnect, no silence, no
disconnected page. That matters because the alternative -- restarting the
pipeline per switch -- leaves the wall blank while a new ssh connects, and
that gap is the one visible flash the demo had.

**The receiver owns its area.** We request a font size; the area that
produces is a fact the receiver publishes, and it may change without us
asking -- someone drags a window. A watcher follows whatever it reports and
the render loop adopts it.

**Sources are interchangeable.** A terminal program is one content source,
not the architecture. A python profile computes cells directly with no pty and no
subprocess, and composites through exactly the same path, so nothing
downstream can tell them apart.

What bitwall is NOT: it does not draw anything for you beyond the border.
bitwall defines the cell format; what goes on the grid is the
sender's business.
"""
#: The release, MAJOR.MINOR.PATCH. MAJOR is the protocol's version, the
#: `v1` a wall says in `hello`, so a sender pinned to a major release
#: speaks the protocol its walls speak; tests/bitwall-tests holds the two
#: together. The one place the version is written: pyproject.toml reads it.
__version__ = "1.4.0"

import collections
import contextlib
import itertools
import os
import re
import select
import sys
import threading
import time

from . import protocol                                       # noqa: F401
from . import moving
from . import session
from . import video                                          # noqa: F401
from .capture import CaptureError, SessionInfo               # noqa: F401
from . import capture as _capture
from .cells import BLANK, cell                                # noqa: F401
from .stream import (compose, serialise, DEPTHS,              # noqa: F401
                     TmuxSource, CommandSource, FunctionSource)
from .session import (Link, Session, Controller, LOCAL, RUN_HERE,  # noqa: F401
                      WALL_TOKEN, address, wall_run, session_name)

__all__ = ["attach", "Wall", "TmuxSource", "CommandSource",
           "FunctionSource", "cell", "BLANK",
           "compose", "serialise",
           "Controller", "Session", "Link", "address", "wall_run",
           "session_name", "layer", "CellsLayer", "CaptureError", "SessionInfo"]


_TRACE = os.environ.get("BITWALL_TRACE")


def _trace(msg):
    if _TRACE:
        sys.stderr.write("%.3f %s\n" % (time.monotonic(), msg))
        sys.stderr.flush()


# --------------------------------------------------- what a receiver says
#
# Everything a receiver publishes is read here, and none of it is trusted.
# A receiver is a screen on a wall: it may be a browser tab anyone on the
# network can open, a Pi in another room, a page left running for a month.
# The sender is the machine with the ssh keys and the files on it, so the
# run directory is a boundary and everything crossing it inbound is taken
# as hostile until it has been checked.
#
# The rule throughout is to return a value of the shape the caller expects
# or nothing at all -- never the receiver's own object, never a string of
# its length, never a number of its choosing.

#: The largest grid we will believe. A frame is built by allocating every
#: cell, twice over to diff against the last one, so a receiver claiming
#: 99999x99999 is asking the sender for ten billion of them -- which is
#: not a rendering bug, it is the receiver choosing how much memory the
#: sender uses.
#:
#: The cell count is the bound that matters, because a wall may be wide
#: and short or narrow and tall and neither is suspicious.
#:
#: Sized for a 5K screen filled at the smallest font a receiver will
#: render: a Mac painter full screen on 5120x2880 at a 3x6 cell is
#: 1706x480, or 819,000 cells, and that is the densest wall there is --
#: half blocks make it a 1706x960 picture. A reading font on the same
#: screen is a tenth of that, so this is only reached deliberately.
MAX_COLS = 2000
MAX_ROWS = 1000
MAX_CELLS = 1000000

#: The largest font size we will ask for, and the most viewers we will
#: believe are watching.
MAX_FONT = 400
MAX_WATCHING = 10000

#: What a receiver will take as metadata: the key's shape, and how long a
#: value may be. Both are the receiver's rules; holding to them here means
#: a sender hears about a mistake where it made it.
META_KEY = ("abcdefghijklmnopqrstuvwxyz0123456789._-", 32)
META_VALUE = 256


def _plain(text):
    """Text a command can carry: printable, one line, nothing that could
    end the command string it travels in."""
    return "".join(c if c.isprintable() else " " for c in str(text)).strip()


def _meta_key(key):
    """The key, or None where a receiver would refuse it."""
    ok, most = META_KEY
    return key if 1 <= len(key) <= most and all(c in ok for c in key) else None

#: What a receiver may claim it can do. A name outside this set is
#: dropped rather than passed on: a sender that iterates capabilities
#: should never see a key it has not heard of.
CAP_NAMES = ("touch", "keyboard", "buttons")

#: What a press may be, and what may modify it.
KINDS = ("tap", "key", "button")
#: The fastest a receiver may sensibly ask to be drawn for. High enough
#: that no real screen is refused, low enough that a bad number cannot
#: turn the render loop into a spin.
MAX_RATE = 240

#: How long to wait before looking again when a frame is already due.
#: Small enough not to be part of the frame rate, large enough that a
#: wall with nothing to show is not a spinning loop.
POLL = 0.002
#: The capability a kind of press belongs to. A receiver reports presses
#: and a sender asks for capabilities, so this is what joins the two.
OF_KIND = {"tap": "touch", "key": "keyboard", "button": "buttons"}
MODS = ("ctrl", "alt", "shift", "meta")

#: Key names a receiver may report. Anything else has to be a single
#: printable character.
#:
#: This is the narrowest part of the boundary and the one that matters
#: most. A sender turns a name into an action -- `tmux send-keys`, a menu
#: choice, a game control -- so an unchecked name is the receiver choosing
#: what the sender does. A long string here is a command typed into
#: somebody's terminal.
KEY_NAMES = frozenset(
    ("Return", "BackSpace", "Tab", "Escape", "space", "Delete", "Insert",
     "Up", "Down", "Left", "Right", "Home", "End", "PageUp", "PageDown")
    + tuple("F%d" % n for n in range(1, 13)))


def _int(value, low, high):
    """An integer in range, or None. Booleans are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _pair(value, low, high):
    """A two-integer list, as a tuple, or None."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    a, b = _int(value[0], low, high), _int(value[1], low, high)
    return None if a is None or b is None else (a, b)


def _is_key(name):
    """A key a receiver is allowed to have pressed.

    One printable character, or one of the names every receiver agrees to
    spell the same way. `str.isprintable` excludes the control characters,
    which is what stops a receiver from reporting an escape sequence as
    though it were a keystroke.
    """
    if not isinstance(name, str):
        return False
    if name in KEY_NAMES:
        return True
    return len(name) == 1 and name.isprintable()


def clean_caps(raw):
    """What a receiver says it can do, reduced to what we understand."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name in CAP_NAMES:
        value = raw.get(name)
        if name == "buttons":
            count = _int(value, 0, 64)
            if count is not None:
                out[name] = count
        elif isinstance(value, bool):
            out[name] = value
    can = raw.get("can")
    if isinstance(can, dict):
        out["can"] = {name: can[name] for name in CAP_NAMES
                      if isinstance(can.get(name), bool)}
    return out


def _as_press(said):
    """An `input` announcement as the press dict a sender expects.

        input key key=Return mods=ctrl
        input tap cell=3,4 area=174,42
    """
    kind = said.args[0] if said.args else None
    out = {"kind": kind}
    if kind == "key":
        out["key"] = said.get("key")
        mods = said.get("mods")
        if mods:
            out["mods"] = mods.split(",")
    elif kind == "tap":
        for name in ("cell", "area"):
            pair = [protocol.number(p)
                    for p in (said.get(name) or "").split(",")]
            if len(pair) == 2 and all(p is not None for p in pair):
                out[name] = pair
    elif kind == "button":
        out["n"] = said.int("n", 0, 64)
    return out


def clean_input(raw):
    """One press, rebuilt from scratch, or None.

    Rebuilt rather than filtered: what comes back is a dict this function
    made, holding only values it checked, so a sender cannot be handed an
    extra field it did not expect or a type it will not survive.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        return None

    if kind == "key":
        key = raw.get("key")
        if not _is_key(key):
            return None
        out = {"kind": "key", "key": key}
        mods = raw.get("mods")
        if isinstance(mods, (list, tuple)):
            named = sorted({m for m in mods if m in MODS})
            if named:
                out["mods"] = named
        return out

    if kind == "tap":
        cell_at = _pair(raw.get("cell"), 0, max(MAX_COLS, MAX_ROWS))
        area = _pair(raw.get("area"), 1, max(MAX_COLS, MAX_ROWS))
        if cell_at is None or area is None:
            return None
        # A press outside the grid it was taken against is not a press.
        if cell_at[0] >= area[0] or cell_at[1] >= area[1]:
            return None
        return {"kind": "tap", "cell": list(cell_at), "area": list(area)}

    count = _int(raw.get("n"), 0, 64)
    return None if count is None else {"kind": "button", "n": count}


# -------------------------------------------------------------------- wall

#: A rendered message, wrapped for the frame stream. Kept here so that
#: what is logged and what is sent are the same string.
APC_WRAP = protocol.APC + protocol.MARK + " %s" + protocol.ST

#: A controller's instance id: lower case letters, digits and `-`.
INSTANCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

#: How the base is asked for at the door: layer 0, carrying cells,
#: displacing whoever holds it.
BASE = {"z": 0, "type": "cells", "take": 1}

#: How long a session may take to come to anything before it is given up
#: on and asked for again: the door's wait, and an ssh connection's.
SETTLE = session.OPEN_WAIT + 30.0

#: Which Wall of this process each is, so two in one process never ask
#: for the same session name.
_walls = itertools.count(1)



#: Pass-through bytes held while there is no session to write them to, at
#: most: the newest kept, as a picture is sent whole and the last is the one
#: that counts.
THROUGH_HELD = 32 << 20

class Wall:
    """A live connection to one wall: the base session on it."""

    def __init__(self, target, user=None,
                 resync=30.0, min_frame=0.05, run=None, compress=None):
        #: How the wall is reached: its host, its directory there, and
        #: whether that is through ssh.
        self._link = Link(target, user, run)
        #: Whether its ssh compresses the stream: a cell stream does,
        #: unless told otherwise.
        self._compress = compress
        self._seq = next(_walls)
        #: What the wall was asked for by, `host` or `host:N`, for saying
        #: which wall this is.
        self.target = target
        #: How long before a full repaint goes out unasked. A backstop,
        #: not the mechanism: a receiver that needs one asks, and this
        #: only catches the case where something went wrong quietly. Long,
        #: because every wall pays a full frame each period for ever, to
        #: say nothing new: on a dashboard a two second period would be
        #: 83% of everything sent.
        self.resync = resync
        #: Whether the wall has opened our session, and what was asked of
        #: it before it had, waiting to be said once it has.
        self._talks = False
        self._pending = []
        self._early_lock = threading.Lock()
        #: Putting a lost wall right, so it is done once at a time.
        self._recovering = False
        #: The session that is the base, while we hold one; how many have
        #: been asked for, which names the next; whether the last has come
        #: to anything yet, for the thread waiting on it; and why we are
        #: staying off the wall, once we are.
        self._session = None
        self._opens = 0
        self._settled = threading.Event()
        self._displaced_by = False
        self._refused = None
        self._stopped = None
        #: The last thing the receiver said of each kind, and the presses
        #: it has reported and we have not yet handed on.
        self._said = {}
        self._heard = collections.deque(maxlen=64)
        #: Set once a receiver has been given its chance to describe its
        #: screen and did not, so the waiting happens once rather than on
        #: every question.
        self._screen_late = False
        #: The input this sender has asked for, by capability name. A
        #: sender that has asked for nothing is not in the business of
        #: presses, whatever a receiver chooses to report.
        self._wanted = {}
        #: What has crossed the channel lately, newest last, as
        #: (when, "out"|"in", text). Kept so a sender can show the
        #: conversation rather than only its effects -- which is the only
        #: way to watch a protocol that succeeds silently.
        self._log = collections.deque(maxlen=200)
        self.min_frame = min_frame
        #: What this sender asked for, kept so a screen's rate can only
        #: slow it down. A sender that wants 4fps on a 60fps screen still
        #: gets 4: the announcement is a ceiling, not a target.
        self._asked_frame = min_frame

        self.cols, self.rows = 120, 30
        #: What this sender says about itself, and which of it is kept off
        #: the glass. Held so it can be said again whenever we attach or
        #: the receiver asks.
        self._meta = {}
        self._hidden = set()
        self._instance = None     # the id a controller started us under
        self._src = None
        self._retire = []            # sources waiting to be closed
        self._prev = None
        self._force = False
        self._blanked = False
        self._hold = 0               # >0 while a transition is in progress
        self._gen = 0                # bumped whenever _prev is invalidated
        self._watching = None        # viewers, when the receiver counts them
        self.bytes_sent = 0          # so "we stopped sending" is measurable
        self.frames_sent = 0
        self._stop = threading.Event()
        self._lock = threading.RLock()
        #: Held around every whole write to the stream, so a command can
        #: never land inside a frame's payload.
        self._wlock = threading.Lock()
        #: The one frame waiting for the writer, or None. A newer frame
        #: replaces it rather than queueing behind it: a frame that has
        #: been superseded is only latency.
        self._next = None
        #: The grid of the frame the writer last took.
        self._taken = None
        #: Bytes for the receiver's terminal, in the order given, written
        #: ahead of the next frame and never dropped with one.
        self._through = []
        #: Called after each frame the source composed, and before leaving:
        #: what `letters` uses to take down pictures no longer drawn.
        self._after_frame = []
        self._when_lost = []
        self._before_leaving = []
        self._ready = threading.Condition()
        #: Set when the wall is not ours any more: another sender took it,
        #: or it would not have us.
        self._broken = threading.Event()
        self._threads = []
        self._font = None
        #: The last size asked for, a font or a grid, said again to a
        #: receiver that has just introduced itself.
        self._asked_size = None
        #: Pictures, mirrors and the sessions list, asked on the base.
        self._capture = _capture.Capture(self._send)

    # ------------------------------------------------------------ plumbing

    @property
    def host(self):
        return self._link.host

    @property
    def wall(self):
        return self._link.wall

    @property
    def local(self):
        return self._link.local

    @property
    def user(self):
        return self._link.user

    @property
    def run(self):
        """This wall's directory on its host, `<run>/bitwall/<wall>`."""
        return self._link.run

    @property
    def _cm(self):
        return self._link.cm

    def _argv(self, cmd, extra=()):
        """How to run a shell command on the wall's host."""
        return self._link.argv(cmd, extra)

    def _ssh(self, cmd, timeout=15):
        """Run a command on the wall's host."""
        return self._link.shell(cmd, timeout)

    @property
    def session(self):
        """The base session as we hold it, or None between one and the next."""
        return self._session

    def _inner(self):
        return (self.cols, self.rows)

    def _read_report(self):
        """The receiver's current (cols, rows, fontsize), as it last
        announced them, or None before it has.

        The size is stamped alongside the grid so we can tell a current
        report from our own request echoed back beside a stale grid.
        """
        said = self._said.get("area")
        area = self._area_from(said) if said is not None else None
        if not area:
            return None
        font = said.args[1] if len(said.args) > 1 else "?"
        return area[0], area[1], (font if _int(protocol.number(font), 1, MAX_FONT)
                                  else "?")

    def _read_area(self):
        rep = self._read_report()
        return (rep[0], rep[1]) if rep else None

    # --------------------------------------------------------------- api

    def attach(self):
        """Open the base: one session for the life of the Wall, made again
        whenever the wall is lost. Whoever held the base is displaced by
        it, so two senders never interleave two streams onto one screen."""
        # A sender a controller started carries its instance id, and the
        # wall carries it too: on the `open`, so `sessions` lists it, and
        # in the meta, off the glass. That is how the controller joins
        # what it started to the wall it is on, with no help from the
        # sender. Taken from the environment, not read: a process the
        # sender starts that opens a Wall of its own is not this instance.
        instance = os.environ.pop("BITWALL_INSTANCE", None)
        self._instance = instance if instance and INSTANCE.match(instance) else None
        if self._instance:
            moving.hold(self, os.environ.get("BITWALL_RECORD"))
        self._open()
        self._threads = [
            threading.Thread(target=self._render_loop, daemon=True),
            threading.Thread(target=self._write_loop, daemon=True,
                             name="bitwall-writer"),
        ]
        for t in self._threads:
            t.start()
        self._say_instance()
        return self

    def _say_instance(self):
        if self._instance:
            self.meta(instance=self._instance).meta_hide("instance")

    def _name(self):
        """The next session's name: `<program>-<pid>`, with which Wall of
        this process this is after it when there is more than one, and
        `r<k>` for the k-th time the base is asked for again, so a fresh
        session never shares a name with one of ours the wall has not yet
        let go of, and never reads that one's `out` as its own."""
        parts = ([str(self._seq)] if self._seq > 1 else []) \
            + ([f"r{self._opens}"] if self._opens else [])
        return session.session_name(tag="-".join(parts) or None)

    def _make_session(self):
        """The base as it will be asked for, not yet opened."""
        opts = {**BASE, "instance": self._instance} if self._instance else BASE
        return Session(self._link, self._name(), "layer", opts,
                       heard=self._heard_from, ended=self._ended, compress=self._compress)

    def _open(self):
        """Ask at the door for the base and listen on the session that
        answers; the writer follows `opened`. Made again, whole, whenever
        the wall is lost."""
        self._settled.clear()
        self._session = self._make_session()
        self._opens += 1
        self._session.open()

    # ---------------------------------------------------- losing the wall

    def _heard_from(self, sess, said):
        """A line from the session's `out`, if it is still ours."""
        if sess is not self._session:
            return
        try:
            self._capture.feed(said)
            if isinstance(said, protocol.Message):
                self._told(said)
        except Exception:
            pass                      # one bad message is not a wall down

    def _ended(self, sess):
        """The session's `out` came to an end, or its writer did."""
        if sess is not self._session or self._stop.is_set():
            return
        if sess.state == "absent":
            why = "no wall is there"
        elif sess.state == "dropped":
            why = "the door did not answer"
        elif sess.state == "refused":
            self._refused = sess.refused
            why = f"it refused the base: {sess.refused}"
        elif sess.state == "closed":
            why = f"it closed the session ({sess.closed})"
        else:
            why = "its session ended without a word"
        self._settled.set()
        self._lost(why)

    def _lost(self, why):
        """The wall has gone from under us, or sent us away. Put right
        once, on a thread of its own, however many noticed."""
        with self._early_lock:
            if self._recovering or self._stop.is_set():
                return
            self._recovering = True
        threading.Thread(target=self._recover, args=(why,), daemon=True).start()

    def _close_pipes(self):
        sess, self._session = self._session, None
        if sess is not None:
            sess.kill()

    def _stay_out(self, why):
        """Stop for good: the wall is somebody else's, or will not have us."""
        print(f"bitwall: {self.target}: {why}; stopping",
              file=sys.stderr, flush=True)
        self._stopped = why
        with self._wlock:
            self._close_pipes()
        self._broken.set()
        with self._ready:
            self._ready.notify_all()

    @property
    def stopped(self):
        """Why this sender has left the wall for good and stopped drawing,
        or None while it is on the wall or waiting to be: another sender
        displaced it, or the wall refused it the base."""
        return self._stopped

    def _sent_away(self):
        """Whether the wall's last word to us was to stay away."""
        if self._displaced_by:
            self._stay_out("another sender has this wall")
        elif self._refused:
            self._stay_out(f"the wall refused the base: {self._refused}")
        return self._stopped is not None

    def _invalidate(self):
        """Nothing the wall holds of ours can be diffed against."""
        with self._lock:
            self._prev = None
            self._force = True
            self._gen += 1

    def _recover(self, why):
        """Open the base again once the wall is back, and paint it whole:
        a receiver that restarted holds nothing of ours. A sender another
        has displaced stays out rather than fight back, and so does one
        the wall refused."""
        try:
            # The wall's last word, `closed` and why, is on `out` ahead
            # of the end of it; wait for the end, so it is heard whole
            # before the session is judged, though not on a wall that
            # never ends it.
            sess = self._session
            if sess is not None:
                sess.eof.wait(3.0)
            if self._sent_away():
                return
            print(f"bitwall: {self.target}: {why}; attaching again when it is back",
                  file=sys.stderr, flush=True)
            for fn in list(self._when_lost):
                try:
                    fn()
                except Exception as e:                          # noqa: BLE001
                    print(f"bitwall: {self.target}: when_lost: {e}",
                          file=sys.stderr, flush=True)
            with self._wlock:
                self._close_pipes()
            with self._early_lock:
                self._talks = False
            # What the last receiver said about who was watching is no
            # evidence about the next one.
            self._said.pop("watching", None)
            self._watching = None
            wait = 0.25
            while True:
                with self._wlock:
                    if self._stop.is_set():
                        return
                    # Before the door is knocked at, so the first frame the
                    # new session is handed is whole, never a diff against
                    # what the old one had.
                    self._invalidate()
                    self._open()
                self._settled.wait(SETTLE)
                # Judged and acted on under the write lock, as one thing:
                # a session found open is kept, and an `opened` that lands
                # after this has looked is on a session already let go of,
                # which the next turn of the loop replaces.
                with self._wlock:
                    sess = self._session
                    if self._stop.is_set():
                        self._close_pipes()
                        return
                    if sess is not None and sess.state == "open":
                        break
                    self._close_pipes()
                if self._sent_away():
                    return
                self._stop.wait(wait)
                wait = min(wait * 2, 5.0)
            _trace("attached again")
            print(f"bitwall: {self.target}: attached again",
                  file=sys.stderr, flush=True)
        finally:
            with self._early_lock:
                self._recovering = False

    @property
    def talking(self):
        """Whether the wall has opened our session."""
        return self._talks

    def ask(self):
        """Ask the wall to say everything it knows.

        Rarely needed -- it announces when things change, and is asked
        as soon as the session opens. Useful when something has gone
        quiet and you want to know whether it is still there.
        """
        self._say("report")
        return self

    def traffic(self, since=0.0):
        """What has crossed the channel, as (when, direction, text).

        A sender showing this is showing the protocol working, which is
        otherwise invisible: nothing acknowledges anything, so a request
        that went and a request that was dropped look identical from
        here until the announcement that follows it.
        """
        return [row for row in list(self._log) if row[0] > since]

    def _told(self, said):
        """One thing the receiver announced."""
        self._log.append((time.time(), "in",
                          protocol.render(said.name, *said.args,
                                          **said.opts)))
        if said.name == "hello":
            _trace("hello %s" % protocol.version_of(said))
            return

        if said.name == "opened":
            # The session is ours, and the wall can be told things. The
            # report goes first, which is what connects the session; then
            # what was asked before it opened, in the order it was asked;
            # then who we are and what we want, since a wall that has just
            # opened a session knows nothing about us, whether it is new
            # or was restarted underneath.
            with self._early_lock:
                self._talks = True
                pending, self._pending = self._pending, []
            self._log.append((time.time(), "out", "report"))
            self._write(protocol.command("report"), counted=False)
            for text in pending:
                self._log.append((time.time(), "out", text))
                self._write(APC_WRAP % text, counted=False)
            self._watching = None
            self._resay_meta()
            self._resay_asked()
            self._capture.resay()
            self._settled.set()
            _trace("opened %s" % " ".join(said.args))
            return

        if said.name == "closed":
            # The wall has ended our session and says why. Displaced is
            # the one reason to stay away: another sender has the wall.
            if said.args[1:2] == ("displaced",):
                self._displaced_by = True
            _trace("closed %s" % " ".join(said.args))
            return

        if said.name == "screen":
            # What the screen can show, as against how many cells it has.
            # The rate is applied here rather than left to each sender: a
            # sender asking for more frames than a screen can take is
            # spending the wire and, on electronic paper, the panel.
            self._said["screen"] = said
            rate = said.int("rate", 1, MAX_RATE)
            if rate:
                self.min_frame = max(self._asked_frame, 1.0 / rate)
            _trace("screen %s" % protocol.render(said.name, *said.args,
                                                 **said.opts))
            return

        if said.name in ("area", "caps", "accepts", "watching"):
            self._said[said.name] = said
            if said.name == "area":
                self._adopt(self._area_from(said))
            elif said.name == "watching":
                # The render loop reads this directly; someone coming
                # back to a wall we stopped drawing gets a whole picture.
                was, self._watching = self._watching, self.watching()
                if was == 0 and self._watching != 0:
                    with self._lock:
                        self._prev = None
                        self._force = True
                        self._gen += 1
                    _trace("watching resumed")
            return

        if said.name == "ping":
            # A wall that has had nothing new from us wants to know the
            # difference between a quiet sender and a gone one, whether
            # or not anybody is looking at it.
            self._say("pong")
            return

        if said.name == "repaint":
            with self._lock:
                self._prev = None
                self._force = True
            _trace("repaint asked")
            return

        if said.name == "input":
            self._heard.append(said)

    @staticmethod
    def _area_from(said):
        """(cols, rows) out of an `area 174x42 14` announcement."""
        if not said.args or "x" not in said.args[0]:
            return None
        c, _, r = said.args[0].partition("x")
        cols = _int(protocol.number(c), 1, MAX_COLS)
        rows = _int(protocol.number(r), 1, MAX_ROWS)
        if cols is None or rows is None or cols * rows > MAX_CELLS:
            return None
        return cols, rows

    def _say(self, name, *args, **opts):
        """Send a request down the frame stream. False if it cannot be said
        in words at all."""
        try:
            text = protocol.render(name, *args, **opts)
        except ValueError:
            return False
        return self._send(text)

    def _send(self, text):
        """A request, or held until the receiver has said hello: one asked
        in the moment before it introduces itself -- a font size, as a
        sender starts -- goes as soon as it has."""
        with self._early_lock:
            if not self._talks:
                self._pending.append(text)
                return True
        self._log.append((time.time(), "out", text))
        return self._write(APC_WRAP % text, counted=False)

    def set_font_size(self, n, wait=True, timeout=8.0):
        """Request a font size.

        The area it produces is the receiver's to report; we never assume
        it. With wait=True this blocks until the receiver confirms it is
        rendering at that size, so a caller can then show() without
        painting a source at the previous area for a moment. If the
        receiver is already there, it returns at once.
        """
        # Our own number, checked before it becomes part of a command
        # line. A size is a small integer and nothing else.
        size = _int(n if isinstance(n, int) else None, 1, MAX_FONT)
        if size is None:
            raise ValueError(f"font size must be 1..{MAX_FONT}, not {n!r}")
        self._font = str(size)
        self._asked_size = ("font", size)
        self._say("font", size)
        if not wait:
            return self
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rep = self._read_report()
            if rep and rep[2] == self._font:
                with self._lock:
                    if (rep[0], rep[1]) != (self.cols, self.rows):
                        self.cols, self.rows = rep[0], rep[1]
                        self._prev = None
                        self._force = True
                        self._gen += 1
                        _trace("font  area=%dx%d gen=%d" % (rep[0], rep[1], self._gen))
                return self
            time.sleep(0.15)
        return self

    @contextlib.contextmanager
    def transition(self):
        """Settle a new area and a new source without painting in between.

        set_font_size settles the area and set_source settles the content.
        Between the two the wall holds a source built for the other area,
        and painting it there puts one wrong frame on the screen. Inside a
        transition the render loop stands still; one full repaint goes out
        when both have landed.

        It also makes the order safe either way round. A source that
        resizes what it is reading, as a tmux source does, only ever sees
        the area it was given here.
        """
        with self._lock:
            self._hold += 1
            _trace("hold  taken area=%dx%d" % (self.cols, self.rows))
        try:
            yield self
        finally:
            with self._lock:
                self._hold -= 1
                if self._hold == 0:
                    self._prev = None
                    self._force = True
                    self._gen += 1
                    _trace("hold  released area=%dx%d gen=%d"
                           % (self.cols, self.rows, self._gen))

    def watching(self):
        """How many are looking, or None if the receiver does not count.

        A screen on a wall has nobody to count, so most receivers publish
        nothing and this is None -- which means assume someone is there. A
        window or a browser tab knows, and one that says nobody is looking
        is worth believing.
        """
        said = self._said.get("watching")
        if said is None:
            return None
        # `watching 1` and `watching n=1` say the same thing, and the bare
        # form is bounded like the named one.
        bare = _int(protocol.number(said.args[0]) if said.args else None,
                    0, MAX_WATCHING)
        return said.int("n", 0, MAX_WATCHING, default=bare)

    @property
    def depth(self):
        """The colour to send, which is the screen's answer unless a
        sender has overridden it with `use_depth`.

        Read on the way out of every frame, so a sender gets the reduction
        without doing anything. It is here as well because a sender that
        knows can do better than be reduced -- a gradient quantised to two
        colours is banding, where a design chosen for two colours is a
        picture.
        """
        want = getattr(self, "_depth_want", None)
        if want:
            return want
        said = self._said.get("screen")
        if said is None:
            return "true"
        told = said.get("colour") or (said.args[0] if said.args else None)
        return told if told in DEPTHS else "true"

    def use_depth(self, depth):
        """Send in this colour whatever the screen says it can take.

        The screen's own answer is the right one and this overrides it,
        so it is for the case the announcement cannot express: a screen
        that could show every colour but is on the end of a link that
        cannot carry them. Reducing at the sender costs a frame nothing
        and saves about half the bytes of every cell that changes, which
        on a film is every cell.
        """
        if depth not in DEPTHS:
            raise ValueError(f"depth must be one of {DEPTHS}, not {depth!r}")
        with self._lock:
            self._depth_want = depth
            self._prev = None       # everything has to be resent, reduced
            self._force = True
        return self

    def _screen(self, timeout=2.0):
        """The screen announcement, waiting for it if it is still coming.

        A sender asks what it may draw while it is starting, which is the
        same moment the receiver is still introducing itself. Answering
        from silence there would tell every sender it may do as it likes
        and be wrong a tenth of a second later -- and a sender only asks
        once, so it would be wrong for the whole run.

        Waited for rather than required: a receiver that never describes
        its screen is answered as silence after one short wait, and a
        sender is then as free as it was before any of this existed.
        """
        said = self._said.get("screen")
        if said is not None or self._screen_late:
            return said
        # Not conditioned on the receiver having said hello yet: a sender
        # asks this within a moment of attaching, and at that moment a
        # receiver that is about to introduce itself and one that never
        # will look exactly alike. Waiting is the only way to tell them
        # apart, and it is waited through once.
        until = time.time() + timeout
        while time.time() < until:
            time.sleep(0.02)
            said = self._said.get("screen")
            if said is not None:
                return said
        self._screen_late = True
        return None

    @property
    def glyphs(self):
        """Code points this screen can draw, as [(low, high)], or ().

        Empty means the receiver has not said, which is not the same as
        saying it can draw nothing: a sender told nothing may do as it
        likes, which is what every sender written before this did.
        """
        said = self._screen()
        return () if said is None else protocol.ranges(said.get("glyphs"))

    def can(self, *text):
        """Whether the screen can draw these characters.

        Unlike the colour and the rate, this one cannot be applied for a
        sender. A gradient reduced to two colours is still the picture,
        and a frame withheld is a frame nobody missed -- but there is no
        general way to turn a braille sparkline into an ASCII one, because
        only whoever composed it knows what it was a picture of. So this
        is a question a sender asks, and answers for itself.
        """
        return all(protocol.covers(self.glyphs, one) for one in text)

    def screen(self):
        """What the screen can show, as it announced it, or {}.

        Area says how many cells there are; this says what a cell can be.
        `cell`, where a screen says it, is a cell's size in pixels as
        (wide, tall).
        """
        said = self._screen()
        if said is None:
            return {}
        out = {"colour": self.depth}
        rate = said.int("rate", 1, MAX_RATE)
        if rate:
            out["rate"] = rate
        if self.glyphs:
            out["glyphs"] = self.glyphs
        cell = said.get("cell") or ""
        if "x" in cell:
            w, _, h = cell.partition("x")
            w = _int(protocol.number(w), 1, 4096)
            h = _int(protocol.number(h), 1, 4096)
            if w and h:
                out["cell"] = (w, h)
        return out

    def box(self, cells):
        """A rectangle of this wall's cells, (x, y, w, h), as the rectangle
        of the glass it covers, (x, y, w, h) in pixels: what a layer's
        `frame` takes as `at` and `size`, to put a picture over cells. The
        grid sits in the middle of the glass, as the receivers put it. From
        the base, which hears every change of area and cell; a layer is
        told neither. None until the wall has said its glass and its
        cell."""
        said = self._said.get("screen")
        if said is None:
            return None
        sizes = []
        for key in ("pixels", "cell"):
            w, _, h = str(said.get(key) or "").partition("x")
            w, h = _int(protocol.number(w), 1, 1 << 16), _int(protocol.number(h), 1, 1 << 16)
            if not (w and h):
                return None
            sizes.append((w, h))
        (gw, gh), (cw, ch) = sizes
        cols, rows = self.area()
        x, y, w, h = cells
        ox, oy = (gw - cols * cw) // 2, (gh - rows * ch) // 2
        return ox + x * cw, oy + y * ch, w * cw, h * ch

    @property
    def font_size(self):
        """The font size the receiver says its area is drawn at, or None
        where it has not said, or says 0 for a size of its own."""
        said = self._said.get("area")
        if said is None or len(said.args) < 2:
            return None
        return _int(protocol.number(said.args[1]), 1, 1000) or None

    def capabilities(self):
        """What the receiver says it can do, or {} if it says nothing.

        Published by the receiver the way its area is, rather than asked
        for: a sender reads what is there and a receiver that reports
        nothing is simply a screen.
        """
        told = self._said.get("accepts")
        if told is None:
            return {}
        # `accepts touch keyboard buttons=2` -- a name alone is on, and a
        # name with a number is how many.
        out = {name: True for name in told.args if name in CAP_NAMES}
        for name in CAP_NAMES:
            count = told.int(name, 0, 64)
            if count is not None:
                out[name] = count if name == "buttons" else bool(count)
        return clean_caps(out)

    def meta(self, **keys):
        """Say what this is, for whoever is looking at the wall.

        A receiver paints cells, and cells are not a name, so a sender says
        who it is and the receiver keeps it and hands it on: to a controller
        listing walls, to a page naming one, to a person at a shell
        wondering what that screen is. `name` and `description` are agreed
        keys, and `instance` is set here from `BITWALL_INSTANCE`; the rest
        is between whoever sets it and whoever reads it. An underscore in a keyword is a dot in the key,
        because `ops.pid` is not something python will let you type.

            w.meta(name="Galaga", description="an arcade shooter")
            w.meta(name=None)                 # that key goes

        Kept here as well as sent, and said again whenever a session opens,
        so a receiver that restarts is told again without the sender
        knowing. Nothing here is private: whoever can read the wall can
        read it.
        """
        for key, value in keys.items():
            key = _meta_key(key.replace("_", "."))
            if not key:
                raise ValueError(f"not a metadata key: {key!r}")
            if value is None:
                self._meta.pop(key, None)
                self._hidden.discard(key)
                self._say_meta("clear", key)
                continue
            text = _plain(value)[:META_VALUE]
            self._meta[key] = text
            self._say_meta("set", key, text)
        return self

    def meta_hide(self, *keys, hide=True):
        """Keep keys off the glass where a wall shows what it was told.

        Not a secret. A hidden key is still in every report, because that
        is where a controller reads it. A process id on
        the wall of a room is noise; in a controller it is how you stop the
        thing.
        """
        for key in keys:
            if hide:
                self._hidden.add(key)
            else:
                self._hidden.discard(key)
            self._say_meta("hide" if hide else "show", key)
        return self

    def meta_show(self, *keys):
        return self.meta_hide(*keys, hide=False)

    def meta_clear(self):
        """Forget the lot, here and at the receiver -- all but the
        instance id a controller gave, which the sender never set and is
        said again at once."""
        self._meta.clear()
        self._hidden.clear()
        self._say_meta("clear")
        self._say_instance()
        return self

    def _say_meta(self, verb, key=None, value=None):
        """One metadata statement, built rather than rendered: its value is
        the rest of the line, which `protocol.render` would refuse for the
        spaces a name may well have."""
        text = "meta " + verb
        if key:
            text += " " + key
        if value is not None:
            text += " " + value
        return self._send(text)

    def _resay_meta(self):
        """Everything we hold, again: a session has just opened on a wall
        that knows nothing about us."""
        for key, value in list(self._meta.items()):
            self._say_meta("set", key, value)
        for key in list(self._hidden):
            self._say_meta("hide", key)

    def _resay_asked(self):
        """What we have asked of the wall and still want, again: the size,
        the inputs, a blank. A receiver restarted underneath us starts
        from its own defaults and would otherwise stay there."""
        if self._asked_size:
            self._say(*self._asked_size)
        if self._wanted:
            self._say("want", **{k: int(v) for k, v in self._wanted.items()})
        if self._blanked:
            self._say("blank", 1)

    def grid(self, cols, rows, wait=True, timeout=8.0):
        """Ask for a number of cells, as set_font_size asks for a size.

        A game with a fixed board wants cells, not a font, and which font
        makes them is the receiver's business. A request like any other:
        what is true afterwards is the area there actually is, which is
        what this returns.
        """
        want_cols = _int(cols if isinstance(cols, int) else None, 1, MAX_COLS)
        want_rows = _int(rows if isinstance(rows, int) else None, 1, MAX_ROWS)
        if want_cols is None or want_rows is None:
            raise ValueError("a grid is cols and rows, both positive")
        self._asked_size = ("grid", f"{want_cols}x{want_rows}")
        self._say("grid", f"{want_cols}x{want_rows}")
        deadline = time.monotonic() + (timeout if wait else 0)
        while time.monotonic() < deadline:
            rep = self._read_report()
            if rep and (rep[0], rep[1]) == (want_cols, want_rows):
                break
            time.sleep(0.15)
        rep = self._read_report()
        if rep:
            with self._lock:
                if (rep[0], rep[1]) != (self.cols, self.rows):
                    self.cols, self.rows = rep[0], rep[1]
                    self._prev = None
                    self._force = True
                    self._gen += 1
                    _trace("grid  area=%dx%d gen=%d" % (rep[0], rep[1], self._gen))
        return self.cols, self.rows

    def want(self, **kinds):
        """Ask the receiver to turn inputs on or off: w.want(touch=True).

        A request, like a font size, and refused the same way. A browser
        that captures taps stops scrolling and pinching, and whether a page
        gives that up is the page's business, not ours. What is actually on
        comes back in capabilities().
        """
        asked = {k: bool(v) for k, v in kinds.items()}
        # What was asked for, so that a press of a kind nobody asked for
        # can be dropped here. A receiver decides what it will send; a
        # sender decides what it will act on, and neither takes the
        # other's word for it.
        self._wanted.update({k: v for k, v in asked.items()
                             if k in CAP_NAMES})
        self._say("want", **{k: int(v) for k, v in asked.items()})
        return self

    def input(self):
        """Whatever the receiver last reported, once.

        A tap, a keystroke, a button: bitwall carries it and has no opinion
        on what it means. The payload is the receiver's to define and the
        sender's to interpret.

        The kinds a receiver reports, by convention rather than rule, match
        what it says it can do:

            touch     {"kind": "tap", "cell": [x, y], "area": [cols, rows]}
            keyboard  {"kind": "key", "key": "a"}
            buttons   {"kind": "button", "n": 1}

        A tap carries the area it was taken against, because a press that
        crosses a resize would otherwise be applied to a grid that no
        longer exists.

        Each press is returned once, in the order they came.
        """
        while self._heard:
            said = self._heard.popleft()
            got = self._asked_for(clean_input(_as_press(said)))
            if got is not None:
                return got
        return None

    def _asked_for(self, press):
        """A press of a kind this sender asked for, or None.

        A receiver reports what it likes, and the reporting is honest --
        somebody did press a key. Whether this sender is in the business
        of keys is its own question, and one it answers rather than
        inferring from what arrives.
        """
        if press is None:
            return None
        return press if self._wanted.get(OF_KIND.get(press["kind"])) else None

    def area(self):
        with self._lock:
            return self.cols, self.rows

    def set_source(self, source):
        """Choose what feeds the transport. The connection is untouched.

        Takes a plain render(cols, rows) function, or a source object such
        as TmuxSource for content that needs arguments.

        A Wall has one source at a time and this replaces it. A method
        rather than an attribute because it does real work -- it starts the
        source and forces a repaint -- and it pairs with set_font_size.
        """
        if callable(source) and not hasattr(source, "cells"):
            source = FunctionSource(source)
        with self._lock:
            old = self._src
            ic, ir = self._inner()
            source.start(ic, ir)
            source.resize(ic, ir)
            self._src = source
            self._prev = None        # new content; nothing to diff against
            self._force = True
            self._gen += 1
            _trace("src   %s area=%dx%d gen=%d"
                   % (type(source).__name__, self.cols, self.rows, self._gen))
            # Do NOT close the old source here. The render loop may be
            # between taking a reference to it and calling pump(), and
            # closing its pty underneath gives EBADF. Hand it over and let
            # the render thread retire it when it is provably not in use.
            if old is not None:
                self._retire.append(old)
        return self

    def blank(self):
        """Black the receiver and stop sending.

        Rendering and shipping frames to a screen nobody can see is pure
        waste, and on a Pi 3B it is not free. The receiver escalates to
        standby on its own timer -- that escalation belongs to it, because
        it has to keep working if we vanish, which is exactly when you most
        want the screen off.
        """
        with self._lock:
            self._blanked = True
        self._say("blank", 1)
        return self

    def unblank(self):
        """Undo a blank this sender asked for, and repaint in full.

        A sender that never blanked sends nothing, so one that unblanks as
        it starts does not light a wall somebody else made dark, nor one
        dark for want of anybody there. A wall has one blank, though: if
        this sender and a controller both asked for it, lifting it lifts
        both. Full, not a diff: a receiver that was dark may hold nothing
        of what we last sent.
        """
        with self._lock:
            mine, self._blanked = self._blanked, False
            self._prev = None
            self._force = True
        if mine:
            self._say("blank", 0)
        return self

    @property
    def blanked(self):
        with self._lock:
            return self._blanked

    def sessions(self, timeout=None):
        """Every session on the wall, as `SessionInfo(name, role, z, type)`:
        the base is the one at z 0."""
        return self._capture.sessions(timeout)

    def picture(self, target, size=None, timeout=None, format=None, quality=None):
        """A picture of `target`, `glass` or a session's name as
        `sessions()` gives it: a `bitwall.capture.Picture`, whose cells a
        cells layer's can be read by. The glass is QOI, or a JPEG, its
        bytes untouched, with `format="jpeg"` and `quality` 1 to 100 (85
        unsaid) where the wall's `pictures` says `jpeg`; `Picture.format`
        says which came. Raises CaptureError if the wall will not."""
        return self._capture.picture(target, size, timeout, format, quality)

    def mirror(self, target, every=None, size=None, format=None, quality=None):
        """A mirror of `target`, kept current as the wall sends it: a
        `bitwall.capture.Mirror`, read as a picture is, with `version` and
        `changed` for noticing a change and `off()` to stop it. `size`,
        `format` and `quality` are the glass's, as for `picture`."""
        return self._capture.mirror(target, every, size, format, quality)

    def layer(self, **kw):
        """A layer above this wall's base, `layer()` on the same wall, saying
        the same instance as the base, so a controller knows the two as one
        sender's."""
        if self._instance:
            kw.setdefault("instance", self._instance)
        return layer(self.target, user=self.user, run=self.run, **kw)

    def after_frame(self, fn):
        """Call `fn()` after each frame the source composes -- not after a
        source says nothing changed, since then nothing was drawn -- and
        `leaving` before detaching. For something drawn beside the cells
        that has to know what a frame no longer draws: `letters` takes its
        pictures down this way. Once for each `fn`."""
        if fn not in self._after_frame:
            self._after_frame.append(fn)

    def when_lost(self, fn):
        """Call `fn()` when the wall goes and this sender will attach again
        once it is back. The wall that comes back may be a receiver
        restarted, holding nothing sent beside the cells, so `letters`
        forgets which pictures it has shown and sends them again. Called
        before the new session is asked for, so nothing is sent from it.
        Once for each `fn`."""
        if fn not in self._when_lost:
            self._when_lost.append(fn)

    def before_leaving(self, fn):
        """Call `fn()` as `detach` starts, while the session can still carry
        what it sends: `letters` takes its pictures off a terminal, which
        would otherwise keep them after the sender goes."""
        if fn not in self._before_leaving:
            self._before_leaving.append(fn)

    def retarget(self, target, run=None):
        """Move to the wall `target` names, keeping everything this sender
        holds: its source, meta, instance, font or grid, wants and blank.

        What was drawn beside the cells is taken off the old wall, which is
        told `detach` and is free at once; the new wall is attached to as a
        wall that came back is, told who we are and what we asked for, and
        painted whole. Everything the old wall said of itself -- its area,
        screen, caps, who is watching -- is forgotten, since the new one
        says its own. A controller moves a sender this way through
        `bitwall.moving`; a sender may call it itself."""
        if self._stopped is not None:
            raise RuntimeError(f"this sender has left {self.target} for good: {self._stopped}")
        link = Link(target, self._link.user, run)
        if (link.host, link.wall) == (self.host, self.wall) and run is None:
            return self
        was = self.target
        for fn in list(self._before_leaving):
            try:
                fn()
            except Exception:
                pass
        end = time.monotonic() + 0.3
        while self._through and time.monotonic() < end:
            time.sleep(0.01)
        with self._wlock:
            sess, self._session = self._session, None
            self._link, self.target = link, target
            self._said.clear()
            self._screen_late = False
            self._displaced_by, self._refused = False, None
            self.min_frame = self._asked_frame
            with self._early_lock:
                self._talks = False
        if sess is not None:
            sess.close(last=(APC_WRAP % "detach").encode(), linger=0.5)
        print(f"bitwall: moving from {was} to {target}", file=sys.stderr, flush=True)
        # A recovery under way opens its next session on the new wall.
        self._settled.set()
        self._lost(f"moved from {was}")
        return self

    def detach(self):
        """Leave the wall: stop drawing, close the sources, and say
        `detach` as the last thing on the session, after the last frame,
        so the wall is ready for the next sender at once rather than when
        a ping goes unanswered."""
        for fn in list(self._before_leaving):
            try:
                fn()
            except Exception:
                pass
        # What those sent goes out before the writer stops.
        end = time.monotonic() + 0.3
        while self._through and time.monotonic() < end:
            time.sleep(0.01)
        self._stop.set()
        with self._ready:
            self._ready.notify_all()
        for t in self._threads:
            t.join(timeout=0.3)
        with self._lock:
            retired, self._retire = self._retire, []
            if self._src:
                retired.append(self._src)
                self._src = None
        for old in retired:
            try:
                old.close()
            except Exception:
                pass
        # Under the write lock, so a recovery under way cannot open a
        # session after this one has gone and leave a base behind.
        with self._wlock:
            sess, self._session = self._session, None
        if sess is not None:
            # Bounded well inside the five seconds a controller allows a
            # sender to stop before it is killed.
            sess.close(last=(APC_WRAP % "detach").encode(), linger=0.5)

    # ------------------------------------------------------------- loops

    def _writable(self):
        """Whether there is a session to write to."""
        sess = self._session
        return sess is not None and sess.writable

    def _write(self, payload, counted=True):
        """Put one whole payload on the stream. False if the pipe is gone."""
        if not payload or not self._writable():
            return True
        data = payload.encode("utf-8")
        with self._wlock:
            return self._put(data, counted)

    def _put(self, data, counted):
        """The write itself, with the write lock already held. The
        session waits on a full pipe a little at a time, so a wall that
        has stopped reading holds a write only until the wall is detached,
        not for ever."""
        sess = self._session
        if sess is None:
            return False
        sent = sess.write(data, self._stop)
        if sent is None:
            return False
        self.bytes_sent += sent
        if counted:
            self.frames_sent += 1
        return True

    def _offer(self, frame):
        """Hand the writer a frame, replacing any it has not yet taken.

        A replaced frame's full repaint is carried by the one replacing
        it, or a repaint that was asked for would be lost with it; a diff
        made for the replacement is then no use, and the writer makes the
        full one.
        """
        with self._ready:
            if self._next is not None and self._next["full"] \
                    and not frame["full"]:
                frame["full"] = True
                frame["data"] = None
            self._next = frame
            self._ready.notify()

    def _base(self):
        """The grid the receiver will hold when the writer next takes a
        frame: the last one it took, which is written or being written."""
        with self._ready:
            return self._taken

    def _stale(self, f):
        """Whether what a frame was built for is no longer true."""
        return (self._gen != f["gen"] or self._hold or self._blanked
                or self._src is not f["src"])

    def passthrough(self, data):
        """Bytes for the terminal the receiver draws on, untouched.

        bitwall carries them and reads none of them: kitty graphics, say,
        which a shell receiver in Ghostty cats straight to a terminal that
        draws them. They go out in the order given, between frames, ahead
        of the next one, and are never dropped as a superseded frame is,
        so a picture placed once stays placed. A frame that does not change
        sends nothing, so they are written on their own when no frame is
        coming. They must not hold a bare `ESC[0m`, which is the frame
        delimiter; `offers()` says whether the wall's terminal wants them.
        """
        if not data:
            return
        with self._ready:
            self._through.append(bytes(data))
            self._ready.notify()

    def offers(self, word):
        """Whether the wall's `caps` says `word`, as `kitty` from a shell
        receiver whose terminal draws kitty graphics. A wall that has not
        said its caps yet offers nothing."""
        said = self._said.get("caps")
        return said is not None and (word in said.args or word in said.opts)

    def _write_loop(self):
        """Write the latest frame while the render loop builds the next.

        A frame arrives already serialised against the grid the render
        loop expected the receiver to hold, and goes out as it is when
        that is the grid last written. When it is not -- the frame it was
        diffed against was dropped, or the receiver's copy was
        invalidated -- a diff against it would leave stale cells, so it is
        serialised again here against the last grid actually written.
        """
        while not self._stop.is_set():
            with self._ready:
                while self._next is None and not self._through \
                        and not self._stop.is_set():
                    self._ready.wait()
                f, self._next = self._next, None
                through, self._through = self._through, []
                if f is not None:
                    self._taken = f["grid"]
            if through and self._writable():
                with self._wlock:
                    if not self._put(b"".join(through), True):
                        self._lost("nobody is reading")
            elif through:
                # No session yet: kept for the next, never dropped, since a
                # picture sent again while the wall was gone (`when_lost`)
                # is believed sent. Only the newest THROUGH_HELD bytes, so a
                # wall gone for long does not grow it without end.
                with self._ready:
                    held = through + self._through
                    while len(held) > 1 and sum(map(len, held)) > THROUGH_HELD:
                        held.pop(0)
                    self._through = held
                if f is None:
                    self._stop.wait(0.1)
                    continue
            if f is None:
                if self._stop.is_set():
                    return
                continue
            with self._lock:
                if self._stale(f):
                    _trace("drop  frame at %dx%d gen %d->%d"
                           % (f["cols"], f["rows"], f["gen"], self._gen))
                    continue
                prev = self._prev
                depth = self.depth
            full = f["full"] or prev is None
            data = f["data"]
            if data is None or f["depth"] != depth or not (
                    f["full"] or (not full and f["base"] is prev)):
                payload = serialise(f["grid"], prev, full, f["cols"],
                                    f["rows"], depth)
                data = payload.encode("utf-8") if payload else b""
            if not data or not self._writable():
                continue
            with self._wlock:
                # Building a frame takes long enough for a whole transition
                # to come and go, so what it was built for has to be true
                # still at the moment it goes out. A last frame that was
                # invalidated meanwhile makes this diff wrong too; the
                # repaint that invalidation forced will follow.
                with self._lock:
                    if self._stale(f) or self._prev is not prev:
                        _trace("drop  frame at %dx%d full=%s bytes=%d gen %d->%d"
                               % (f["cols"], f["rows"], full, len(data),
                                  f["gen"], self._gen))
                        continue
                if not self._put(data, True):
                    # Nobody reading: the frame is dropped, and the wall
                    # attached to again once the receiver is back.
                    self._lost("nobody is reading")
                    continue
            _trace("frame %dx%d full=%s bytes=%d"
                   % (f["cols"], f["rows"], full, len(data)))
            with self._lock:
                if (self._src is f["src"] and self._gen == f["gen"]
                        and self._prev is prev):
                    self._prev = f["grid"]

    def _adopt(self, area):
        """Follow an area the receiver has reported, however it arrived."""
        if not area:
            return
        with self._lock:
            if area == (self.cols, self.rows):
                return
            self.cols, self.rows = area
            ic, ir = self._inner()
            # Not during a transition: the source on its way out is about
            # to be replaced, and one that resizes what it is reading
            # would take the scene down with it. set_source resizes the
            # incoming one to the area it settles on.
            if self._src and not self._hold:
                try:
                    self._src.resize(ic, ir)
                except Exception:
                    pass
            self._prev = None        # new surface starts blank
            self._force = True
            self._gen += 1
            _trace("area  %dx%d gen=%d" % (self.cols, self.rows, self._gen))

    def _render_loop(self):
        """Build grids and hand them to the writer, never waiting on it.

        What is compared here is the last grid built, not the last one
        written: the writer only ever holds the newest, so a grid equal to
        the last built is already on its way or already out.
        """
        last_frame = 0.0
        last_full = 0.0
        built, built_gen = None, None
        while not self._stop.is_set() and not self._broken.is_set():
            with self._lock:
                # Safe point: we are not inside any source's pump(). They
                # are closed once the lock is let go, since a source may
                # wait a moment for its program to end, and set_source and
                # a new area wait on the lock meanwhile.
                retired, self._retire = self._retire, []
            for old in retired:
                try:
                    old.close()
                except Exception:
                    pass
            with self._lock:
                src = self._src
                blanked = self._blanked or self._hold > 0
                unwatched = self._watching == 0
            if src is None or blanked or unwatched:
                # Nothing to draw, or nobody to draw for. A receiver that
                # counts its viewers and finds none is the clearest case:
                # composing a frame for an empty room is the one cost that
                # buys nothing at all, and thirty idle demos is thirty of
                # them. A receiver that does not count says nothing, and
                # then we assume someone is there.
                time.sleep(self.min_frame)
                continue

            fd = src.fileno()
            if fd is None:
                # Only what is left of the period, not another whole
                # one. A source with nothing to wait on is polled, and
                # sleeping a full interval every time adds itself to the
                # work that interval was meant to cover: composing takes
                # thirty milliseconds, the sleep adds forty, and a wall
                # asked for twenty-five frames a second delivers
                # thirteen. Where the period has already passed, a short
                # poll rather than none, so a loop that has nothing to
                # do does not spin.
                left = (last_frame + self.min_frame) - time.monotonic()
                time.sleep(left if left > 0 else POLL)
            else:
                # A source can still be closed under us by detach(); treat a
                # dead fd as "nothing to read" rather than killing the wall.
                try:
                    r, _, _ = select.select([fd], [], [], self.min_frame)
                    if fd in r and not src.pump():
                        time.sleep(self.min_frame)
                except (OSError, ValueError):
                    time.sleep(self.min_frame)
                    continue

            now = time.monotonic()
            with self._lock:
                if self._src is not src or self._blanked or self._hold:
                    continue          # switched, blanked or mid transition
                cols, rows = self.cols, self.rows
                forced = self._force
                self._force = False
                gen = self._gen
            prev = built if built_gen == gen else None

            if not forced and (now - last_frame) < self.min_frame:
                continue

            ic, ir = cols, rows
            # Asking is what the frame rate limits, not sending: a source
            # whose picture stands still is otherwise asked again every
            # poll, which is all of a dashboard's time spent on nothing.
            #
            # The next ask is due a period after this one was due, not a
            # period after it happened. A sleep wakes late, and a late
            # wake added to every period is a wall asked for thirty
            # frames a second that delivers twenty-eight. A whole period
            # late, or forced early, the clock starts again from now
            # rather than catching up in a burst.
            due = last_frame + self.min_frame
            last_frame = due if due <= now < due + self.min_frame else now
            # A source holding its picture as arrays hands those over
            # instead, and the frame is compared and serialised in bulk
            # rather than a cell at a time.
            offered = getattr(src, "arrays", None)
            try:
                cells = (offered or src.cells)(ic, ir)
            except Exception:
                continue
            if cells is not None:
                for fn in list(self._after_frame):
                    try:
                        fn()
                    except Exception:
                        pass

            full = forced or prev is None or (now - last_full) >= self.resync
            if cells is None:
                # The source says nothing has changed. Composing and
                # serialising a grid to discover that costs more than
                # everything else in this loop put together, so a source
                # that knows is worth believing.
                if not full or prev is None:
                    continue
                grid = prev
            elif isinstance(cells, list):
                grid = compose(cells, cols, rows)
            else:
                try:
                    grid = _frame_of(cells, cols, rows)
                except Exception:
                    continue          # arrays of a shape that is no frame
            if full or grid != prev:
                # Serialised here rather than by the writer, so the writer
                # thread is left with nothing but a write that gives up
                # the GIL, and serialise overlaps with it.
                base, depth = self._base(), self.depth
                data = None
                if full or base is not None:
                    payload = serialise(grid, base, full, cols, rows, depth)
                    data = payload.encode("utf-8") if payload else None
                self._offer({"grid": grid, "full": full, "cols": cols,
                             "rows": rows, "gen": gen, "src": src,
                             "data": data, "base": base, "depth": depth})
                built, built_gen = grid, gen
                if full:
                    last_full = now


def _frame_of(arrays, cols, rows):
    """A source's arrays as a frame, importing numpy only when one does."""
    from .arrays import Frame
    return Frame.fit(arrays, cols, rows)


class CellsLayer(Wall):
    """A cells layer above the base: the wall's grid again, at a place of
    its own over whatever the base and the layers beneath it show, every
    cell it has not drawn transparent.

    Written to as a Wall is, with `set_source`, and told the area and
    `repaint` as the base is. Until it asks otherwise its grid is the
    base's, cell for cell; `set_font_size` or `grid` gives it a grid of its
    own, kept through a change of glass, and `follow_base` goes back. Over a
    film there is no base grid, and a layer that has not asked has the
    wall's font as it stands. A colour may carry an alpha, `'rrggbbaa'`, which the wall blends over
    what is beneath; a ground left `"default"` is none, so a cell in the
    default colours is transparent. `z` is the place the wall gave, once
    it has opened the layer. A wall whose `caps` do not say `cells` in
    `above=` refuses it.
    """

    def __init__(self, target, place="above", **kw):
        self._place = place
        #: The place the wall gave, from `opened`; None until it has.
        self.z = None
        super().__init__(target, **kw)

    def _make_session(self):
        # Asked again, a layer placed from below asks for the slot it had,
        # so the stack comes back in its order; one placed `top`, the top.
        again = self.z is not None and self.z > 0 and self._place != "top"
        opts = {"z": str(self.z) if again else self._place, "type": "cells"}
        if self._instance:
            opts["instance"] = self._instance
        return Session(self._link, self._name(), "layer", opts,
                       heard=self._heard_from, ended=self._ended, compress=self._compress)

    def _told(self, said):
        if said.name in ("opened", "moved"):
            z = said.opts.get("z")
            self.z = int(z) if z is not None and str(z).isdigit() else self.z
            if said.name == "moved":
                return None
        return super()._told(said)

    def attach(self, timeout=8.0):
        """Open the layer, and wait for the wall to say its area: a base
        learns it from the font it asks for, and a layer asks for none."""
        super().attach()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rep = self._read_report()
            if rep:
                with self._lock:
                    if (rep[0], rep[1]) != (self.cols, self.rows):
                        self.cols, self.rows = rep[0], rep[1]
                        self._prev = None
                        self._force = True
                        self._gen += 1
                break
            time.sleep(0.1)
        return self

    def retarget(self, target, run=None):
        """Moved to another wall, the layer asks for its place there afresh,
        not for the slot it had on this one."""
        self.z = None
        return super().retarget(target, run=run)

    def follow_base(self):
        """Give up a font or grid of the layer's own and follow the base's
        grid again, as a layer that has asked neither does."""
        self._asked_size = ("font", "base")
        self._font = None
        self._say("font", "base")
        return self


def attach(target, **kw):
    """Open a connection to a receiver."""
    return Wall(target, **kw).attach()


def layer(target, place=None, type="video", **kw):
    """Open a layer: a session of its own. `place` is `"above"`, `"top"`
    or a number, which leaves the base to whoever holds it, or 0, the base
    itself, a video base, taken from its holder with `take=True`; the
    layer's `z` is the one the wall gave. A video layer is pictures:
    `data=True` with `codec`, `fps` and `start` opens a stream the wall
    decodes and plays out (`bitwall.video`). `type="cells"` is a cells
    layer, a `CellsLayer`, attached and written to as a Wall is."""
    if type == "cells":
        if place is None:
            told = os.environ.get("BITWALL_PLACE")
            place = told if told and told != "controller" else "above"
        return CellsLayer(target, place=place, **kw).attach()
    return video.Layer(target, place=place, type=type, **kw).open()
