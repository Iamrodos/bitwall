"""A terminal emulator, for a program shown on a wall.

What a terminal program writes goes in, and the screen it drew comes out as
cells. Inside is libghostty-vt, Ghostty's own emulator, behind the few calls
in `bitwall/vt/bwvt.c`, built by `bitwall/vt/build` into one library. It is
looked for in `bitwall/vt/lib/`, or wherever `BITWALL_VT_LIB` says.

A program asks its terminal questions -- where the cursor is, what the
terminal is -- and expects answers on its input. `said()` is what is owed,
for the host to write back to the program's pty.
"""
import ctypes
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAME = {"darwin": "libbitwall-vt.dylib",
         "win32": "bitwall-vt.dll"}.get(sys.platform, "libbitwall-vt.so")

#: The record bwvt.c writes for a cell: fg, bg, their kinds, flags, wide.
_RECORD = struct.Struct("<IIBBBB")

_lib = None


def library():
    """Where the emulator is looked for."""
    return os.environ.get("BITWALL_VT_LIB") or os.path.join(
        _HERE, "vt", "lib", _NAME)


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
                                  ctypes.POINTER(u16), ctypes.POINTER(u16),
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
    """One screen of cols x rows, fed with what a program writes."""

    def __init__(self, cols, rows):
        self._lib = _load()
        self._vt = self._lib.bwvt_new(cols, rows)
        if not self._vt:
            raise MemoryError("the terminal emulator could not be made")
        self.cols, self.rows = cols, rows
        self._grid = []
        self._shape = None
        self._looks = {}
        self._buffers(cols, rows, 8)

    def _buffers(self, cols, rows, text_per_cell):
        self._recs = ctypes.create_string_buffer(cols * rows * _RECORD.size)
        self._text = ctypes.create_string_buffer(cols * rows * text_per_cell)
        self._ys = (ctypes.c_uint16 * max(1, rows))()

    def write(self, data):
        """What the program wrote, as bytes, in any pieces."""
        if data:
            self._lib.bwvt_write(self._vt, data, len(data))

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self._lib.bwvt_resize(self._vt, cols, rows)
        if len(self._ys) < rows or len(self._recs) < cols * rows * _RECORD.size:
            self._buffers(cols, rows, 8)

    def said(self):
        """The replies the program is owed, as bytes; each is given once."""
        out = b""
        buf = ctypes.create_string_buffer(4096)
        while True:
            n = self._lib.bwvt_take_said(self._vt, buf, len(buf))
            out += buf.raw[:n]
            if n < len(buf):
                return out

    def cells(self):
        """The screen, a list of rows of cells, as `bitwall.cells` has them.

        Only the rows that changed since the last call are read again; the
        rest are the same row objects as before, which a caller must not
        change.
        """
        cols, rows = ctypes.c_uint16(), ctypes.c_uint16()
        used = ctypes.c_size_t()
        every = self._shape != (self.cols, self.rows)
        n = self._read(every, cols, rows, used)
        while n < 0:
            # A buffer was short: grapheme clusters longer than were
            # allowed for. Every row is read again into larger ones.
            self._buffers(self.cols, self.rows, len(self._text)
                          // max(1, self.cols * self.rows) * 4)
            n = self._read(True, cols, rows, used)
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
                                   self._ys, self._recs, len(self._recs),
                                   self._text, len(self._text),
                                   ctypes.byref(used))

    def close(self):
        if self._vt:
            self._lib.bwvt_free(self._vt)
            self._vt = None

    def __del__(self):
        try:
            self.close()
        except Exception:                                     # noqa: BLE001
            pass
