"""What is measured about this machine, and on what clock.

Every number on the wall comes from a `Clock`: a probe, a period, and a
thread that runs the probe on that period and keeps the result as a
`Reading`. A reading carries the time it was taken and, if the probe
failed, why. Nothing here fills a gap with a guess: a probe that fails
leaves the last good value in place with its original timestamp and the
error alongside, so the wall can show a number, say how old it is, and
say what stopped it -- rather than a zero it did not measure.

Clocks run at different rates because the things they measure change at
different rates. CPU and network are read every second; memory and load
every few seconds; disk space, addresses and uptime once a minute;
tailscale and the service checks, which run other programs, every twenty
to thirty seconds.

Clocks that keep history record every tick, failures included, as
`(time, value_or_None)`. The panel draws a `None` as a gap.

Sources, in order of preference: mach and sysctl on macOS, /proc on
Linux, and a reported error anywhere else. Standard library only, so the
systems rotator can import this into its own process.
"""
import collections
import ctypes
import ctypes.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

Reading = collections.namedtuple("Reading", "value at error")
NOTHING = Reading(None, 0.0, "not read yet")


class Warming(Exception):
    """A probe that needs two samples to say anything has taken one."""


def _run(cmd, timeout=5.0):
    """A command's stdout, or a RuntimeError naming what went wrong."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]}: no answer in {timeout:.0f}s")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: exit {p.returncode}")
    return p.stdout


# ---------------------------------------------------------------- cpu

class Cpu:
    """Busy fraction of all cores since the previous call, 0..1.

    Cumulative tick counters differenced between calls, which is how
    every monitor measures CPU: an instantaneous figure does not exist.
    The first call has nothing to difference against and says so.
    """

    def __init__(self):
        self._prev = None
        self._lib = None
        self._host = None

    def _ticks(self):
        if sys.platform == "darwin":
            return self._mach_ticks()
        if os.path.exists("/proc/stat"):
            with open("/proc/stat") as f:
                parts = f.readline().split()
            if parts and parts[0] == "cpu":
                nums = [int(x) for x in parts[1:]]
                idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                return sum(nums), idle
            raise RuntimeError("/proc/stat: no cpu line")
        raise RuntimeError(f"no cpu source on {platform.system()}")

    def _mach_ticks(self):
        # host_statistics(HOST_CPU_LOAD_INFO) is what top and Activity
        # Monitor read. Four uint32 tick counters: user, system, idle, nice.
        if self._lib is None:
            self._lib = ctypes.CDLL(ctypes.util.find_library("c"))
            self._host = self._lib.mach_host_self()
        counts = (ctypes.c_uint32 * 4)()
        n = ctypes.c_uint32(4)
        kr = self._lib.host_statistics(self._host, 3, ctypes.byref(counts),
                                       ctypes.byref(n))
        if kr != 0:
            raise RuntimeError(f"host_statistics: kern_return {kr}")
        user, system, idle, nice = counts
        return user + system + idle + nice, idle

    def __call__(self):
        total, idle = self._ticks()
        prev, self._prev = self._prev, (total, idle)
        if prev is None:
            raise Warming("first sample")
        dt = total - prev[0]
        if dt <= 0:
            raise Warming("no ticks since last sample")
        return max(0.0, min(1.0, 1.0 - (idle - prev[1]) / dt))


def core_groups():
    """Cores by kind, as [(label, [cpu indices])], or None when unknowable.

    macOS says how many logical cpus sit at each performance level, and on
    Apple silicon the indices run efficiency cluster first: powermetrics
    lists the E-cluster as cpus 0-3 and the P-cluster as 4-7 on an M1. So
    the first perflevel1 cpus are the efficiency cores and the rest are
    performance. Linux exposes a capacity per cpu for big.LITTLE parts, and
    the largest capacity is the performance kind. One level, or no hint at
    all, is None: the cores are then shown as a single kind rather than
    guessed at.
    """
    if sys.platform == "darwin":
        try:
            levels = int(_run(["sysctl", "-n", "hw.nperflevels"]).strip())
            if levels < 2:
                return None
            n_p = int(_run(["sysctl", "-n", "hw.perflevel0.logicalcpu"]).strip())
            n_e = int(_run(["sysctl", "-n", "hw.perflevel1.logicalcpu"]).strip())
        except (RuntimeError, ValueError):
            return None
        return [("perf", list(range(n_e, n_e + n_p))), ("eff", list(range(n_e)))]
    base = "/sys/devices/system/cpu"
    if not os.path.isdir(base):
        return None
    cap = {}
    for name in os.listdir(base):
        if not (name.startswith("cpu") and name[3:].isdigit()):
            continue
        i = int(name[3:])
        for rel in ("cpu_capacity", "cpufreq/cpuinfo_max_freq"):
            try:
                with open(os.path.join(base, name, rel)) as f:
                    cap[i] = int(f.read().strip())
                break
            except (OSError, ValueError):
                continue
    kinds = sorted(set(cap.values()), reverse=True)
    if len(kinds) < 2:
        return None
    labels = ["perf", "eff"] if len(kinds) == 2 else [f"lvl{k}" for k in range(len(kinds))]
    return [(labels[k], sorted(i for i, c in cap.items() if c == kinds[k]))
            for k in range(len(kinds))]


class Cores:
    """Busy fraction of each logical core, and a quantised level for a tile.

    The same tick difference as `Cpu`, per core, from host_processor_info
    on macOS and the cpuN lines of /proc/stat on Linux. Each core also
    carries `lit`, its busy fraction in LEVELS steps with hysteresis: the
    step changes only once the fraction is a fifth of a step past the
    boundary, so a core hovering on one does not flick a segment on and
    off every sample. That is what lets eight tiles sit as still as one
    bar.

    Returns {"busy": [fraction...], "lit": [0..LEVELS...], "groups": ...}
    where groups is `core_groups()`, read once.
    """

    LEVELS = 6
    SLACK = 0.2

    def __init__(self):
        self._prev = None
        self._lit = None
        self._lib = None
        self._host = None
        self.groups = None
        self._grouped = False

    def _ticks(self):
        if sys.platform == "darwin":
            return self._mach_ticks()
        if os.path.exists("/proc/stat"):
            out = []
            with open("/proc/stat") as f:
                for line in f:
                    p = line.split()
                    if p and p[0].startswith("cpu") and p[0] != "cpu":
                        nums = [int(x) for x in p[1:]]
                        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                        out.append((sum(nums), idle))
            if not out:
                raise RuntimeError("/proc/stat: no cpuN lines")
            return out
        raise RuntimeError(f"no per-core source on {platform.system()}")

    def _mach_ticks(self):
        # PROCESSOR_CPU_LOAD_INFO hands back four uint32 tick counters per
        # cpu -- user, system, idle, nice -- in memory this task has to
        # give back with vm_deallocate, or a second a sample leaks forever.
        if self._lib is None:
            self._lib = ctypes.CDLL(ctypes.util.find_library("c"))
            self._host = self._lib.mach_host_self()
        count = ctypes.c_uint32()
        info = ctypes.POINTER(ctypes.c_uint32)()
        n = ctypes.c_uint32()
        kr = self._lib.host_processor_info(self._host, 2, ctypes.byref(count),
                                           ctypes.byref(info), ctypes.byref(n))
        if kr != 0:
            raise RuntimeError(f"host_processor_info: kern_return {kr}")
        try:
            return [(sum(info[i * 4:(i + 1) * 4]), info[i * 4 + 2])
                    for i in range(count.value)]
        finally:
            self._lib.vm_deallocate(self._lib.mach_task_self(),
                                    ctypes.cast(info, ctypes.c_void_p),
                                    n.value * 4)

    def __call__(self):
        if not self._grouped:
            self.groups = core_groups()
            self._grouped = True
        ticks = self._ticks()
        prev, self._prev = self._prev, ticks
        if prev is None or len(prev) != len(ticks):
            raise Warming("first sample")
        busy = []
        for (t1, i1), (t0, i0) in zip(ticks, prev):
            dt = t1 - t0
            busy.append(max(0.0, min(1.0, 1.0 - (i1 - i0) / dt)) if dt > 0 else 0.0)
        if self._lit is None or len(self._lit) != len(busy):
            self._lit = [int(round(b * self.LEVELS)) for b in busy]
        else:
            for i, b in enumerate(busy):
                exact = b * self.LEVELS
                if abs(exact - self._lit[i]) > 0.5 + self.SLACK:
                    self._lit[i] = int(round(exact))
        return {"busy": busy, "lit": list(self._lit), "groups": self.groups}


# ------------------------------------------------------------- memory

def memory():
    """Physical memory as (used, total, swap_used, swap_total) in bytes.

    On macOS "used" is what Activity Monitor calls it: active, wired and
    compressed pages. Inactive and speculative pages are cache the kernel
    will hand back, and counting them makes every Mac read 99% forever.
    """
    if sys.platform == "darwin":
        total = int(_run(["sysctl", "-n", "hw.memsize"]).strip())
        out = _run(["vm_stat"])
        m = re.search(r"page size of (\d+)", out)
        page = int(m.group(1)) if m else 4096
        pages = {}
        for line in out.splitlines():
            key, _, val = line.partition(":")
            val = val.strip().rstrip(".")
            if val.isdigit():
                pages[key.strip().lower()] = int(val)
        try:
            used = (pages["pages active"] + pages["pages wired down"]
                    + pages["pages occupied by compressor"]) * page
        except KeyError as e:
            raise RuntimeError(f"vm_stat: no {e.args[0]!r}")
        swap = _run(["sysctl", "-n", "vm.swapusage"])
        nums = re.findall(r"(\w+) = ([\d.]+)([KMG])", swap)
        scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
        sw = {k: float(v) * scale[u] for k, v, u in nums}
        return used, total, sw.get("used", 0.0), sw.get("total", 0.0)
    if os.path.exists("/proc/meminfo"):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, val = line.partition(":")
                info[key] = int(val.split()[0]) * 1024
        try:
            total = info["MemTotal"]
            used = total - info["MemAvailable"]
            return (used, total, info["SwapTotal"] - info["SwapFree"],
                    info["SwapTotal"])
        except KeyError as e:
            raise RuntimeError(f"/proc/meminfo: no {e.args[0]}")
    raise RuntimeError(f"no memory source on {platform.system()}")


# ------------------------------------------------------------ network

class Net:
    """Bytes per second in and out, summed over the physical interfaces.

    Physical only -- `en*` on macOS, everything but loopback on Linux --
    because a tunnel's traffic also crosses the interface that carries it
    and would be counted twice.
    """

    def __init__(self):
        self._prev = None

    def _counters(self):
        rx = tx = 0
        if sys.platform == "darwin":
            for line in _run(["netstat", "-ibn"]).splitlines():
                f = line.split()
                if len(f) < 10 or "<Link#" not in line:
                    continue
                if not f[0].startswith("en"):
                    continue
                rx += int(f[-5])
                tx += int(f[-2])
            return rx, tx
        if os.path.exists("/proc/net/dev"):
            with open("/proc/net/dev") as fh:
                for line in fh:
                    name, _, rest = line.partition(":")
                    if not rest or name.strip() == "lo":
                        continue
                    f = rest.split()
                    rx += int(f[0])
                    tx += int(f[8])
            return rx, tx
        raise RuntimeError(f"no network source on {platform.system()}")

    def __call__(self):
        now = time.monotonic()
        rx, tx = self._counters()
        prev, self._prev = self._prev, (now, rx, tx)
        if prev is None:
            raise Warming("first sample")
        dt = now - prev[0]
        if dt <= 0:
            raise Warming("no time since last sample")
        return max(0.0, (rx - prev[1]) / dt), max(0.0, (tx - prev[2]) / dt)


# --------------------------------------------------------------- disk

def disks(paths):
    """Each path as (label, used, total) in bytes; a missing one as an error.

    A list rather than one reading per disk so an unmounted volume shows
    up as "not mounted" in its place, which is a fact worth a line.
    """
    out = []
    for label, path in paths:
        try:
            total, _, free = shutil.disk_usage(path)
        except OSError as e:
            out.append((label, None, None, e.strerror or str(e)))
            continue
        out.append((label, total - free, total, None))
    return out


# --------------------------------------------------------------- load

def load():
    """The three load averages and the core count they are against."""
    return os.getloadavg() + (os.cpu_count() or 1,)


# ----------------------------------------------------------- identity

def uptime():
    """Seconds since boot."""
    if sys.platform == "darwin":
        out = _run(["sysctl", "-n", "kern.boottime"])
        m = re.search(r"sec = (\d+)", out)
        if not m:
            raise RuntimeError("kern.boottime: unreadable")
        return time.time() - int(m.group(1))
    if os.path.exists("/proc/uptime"):
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    raise RuntimeError(f"no uptime source on {platform.system()}")


TAILSCALE_V6 = "fd7a:115c:a1e0:"


def pick_v6(candidates):
    """The one IPv6 address worth reading off a wall, or None.

    `candidates` is [(address, flags)] for one interface. Link-local is the
    same fact as "this interface is up" and every interface has one, so it
    is noise. Temporary (privacy) addresses rotate and a deprecated one is
    already on its way out; neither is an address anyone would type. The
    tailnet address is on the tailscale line. What is left is the stable
    global, which is the one that reaches this machine from anywhere, and
    failing that the stable ULA, which at least reaches it from here.
    """
    global_, ula = None, None
    for addr, flags in candidates:
        a = addr.split("%")[0].lower()
        if a.startswith("fe80:") or a == "::1" or a.startswith(TAILSCALE_V6):
            continue
        if flags & {"temporary", "deprecated", "tentative", "duplicated"}:
            continue
        if a.startswith(("fc", "fd")):
            ula = ula or a
        else:
            global_ = global_ or a
    return global_ or ula


def addresses():
    """Live interfaces with an IPv4 address, as [(name, v4, v6, note)].

    `v6` is the address `pick_v6` chose or None, and `note` says
    "link-local only" when that is the reason there is none, so the wall
    can tell no IPv6 from IPv6 it declined to show.

    Tunnels are left out; tailscale reports its own address, and a VPN's
    is not where this machine lives on the network someone would walk to.
    """
    if sys.platform == "darwin":
        found, name, active, addr, v6s = [], None, False, None, []

        def close():
            if name and addr and active:
                found.append((name, addr, list(v6s)))

        for line in _run(["ifconfig"]).splitlines() + [""]:
            if line and not line[0].isspace():
                close()
                name, active, addr, v6s = line.split(":")[0], False, None, []
                continue
            s = line.strip().split()
            if not s:
                continue
            if s[0] == "inet":
                addr = s[1]
            elif s[0] == "inet6":
                v6s.append((s[1], set(s[2:])))
            elif line.strip().startswith("status: active"):
                active = True
        found = [(n, a, v) for n, a, v in found
                 if not n.startswith(("lo", "utun", "bridge", "awdl", "llw"))]
    elif shutil.which("ip"):
        v4, v6 = {}, {}
        for line in _run(["ip", "-o", "-4", "addr", "show", "up"]).splitlines():
            f = line.split()
            if len(f) >= 4 and f[1] != "lo" and not f[1].startswith("tailscale"):
                v4.setdefault(f[1], f[3].split("/")[0])
        for line in _run(["ip", "-o", "-6", "addr", "show", "up"]).splitlines():
            f = line.split()
            if len(f) >= 4:
                v6.setdefault(f[1], []).append((f[3].split("/")[0], set(f[4:])))
        found = [(n, a, v6.get(n, [])) for n, a in v4.items()]
    else:
        # The one address a resolver would give for the host name; a poor
        # answer, and reported as the only one available.
        return [("host", socket.gethostbyname(socket.gethostname()), None,
                 None)]
    out = []
    for n, a, v6s in found:
        chosen = pick_v6(v6s)
        note = None
        if chosen is None and v6s:
            note = "link-local only"
        out.append((n, a, chosen, note))
    return out


def hostname():
    return socket.gethostname().split(".")[0]


# ---------------------------------------------------------- tailscale

TAILSCALE_PATHS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                   "/usr/local/bin/tailscale", "/usr/bin/tailscale")


def find_tailscale(explicit=None):
    """The tailscale binary, or None; `explicit` is used as given."""
    if explicit:
        return explicit
    found = shutil.which("tailscale")
    if found:
        return found
    for p in TAILSCALE_PATHS:
        if os.path.exists(p):
            return p
    return None


class Tailscale:
    """This node on its tailnet, from `tailscale status --json`.

    Returns a dict: state, ip, name (the short DNS name), online and total
    peers. No binary is an error, not a node with zero peers.
    """

    def __init__(self, binary=None):
        self.binary = binary

    def __call__(self):
        if not self.binary or not os.path.exists(self.binary):
            raise RuntimeError("tailscale: not installed")
        out = _run([self.binary, "status", "--json"], timeout=10.0)
        try:
            d = json.loads(out)
        except ValueError:
            raise RuntimeError("tailscale: unreadable status")
        me = d.get("Self") or {}
        peers = (d.get("Peer") or {}).values()
        ips = [ip for ip in (d.get("TailscaleIPs") or []) if ":" not in ip]
        return {
            "state": d.get("BackendState") or "unknown",
            "ip": ips[0] if ips else None,
            "name": (me.get("DNSName") or "").rstrip(".").split(".")[0] or None,
            "online": sum(1 for p in peers if p.get("Online")),
            "peers": len(peers),
        }


# ----------------------------------------------------------- services

class Services:
    """Whether each configured service is up, by running its check.

    A check is a shell command: exit 0 is up and any other exit is down,
    except 126 and 127, which are the shell saying the check itself could
    not run. That is the distinction that matters -- a service whose check
    is broken is not a service that is down, and reporting it as one is
    the kind of made-up answer this wall exists to avoid.

    Each service is (label, state, detail): state is "up", "down" or None
    for unknown, and detail says why when it is not simply up.
    """

    TIMEOUT = 10.0

    def __init__(self, services):
        self.services = list(services)

    def __call__(self):
        out = []
        for label, cmd in self.services:
            try:
                p = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=self.TIMEOUT)
            except subprocess.TimeoutExpired:
                out.append((label, None, f"no answer in {self.TIMEOUT:.0f}s"))
                continue
            except OSError as e:
                out.append((label, None, e.strerror or str(e)))
                continue
            if p.returncode == 0:
                out.append((label, "up", None))
            elif p.returncode in (126, 127):
                what = "not found" if p.returncode == 127 else "cannot run"
                out.append((label, None, f"{what}: {cmd.split()[0]}"))
            else:
                out.append((label, "down", f"exit {p.returncode}"))
        return out


# -------------------------------------------------------------- clocks

class Clock:
    """One probe, run on its own thread at its own period.

    `reading` is the latest `Reading`; on failure the last good value stays
    with its own timestamp and the error is set beside it. With `history`,
    every tick is appended as (time, value) with None for a failed tick, so
    a graph can leave the gap where the measurement was not made.

    Ticks are aligned to multiples of the period so a graph slotting
    samples by time lands one per column rather than two in one and none
    in the next.
    """

    def __init__(self, name, period, probe, history=0, gate=None):
        self.name = name
        self.period = period
        self.probe = probe
        self.gate = gate
        self.reading = NOTHING
        self.samples = collections.deque(maxlen=history) if history else None
        self.lock = threading.Lock()

    def tick(self):
        at = time.time()
        try:
            value = self.probe()
            new = Reading(value, at, None)
        except Warming as e:
            new = Reading(self.reading.value, self.reading.at, str(e))
            value = None
        except Exception as e:                       # noqa: BLE001
            msg = str(e) or type(e).__name__
            new = Reading(self.reading.value, self.reading.at, msg)
            value = None
        with self.lock:
            self.reading = new
            if self.samples is not None:
                self.samples.append((at, value))

    def run(self, stop):
        while not stop.is_set():
            # A blanked wall is not looked at; a gated clock (one that runs
            # other programs) waits rather than spends. Cheap clocks keep
            # ticking so their graphs have no gap when the screen comes back.
            if self.gate is not None and self.gate():
                stop.wait(2.0)
                continue
            self.tick()
            now = time.time()
            stop.wait(self.period - (now % self.period))

    def snapshot(self):
        with self.lock:
            return self.reading, (list(self.samples) if self.samples else None)


class Machine:
    """Every clock this wall reads, and the snapshot the panel draws from.

    `services` is [(label, command)], `volumes` is [(label, path)], and
    `tailscale` a binary path or None to look for one. With no services
    the snapshot says so, and the panel gives them no room.
    """

    HISTORY = 600

    def __init__(self, services=(), volumes=(), tailscale=None, gate=None):
        self.clocks = [
            Clock("cpu", 1.0, Cpu(), history=self.HISTORY),
            Clock("cores", 1.0, Cores()),
            Clock("net", 1.0, Net(), history=self.HISTORY),
            Clock("mem", 5.0, memory),
            Clock("load", 5.0, load),
            Clock("disk", 60.0, lambda: disks(list(volumes))),
            Clock("uptime", 60.0, uptime),
            Clock("addresses", 60.0, addresses),
            Clock("tailscale", 30.0, Tailscale(find_tailscale(tailscale)),
                  gate=gate),
            Clock("services", 20.0, Services(services), gate=gate),
        ]
        self.hostname = hostname()
        self.services = bool(services)

    def start(self, stop):
        for c in self.clocks:
            threading.Thread(target=c.run, args=(stop,), daemon=True,
                             name=f"machine-{c.name}").start()

    def snapshot(self):
        """Readings and histories by clock name, plus the periods."""
        snap = {"hostname": self.hostname, "now": time.time(), "period": {},
                "services_asked": self.services}
        for c in self.clocks:
            reading, samples = c.snapshot()
            snap[c.name] = reading
            snap["period"][c.name] = c.period
            if samples is not None:
                snap[c.name + "_history"] = samples
        return snap

