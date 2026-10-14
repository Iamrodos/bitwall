"""A video layer's stream on `data`, cut into the frames it carries.

A receiver that decodes nothing itself still has to know where one frame
ends and the next begins, to play each out at its own time and to hand a
glass joining late the frames it needs. QOI images back to back are cut
by walking each image's ops to its end marker; JPEGs back to back, MJPEG,
by walking each picture's segments to the end-of-image marker that ends
it; HEVC and H.264 in Annex B
are cut into access units, a picture each, by the start codes and the
first bytes of each NAL unit, and an access unit that starts a picture
nothing earlier is needed for -- an IDR, or HEVC's IRAP -- is a key.
"""

from bitwall import protocol

#: The most a side of a picture on `data` may be, as a frame's.
QOI_SIDE = 4096
QOI_END = b"\0" * 7 + b"\1"
#: The most one JPEG on `data` may be, so a stream that never ends one is
#: let go rather than held.
JPEG_MOST = 32 << 20


def qoi_end(b, start=0):
    """Where the QOI image at `b[start:]` ends, as an index into `b`: None
    when it has not all arrived, or -1 when it is not a QOI image."""
    n = len(b)
    if n - start < 14:
        return None if b"qoif".startswith(bytes(b[start:start + 4])) else -1
    if b[start:start + 4] != b"qoif":
        return -1
    w = int.from_bytes(b[start + 4:start + 8], "big")
    h = int.from_bytes(b[start + 8:start + 12], "big")
    if not (1 <= w <= QOI_SIDE and 1 <= h <= QOI_SIDE) or b[start + 12] not in (3, 4):
        return -1
    p, px, total = start + 14, 0, w * h
    while px < total:
        if p >= n:
            return None
        op = b[p]
        if op == 0xFE:
            p += 4
            px += 1
        elif op == 0xFF:
            p += 5
            px += 1
        elif op >> 6 == 2:
            p += 2
            px += 1
        elif op >> 6 == 3:
            p += 1
            px += (op & 0x3F) + 1
        else:
            p += 1
            px += 1
    if px > total:
        return -1
    if p + 8 > n:
        return None
    return p + 8 if b[p:p + 8] == QOI_END else -1


class QoiCutter:
    """QOI images back to back: `feed(bytes)` gives each image whole as it
    completes, and raises ValueError on bytes that are no QOI image."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out, p = [], 0
        while p < len(self.buf):
            end = qoi_end(self.buf, p)
            if end is None:
                break
            if end < 0:
                self.buf = bytearray()
                raise ValueError("not a QOI image")
            out.append(bytes(self.buf[p:end]))
            p = end
        del self.buf[:p]
        return out

    def flush(self):
        self.buf = bytearray()
        return []


class MjpegCutter:
    """JPEGs back to back: `feed(bytes)` gives each picture whole as it
    completes, and raises ValueError on bytes that are no JPEG a wall
    draws.

    A picture ends at its end-of-image marker, FFD9. The segments before
    the scan are stepped over by their lengths, so a thumbnail in a
    header, with an FFD9 of its own, is passed over; the scan's
    entropy-coded data never holds a marker but a restart, since an FF
    there is followed by 00, so the next other marker after it is the
    scan's end."""

    def __init__(self):
        self.buf = bytearray()
        self._fresh()

    def _fresh(self):
        self.pos, self.scan = 2, False

    def feed(self, data):
        self.buf += data
        out, b = [], self.buf
        while True:
            if len(b) < 2:
                if not b"\xff\xd8".startswith(bytes(b)):
                    return self._bad()
                break
            if b[0] != 0xFF or b[1] != 0xD8:
                return self._bad()
            try:
                end = self._end()
            except ValueError:
                return self._bad()
            if end is None:
                if len(b) > JPEG_MOST:
                    return self._bad()
                break
            jpeg = bytes(b[:end])
            del b[:end]
            self._fresh()
            size = protocol.image_size("jpeg", jpeg)
            if size is None or not all(1 <= x <= QOI_SIDE for x in size):
                return self._bad()
            out.append(jpeg)
        return out

    def _end(self):
        """Where the picture at the buffer's start ends, carrying on from
        where the last look stopped: None until its end has arrived."""
        b, n, p = self.buf, len(self.buf), self.pos
        while True:
            if self.scan:
                q = b.find(b"\xff", p)
                if q < 0 or q + 1 >= n:
                    self.pos = max(p, n) if q < 0 else q
                    return None
                m = b[q + 1]
                if m == 0x00 or 0xD0 <= m <= 0xD7:
                    p = q + 2
                    continue
                if m == 0xFF:
                    p = q + 1
                    continue
                self.scan, p = False, q
            if p + 2 > n:
                self.pos = p
                return None
            if b[p] != 0xFF:
                raise ValueError("not a JPEG")
            m = b[p + 1]
            if m == 0xFF:
                p += 1
            elif m == 0xD9:
                return p + 2
            elif m == 0x01 or 0xD0 <= m <= 0xD7:
                p += 2
            elif m == 0xD8:
                raise ValueError("not a JPEG")
            else:
                if p + 4 > n:
                    self.pos = p
                    return None
                length = int.from_bytes(b[p + 2:p + 4], "big")
                if length < 2:
                    raise ValueError("not a JPEG")
                p += 2 + length
                self.scan = m == 0xDA

    def _bad(self):
        self.buf = bytearray()
        self._fresh()
        raise ValueError("not a JPEG")

    def flush(self):
        self.buf = bytearray()
        self._fresh()
        return []


class AnnexB:
    """HEVC or H.264 in Annex B: `feed(bytes)` gives each access unit, as
    (bytes, key), once the next has begun, and `flush()` the last.

    A NAL unit starts a new access unit where the one being gathered has a
    picture's slice already and this one is a parameter set, a delimiter
    or an SEI, or a slice that is the first of its picture."""

    def __init__(self, codec):
        self.hevc = codec == "hevc"
        self.buf = bytearray()
        self.au = bytearray()
        self.vcl = False
        self.key = False

    def _nal(self, nal):
        """One NAL unit, start code taken off: (begins a new access unit,
        is a slice, is a key slice)."""
        if self.hevc:
            if len(nal) < 3:
                return False, False, False
            t = (nal[0] >> 1) & 0x3F
            if t < 32:
                return bool(nal[2] & 0x80), True, 16 <= t <= 23
            return t in (32, 33, 34, 35, 39) or 41 <= t <= 44 or 48 <= t <= 55, False, False
        if len(nal) < 2:
            return False, False, False
        t = nal[0] & 0x1F
        if 1 <= t <= 5:
            return bool(nal[1] & 0x80), True, t == 5
        return t in (6, 7, 8, 9) or 14 <= t <= 18, False, False

    def _take(self, nal, out):
        first, vcl, key = self._nal(nal)
        if first and self.vcl:
            out.append((bytes(self.au), self.key))
            self.au, self.vcl, self.key = bytearray(), False, False
        self.au += b"\0\0\0\1" + nal
        self.vcl |= vcl
        self.key |= key

    def feed(self, data):
        self.buf += data
        out, b = [], self.buf
        start = b.find(b"\0\0\1")
        if start < 0:
            # Nothing that starts a unit yet: keep the last two bytes in
            # case a start code is split across reads.
            del b[:max(0, len(b) - 2)]
            return out
        while True:
            nxt = b.find(b"\0\0\1", start + 3)
            if nxt < 0:
                break
            end = nxt
            while end > start + 3 and b[end - 1] == 0:
                end -= 1          # the leading zero of a four-byte start code
            self._take(b[start + 3:end], out)
            start = nxt
        del b[:start]
        return out

    def flush(self):
        out = []
        if len(self.buf) > 3 and self.buf.find(b"\0\0\1") == 0:
            self._take(bytes(self.buf[3:]).rstrip(b"\0"), out)
        self.buf = bytearray()
        if self.vcl:
            out.append((bytes(self.au), self.key))
        self.au, self.vcl, self.key = bytearray(), False, False
        return out


def cutter(codec):
    if codec == "qoi":
        return QoiCutter()
    if codec == "mjpeg":
        return MjpegCutter()
    return AnnexB(codec)
