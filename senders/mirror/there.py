"""The mirror's encoder on the source's host, for a mirror that pulls.

A mirror run on a destination's host (`--pull`) has the source's glass
encoded where it is drawn, so only the encoded stream crosses the
network: this runs there, through ssh, carried on the command line as
`bitwall/relay.py` is, so the source host keeps no copy to go stale.

    encode <glass> <w> <h> <fps> <ffmpeg argv...>
    probe <encoder>...

`encode` reads the painter's raw glass stream at `<glass>` (PROTOCOL.md,
"The glass as a raw stream"), feeds ffmpeg the I420 planes, and writes
records to stdout, each a type byte and a little-endian length:

    F  a frame given ffmpeg: drawn (µs of this host's clock), filled
    B  a frame ffmpeg was not ready for, not given it: drawn, filled
    J  the frames stopped being one a tick: a jump of the source's
    D  ffmpeg's output, as it came
    L  a line for the mirror's log

A frame the painter let go is made up by repeating the one before
(`filled`), and a jump of more than a second is not filled, as the
mirror does with a stream read where it runs. An F is written before its
frame goes to ffmpeg, so the mirror always has a frame's time before
the access unit made of it. It ends, ffmpeg with it, when stdin ends
(the mirror let it go, or its ssh died), when its stdout breaks, or
when the stream does; ffmpeg runs at nice 10, below the painter.

`probe` prints each encoder ffmpeg here encodes with, a line each.
"""
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time

HEADER = struct.Struct("<4sIIQQI")
RECORD = struct.Struct("<cI")
FRAME = struct.Struct("<QB")
#: Frames waiting for ffmpeg, as the mirror's own FEED_QUEUE.
FEED_QUEUE = 3

_out = threading.Lock()
_ffmpeg = []


def _bye(code=0):
    for p in _ffmpeg:
        try:
            p.kill()
        except OSError:
            pass
    os._exit(code)


def send(kind, payload=b""):
    rec = RECORD.pack(kind, len(payload)) + payload
    with _out:
        mv = memoryview(rec)
        try:
            while mv:
                mv = mv[os.write(1, mv):]
        except OSError:
            _bye(0)


def say(text):
    send(b"L", text.encode("utf-8", "replace"))


def _fill(fd, mv, path):
    """Read into `mv` until full; how much came. A fifo with no writer
    reads nothing, and is waited on while the stream is there."""
    got = 0
    while got < len(mv):
        try:
            n = os.readv(fd, [mv[got:]])
        except BlockingIOError:
            select.select([fd], [], [], 0.2)
            continue
        except InterruptedError:
            continue
        if n == 0:
            if not os.path.exists(path):
                return got
            time.sleep(0.02)
            continue
        got += n
    return got


def encode(path, w, h, fps, argv):
    w, h, fps = int(w), int(h), float(fps)
    fd = None
    for _ in range(100):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            break
        except FileNotFoundError:
            time.sleep(0.05)
    if fd is None:
        say(f"no glass stream at {path}")
        return 2
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0,
                            preexec_fn=lambda: os.nice(10))
    _ffmpeg.append(proc)
    err = []

    def stdin_ends():
        while os.read(0, 4096):
            pass
        _bye(0)

    def pump():
        o = proc.stdout.fileno()
        while True:
            data = os.read(o, 1 << 16)
            if not data:
                break
            send(b"D", data)
        proc.wait()
        time.sleep(0.2)
        say(f"ffmpeg ended ({proc.returncode}): {' / '.join(err[-3:]) or 'nothing said'}")
        _bye(1)

    q = queue.Queue(maxsize=FEED_QUEUE)

    def feed():
        i = proc.stdin.fileno()
        while True:
            planes, drawn, filled = q.get()
            send(b"F", FRAME.pack(drawn, filled))
            mv = memoryview(planes)
            try:
                while mv:
                    mv = mv[os.write(i, mv):]
            except OSError:
                return

    threading.Thread(target=stdin_ends, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=lambda: err.extend(proc.stderr.read().decode(
        "utf-8", "replace").splitlines()), daemon=True).start()
    threading.Thread(target=feed, daemon=True).start()

    def put(planes, drawn, filled):
        try:
            q.put_nowait((planes, drawn, filled))
        except queue.Full:
            send(b"B", FRAME.pack(drawn, filled))

    length = w * h * 3 // 2
    head = bytearray(HEADER.size)
    last = None
    while True:
        got = _fill(fd, memoryview(head), path)
        if got < HEADER.size:
            say("the glass stream ended" if got == 0 else f"a header cut short at {got} bytes")
            break
        magic, fw, fh, n, us, size = HEADER.unpack(head)
        if magic != b"BWG1" or size != fw * fh * 3 // 2:
            say(f"not a frame: {bytes(head[:4])!r} {fw}x{fh} {size} bytes")
            break
        planes = bytearray(size)
        if _fill(fd, memoryview(planes), path) < size:
            say("a frame cut short")
            break
        if (fw, fh) != (w, h) or size != length:
            continue
        if last is not None:
            gap = n - last[0] - 1
            if n <= last[0] or gap > fps:
                send(b"J")
            elif gap > 0:
                for j in range(1, gap + 1):
                    put(last[1], last[2] + int(j * 1e6 / fps), 1)
        put(planes, us, 0)
        last = (n, planes, us)
    _bye(1)


def probe(names):
    """Each encoder tried at once, side by side, since every try is
    mostly ffmpeg starting."""
    ok = {}

    def one(name):
        fmt = "yuvj420p" if name == "mjpeg" else "yuv420p"
        try:
            ok[name] = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-frames:v", "2",
                 "-pix_fmt", fmt, "-c:v", name, "-f", "null", "-"],
                capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok[name] = False
    threads = [threading.Thread(target=one, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in names:
        if ok.get(name):
            print(name, flush=True)
    return 0


def main(args):
    verb, rest = args[0], args[1:]
    if verb == "encode":
        return encode(rest[0], rest[1], rest[2], rest[3], rest[4:])
    if verb == "probe":
        return probe(rest)
    print(f"there: no verb {verb!r}", file=sys.stderr)
    return 2


# Carried (`python3 -c`), the mirror says what to run after this.
if __name__ == "__main__" and sys.argv[0] != "-c":
    sys.exit(main(sys.argv[1:]))
