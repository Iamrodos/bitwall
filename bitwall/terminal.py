"""A terminal emulator, for a program shown on a wall.

What a terminal program writes goes in, and the screen it drew comes out as
cells. Inside is libghostty-vt, Ghostty's own emulator, behind the few calls
in `bitwall/vt/bwvt.c`, built by `bitwall/vt/build` into one library, which
`library()` says where to find.

A program drawing a frame it wants seen whole (synchronized output, mode
2026) is read as the screen stood before the frame, until it is done, or
for a second at most.

A program asks its terminal questions -- where the cursor is, what the
terminal is -- and expects answers on its input. `said()` is what is owed,
for the host to write back to the program's pty.
"""
import ctypes
import os
import platform
import struct
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAME = {"darwin": "libbitwall-vt.dylib",
         "win32": "bitwall-vt.dll"}.get(sys.platform, "libbitwall-vt.so")

#: The record bwvt.c writes for a cell: fg, bg, their kinds, flags, wide.
_RECORD = struct.Struct("<IIBBBB")

#: What bwvt_read says when it took no rows: a buffer was too small, or
#: the terminal could not be read and asking again will not help.
_SHORT, _FAILED = -1, -2

#: Bytes of text a cell is allowed at first, and at most: a grapheme of a
#: few code points fits the first, and the most is past any that is drawn.
_TEXT_FIRST, _TEXT_MOST = 8, 512

_lib = None


#: The zig target `bitwall/vt/build <target>` is given for a machine, by
#: what Python calls it: the folder of lib/ a cross-built library is in.
_TARGETS = {("linux", "aarch64"): "aarch64-linux-gnu",
            ("linux", "armv7l"): "arm-linux-gnueabihf",
            ("linux", "armv6l"): "arm-linux-gnueabihf",
            ("linux", "x86_64"): "x86_64-linux-gnu",
            ("darwin", "arm64"): "aarch64-macos",
            ("darwin", "x86_64"): "x86_64-macos"}


def library():
    """Where the emulator is looked for: `BITWALL_VT_LIB`, else one built
    here in `bitwall/vt/lib/`, else one cross-built for this machine in
    `bitwall/vt/lib/<target>/`."""
    if os.environ.get("BITWALL_VT_LIB"):
        return os.environ["BITWALL_VT_LIB"]
    lib = os.path.join(_HERE, "vt", "lib")
    here = os.path.join(lib, _NAME)
    target = _TARGETS.get((sys.platform.rstrip("0123456789"),
                           platform.machine()))
    crossed = os.path.join(lib, target, _NAME) if target else None
    if not os.path.exists(here) and crossed and os.path.exists(crossed):
        return crossed
    return here


def _load():
    global _lib
    if _lib is None:
        path = library()
        if not os.path.exists(path):
            raise RuntimeError(f"no terminal emulator at {path}: "
                               "build it with bitwall/vt/build")
        lib = ctypes.CDLL(path)
        u16, size, ptr = ctypes.c_uint16, ctypes.c_size_t, ctypes.c_void_p
        lib.bwvt_new.argtypes = [u16, u16]
        lib.bwvt_new.restype = ptr
        lib.bwvt_free.argtypes = [ptr]
        lib.bwvt_free.restype = None
        lib.bwvt_write.argtypes = [ptr, ctypes.c_char_p, size]
        lib.bwvt_write.restype = None
        lib.bwvt_resize.argtypes = [ptr, u16, u16]
        lib.bwvt_resize.restype = ctypes.c_int
        lib.bwvt_take_said.argtypes = [ptr, ctypes.c_char_p, size]
        lib.bwvt_take_said.restype = size
        lib.bwvt_read.argtypes = [ptr, ctypes.c_int, ctypes.POINTER(u16),
                                  ctypes.POINTER(u16), ctypes.POINTER(u16), size,
                                  ctypes.c_char_p, size, ctypes.c_char_p,
                                  size, ctypes.POINTER(size)]
        lib.bwvt_read.restype = ctypes.c_int
        _lib = lib
    return _lib


def available():
    """Whether the emulator is built and loads here."""
    try:
        _load()
    except (RuntimeError, OSError):
        return False
    return True


def _colour(kind, value):
    if kind == 1:
        return value
    if kind == 2:
        return f"{value:06x}"
    return "default"


class Terminal:
    """One screen of cols x rows, fed with what a program writes.

    Safe to use from several threads: a Wall resizes it on one while it is
    fed and read on another. ctypes lets go of the GIL inside the library,
    which allows only one call at a time on a terminal, so every call holds
    the terminal's own lock. Once closed, it takes nothing and reads as it
    last stood.
    """

    def __init__(self, cols, rows):
        self._lock = threading.Lock()
        self._lib = _load()
        self._vt = self._lib.bwvt_new(cols, rows)
        if not self._vt:
            raise MemoryError("the terminal emulator could not be made")
        self.cols, self.rows = cols, rows
        self._grid = []
        self._shape = None
        self._looks = {}
        self._per = _TEXT_FIRST
        self._buffers(cols, rows)

    def _buffers(self, cols, rows):
        self._recs = ctypes.create_string_buffer(max(1, cols * rows) * _RECORD.size)
        self._text = ctypes.create_string_buffer(max(1, cols * rows) * self._per)
        self._ys = (ctypes.c_uint16 * max(1, rows))()

    def write(self, data):
        """What the program wrote, as bytes, in any pieces."""
        if not data:
            return
        with self._lock:
            if self._vt:
                self._lib.bwvt_write(self._vt, data, len(data))

    def resize(self, cols, rows):
        with self._lock:
            if not self._vt:
                return
            if self._lib.bwvt_resize(self._vt, cols, rows) != 0:
                return                  # refused: the screen is as it was
            self.cols, self.rows = cols, rows
            if len(self._ys) < rows or len(self._recs) < cols * rows * _RECORD.size:
                self._buffers(cols, rows)

    def said(self):
        """The replies the program is owed, as bytes; each is given once."""
        out = b""
        buf = ctypes.create_string_buffer(4096)
        with self._lock:
            while self._vt:
                n = self._lib.bwvt_take_said(self._vt, buf, len(buf))
                out += buf.raw[:n]
                if n < len(buf):
                    break
        return out

    def cells(self):
        """The screen, a list of rows of cells, as `bitwall.cells` has them.

        Only the rows that changed since the last call are read again; the
        rest are the same row objects as before, which a caller must not
        change.
        """
        with self._lock:
            if not self._vt:
                return list(self._grid)
            return self._cells()

    def _cells(self):
        cols, rows = ctypes.c_uint16(), ctypes.c_uint16()
        used = ctypes.c_size_t()
        every = self._shape != (self.cols, self.rows)
        n = self._read(every, cols, rows, used)
        while n == _SHORT and self._per < _TEXT_MOST:
            # A buffer was short, for the screen the read said it has or
            # for graphemes longer than allowed for. Every row is read
            # again into larger ones, a few times at most.
            self._per *= 4
            self._buffers(cols.value, rows.value)
            n = self._read(True, cols, rows, used)
        if n < 0:
            # Nothing more can be read of it: the screen stands as it was
            # last read, and the next read starts whole.
            self._shape = None
            return list(self._grid)
        cols, rows = cols.value, rows.value
        if self._shape != (cols, rows):
            self._grid = [None] * rows
            self._shape = (cols, rows)
        texts = self._text.raw[:used.value].decode("utf-8", "replace").split("\0")
        looks = self._looks
        recs = self._recs.raw
        size = _RECORD.size
        for i in range(n):
            y = self._ys[i]
            base = i * cols
            row = []
            for x, rec in enumerate(_RECORD.iter_unpack(
                    recs[base * size:(base + cols) * size])):
                look = looks.get(rec)
                if look is None:
                    fg, bg, fgk, bgk, flags, _ = rec
                    look = (_colour(fgk, fg), _colour(bgk, bg),
                            bool(flags & 1), bool(flags & 2), bool(flags & 4),
                            bool(flags & 8), bool(flags & 16))
                    if flags & 32:
                        look += (True,)
                    if len(looks) > 4096:
                        looks.clear()
                    looks[rec] = look
                row.append((texts[base + x],) + look)
            self._grid[y] = row
        return list(self._grid)

    def _read(self, every, cols, rows, used):
        return self._lib.bwvt_read(self._vt, 1 if every else 0,
                                   ctypes.byref(cols), ctypes.byref(rows),
                                   self._ys, len(self._ys),
                                   self._recs, len(self._recs),
                                   self._text, len(self._text),
                                   ctypes.byref(used))

    def close(self):
        with self._lock:
            if self._vt:
                self._lib.bwvt_free(self._vt)
                self._vt = None

    def __del__(self):
        try:
            self.close()
        except Exception:                                     # noqa: BLE001
            pass
