#!/usr/bin/env python3
"""The machine a wall is on, drawn on that wall.

    ~/bitwall-src/senders/sysinfo/main.py localhost:0
    ~/bitwall-src/senders/sysinfo/main.py --font 12 localhost:1

Runs on the receiver's own machine and describes it: what the machine is,
what the bitwall painter on it is doing, and graphs of the last several
minutes of CPU, load, temperature and memory.

Everything is read from /proc, /sys and the wall's run directory with the
standard library alone, since a Pi has a plain python3 and nothing else.
The reading happens on its own thread, once a second, and never in
render(): render is called at the frame rate and only lays out the last
snapshot, so a slow read can never hold up a frame. Nothing is read while
the wall says nobody is watching, and the gap shows in the graphs as an
empty stretch rather than being closed up.

The layout is decided by the area the wall reports, every frame. A wide
wall has the machine and the receiver side by side over a two by two
block of graphs; a narrow one stacks them; a small one keeps one-line
sparklines and as many facts as fit. Graphs are solid octants where the
screen draws them, braille dots where it draws those, eighth blocks where
it draws neither, and ASCII as the last resort.
"""
import argparse
import collections
import glob
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time

BITWALL = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, BITWALL)

import bitwall                                         # noqa: E402
from bitwall import letters                            # noqa: E402

FONT = 20
HISTORY = 900             # seconds of samples kept, fifteen minutes
WINDOW = 300              # seconds a graph aims to cover
STATIC_EVERY = 30         # host facts that rarely change
PROCS_EVERY = 5           # the painter and the walls
RECEIVERS = ("bitwall-paint",)

# Colours, as truecolor; the wall reduces them if its screen needs it.
BG = "0b0f14"
PANEL = "121821"
FG = "d0d6de"
DIM = "6c7a89"
KEY = "8394a8"
HEAD_BG = "1d3b5c"
HEAD_FG = "ffffff"
#: The clock's unlit dots, a dot-matrix display's, a shade up from the bar.
CLOCK_DIM = "33608f"
ACCENT = "5fafd7"
GOOD = "5fd787"
WARN = "ffaf00"
BAD = "ff5f5f"
SERIES = {"cpu": "5fafff", "load": "d787ff", "temp": "ff8f3f", "mem": "4fd7af"}

BRAILLE_L = (0, 0x40, 0x44, 0x46, 0x47)      # left column, filled bottom up
BRAILLE_R = (0, 0x80, 0xA0, 0xB0, 0xB8)      # right column, filled bottom up
EIGHTHS = " ▁▂▃▄▅▆▇█"

#: The 2x4 patterns other characters draw, which the octants skip. Bit
#: 2r+c is row r from the top, column c, as the characters' names count.
NOT_OCTANTS = {0: " ", 1: "\U0001CEA8", 2: "\U0001CEAB", 3: "\U0001FB82", 5: "▘",
               10: "▝", 15: "▀", 20: "\U0001FBE6", 40: "\U0001FBE7",
               63: "\U0001FB85", 64: "\U0001CEA3", 80: "▖", 85: "▌", 90: "▞",
               95: "▛", 128: "\U0001CEA0", 160: "▗", 165: "▚", 170: "▐", 175: "▜",
               192: "▂", 240: "▄", 245: "▙", 250: "▟", 252: "▆", 255: "█"}


def octant(bits):
    """The one character that draws a cell's 2x4 pattern `bits`."""
    if bits in NOT_OCTANTS:
        return NOT_OCTANTS[bits]
    return chr(0x1CD00 + bits - sum(1 for b in NOT_OCTANTS if b < bits))


#: A pair of columns filled from the bottom, left `a` and right `b` of
#: four, as solid octant cells: OCTANT_UP[a * 5 + b].
OCTANT_UP = [octant(sum(1 << (2 * (3 - r)) for r in range(a))
                    | sum(2 << (2 * (3 - r)) for r in range(b)))
             for a in range(5) for b in range(5)]
ASCII = " .:-=+*#%@"


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} sysinfo {msg}", flush=True)


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


# ------------------------------------------------------------- reading

def fan():
    """The fan on the board's own header, as {rpm, power, level, levels},
    or None where there is none. The Pi 5's reports its speed from its
    tachometer, so a fan that is driven and not turning shows as one."""
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if read(d + "/name").strip() != "pwmfan":
            continue
        rpm, pwm = read(d + "/fan1_input").strip(), read(d + "/pwm1").strip()
        if not rpm.isdigit():
            return None
        out = {"rpm": int(rpm),
               "power": int(pwm) / 255 if pwm.isdigit() else None}
        for c in glob.glob("/sys/class/thermal/cooling_device*"):
            if read(c + "/type").strip() == "pwm-fan":
                cur, top = (read(c + f).strip()
                            for f in ("/cur_state", "/max_state"))
                if cur.isdigit() and top.isdigit():
                    out["level"], out["levels"] = int(cur), int(top)
        return out
    return None


def os_name():
    for line in read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return sys.platform


def addresses():
    """(interface, address) for every global address, IPv4 first."""
    out = []
    try:
        text = subprocess.run(["ip", "-o", "addr", "show", "scope", "global"],
                              capture_output=True, text=True,
                              timeout=5).stdout
        for line in text.splitlines():
            words = line.split()
            if len(words) > 3 and words[2] in ("inet", "inet6"):
                out.append((words[1], words[3].split("/")[0]))
    except (OSError, subprocess.SubprocessError):
        pass
    if not out:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                out.append(("", s.getsockname()[0]))
        except OSError:
            pass
    return sorted(out, key=lambda a: ":" in a[1])


#: The makers a monitor's EDID most often names, by its three-letter code.
MAKERS = {"ACR": "Acer", "AOC": "AOC", "APP": "Apple", "AUS": "Asus",
          "BNQ": "BenQ", "DEL": "Dell", "GSM": "LG", "HWP": "HP",
          "LEN": "Lenovo", "PHL": "Philips", "SAM": "Samsung",
          "SNY": "Sony", "VSC": "ViewSonic"}


def monitors():
    """Each connected monitor, as its EDID says: [(port, what)], the port
    as the kernel names its connector and what as maker, model, size
    across the diagonal, the mode it prefers and the year it was made."""
    out = []
    for c in sorted(glob.glob("/sys/class/drm/card*-*")):
        if read(c + "/status").strip() != "connected":
            continue
        try:
            with open(c + "/edid", "rb") as f:
                e = f.read()
        except OSError:
            continue
        if len(e) < 128:
            continue
        code = (e[8] << 8) | e[9]
        maker = "".join(chr(((code >> s) & 31) + 64) for s in (10, 5, 0))
        model = ""
        for i in range(54, 126, 18):
            d = e[i:i + 18]
            if d[0] == 0 and d[1] == 0 and d[3] == 0xFC:
                model = d[5:18].split(b"\n")[0].decode("ascii", "replace")
                model = model.strip()
        bits = [MAKERS.get(maker, maker), model]
        if e[21] and e[22]:
            bits.append(f'{(e[21] ** 2 + e[22] ** 2) ** 0.5 / 2.54:.0f}"')
        mode = read(c + "/modes").split("\n")[0].strip()
        if mode:
            bits.append(mode)
        if e[17]:
            bits.append(str(1990 + e[17]))
        port = os.path.basename(c).split("-", 1)[1].replace("-A-", "-")
        out.append((port, " ".join(b for b in bits if b)))
    return out


def meminfo():
    info = {}
    for line in read("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        words = rest.split()
        if words and words[0].isdigit():
            info[name] = int(words[0]) * 1024
    return info


def cpu_times():
    """{'cpu': (busy, total), 'cpu0': ...} from /proc/stat."""
    out = {}
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        words = line.split()
        nums = [int(n) for n in words[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums[:8])
        out[words[0]] = (total - idle, total)
    return out


def boot_time():
    for line in read("/proc/stat").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return None


def proc_ticks(pid):
    """utime + stime of a process, in clock ticks, or None."""
    text = read(f"/proc/{pid}/stat")
    if not text:
        return None
    fields = text.rsplit(")", 1)[-1].split()
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def proc_start(pid, btime):
    text = read(f"/proc/{pid}/stat")
    if not text or btime is None:
        return None
    try:
        ticks = int(text.rsplit(")", 1)[-1].split()[19])
    except (IndexError, ValueError):
        return None
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def processes():
    """(pid, argv) for every process we can read."""
    out = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if raw:
            argv = raw.rstrip(b"\0").decode("utf-8", "replace").split("\0")
            out.append((int(pid), argv))
    return out


def flags(argv):
    """An argv's `--name value` pairs, in order, repeats kept."""
    out, i = [], 0
    while i < len(argv):
        word = argv[i]
        if word.startswith("--"):
            name, eq, value = word[2:].partition("=")
            if not eq and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                value, i = argv[i + 1], i + 1
            out.append((name, value))
        i += 1
    return out


def walls_in(run):
    """Walls under a run directory: {name: path}, each `bitwall/<name>`
    with a door."""
    out = {}
    top = os.path.join(run, "bitwall")
    try:
        names = sorted(os.listdir(top))
    except OSError:
        return out
    for name in names:
        path = os.path.join(top, name)
        if os.path.exists(os.path.join(path, "door.in")):
            out[name] = path
    return out


# ----------------------------------------------------------- gathering

class Gatherer:
    """Reads the machine once a second and publishes a snapshot.

    `snap` is replaced whole, never changed in place, so render reads it
    without a lock and always sees one consistent moment.
    """

    def __init__(self, wall, stop):
        self.w = wall
        self.stop = stop
        self.hist = {k: collections.deque(maxlen=HISTORY) for k in SERIES}
        self.end = -1              # index of the newest sample
        self.cores = []
        self.snap = {"hist": {k: [] for k in SERIES}, "end": -1,
                     "static": {}, "procs": {}, "wall": {}, "now": {}}
        self.errors = set()
        self.heard = {}
        self.heard_at = 0.0
        self.style = None
        self.last_painter = None
        self.last_watching = None
        self.ncpu = os.cpu_count() or 1
        self.btime = boot_time()
        self.tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def trouble(self, what, e):
        key = f"{what}: {type(e).__name__}: {e}"
        if key not in self.errors:
            self.errors.add(key)
            log(f"could not read {key}")

    def run(self):
        tick = 0
        prev_cpu = prev_paint = prev_self = None
        paused_at = None
        static, procs = {}, {}
        painter_pid = None
        while not self.stop.is_set():
            started = time.monotonic()
            watching = self.w.watching()
            if watching != self.last_watching:
                if watching == 0:
                    log("nobody watching; not reading")
                elif self.last_watching == 0:
                    log(f"watching {watching}; reading again")
                self.last_watching = watching
            if watching == 0:
                paused_at = paused_at or time.monotonic()
                self.stop.wait(1.0)
                continue
            if paused_at is not None:
                gap = int(time.monotonic() - paused_at)
                for d in self.hist.values():
                    d.extend([None] * min(gap, HISTORY))
                self.end += gap
                paused_at = None
                prev_cpu = prev_paint = prev_self = None

            if self.style is None:
                self.style = self.choose_style()
            if tick % STATIC_EVERY == 0:
                try:
                    static = self.read_static()
                except Exception as e:                 # noqa: BLE001
                    self.trouble("host facts", e)
            if tick % PROCS_EVERY == 0:
                try:
                    procs = self.read_procs()
                    painter_pid = procs.get("painter", {}).get("pid")
                except Exception as e:                 # noqa: BLE001
                    self.trouble("processes", e)

            now = {}
            try:
                cpu = cpu_times()
                if prev_cpu:
                    per = []
                    for name, (busy, total) in cpu.items():
                        b0, t0 = prev_cpu.get(name, (busy, total))
                        pct = 100.0 * (busy - b0) / (total - t0) if total > t0 else 0.0
                        if name == "cpu":
                            now["cpu"] = pct
                        else:
                            per.append(pct)
                    now["cores"] = per
                prev_cpu = cpu
            except Exception as e:                     # noqa: BLE001
                self.trouble("/proc/stat", e)
            try:
                load = read("/proc/loadavg").split()
                if load:
                    now["loadavg"] = [float(x) for x in load[:3]]
                    now["load"] = now["loadavg"][0]
            except ValueError as e:
                self.trouble("/proc/loadavg", e)
            t = read("/sys/class/thermal/thermal_zone0/temp").strip()
            if t.lstrip("-").isdigit():
                now["temp"] = int(t) / 1000.0
            mem = meminfo()
            if mem.get("MemTotal"):
                avail = mem.get("MemAvailable", mem.get("MemFree", 0))
                now["mem"] = 100.0 * (mem["MemTotal"] - avail) / mem["MemTotal"]
                now["mem_used"] = mem["MemTotal"] - avail
                now["mem_total"] = mem["MemTotal"]
                now["swap_used"] = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
            up = read("/proc/uptime").split()
            if up:
                now["uptime"] = float(up[0])
            f = fan()
            if f:
                now["fan"] = f
            t_now = time.monotonic()
            ticks = proc_ticks(painter_pid) if painter_pid else None
            if ticks is not None and prev_paint and prev_paint[2] == painter_pid:
                dt = t_now - prev_paint[0]
                if dt > 0:
                    now["painter_cpu"] = 100.0 * (ticks - prev_paint[1]) / self.tck / dt
            prev_paint = (t_now, ticks, painter_pid) if ticks is not None else None
            mine = os.times()
            mine = mine.user + mine.system
            if prev_self:
                now["self_cpu"] = 100.0 * (mine - prev_self[1]) / (t_now - prev_self[0])
            prev_self = (t_now, mine)

            self.end += 1
            for k, d in self.hist.items():
                d.append(now.get(k))
            try:
                wall = self.read_wall()
            except Exception as e:                     # noqa: BLE001
                self.trouble("the wall", e)
                wall = self.snap["wall"]
            self.snap = {"hist": {k: list(d) for k, d in self.hist.items()},
                         "end": self.end, "static": static, "procs": procs,
                         "wall": wall, "now": now, "style": self.style,
                         "ncpu": self.ncpu}
            tick += 1
            self.stop.wait(max(0.05, 1.0 - (time.monotonic() - started)))

    def choose_style(self):
        """How graphs are drawn, from what the screen says it can draw."""
        if self.w.can(*OCTANT_UP):
            style = "octants"
        elif self.w.can("⠀⣿"):
            style = "braille"
        elif self.w.can(EIGHTHS[1:]):
            style = "blocks"
        else:
            style = "ascii"
        log(f"graphs in {style}")
        return style

    def read_static(self):
        u = os.uname()
        st = os.statvfs("/")
        return {"host": socket.gethostname().split(".")[0],
                "model": read("/proc/device-tree/model").rstrip("\0\n ")
                or u.machine,
                "os": os_name(),
                "kernel": f"{u.release} {u.machine}",
                "ips": addresses(),
                "monitors": monitors(),
                "disk": (st.f_blocks * st.f_frsize,
                         (st.f_blocks - st.f_bavail) * st.f_frsize),
                "python": sys.version.split()[0]}

    def read_procs(self):
        """The painter, bitwall-screen, the walls and the sessions on them."""
        out = {}
        run = os.path.dirname(os.path.dirname(self.w.run))
        for pid, argv in processes():
            base = os.path.basename(argv[0])
            if base in RECEIVERS and "painter" not in out:
                out["painter"] = {"pid": pid, "name": base,
                                  "flags": flags(argv[1:]),
                                  "since": proc_start(pid, self.btime)}
            elif any(os.path.basename(a) == "bitwall-screen" for a in argv[:2]):
                i = next(i for i, a in enumerate(argv)
                         if os.path.basename(a) == "bitwall-screen")
                out["screen"] = {"pid": pid, "args": argv[i + 1:]}
        walls = []
        for name, path in walls_in(run).items():
            try:
                sessions = sorted(os.listdir(os.path.join(path, "s")))
            except OSError:
                sessions = []
            walls.append({"name": name, "path": path, "sessions": sessions,
                          "this": name == self.w.wall})
        out["walls"] = walls
        out["run"] = run
        painter = out.get("painter")
        key = painter and (painter["pid"], painter["name"])
        if key != self.last_painter:
            if painter:
                log(f"{painter['name']} pid {painter['pid']} "
                    + " ".join(f"--{k} {v}".strip() for k, v in painter["flags"]
                               if k != "font-file"))
            else:
                log("no painter running")
            self.last_painter = key
        return out

    def read_wall(self):
        """What the wall has said to this sender, and what we have sent."""
        for when, way, text in self.w.traffic(since=self.heard_at):
            self.heard_at = max(self.heard_at, when)
            if way == "in" and text:
                name = text.split()[0]
                if name not in ("meta", "ping", "input"):
                    self.heard[name] = text
        return {"heard": dict(self.heard),
                "caps": self.w.capabilities(),
                "watching": self.w.watching(),
                "screen": self.w.screen(),
                "talking": self.w.talking,
                "frames": self.w.frames_sent,
                "bytes": self.w.bytes_sent}


# ------------------------------------------------------------ drawing

def lerp(a, b, t):
    """A colour between two hex colours."""
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


class Canvas:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        blank = bitwall.cell(" ", FG, BG)
        self.g = [[blank] * cols for _ in range(rows)]

    def put(self, x, y, text, fg=FG, bg=None, bold=False, width=None):
        """Text at x, y, clipped to the canvas and to `width`."""
        if not 0 <= y < self.rows:
            return x
        row = self.g[y]
        end = self.cols if width is None else min(self.cols, x + width)
        for ch in text:
            if x >= end:
                break
            if x >= 0:
                row[x] = bitwall.cell(ch, fg, row[x][2] if bg is None else bg,
                                      bold=bold)
            x += 1
        return x

    def fill(self, x, y, w, h, bg):
        blank = bitwall.cell(" ", FG, bg)
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.g[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                row[xx] = blank


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024.0


def span(secs):
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s" if m < 10 else f"{m}m"
    return f"{s}s"


def level(value, warn, bad):
    if value is None:
        return DIM
    return BAD if value >= bad else WARN if value >= warn else GOOD


def buckets(hist, end, npts, k):
    """npts points, each the mean of k samples, aligned to the sample
    index so a bucket keeps its samples as time moves on."""
    first = end - len(hist) + 1
    last = end // k
    out = []
    for b in range(last - npts + 1, last + 1):
        lo, hi = max(b * k, first), min(b * k + k, end + 1)
        vals = [v for v in hist[lo - first:hi - first] if v is not None] \
            if hi > lo else ()
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def scale(name, vals, ncpu):
    """The range a series is drawn against."""
    seen = [v for v in vals if v is not None]
    if name in ("cpu", "mem"):
        return 0.0, 100.0
    if name == "load":
        return 0.0, max(float(ncpu), max(seen, default=0.0) * 1.15)
    lo = math.floor(min(seen, default=40.0)) - 3
    hi = math.ceil(max(seen, default=50.0)) + 3
    return lo, max(hi, lo + 12)


def chart(cv, x, y, w, h, vals, lo, hi, colour, style):
    """An area chart of vals, one per dot column, in a w x h box.

    Only the filled cells are visited, from the bottom up, and each is
    looked up from a table made once per row, so a quiet graph costs
    almost nothing and a full one little more.
    """
    if w <= 0 or h <= 0:
        return
    top = lerp(colour, "ffffff", 0.25)
    bottom = lerp(colour, BG, 0.45)
    rng = (hi - lo) or 1.0
    paired = style in ("braille", "octants")
    ramp = EIGHTHS if style == "blocks" else ASCII
    steps = 4 if paired else len(ramp) - 1
    per = 2 if paired else 1
    dots = steps * h
    levels = []
    for v in vals[:w * per]:
        if v is None:
            levels.append(0)
        else:
            n = round((v - lo) / rng * dots)
            levels.append(dots if n > dots else n if n > 0 else 1 if v > lo else 0)
    levels += [0] * (w * per - len(levels))
    # tables[r][i]: the cell for row r from the bottom, i its fill code.
    rows, tables = [], []
    for r in range(h):
        yy = y + h - 1 - r
        rows.append(cv.g[yy] if 0 <= yy < cv.rows else None)
        fg = lerp(bottom, top, r / (h - 1)) if h > 1 else colour
        if style == "octants":
            tables.append([bitwall.cell(ch, fg, PANEL) for ch in OCTANT_UP])
        elif paired:
            tables.append([bitwall.cell(chr(0x2800 + (BRAILLE_L[i // 5]
                                                      | BRAILLE_R[i % 5])),
                                        fg, PANEL) for i in range(25)])
        else:
            tables.append([bitwall.cell(ch, fg, PANEL) for ch in ramp])
    for cx in range(w):
        xx = x + cx
        if not 0 <= xx < cv.cols:
            continue
        if paired:
            a, b = levels[2 * cx], levels[2 * cx + 1]
            fa, ra = divmod(a, 4)
            fb, rb = divmod(b, 4)
            for r in range(((a if a > b else b) + 3) // 4):
                row = rows[r]
                if row is not None:
                    sa = 4 if r < fa else ra if r == fa else 0
                    sb = 4 if r < fb else rb if r == fb else 0
                    row[xx] = tables[r][sa * 5 + sb]
        else:
            a = levels[cx]
            full, rest = divmod(a, steps)
            for r in range((a + steps - 1) // steps):
                row = rows[r]
                if row is not None:
                    row[xx] = tables[r][steps if r < full else rest]


GRAPHS = (("cpu", "CPU"), ("load", "load"), ("temp", "temp"), ("mem", "memory"))


def reading(name, v, snap):
    """A series' current value as text, and its colour."""
    ncpu = snap.get("ncpu", 1)
    if v is None:
        return "--", DIM
    if name == "cpu":
        return f"{v:.0f}%", level(v, 50, 85)
    if name == "load":
        return f"{v:.2f}", level(v / ncpu, 0.7, 1.0)
    if name == "temp":
        return f"{v:.1f}C", level(v, 65, 78)
    return f"{v:.0f}%", level(v, 75, 90)


def graph(cv, x, y, w, h, name, title, snap):
    """A titled chart: a title row, then h - 1 rows of chart."""
    style = snap.get("style") or "braille"
    per = 2 if style in ("braille", "octants") else 1
    npts = max(1, w * per)
    k = max(1, math.ceil(WINDOW / npts))
    hist = snap["hist"].get(name, [])
    vals = buckets(hist, snap["end"], npts, k)
    lo, hi = scale(name, vals, snap.get("ncpu", 1))
    colour = SERIES[name]
    now = hist[-1] if hist else None
    text, tone = reading(name, now, snap)
    seen = [v for v in vals if v is not None]
    cv.fill(x, y, w, h, PANEL)
    at = cv.put(x + 1, y, title, colour, bold=True, width=w - 1)
    at = cv.put(at + 1, y, text, tone, bold=True, width=x + w - at - 1)
    if seen:
        peak, _ = reading(name, max(seen), snap)
        right = f"peak {peak}  {span(npts * k)}"
        if name in ("load", "temp"):
            right = f"{lo:g}-{hi:.0f}  " + right
        if at + 2 + len(right) < x + w:
            cv.put(x + w - len(right) - 1, y, right, DIM)
    chart(cv, x, y + 1, w, h - 1, vals, lo, hi, colour, style)


def sparkline(cv, x, y, w, name, title, snap):
    """One row: a label, the value, and a chart of what fits."""
    hist = snap["hist"].get(name, [])
    now = hist[-1] if hist else None
    text, tone = reading(name, now, snap)
    lab = 7 if w >= 24 else 5
    cv.put(x, y, title[:lab - 1], SERIES[name], bold=True)
    cv.put(x + lab, y, f"{text:>6}", tone, bold=True)
    cw = w - lab - 7
    if cw < 2:
        return
    style = snap.get("style") or "braille"
    style = "blocks" if style in ("braille", "octants") else style
    k = max(1, math.ceil(WINDOW / cw))
    vals = buckets(hist, snap["end"], cw, k)
    lo, hi = scale(name, vals, snap.get("ncpu", 1))
    cv.fill(x + lab + 7, y, cw, 1, PANEL)
    chart(cv, x + lab + 7, y, cw, 1, vals, lo, hi, SERIES[name], style)


def bar(frac, width, style):
    """A horizontal bar as text, for a fraction."""
    if width <= 0:
        return ""
    frac = max(0.0, min(1.0, frac))
    if style == "ascii":
        n = round(frac * width)
        return "#" * n + "." * (width - n)
    full = int(frac * width)
    rest = int((frac * width - full) * 8)
    part = "█" * full + (EIGHTHS[rest] if rest and full < width else "")
    return part + " " * (width - len(part))


def machine_lines(snap):
    """(key, value, colour) facts about the machine."""
    st, now = snap["static"], snap["now"]
    style = snap.get("style") or "braille"
    out = []
    if not st and not now:
        return [("", "reading...", DIM)]
    out.append(("host", st.get("host", "?"), FG))
    out.append(("model", st.get("model", "?"), FG))
    out.append(("os", st.get("os", "?"), FG))
    out.append(("kernel", st.get("kernel", "?"), FG))
    for iface, addr in st.get("ips", []) or [("", "no address")]:
        out.append(("ip", f"{iface}  {addr}".strip(), FG))
    for port, what in st.get("monitors", []):
        out.append((port, what, FG))
    if "uptime" in now:
        out.append(("uptime", span(now["uptime"]), FG))
    cores = now.get("cores") or []
    ramp = EIGHTHS if style != "ascii" else ASCII
    spark = "".join(ramp[max(1, min(len(ramp) - 1,
                                    round(c / 100 * (len(ramp) - 1))))]
                    for c in cores)
    la = now.get("loadavg")
    out.append(("cpu", f"{snap.get('ncpu', '?')} cores {spark}"
                + (f"  load {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}" if la else ""),
                FG))
    if now.get("mem_total"):
        out.append(("memory", f"{human(now['mem_used'])} of "
                    f"{human(now['mem_total'])} ({now['mem']:.0f}%)"
                    + (f", swap {human(now['swap_used'])}"
                       if now.get("swap_used") else ""),
                    level(now["mem"], 75, 90)))
    if st.get("disk"):
        total, used = st["disk"]
        pct = 100.0 * used / total if total else 0
        out.append(("disk /", f"{human(used)} of {human(total)} ({pct:.0f}%) "
                    + bar(pct / 100, 10, style), level(pct, 80, 92)))
    if "temp" in now:
        out.append(("temp", f"{now['temp']:.1f}C", level(now["temp"], 65, 78)))
    f = now.get("fan")
    if f:
        stage = (f"  level {f['level']}/{f['levels']}"
                 if "levels" in f else "")
        if f["power"] and not f["rpm"]:
            out.append(("fan", "stopped while driven" + stage, BAD))
        elif not f["rpm"]:
            out.append(("fan", "off" + stage, DIM))
        else:
            power = ("" if f["power"] is None
                     else f"  {f['power'] * 100:.0f}%")
            out.append(("fan", f"{f['rpm']} rpm{power}{stage}", FG))
    out.append(("python", st.get("python", "?"), DIM))
    return out


def wall_lines(snap, target):
    """(key, value, colour) facts about the receiver and this wall."""
    procs, wall, now = snap["procs"], snap["wall"], snap["now"]
    out = []
    p = procs.get("painter")
    if p:
        since = p.get("since")
        when = ""
        if since:
            fmt = "%H:%M" if time.time() - since < 86400 else "%d %b %H:%M"
            when = (f", up {span(time.time() - since)} since "
                    f"{time.strftime(fmt, time.localtime(since))}")
        name = "" if p["name"] == "bitwall-paint" else p["name"] + " "
        out.append(("painter", f"{name}pid {p['pid']}{when}", GOOD))
        simple = [f"{k} {v}".strip() for k, v in p["flags"]
                  if k not in ("presence", "run", "font-file")]
        out.append(("flags", ", ".join(simple) or "none", FG))
        fonts = [os.path.basename(v) for k, v in p["flags"] if k == "font-file"]
        if fonts:
            out.append(("font", fonts[0], DIM))
        pres = [v for k, v in p["flags"] if k == "presence"]
        out.append(("presence", " ".join(pres) or "none", FG if pres else DIM))
    elif procs:
        out.append(("painter", "not running", BAD))
    scr = procs.get("screen")
    if scr:
        out.append(("screen", " ".join(scr["args"]) or "no arguments", FG))
    for wl in procs.get("walls", []):
        bits = ["this wall"] if wl["this"] else []
        bits.append("sessions " + " ".join(wl["sessions"]) if wl["sessions"]
                    else "no sessions")
        out.append((f"wall {wl['name']}", ", ".join(bits),
                    ACCENT if wl["this"] else FG))
    heard = wall.get("heard", {})
    area = heard.get("area", "").split()
    if len(area) >= 2:
        out.append(("area", f"{area[1]} cells at font {area[2] if len(area) > 2 else '?'}"
                    f"  ({target})", FG))
    sc = wall.get("screen") or {}
    if sc:
        rate = f", {sc['rate']} Hz" if sc.get("rate") else ""
        out.append(("colour", f"{sc.get('colour', '?')}{rate}, "
                    f"{len(sc.get('glyphs', ()))} glyph ranges", FG))
    state = [heard.get(k) for k in ("display", "power")]
    w_n = wall.get("watching")
    state.append(f"watching {w_n}" if w_n is not None else None)
    state = [s for s in state if s]
    if state:
        out.append(("state", ", ".join(state), FG))
    caps = wall.get("caps") or {}
    out.append(("accepts", " ".join(sorted(caps)) or "no input", FG if caps else DIM))
    if "caps" in heard:
        out.append(("caps", heard["caps"].split(None, 1)[-1], DIM))
    if wall:
        out.append(("sent", f"{wall.get('frames', 0)} frames, "
                    f"{human(wall.get('bytes', 0))}"
                    + ("" if wall.get("talking") else ", wall not talking yet"),
                    DIM))
    cpu = []
    if "painter_cpu" in now:
        cpu.append(f"painter {now['painter_cpu']:.1f}%")
    if "self_cpu" in now:
        cpu.append(f"sysinfo {now['self_cpu']:.1f}%")
    if cpu:
        out.append(("cpu use", "  ".join(cpu), DIM))
    return out


def section(cv, x, y, w, h, title, lines, kw):
    """A titled block of key/value lines. Returns the rows it used."""
    if h <= 0 or w <= 0:
        return 0
    at = cv.put(x, y, title, ACCENT, bold=True, width=w)
    cv.put(at + 1, y, "─" * max(0, x + w - at - 1), "243447")
    used = 1
    for key, value, colour in lines:
        if used >= h:
            break
        cv.put(x, y + used, key[:kw], KEY, width=kw)
        cv.put(x + kw + 1, y + used, str(value), colour, width=w - kw - 1)
        used += 1
    return used


class View:
    """The picture, laid out afresh for whatever area it is given."""

    def __init__(self, gatherer, target, dots=False, cuts=False):
        self.g = gatherer
        self.target = target
        #: Whether the screen draws braille, and so the host's name can be
        #: set in letters twice the height of the text round it.
        self.dots = dots
        #: Whether it draws the diagonal halves, and so the header's band
        #: can have its corners cut.
        self.cuts = cuts

    def corners(self, cv, cols, tall):
        """The header band's four corners cut on the diagonal: each corner
        cell the half of the band that stays, over the page behind. A cell
        is twice as tall as wide, so the cut is a steep one."""
        if not self.cuts or cols < 4:
            return
        cut = [(0, 0, "◢"), (cols - 1, 0, "◣")]
        # A band one row deep has one cell at each end, which can lose
        # only one of its corners: the top ones, so it sits on the page.
        if tall > 1:
            cut += [(0, tall - 1, "◥"), (cols - 1, tall - 1, "◤")]
        for x, y, ch in cut:
            cv.g[y][x] = bitwall.cell(ch, HEAD_BG, BG)

    def header(self, cv, cols, rows, host, model, up, clock):
        """The machine's name, what it is, and the time, across the top.

        The name and the time in braille letters two rows high where the
        screen draws braille and the wall has the room, so they are the
        first things read -- the time's digits all one width, so it does not
        shift as it runs; otherwise one row of text. Returns the rows it
        took.
        """
        big = None
        if self.dots and rows >= 16:
            big = letters.size(host, letters.DOTS)
            tick = letters.size(clock, letters.DOTS, spacing="mono",
                                ghost=CLOCK_DIM)
            if big[0] + tick[0] + 6 > cols:
                big = None
        if big is None:
            cv.fill(0, 0, cols, 1, HEAD_BG)
            head = f" {host}"
            if cols >= 50:
                head += f"   {model}"
            if up is not None and cols >= 80:
                head += f"   up {span(up)}"
            cv.put(0, 0, head, HEAD_FG, bold=True, width=cols - len(clock) - 2)
            if cols > len(clock) + 4:
                cv.put(cols - len(clock) - 1, 0, clock, HEAD_FG)
            self.corners(cv, cols, 1)
            return 1
        wide, tall = big
        cv.fill(0, 0, cols, tall, HEAD_BG)
        letters.draw(cv.g, host, 1, 0, HEAD_FG, font=letters.DOTS)
        room = cols - wide - tick[0] - 6
        cv.put(wide + 3, 0, model, HEAD_FG, width=room)
        if up is not None:
            cv.put(wide + 3, 1, f"up {span(up)}", KEY, width=room)
        letters.draw(cv.g, clock, cols - tick[0] - 1, 0, HEAD_FG,
                     font=letters.DOTS, spacing="mono", ghost=CLOCK_DIM)
        self.corners(cv, cols, tall)
        return tall

    @staticmethod
    def layout_for(cols, rows):
        if cols >= 90 and rows >= 24:
            return "wide"
        if rows >= 16 and cols >= 24:
            return "stacked"
        return "small"

    def __call__(self, cols, rows):
        cv = Canvas(cols, rows)
        snap = self.g.snap
        st = snap["static"]
        # The header: the machine, and the time.
        clock = time.strftime("%H:%M:%S")
        up = snap["now"].get("uptime")
        host = st.get("host", "sysinfo")
        top = self.header(cv, cols, rows, host, st.get("model", ""), up, clock)
        if rows < top + 1:
            return cv.g

        mach = machine_lines(snap)
        recv = wall_lines(snap, self.target)
        layout = self.layout_for(cols, rows)
        body = rows - top
        if layout == "wide":
            half = (cols - 3) // 2
            kw = 9
            info_h = 1 + max(len(mach), len(recv))
            # Graphs want at least two rows of four each; the facts give
            # way to them when rows are short.
            info_h = min(info_h, body - 2 * 5 - 1)
            section(cv, 1, top, half, info_h, "MACHINE", mach, kw)
            section(cv, half + 2, top, cols - half - 3, info_h, "RECEIVER",
                    recv, kw)
            gy = top + info_h + 1
            gh = rows - gy
            gw = (cols - 3) // 2
            h1 = (gh - 1) // 2
            spots = ((1, gy, gw, h1), (gw + 2, gy, cols - gw - 3, h1),
                     (1, gy + h1 + 1, gw, gh - h1 - 1),
                     (gw + 2, gy + h1 + 1, cols - gw - 3, gh - h1 - 1))
            for (name, title), (x, y, w, h) in zip(GRAPHS, spots):
                graph(cv, x, y, w, h, name, title, snap)
            return cv.g

        kw = 8 if cols >= 40 else 6
        width = cols - 2
        if layout == "small":
            # Sparklines first, then what facts fit.
            y = top
            for name, title in GRAPHS:
                if y >= rows:
                    return cv.g
                sparkline(cv, 0, y, cols, name, title, snap)
                y += 1
            for key, value, colour in mach[:1] + recv[:1] + mach[1:]:
                if y >= rows:
                    break
                cv.put(0, y, key[:kw], KEY, width=kw)
                cv.put(kw + 1, y, str(value), colour, width=cols - kw - 1)
                y += 1
            return cv.g

        # Stacked: full graphs if there is room for them beside every
        # fact, otherwise sparklines and the facts that fit.
        need = 2 + len(mach) + 1 + len(recv)
        spare = body - need - 1
        if spare >= 4 * 4:
            gh = spare // 4
            y = top
            for name, title in GRAPHS:
                graph(cv, 1, y, width, gh, name, title, snap)
                y += gh
            y += 1
        else:
            y = top
            for name, title in GRAPHS:
                sparkline(cv, 1, y, width, name, title, snap)
                y += 1
            y += 1
        # Where both will not fit, each has half of what is left.
        room = rows - y
        top = room
        if 1 + len(mach) + 2 + len(recv) > room:
            top = max(room - 2 - len(recv), (room + 1) // 2)
        y += section(cv, 1, y, width, top, "MACHINE", mach, kw)
        if rows - y - 1 >= 2:
            section(cv, 1, y + 1, width, rows - y - 1, "RECEIVER", recv, kw)
        return cv.g


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", type=int, default=FONT,
                    help="receiver font size (default %(default)s)")
    ap.add_argument("target", nargs="?", default="localhost",
                    help="the wall to draw on, host:wall (default %(default)s)")
    args = ap.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    try:
        w = bitwall.attach(args.target, min_frame=1.0)
    except Exception as e:                             # noqa: BLE001
        log(f"could not attach to {args.target}: {e}")
        return 1
    gatherer = Gatherer(w, stop)
    dots = letters.usable(letters.DOTS, w)
    view = View(gatherer, args.target, dots, cuts=w.can("◢◣◤◥"))
    with w.transition():
        w.set_font_size(args.font)
        w.set_source(view)
    w.meta(name="sysinfo",
           description="This machine: host, receiver and walls, "
                       "with graphs of CPU, load, temperature and memory")
    cols, rows = w.area()
    log(f"attached to {args.target} ({w.run}), font {args.font}, "
        f"{cols}x{rows}, {View.layout_for(cols, rows)} layout, "
        f"name in {'braille letters' if dots else 'text'}")
    threading.Thread(target=gatherer.run, daemon=True).start()

    area = (cols, rows)
    while not stop.wait(1.0):
        now = w.area()
        if now != area:
            log(f"resized {area[0]}x{area[1]} -> {now[0]}x{now[1]}, "
                f"{View.layout_for(*now)} layout")
            area = now
    log("stopping")
    w.detach()
    log("detached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
