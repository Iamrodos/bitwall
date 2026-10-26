"""Content sources, and the bytes a receiver paints.

Three steps, all on grids of cells (see `bitwall.cells` for the format):

    source   a python profile, or a terminal program -- run here, or in a
             tmux session -- read through a terminal emulator; both
             yield a grid
    compose  place the grid in the receiver's area and draw the border
    serialise  emit the cells that differ from the last frame

Sources are interchangeable because they meet at the grid, so a sender can
compute cells directly or host a terminal program, and what leaves here is
the same either way.
"""
import functools
import os
import select
import signal
import struct
import subprocess
import threading
import time


from .cells import BLANK


# ----------------------------------------------------------------- colours
# A cell is (text, fg, bg, bold, italics, underscore, strikethrough,
# reverse), and faint after them where it is set (cells.py). fg/bg are
# 'default', one of the eight colour names, a 'rrggbb' hex string, or an
# int meaning a 256-palette index, so a grid computed in python and one
# decoded from a terminal are the same kind of thing.

NAMED = {"black": 0, "red": 1, "green": 2, "brown": 3,
         "blue": 4, "magenta": 5, "cyan": 6, "white": 7}

#: The colour a screen can show, coarsest first. A receiver announces one
#: of these and everything on the way out is reduced to it, so a screen
#: that can hold two colours is sent two rather than sent a gradient and
#: left to flatten it itself.
DEPTHS = ("mono", "grey", "16", "256", "true")

#: sRGB luminance. The weights are the eye's, not the wire's: a pure blue
#: and a pure yellow of the same value are not the same brightness, and a
#: screen with one ink shows the difference as black against white.
_LUMA = (0.2126, 0.7152, 0.0722)

#: The 16 terminal colours as RGB, for picking the nearest. Values are the
#: xterm defaults; a screen with its own palette is still closer to these
#: than to an arbitrary triple.
_BASIC = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
          (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
          (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
          (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))


def _rgb(colour):
    """One colour as (r, g, b), or None if it has no fixed value."""
    if isinstance(colour, str) and len(colour) == 6:
        try:
            return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
    if colour in NAMED:
        return _BASIC[NAMED[colour]]
    if isinstance(colour, int) and 0 <= colour < 16:
        return _BASIC[colour]
    return None


def _luma(rgb):
    return sum(w * c for w, c in zip(_LUMA, rgb))


def _reduce(colour, depth):
    """One colour as the coarsest screen that will show it would have it.

    Reducing here rather than at the receiver is what makes it worth
    announcing: the cells that survive are fewer and repeat, so a frame
    both compresses better and produces a smaller diff against the last.
    """
    if depth == "true" or depth not in DEPTHS:
        return colour
    if isinstance(colour, str) and len(colour) == 8:
        colour = colour[:6]              # a screen this coarse has no alpha
    rgb = _rgb(colour)
    if rgb is None:
        return colour                    # 'default' has no value to reduce
    # The coarse screens get the short spelling as well as the few
    # colours. A screen with one ink is the one that least wants six
    # bytes of truecolour per cell to say black.
    if depth == "mono":
        return "white" if _luma(rgb) >= 128 else "black"
    if depth == "grey":
        # The 24-step ramp runs from 8 to 238 in tens.
        return 232 + min(23, max(0, round((_luma(rgb) - 8) / 10)))
    if depth == "16":
        best = min(range(16), key=lambda i: sum(
            (a - b) ** 2 for a, b in zip(_BASIC[i], rgb)))
        return best if best < 8 else f"{_BASIC[best][0]:02x}" \
            f"{_BASIC[best][1]:02x}{_BASIC[best][2]:02x}"
    # 256: the nearest of the colours the palette has, in the 6x6x6 cube
    # or on the grey ramp. The cube's levels are xterm's, 0 then 95 to 255
    # in forties, not even steps, so a dark colour is matched against what
    # is really there rather than rounded up onto the first step.
    return _nearest_256(rgb)


#: The xterm cube's six levels a channel.
CUBE = (0, 95, 135, 175, 215, 255)


def _nearest_level(v):
    return min(range(6), key=lambda i: abs(CUBE[i] - v))


@functools.lru_cache(maxsize=65536)
def _nearest_256(rgb):
    """The 256-colour index nearest rgb: the cube or the grey ramp."""
    r, g, b = (_nearest_level(v) for v in rgb)
    cube = (CUBE[r], CUBE[g], CUBE[b])
    grey = min(23, max(0, round((sum(rgb) / 3 - 8) / 10)))
    level = 8 + 10 * grey

    def far(c):
        return sum((a - b) ** 2 for a, b in zip(c, rgb))

    if far((level,) * 3) < far(cube):
        return 232 + grey
    return 16 + 36 * r + 6 * g + b


def palette_256(index):
    """The colour an xterm 256-colour index shows, (r, g, b), from 16 on."""
    if index >= 232:
        v = 8 + 10 * (index - 232)
        return (v, v, v)
    index -= 16
    return (CUBE[index // 36], CUBE[index // 6 % 6], CUBE[index % 6])


def _colour_params(colour, base, depth="true"):
    """SGR parameters for one colour. base is 30 (fg) or 40 (bg)."""
    colour = _reduce(colour, depth)
    if colour == "default":
        return [str(base + 9)]
    if isinstance(colour, int):
        return [str(base + 8), "5", str(colour)]
    if colour in NAMED:
        return [str(base + NAMED[colour])]
    if isinstance(colour, str) and len(colour) == 8:
        # A colour with an alpha, `rrggbbaa`, for a cells layer above the
        # base, which blends it over what is beneath. Opaque is said as
        # the truecolour it is.
        try:
            r, g, b, a = (int(colour[i:i + 2], 16) for i in (0, 2, 4, 6))
        except ValueError:
            return [str(base + 9)]
        if a == 255:
            return [str(base + 8), "2", str(r), str(g), str(b)]
        return [str(base + 8), "6", str(r), str(g), str(b), str(a)]
    if isinstance(colour, str) and len(colour) == 6:
        try:
            r, g, b = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return [str(base + 9)]
        # Truecolour reproduces exactly what the source kept.
        return [str(base + 8), "2", str(r), str(g), str(b)]
    return [str(base + 9)]


def _sgr(cell, depth="true"):
    return _style(*cell[1:8], len(cell) > 8 and bool(cell[8]), depth)


@functools.lru_cache(maxsize=65536)
def _style(fg, bg, bold, italics, underscore, strike, reverse, faint, depth):
    """The SGR sequence for one look: colours, attributes and depth.

    Kept once per look rather than worked out per cell, because a frame
    asks for the same few thousand looks tens of thousands of times, and
    turning a colour back into numbers was most of what serialising cost.
    The character is not part of it, so every cell drawn alike shares one.
    """
    p = ["0"]
    if bold: p.append("1")
    if faint: p.append("2")
    if italics: p.append("3")
    if underscore: p.append("4")
    if strike: p.append("9")
    if reverse: p.append("7")
    p.append(_colour_text(fg, 30, depth))
    p.append(_colour_text(bg, 40, depth))
    return "\033[" + ";".join(p) + "m"


@functools.lru_cache(maxsize=65536)
def _colour_text(colour, base, depth):
    """One colour's SGR parameters, joined. Kept apart from the look it is
    part of because a film repeats its colours far more than it repeats
    any pairing of them."""
    return ";".join(_colour_params(colour, base, depth))


# -------------------------------------------------------------- compositor

GAP = 6      # re-homing the cursor costs ~8 bytes; carrying a few unchanged
             # cells inside a run is cheaper than breaking it


def compose(cells, cols, rows):
    """Place a source's cells in the receiver's area, clipped to fit.

    Takes a plain grid rather than an emulator, so cells computed in python
    and cells decoded from a terminal travel the same path from here on.

    A grid larger than the area loses what falls outside; a smaller one
    leaves the rest blank. Drawing a border, or anything else about how the
    content looks, is the sender's business.
    """
    grid = [[BLANK] * cols for _ in range(rows)]
    for y in range(min(rows, len(cells))):
        row, grow = cells[y], grid[y]
        for x in range(min(cols, len(row))):
            grow[x] = row[x]
    return grid


def serialise(grid, prev, full, cols, rows, depth="true"):
    """ANSI for the cells that changed between prev and grid.

    A frame is absolutely positioned runs, so nothing here depends on
    wrapping, scrolling or margins, and a receiver needs a cursor, a colour
    and an attribute set rather than a terminal.

    **A bare `ESC[0m` delimits a frame.** One opens a full repaint and one
    closes any frame that drew something, and every other SGR carries
    parameters -- so it is the one place a receiver can be sure the grid is
    whole. Receivers paint on it: a frame larger than the 64KB pipe arrives
    in pieces, and painting a piece shows a band sweeping down the wall.
    Both resets have to stay.

    `depth` is what the screen said it can show, and every colour is
    reduced to it on the way out rather than sent whole for the receiver
    to flatten.

    A frame a source handed over as arrays (`bitwall.arrays.Frame`) is
    serialised by that frame, in bulk, to the same bytes.
    """
    if not isinstance(grid, list):
        return grid.serialise(prev, full, depth)
    if prev is not None and not isinstance(prev, list):
        prev = prev.cells()
    out = []
    cur = None
    if full:
        # Reset attributes but do NOT clear. A full repaint writes every
        # cell anyway, and the grid covers the whole area -- so ED tidies
        # nothing. What it does do is blank the screen for as long as the
        # repaint takes to arrive, which reads as a flash.
        out.append("\033[0m")

    for y in range(rows):
        row = grid[y]
        old = None if full else (prev[y] if prev and y < len(prev) else None)
        x = 0
        while x < cols:
            if old is not None and x < len(old) and row[x] == old[x]:
                x += 1
                continue
            start, end, gap, probe = x, x + 1, 0, x + 1
            while probe < cols:
                if old is not None and probe < len(old) and row[probe] == old[probe]:
                    gap += 1
                    if gap > GAP:
                        break
                else:
                    end = probe + 1
                    gap = 0
                probe += 1

            out.append(f"\033[{y + 1};{start + 1}H")
            for i in range(start, end):
                cell = row[i]
                if cell[0] == "":
                    continue      # second half of a double-width glyph
                a = cell[1:]
                if a != cur:
                    out.append(_sgr(cell, depth))
                    cur = a
                out.append(cell[0])
            x = end

    if out:
        out.append("\033[0m")
    return "".join(out)


# ----------------------------------------------------------------- sources

class _TerminalSource:
    """A program in a pty of its own, its screen read by bitwall.terminal.

    What the program asks of its terminal -- where the cursor is, what the
    terminal is -- is answered on the pty, as a terminal would.
    """

    kind = "terminal"

    def __init__(self):
        self.pid = self.fd = None
        self.term = None
        self._owed = b""
        self._owing = threading.Lock()

    def argv(self):
        raise NotImplementedError

    def environ(self):
        return {"TERM": "xterm-256color"}

    def start(self, cols, rows):
        # A pty is Unix's: only a terminal source asks for one.
        import pty
        from . import terminal
        self.term = terminal.Terminal(cols, rows)
        argv = self.argv()
        # Everything the child needs is made before the fork. The process
        # has threads, and a child that allocated or took a lock between
        # fork and exec could wait for good on one held by a thread that
        # did not come with it.
        env = {"LANG": "en_AU.UTF-8", "LC_ALL": "en_AU.UTF-8",
               **os.environ, **self.environ()}
        failed = f"bitwall: cannot run {argv[0]}\r\n".encode(errors="replace")
        import shutil
        path = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(path, argv, env)
            except BaseException:
                # On the wall, where whoever is looking will see it.
                os.write(1, failed)
            finally:
                os._exit(127)
        # Never blocked on the program: a reply it does not read must not
        # stop the wall (`_answer`).
        os.set_blocking(self.fd, False)
        self.resize(cols, rows)

    def fileno(self):
        return self.fd

    def resize(self, cols, rows):
        if self.fd is None:
            return
        import fcntl
        import termios
        self.term.resize(cols, rows)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        self._answer()

    def pump(self):
        got = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except (OSError, BlockingIOError):
                data = b""
            if not data:
                break
            self.term.write(data)
            got = True
            rr, _, _ = select.select([self.fd], [], [], 0)
            if self.fd not in rr:
                break
        self._answer()
        return got

    #: The most a program may be owed before what it has not taken is let go.
    OWED = 4096

    def _answer(self):
        """Write the program the replies it is owed, as much as its input
        takes now. One that never reads its input -- asking where the cursor
        is and never looking -- fills it, and is let go its replies rather
        than left to stop the thread that writes them, and the wall."""
        with self._owing:
            owed = self._owed + self.term.said()
            if owed and self.fd is not None:
                try:
                    owed = owed[os.write(self.fd, owed):]
                except OSError:
                    pass
            self._owed = owed if len(owed) <= self.OWED else b""

    def cells(self, cols, rows):
        grid = self.term.cells()
        if len(grid) == rows and grid and len(grid[0]) == cols:
            return grid
        return compose(grid, cols, rows)

    def dump(self):
        if self.term is None:
            return []
        return ["".join(c[0] for c in row) for row in self.term.cells()]

    def close(self):
        # The pty goes first. A program stuck writing to a pty nobody reads
        # never gets round to a SIGTERM; with its terminal gone, its write
        # fails and it hangs up. One that still lingers is killed.
        with self._owing:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                end = time.monotonic() + 1.0
                while not os.waitpid(self.pid, os.WNOHANG)[0]:
                    if time.monotonic() > end:
                        os.kill(self.pid, signal.SIGKILL)
                        os.waitpid(self.pid, 0)
                        break
                    time.sleep(0.01)
            except (ProcessLookupError, ChildProcessError):
                pass
            self.pid = None
        if self.term is not None:
            # Kept, closed: the render thread may still be reading it.
            self.term.close()


class TmuxSource(_TerminalSource):
    """A read-only tmux client in a pty, its screen read into cells.

    tmux is kept because it hosts arbitrary TUIs -- btop, lazygit, a log
    tailer -- for the price of one line of config, and keeps a session
    someone else may be attached to as well. A program the wall alone
    shows needs no tmux: that is CommandSource.
    """

    kind = "tmux"

    def __init__(self, session, sock=None, readonly=True):
        super().__init__()
        self.session = session
        self.sock = sock or os.environ.get("BITWALL_SOCK", "")
        #: Read-only keeps a wall from ever typing into what it is showing.
        #: A sender that carries a viewer's keystrokes back needs the other
        #: kind: tmux refuses `send-keys` while a read-only client is the
        #: one the command resolves to.
        self.readonly = readonly

    def argv(self):
        argv = ["tmux", "-u", "-S", self.sock, "attach"]
        if self.readonly:
            argv.append("-r")
        return argv + ["-t", self.session]

    def resize(self, cols, rows):
        if self.fd is None:
            return
        # Resize the window first, then the pty: our client must be able to
        # draw the window, and tmux stalls redraws on a client that cannot.
        if self.sock:
            subprocess.run(["tmux", "-S", self.sock, "resize-window",
                            "-t", self.session, "-x", str(cols), "-y", str(rows)],
                           capture_output=True)
        super().resize(cols, rows)


class CommandSource(_TerminalSource):
    """A program run in a pty of its own, its screen read into cells.

        CommandSource(["btop"])
        CommandSource("htop -d 10")

    The program is the wall's alone: it starts with the source, is told
    the wall's area as its terminal's size, and ends when the source is
    closed. It is told it may use truecolour, which the wall shows as is.
    """

    kind = "command"

    def __init__(self, command, env=None):
        super().__init__()
        import shlex
        self.command = shlex.split(command) if isinstance(command, str) \
            else list(command)
        self.env = dict(env or {})

    def argv(self):
        return self.command

    def environ(self):
        return {"TERM": "xterm-256color", "COLORTERM": "truecolor", **self.env}

    def exited(self):
        """Whether the program has ended, by itself or otherwise."""
        if not self.pid:
            return True
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid:
            self.pid = None
        return bool(pid)


class FunctionSource:
    """Cells computed by a function.

    Accepts a callable, a module with a render() attribute, or the name of
    such a module. Whichever it is, it is called as render(cols, rows) and
    must return a grid in the cell format.

    There is nothing to wait on, so frames are produced on a tick. An
    exception inside render() paints its message on the top row rather than
    stopping the wall.

    `arrays`, if given, is the same picture as `(ch, fg, bg)` arrays (see
    `bitwall.arrays`), and a wall asks for that instead of cells. Given
    alone, it is where the cells come from as well, so a pixel source
    need not be written twice.
    """

    kind = "python"

    def __init__(self, profile=None, arrays=None):
        # A module, a bare render(cols, rows) callable, or an importable
        # name. Profiles live beside the sender that uses them, not on a
        # global path, so taking the object directly is the normal case.
        self.profile = profile
        self.render = None
        self.grid = []
        if arrays is not None:
            self._arrays = arrays
            # An instance attribute rather than a method, because whether
            # a source offers arrays is what the wall asks of it.
            self.arrays = self._arrays_or_error

    def start(self, cols, rows):
        p = self.profile
        if p is None:
            self.render = self._cells_of_arrays
            return
        if isinstance(p, str):
            p = __import__(p)
        self.render = p if callable(p) else p.render

    def _cells_of_arrays(self, cols, rows):
        from .arrays import Frame
        got = self._arrays(cols, rows)
        if got is None or isinstance(got, list):
            return got
        return Frame.fit(got, cols, rows).cells()

    def _arrays_or_error(self, cols, rows):
        """The arrays, or a grid naming what went wrong in making them."""
        try:
            return self._arrays(cols, rows)
        except Exception as exc:
            return self._error(cols, rows, exc)

    def fileno(self):
        return None          # nothing to wait on; redraws on a tick

    def resize(self, cols, rows):
        pass                 # render() is told the area every frame

    def pump(self):
        return True

    def cells(self, cols, rows):
        try:
            self.grid = self.render(cols, rows)
        except Exception as exc:
            self.grid = self._error(cols, rows, exc)
        return self.grid

    @staticmethod
    def _error(cols, rows, exc):
        """A broken profile's message on the top row, rather than the wall
        going down with it."""
        grid = [[BLANK] * cols for _ in range(rows)]
        msg = f"profile error: {type(exc).__name__}: {exc}"[:max(0, cols - 2)]
        for i, ch in enumerate(msg):
            grid[0][i + 1] = (ch, 203, "default",
                              True, False, False, False, False)
        return grid

    def dump(self):
        return ["".join(c[0] for c in row).rstrip() for row in self.grid]

    def close(self):
        pass
