"""The machine wall's picture: one grid of glyphs from one snapshot.

`frame(cols, rows, snap, theme)` lays out a `readings.Machine.snapshot()`
at whatever size it is given. It knows nothing of bitwall or terminals and
holds no state between calls, so it is correct at any size by construction.

THE LOOK
--------
A laboratory instrument panel in a single phosphor, after TMOG. Meters are
rows of segments with the unlit ones still faintly visible, the way a VFD
bar graph shows its whole scale; history is a sweep graph like a scope's,
the pen moving left to right and wiping one column ahead of itself.

The sweep rather than a scrolling trace is what keeps the wall calm. A
trace that scrolls changes every column every sample; a sweep changes two
cells a row -- the new column and the wiped one -- and everything else on
the panel sits still until its own reading changes.

WHAT IS NOT MADE UP
-------------------
A reading that could not be taken says so where the number would be, and
a meter with nothing to show has every segment unlit. A graph column whose
sample never arrived is left as the dim floor, not drawn through. A value
that is older than its clock should allow carries "stale" and its age, in
the alert colour, so a collector that has quietly stopped looks different
from a machine that is quietly idle. Zero is drawn only when zero was
measured, and then it lights the floor segment so it cannot be mistaken
for a gap.

DRAWING
-------
Meters are `▊`, a left seven-eighths block, one segment a cell: receivers
draw block elements as rectangles, so the eighth left unpainted is a clean
gap between segments. Graphs are the lower-eighths blocks, eight levels a
row in one colour. Half-blocks, quadrants and braille would give finer
grids, but a single-series bar graph has nothing to do with a second
colour per cell, and doubling the columns halves the width of each one --
this is read across a room, where a column a cell wide is the point.
"""
import collections
import math
import time

Glyph = collections.namedtuple("Glyph", "ch fg bg bold")

Theme = collections.namedtuple("Theme", "bg dim label ink bright")

#: bg is the panel; dim an unlit segment or gap floor; label the quiet
#: captions; ink ordinary text; bright a lit segment or a live number.
#: Alerts are drawn reversed, ink on bright, so every theme has one.
THEMES = {
    "green": Theme("020804", "0c3318", "3f9a5c", "8fe0a8", "4dff7f"),
    "amber": Theme("080400", "3a2400", "9a6a1c", "e0a850", "ffb000"),
    "blue":  Theme("020408", "0e2a4a", "3f7aa8", "8fc8ff", "5ac8ff"),
    "mono":  Theme("000000", "2e2e2e", "7a7a7a", "c8c8c8", "ffffff"),
    "dark":  Theme("0a0a0c", "2a2a30", "80808c", "d0d0d8", "ffd75a"),
}
DEFAULT_THEME = "green"

EIGHTHS = " ▁▂▃▄▅▆▇█"
SEGMENT = "▊"


def layout_for(cols, rows):
    """Which arrangement fits: full, compact, or tiny."""
    if cols >= 96 and rows >= 22:
        return "full"
    if cols >= 40 and rows >= 8:
        return "compact"
    return "tiny"


# ------------------------------------------------------------- format

def human(n, digits=1):
    """Bytes as 5.9G, 300K, 12M -- three characters of number at most."""
    if n is None:
        return "n/a"
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1000:
            if unit == "":
                return f"{int(n)}B"
            return f"{n:.{digits}f}{unit}" if n < 10 else f"{n:.0f}{unit}"
        n /= 1024.0
    return f"{n:.0f}E"


def rate(bps):
    return human(bps) + "/s"


def duration(secs):
    """8d 14h, 3h 12m, 45m, 30s."""
    secs = int(secs)
    if secs >= 86400:
        return f"{secs // 86400}d {secs % 86400 // 3600}h"
    if secs >= 3600:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    if secs >= 60:
        return f"{secs // 60}m"
    return f"{secs}s"


def age(secs):
    """Coarse, so it changes rarely: 40s, 12m, 4h."""
    secs = int(secs)
    if secs < 60:
        return f"{secs // 10 * 10}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def stale_after(period):
    """How old a reading may be before it is called stale.

    Three periods, and at least fifteen seconds past one, so a check that
    takes a while to answer is not called stale between ticks.
    """
    return max(3 * period, period + 15.0)


def status(reading, period, now):
    """(has_value, note) for a reading: note says stale or why absent."""
    if reading.value is None:
        return False, reading.error or "no reading"
    since = now - reading.at
    if since > stale_after(period):
        return True, f"stale {age(since)}"
    return True, None


def nice_scale(x):
    """The 1-2-5 step at or above x, so a graph's scale moves in jumps."""
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if m * mag >= x:
            return m * mag
    return 10 * mag


# ------------------------------------------------------------- canvas

class Canvas:
    def __init__(self, cols, rows, theme):
        self.cols, self.rows, self.t = cols, rows, theme
        blank = Glyph(" ", theme.ink, theme.bg, False)
        self.g = [[blank] * cols for _ in range(rows)]

    def put(self, x, y, text, fg=None, bg=None, bold=False):
        """Write text, clipped to the canvas; returns the x after it."""
        if y < 0 or y >= self.rows:
            return x
        fg = fg or self.t.ink
        bg = bg or self.t.bg
        row = self.g[y]
        for i, ch in enumerate(text):
            if 0 <= x + i < self.cols:
                row[x + i] = Glyph(ch, fg, bg, bold)
        return x + len(text)

    def alert(self, x, y, text):
        """Reversed -- the one loud thing a single phosphor can do."""
        return self.put(x, y, text, self.t.bg, self.t.bright, True)

    def label(self, x, y, text):
        return self.put(x, y, text, self.t.label)

    def bright(self, x, y, text):
        return self.put(x, y, text, self.t.bright, bold=True)

    def hline(self, y, x0, x1, ch="─"):
        for x in range(max(0, x0), min(self.cols, x1)):
            self.g[y][x] = Glyph(ch, self.t.label, self.t.bg, False)

    def meter(self, x, y, width, frac):
        """A segmented bar, width cells; frac None leaves it all unlit."""
        lit = 0 if frac is None else int(round(max(0.0, min(1.0, frac)) * width))
        for i in range(width):
            if 0 <= x + i < self.cols:
                self.g[y][x + i] = Glyph(
                    SEGMENT, self.t.bright if i < lit else self.t.dim,
                    self.t.bg, False)
        return x + width

    def sweep(self, x, y, width, height, samples, period, now, scale,
              pick=lambda v: v):
        """A scope sweep of `samples` over the last width*period seconds.

        Each column is one period; a sample lands in the column its time
        falls in, so a collector that stalled leaves columns with nothing
        in them. The current period's column and the one past it are
        wiped, which is the pen: it says which way time runs and where the
        newest sample is.

        A gap is the dim floor. A measured value lights at least the floor
        eighth, so zero and nothing look different.
        """
        if width <= 0 or height <= 0:
            return
        pen = int(now // period)
        oldest = pen - width + 2
        col = {}
        for t, v in samples:
            k = int(t // period)
            if oldest <= k < pen:
                col[k % width] = None if v is None else pick(v)
        levels = height * 8
        t = self.t
        for i in range(width):
            cx = x + i
            if cx < 0 or cx >= self.cols:
                continue
            v = col.get(i, None)
            wiped = i in (pen % width, (pen + 1) % width)
            if wiped or v is None:
                for r in range(height):
                    ch = "▁" if (r == height - 1 and not wiped) else " "
                    self.g[y + r][cx] = Glyph(ch, t.dim, t.bg, False)
                continue
            lv = max(1, min(levels, int(round(v / scale * levels)))) \
                if scale > 0 else 1
            for r in range(height):
                # Rows fill from the bottom; this row's share of the levels.
                below = (height - 1 - r) * 8
                n = max(0, min(8, lv - below))
                self.g[y + r][cx] = Glyph(EIGHTHS[n], t.bright, t.bg, False)


# -------------------------------------------------------------- frame

def frame(cols, rows, snap, theme=DEFAULT_THEME):
    """The whole wall at this size, as rows of Glyph."""
    t = THEMES.get(theme, THEMES[DEFAULT_THEME])
    c = Canvas(cols, rows, t)
    mode = layout_for(cols, rows)
    if mode == "full":
        _full(c, snap)
    elif mode == "compact":
        _compact(c, snap)
    else:
        _tiny(c, snap)
    return c.g


def _clock(snap):
    return time.strftime("%H:%M", time.localtime(snap["now"]))


def _pct(frac):
    return "n/a" if frac is None else f"{int(round(frac * 100)):3d}%"


def _tailscale_text(snap):
    """One line on the tailnet: (bright_part, rest, alert) or unavailable."""
    r = snap["tailscale"]
    has, note = status(r, snap["period"]["tailscale"], snap["now"])
    if not has:
        return None, note
    ts = r.value
    if ts["state"] != "Running":
        return None, f"tailscale {ts['state'].lower()}"
    text = (f"{ts['ip'] or '?'}  {ts['name'] or '?'}  "
            f"{ts['online']} of {ts['peers']} peers online")
    return text, note


def _services(snap):
    """[(state, label, detail)] with detail on down/unknown and staleness;
    nothing and no note where no services were asked for."""
    if not snap.get("services_asked", True):
        return [], None
    r = snap["services"]
    has, note = status(r, snap["period"]["services"], snap["now"])
    if not has:
        return [], note
    return [(state, label, detail) for label, state, detail in r.value], note


def _full(c, snap):
    t = c.t
    cols, rows = c.cols, c.rows
    now = snap["now"]
    iw = cols - 2                      # inside the border
    x0 = 2                             # left margin inside the frame

    # -- frame and section rules
    def rule(y, title, left="├", right="┤"):
        c.hline(y, 1, cols - 1)
        c.put(0, y, left, t.label)
        c.put(cols - 1, y, right, t.label)
        if title:
            c.put(2, y, f" {title} ", t.label, bold=True)

    rule(0, None, "┌", "┐")
    c.bright(2, 0, f" {snap['hostname'].upper()} ")
    clock = f" {_clock(snap)} "
    c.put(cols - 2 - len(clock), 0, clock, t.ink)
    for y in range(1, rows - 1):
        c.put(0, y, "│", t.label)
        c.put(cols - 1, y, "│", t.label)
    rule(rows - 1, None, "└", "┘")

    # -- services need their rows counted before the middle is sized
    services, snote = _services(snap)
    pills = []
    for state, label, detail in services:
        if state == "up":
            pills.append(("up", f"● {label}"))
        elif state == "down":
            pills.append(("down", f" DOWN {label} "))
        else:
            pills.append(("unknown", f"◌ {label}  {detail or 'not checked'}"))
    if not services and snote:
        pills.append(("unknown", f"◌ services not measured: {snote}"))
    svc_lines = _wrap_pills(pills, iw - 3) if pills else []
    # The rows the services take with their rule, none where there are none.
    svc_rows = len(svc_lines) + (1 if snote and services else 0) + 1 \
        if svc_lines else 0

    # -- identity: two lines
    y = 1
    up = snap["uptime"]
    x = c.put(x0, y, "up ", t.label)
    if up.value is None:
        x = c.label(x, y, f"n/a ({up.error})")
    else:
        x = c.put(x, y, duration(up.value), t.ink)
    addrs = snap["addresses"]
    x += 3
    if addrs.value is None:
        x = c.label(x, y, f"addresses n/a ({addrs.error})")
    elif not addrs.value:
        x = c.label(x, y, "no live interface")
    else:
        for name, ip, _v6, _note in addrs.value:
            if x + len(name) + len(ip) + 4 > cols - 2:
                break
            x = c.label(x, y, name + " ")
            x = c.put(x, y, ip, t.ink)
            x += 3
    y += 1
    v6_line = _v6_line(addrs.value, iw - 2)
    if v6_line is not None:
        x = c.label(x0, y, "ipv6 ")
        for name, text, is_addr in v6_line:
            if name:
                x = c.label(x, y, name + " ")
            x = (c.put(x, y, text, t.ink) if is_addr else c.label(x, y, text))
            x += 3
        y += 1
    ts_text, ts_note = _tailscale_text(snap)
    x = c.label(x0, y, "tailscale ")
    if ts_text is None:
        x = c.label(x, y, f"n/a  {ts_note}")
    else:
        x = c.put(x, y, ts_text, t.ink)
        if ts_note:
            c.alert(x + 2, y, f" {ts_note} ")
    y += 1

    # -- performance
    rule(y, "PERFORMANCE")
    y += 1
    perf_top = y
    perf_rows = rows - 1 - svc_rows - perf_top
    disks = snap["disk"].value or []
    ndisk = max(1, len(disks))
    fixed = 2 + ndisk                              # mem, load, disks
    # CPU and NET each get h rows of graph -- more rows are more levels --
    # and what is left over spaces the blocks apart. Past eight rows a graph
    # is taller than a glance needs, and the rest is left blank rather than
    # stretched into it.
    h = max(1, min(8, (perf_rows - fixed) // 2))
    spare = perf_rows - fixed - 2 * h
    gap = 2 if spare >= 6 else (1 if spare >= 3 else 0)

    label_w = 5
    left_w = min(56, int(iw * 0.42))
    meter_w = left_w - label_w - 7
    graph_x = x0 + left_w + 1
    graph_w = cols - 2 - graph_x

    # CPU
    cpu = snap["cpu"]
    has, note = status(cpu, snap["period"]["cpu"], now)
    c.label(x0, y, "CPU")
    c.bright(x0 + label_w, y, _pct(cpu.value if has else None))
    c.meter(x0 + label_w + 6, y, meter_w, cpu.value if has else None)
    if note:
        # Under the number when the graph gives a second row, beside the
        # meter's caption otherwise; reversed only when there is a value
        # that has gone stale, since "n/a" is a fact rather than an alarm.
        ny = y + 1 if h > 1 else y
        if has:
            c.alert(x0 + label_w, ny, f" {note} ")
        else:
            c.label(x0 + label_w + 6 + meter_w + 2, ny, note)
    c.sweep(graph_x, y, graph_w, h, snap.get("cpu_history", []),
            snap["period"]["cpu"], now, 1.0)
    if h > 1:
        c.label(x0 + label_w + 6, y + h - 1, "100% is every core busy")
    # Per-core tiles in the quiet space under the meter: row y is the
    # meter, y+1 is kept for the stale alert, the last row is the caption.
    _core_grid(c, snap, x0 + label_w, y + 2, graph_x - 1 - (x0 + label_w),
               h - 3)
    y += h + gap

    # MEM
    mem = snap["mem"]
    has, note = status(mem, snap["period"]["mem"], now)
    c.label(x0, y, "MEM")
    if has:
        used, total, sw_used, sw_total = mem.value
        c.bright(x0 + label_w, y, _pct(used / total if total else None))
        c.meter(x0 + label_w + 6, y, meter_w, used / total if total else None)
        x = c.put(graph_x, y, f"{human(used)} of {human(total)}", t.ink)
        if sw_total:
            x = c.label(x + 3, y, "swap ")
            x = c.put(x, y, f"{human(sw_used)} of {human(sw_total)}", t.ink)
        if note:
            c.alert(x + 2, y, f" {note} ")
    else:
        c.bright(x0 + label_w, y, "n/a")
        c.meter(x0 + label_w + 6, y, meter_w, None)
        c.label(graph_x, y, note)
    y += 1

    # LOAD
    ld = snap["load"]
    has, note = status(ld, snap["period"]["load"], now)
    c.label(x0, y, "LOAD")
    if has:
        l1, l5, l15, n = ld.value
        c.bright(x0 + label_w, y, f"{l1:4.1f}")
        c.meter(x0 + label_w + 6, y, meter_w, l1 / n)
        x = c.put(graph_x, y, f"{l5:.2f}  {l15:.2f}", t.ink)
        x = c.label(x + 2, y, f"5m 15m · full scale is {n} cores")
        if note:
            c.alert(x + 2, y, f" {note} ")
    else:
        c.bright(x0 + label_w, y, "n/a")
        c.meter(x0 + label_w + 6, y, meter_w, None)
        c.label(graph_x, y, note)
    y += 1 + gap

    # NET: rx and tx sweeps side by side, one scale for both.
    net = snap["net"]
    has, note = status(net, snap["period"]["net"], now)
    hist = snap.get("net_history", [])
    half = (graph_w - 1) // 2
    window = now - half * snap["period"]["net"]
    peak = max([max(v) for tm, v in hist if v is not None and tm >= window]
               or [0.0])
    scale = nice_scale(max(peak, 1024.0) / 1024.0) * 1024.0
    c.label(x0, y, "NET")
    if has:
        rx, tx = net.value
        x = c.label(x0 + label_w, y, "↓ ")
        x = c.bright(x, y, f"{rate(rx):>8}")
        x = c.label(x + 3, y, "↑ ")
        x = c.bright(x, y, f"{rate(tx):>8}")
        if note:
            c.alert(x + 2, y, f" {note} ")
    else:
        c.bright(x0 + label_w, y, "n/a")
        c.label(x0 + label_w + 6, y, note)
    if h > 1:
        c.label(x0 + label_w, y + h - 1, f"scale {rate(scale)}  ↓ left  ↑ right")
    c.sweep(graph_x, y, half, h, hist, snap["period"]["net"], now, scale,
            pick=lambda v: v[0])
    for r in range(h):
        c.put(graph_x + half, y + r, "│", t.dim)
    c.sweep(graph_x + half + 1, y, graph_w - half - 1, h, hist,
            snap["period"]["net"], now, scale, pick=lambda v: v[1])
    y += h + gap

    # DISK
    dk = snap["disk"]
    has, note = status(dk, snap["period"]["disk"], now)
    if not has:
        c.label(x0, y, "DISK")
        c.bright(x0 + label_w, y, "n/a")
        c.meter(x0 + label_w + 6, y, meter_w, None)
        c.label(graph_x, y, note)
        y += 1
    else:
        for i, (label, used, total, err) in enumerate(dk.value):
            if y >= rows - 1 - svc_rows:
                break
            c.label(x0, y, "DISK" if i == 0 else "")
            if used is None:
                c.bright(x0 + label_w, y, "n/a")
                c.meter(x0 + label_w + 6, y, meter_w, None)
                c.label(graph_x, y, f"{label}  {err}")
            else:
                c.bright(x0 + label_w, y, _pct(used / total if total else None))
                c.meter(x0 + label_w + 6, y, meter_w,
                        used / total if total else None)
                x = c.put(graph_x, y, label, t.ink, bold=True)
                x = c.label(x + 2, y, f"{human(used)} of {human(total)}")
                if note and i == 0:
                    c.alert(x + 2, y, f" {note} ")
            y += 1

    # -- services
    if not svc_lines:
        return
    y = rows - 1 - svc_rows
    rule(y, "SERVICES")
    y += 1
    for line in svc_lines:
        x = x0
        for kind, text in line:
            if kind == "up":
                x = c.bright(x, y, text)
            elif kind == "down":
                x = c.alert(x, y, text)
            else:
                x = c.label(x, y, text)
            x += 3
        y += 1
    if snote and services:
        c.alert(x0, y, f" services {snote} ")


CORE_SEGMENTS = 6          # readings.Cores.LEVELS; a tile is this wide
CORE_LABEL = 6             # "perf  ", "eff   ", "cores "


def _core_grid(c, snap, x, y, width, rows):
    """Per-core tiles under the CPU meter, if the real core count fits.

    One tile a logical core, six segments wide, in as many columns as the
    width allows; each kind of core starts its own row with the kind named
    at the left, so "perf" and "eff" read as two rows rather than eight
    tiles. Rows needed come from the actual core count and the columns that
    fit, and the grid is left out whole when they exceed the rows given.
    The tiles draw a quantised level, so a tile changes only when a core
    has moved a segment.
    """
    if rows < 1 or width < CORE_LABEL + CORE_SEGMENTS:
        return
    r = snap.get("cores")
    if r is None:
        return
    has, note = status(r, snap["period"]["cores"], snap["now"])
    if not has:
        c.label(x, y, f"cores n/a  {note}"[:width])
        return
    if note:
        c.alert(x, y, f" cores {note} ")
        return
    lit = r.value["lit"]
    groups = r.value["groups"] or [("cores", list(range(len(lit))))]
    fit = (width - CORE_LABEL + 1) // (CORE_SEGMENTS + 1)
    need = sum(-(-len(idx) // fit) for _label, idx in groups)
    if fit < 1 or need > rows:
        return
    t = c.t
    for label, idx in groups:
        for start in range(0, len(idx), fit):
            c.label(x, y, f"{label:<{CORE_LABEL}}" if start == 0 else "")
            tx = x + CORE_LABEL
            for i in idx[start:start + fit]:
                n = lit[i] if i < len(lit) else 0
                for s_ in range(CORE_SEGMENTS):
                    c.g[y][tx + s_] = Glyph(SEGMENT, t.bright if s_ < n else t.dim,
                                            t.bg, False)
                tx += CORE_SEGMENTS + 1
            y += 1


def _v6_line(addrs, width):
    """The IPv6 line as [(name, text, is_address)], or None to leave it out.

    Shown whole or not at all. A v6 address is long, and a line that is
    cut short would look like a rendering fault where it should look like
    a fact; so the threshold is whether every address fits at the same
    spacing as the v4 line above, and a wall too narrow for that shows
    only v4. A machine with no IPv6 is a real answer, and says so.
    """
    if not addrs:
        return None
    parts = [(name, v6, True) for name, _ip, v6, _note in addrs if v6]
    if not parts:
        notes = {note for _n, _i, _v, note in addrs if note}
        why = "none" + (" (link-local only)" if notes else "")
        parts = [("", why, False)]
    need = 5 + sum(len(n) + (1 if n else 0) + len(text) + 3
                   for n, text, _ in parts) - 3
    return parts if need <= width else None


def _wrap_pills(pills, width):
    """Pills into lines no wider than width, three cells between."""
    lines, line, x = [], [], 0
    for pill in pills:
        w = len(pill[1])
        if line and x + w > width:
            lines.append(line)
            line, x = [], 0
        line.append(pill)
        x += w + 3
    if line:
        lines.append(line)
    return lines or [[]]


def _compact(c, snap):
    """A narrow board: one reading a row, meters only, no frame."""
    t = c.t
    cols, rows = c.cols, c.rows
    now = snap["now"]
    y = 0
    c.bright(0, y, snap["hostname"])
    clock = _clock(snap)
    c.put(cols - len(clock), y, clock, t.ink)
    y += 1

    meter_w = max(4, cols - 10)

    def bar(label, frac, text, note, has):
        nonlocal y
        if y >= rows:
            return
        c.label(0, y, f"{label:<4}")
        c.bright(5, y, _pct(frac) if has else "n/a")
        c.meter(10, y, meter_w, frac if has else None)
        y += 1
        if note and y < rows:
            (c.alert if has else c.label)(5, y, f" {note} " if has else note)
            y += 1
        elif text and y < rows and rows >= 12:
            c.label(5, y, text)
            y += 1

    cpu = snap["cpu"]
    has, note = status(cpu, snap["period"]["cpu"], now)
    bar("CPU", cpu.value if has else None, None, note, has)

    mem = snap["mem"]
    has, note = status(mem, snap["period"]["mem"], now)
    if has:
        used, total = mem.value[0], mem.value[1]
        bar("MEM", used / total if total else None,
            f"{human(used)} of {human(total)}", note, True)
    else:
        bar("MEM", None, None, note, False)

    ld = snap["load"]
    has, note = status(ld, snap["period"]["load"], now)
    if y < rows:
        c.label(0, y, "LOAD")
        if has:
            l1, l5, l15, n = ld.value
            c.bright(5, y, f"{l1:4.1f}")
            c.meter(10, y, meter_w, l1 / n)
        else:
            c.bright(5, y, "n/a")
            c.meter(10, y, meter_w, None)
        y += 1

    net = snap["net"]
    has, note = status(net, snap["period"]["net"], now)
    if y < rows:
        c.label(0, y, "NET")
        if has:
            rx, tx = net.value
            x = c.label(5, y, "↓")
            x = c.bright(x, y, f"{rate(rx):>8}")
            x = c.label(x + 2, y, "↑")
            x = c.bright(x, y, f"{rate(tx):>8}")
            if note:
                c.alert(x + 2, y, f" {note} ")
        else:
            c.label(5, y, f"n/a  {note}")
        y += 1

    dk = snap["disk"]
    has, note = status(dk, snap["period"]["disk"], now)
    if has:
        for label, used, total, err in dk.value:
            if y >= rows - 2:
                break
            c.label(0, y, label[:4].upper())
            if used is None:
                c.bright(5, y, "n/a")
                c.meter(10, y, meter_w, None)
            else:
                c.bright(5, y, _pct(used / total if total else None))
                c.meter(10, y, meter_w, used / total if total else None)
            y += 1

    ts_text, ts_note = _tailscale_text(snap)
    if y < rows - 1:
        x = c.label(0, y, "TS  ")
        if ts_text is None:
            c.label(5, y, f"n/a  {ts_note}"[:cols - 5])
        else:
            ts = snap["tailscale"].value
            x = c.put(5, y, f"{ts['ip']}  {ts['online']}/{ts['peers']} peers",
                      t.ink)
            if ts_note:
                c.alert(x + 2, y, f" {ts_note} ")
        y += 1

    services, snote = _services(snap)
    pills = []
    for state, label, detail in services:
        if state == "up":
            pills.append(("up", f"● {label}"))
        elif state == "down":
            pills.append(("down", f" DOWN {label} "))
        else:
            pills.append(("unknown", f"◌ {label}"))
    if not services and snote:
        pills.append(("unknown", f"◌ services: {snote}"))
    for line in _wrap_pills(pills, cols):
        if y >= rows:
            break
        x = 0
        for kind, text in line:
            if kind == "up":
                x = c.bright(x, y, text)
            elif kind == "down":
                x = c.alert(x, y, text)
            else:
                x = c.label(x, y, text)
            x += 3
        y += 1
    if snote and services and y < rows:
        c.alert(0, y, f" services {snote} ")


def _tiny(c, snap):
    """Too small for anything but the name and two numbers."""
    now = snap["now"]
    cpu = snap["cpu"]
    mem = snap["mem"]
    has_c, _ = status(cpu, snap["period"]["cpu"], now)
    has_m, _ = status(mem, snap["period"]["mem"], now)
    m = mem.value[0] / mem.value[1] if has_m and mem.value[1] else None
    x = c.bright(0, 0, snap["hostname"][:c.cols])
    if c.rows > 1:
        x = c.label(0, 1, "cpu")
        x = c.bright(x, 1, _pct(cpu.value if has_c else None))
        x = c.label(x + 1, 1, "mem")
        c.bright(x, 1, _pct(m))
    if c.rows > 2:
        x = 0
        for label, state, detail in snap["services"].value or []:
            if state == "up":
                x = c.bright(x, 2, "●")
            elif state == "down":
                x = c.alert(x, 2, "▼")
            else:
                x = c.label(x, 2, "◌")
