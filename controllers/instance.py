"""The running senders on a sender host: started, stopped, looked at.

    python3 - start|stop|status|repoint|tail|lock|unlock '<json>' < instance.py

Sent over ssh and run on the host each time, as `catalogue.py` is, so
nothing is installed there. Each call is one JSON argument in and one line
of JSON out. The argument may be JSON or that JSON in URL-safe base64,
which is a word every shell passes as it is: PowerShell 5.1 takes the
double quotes out of JSON it hands a program.

**A record is written before anything runs**, as
`~/.bitwall/running/<id>.json`, with an exclusive create, and holds the
sender, the wall, the argv it was given, the pid and when it started. The
id is made here, from this host's name and a random part, and reaches the
sender in `BITWALL_INSTANCE`, which the library puts on the wall as a
hidden meta key, a base's and a layer's alike: that is how a wall is
joined to its record; one placed `controller` says it on its controller
session's open instead. The place its entry asked for is kept too, and
any place but the base is given to the sender in `BITWALL_PLACE`.

**A sender outlives the session that started it.** It runs in a session of
its own (`setsid`), with no terminal, stdin from nowhere and its output in
`<id>.log` beside the record, so the ssh that ran this can go, and a
controller that went away took nothing with it.

**The standard `status` and `stop` act on the pid.** Status is whether
that pid is still the process that was started -- its start time is kept
with it, so a pid the system has handed to something else is not taken
for the sender. Stop ends the sender's whole process group, since a
sender may be several processes, TERM and then KILL. A sender that names
its own `status` or `stop` has them run instead, with `BITWALL_INSTANCE`
and `BITWALL_RECORD` in the environment; a status that exits 0 means up.

**A sender whose entry says it moves is moved where it runs.** `repoint`
writes the new wall into its record, as this host names it (`wall`) and as
the sender does (`target`), and sends the sender's pid alone SIGUSR1: not
its group, since only the library catches it and a child of the sender's
would end.
The library reads `target` from the record and moves there. Any other
sender is refused, since SIGUSR1 ends a process that does not catch it.

**A lock per wall** keeps two starts from here from both finding the wall
free: a directory made with `mkdir`, which only one can make, taken for
the few seconds a start takes and given back after. One left behind by a
start that died is taken over once it is a minute old.

**On Windows a sender is started by WMI**, since whatever an ssh session
starts ends with it, and a process `Win32_Process.Create` makes is outside
that job. WMI runs a launcher written beside the record,
`<id>.launch.py`, which holds the sender's argv, environment and log and
starts it with all three, so nothing is said through a command line but
two paths. A `.py` script, or one whose `#!` runs Python, is started as
`uv run --no-project --with <each dependency> python <script>`, its
dependencies read from its PEP 723 header, which works in and out of an
ssh session where `uv run --script` does not; anything else must be a
program Windows runs. Git's `ssh` goes first on its path where Git is
installed, since Windows' own hangs at the end when a program in an ssh
session reads it through a pipe. The pid kept is the launcher's, which
lives as long as the sender does, and its start time comes from
`GetProcessTimes`.

**On Windows stop and move are written into the record**, as `told`,
since there is no SIGTERM to send and no SIGUSR1: `bitwall.moving`
watches the record in every process of a sender built on the library and
raises the signal the sender stops on, or moves it. Stop asks so, waits
five seconds, then ends the launcher and everything under it with
`taskkill /T /F`.

Runs on whatever python3 the host has, so it keeps to what 3.9 knows.
"""
import base64
import json
import os
import re
import secrets
import signal
import socket
import shutil
import subprocess
import sys
import time

RUNNING = os.path.expanduser(os.environ.get("BITWALL_RUNNING") or "~/.bitwall/running")
STALE_LOCK = 60.0
STOP_WAIT = 5.0
WINDOWS = os.name == "nt"
#: Where Git keeps the ssh that does not hang in an ssh session.
GIT_SSH = r"C:\Program Files\Git\usr\bin"


def record_path(iid):
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", iid or ""):
        raise ValueError(f"not an instance id: {iid!r}")
    return os.path.join(RUNNING, iid + ".json")


def started_at(pid):
    """When the process with this pid started, as ps says it, or None."""
    if WINDOWS:
        return _win_started_at(pid)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def write(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses to replace a file someone has open, and a
            # sender reads its record now and then.
            if not WINDOWS:
                raise
            time.sleep(0.05)
    os.replace(tmp, path)


def env_for(rec, path):
    env = dict(os.environ, BITWALL_INSTANCE=rec["id"], BITWALL_RECORD=path)
    if rec.get("place", "base") != "base":
        env["BITWALL_PLACE"] = rec["place"]
    return env


def up(rec, path):
    if rec.get("status"):
        try:
            return subprocess.run([rec["status"]], env=env_for(rec, path),
                                  stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    pid = rec.get("pid")
    if not pid:
        return False
    now = started_at(pid)
    return now == rec.get("pid_started") or now == HIDDEN


def start(a):
    os.makedirs(RUNNING, exist_ok=True)
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())
    iid = f"{host}-{secrets.token_hex(3)}"
    path = record_path(iid)
    log = os.path.join(RUNNING, iid + ".log")
    rec = {"id": iid, "sender": a["sender"], "wall": a["wall"],
           "place": a.get("place", "base"), "moves": bool(a.get("moves")),
           "argv": a["argv"], "start": a["start"], "stop": a.get("stop"),
           "status": a.get("status"), "options": a.get("options", {}),
           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": None,
           "pid_started": None, "log": log}
    with open(path, "x") as fh:
        json.dump(rec, fh)
    if WINDOWS:
        return _win_start(a, rec, path)
    try:
        with open(log, "ab") as out:
            proc = subprocess.Popen(
                [a["start"]] + a["argv"], cwd=os.path.dirname(a["start"]),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                env=env_for(rec, path), start_new_session=True)
    except OSError as e:
        os.remove(path)
        return {"error": f"could not start {a['start']}: {e}"}
    rec["pid"], rec["pid_started"] = proc.pid, started_at(proc.pid)
    write(path, rec)
    return {"id": iid, "pid": proc.pid, "record": path}


def records():
    out = []
    try:
        names = sorted(os.listdir(RUNNING))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(RUNNING, name)
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append((rec, path))
    return out


def status(a):
    return {"running": [dict(rec, up=up(rec, path)) for rec, path in records()]}


def stop(a):
    path = record_path(a["id"])
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except OSError:
        return {"error": f"no instance {a['id']} here"}
    was_up, forced = up(rec, path), False
    if was_up and WINDOWS and not rec.get("stop"):
        forced = _win_stop(rec, path)
    elif was_up and rec.get("stop"):
        subprocess.run([rec["stop"]], env=env_for(rec, path),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    elif was_up:
        pgid = rec["pid"]
        for sig, wait in ((signal.SIGTERM, STOP_WAIT), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                break
            until = time.monotonic() + wait
            while time.monotonic() < until and up(rec, path):
                time.sleep(0.1)
            if not up(rec, path):
                break
    if up(rec, path):
        return {"error": f"{a['id']} would not stop"}
    for p in (path, rec.get("log"), rec.get("launcher")):
        try:
            os.remove(p)
        except (OSError, TypeError):
            pass
    return dict({"id": a["id"], "stopped": was_up}, **({"forced": True} if forced else {}))


def repoint(a):
    path = record_path(a["id"])
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except OSError:
        return {"error": f"no instance {a['id']} here"}
    if not rec.get("moves"):
        return {"error": f"{a['id']} ({rec['sender']}) does not say it moves"}
    if not up(rec, path):
        return {"error": f"{a['id']} is not running"}
    was = rec["wall"]
    rec["wall"], rec["target"] = a["wall"], a["target"]
    write(path, rec)
    try:
        if WINDOWS:
            tell(path, "move")
        else:
            os.kill(rec["pid"], signal.SIGUSR1)
    except (OSError, ValueError) as e:
        rec["wall"] = was
        rec.pop("target", None)
        write(path, rec)
        return {"error": f"{a['id']} could not be told: {e}"}
    return {"id": a["id"], "from": was, "wall": a["wall"]}


def tail(a):
    """The last lines of an instance's log."""
    try:
        with open(record_path(a["id"])[:-5] + ".log", "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return {"lines": []}
    return {"lines": text.splitlines()[-int(a.get("lines", 8)):]}


def tell(path, what):
    """Write into a record what its sender is asked, for bitwall.moving
    to read: the next `told`, `move` or `stop`."""
    with open(path) as fh:
        rec = json.load(fh)
    told = rec.get("told") if isinstance(rec.get("told"), dict) else {}
    rec["told"] = {"n": int(told.get("n", 0)) + 1, "what": what}
    write(path, rec)


# ---------------------------------------------------------------- Windows

LAUNCHER = """import subprocess, sys
SPEC = %s
with open(SPEC["log"], "ab") as out:
    try:
        p = subprocess.Popen(SPEC["argv"], cwd=SPEC["cwd"], env=SPEC["env"],
                             stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT)
    except OSError as e:
        out.write(("could not start %%s: %%s\\n" %% (SPEC["argv"], e)).encode())
        sys.exit(1)
sys.exit(p.wait())
"""


def _win_deps(script):
    """What a script's PEP 723 header says it needs: its dependencies and
    the Python it asks for, or ([], None)."""
    try:
        with open(script, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(65536).splitlines()
    except OSError:
        return [], None
    block, inside = [], False
    for line in lines:
        if line.rstrip() == "# /// script":
            inside = True
        elif inside and line.rstrip() == "# ///":
            break
        elif inside:
            block.append(line[2:] if line.startswith("# ") else line.lstrip("#"))
    if not block:
        return [], None
    text = "\n".join(block)
    try:
        import tomllib
        meta = tomllib.loads(text)
        return list(meta.get("dependencies", [])), meta.get("requires-python")
    except Exception:                                   # noqa: BLE001
        pass
    deps = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    py = re.search(r"^requires-python\s*=\s*[\"']([^\"']*)", text, re.M)
    return (re.findall(r"[\"']([^\"']+)[\"']", re.sub(r"#[^\n]*", "", deps.group(1)))
            if deps else [], py.group(1) if py else None)


def _win_argv(script, argv):
    """The argv that runs `script` on Windows."""
    first = ""
    try:
        with open(script, "rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace")
    except OSError:
        pass
    if script.lower().endswith(".py") or (
            first.startswith("#!") and ("python" in first or "uv run" in first)):
        import shutil
        uv = shutil.which("uv")
        if uv is None:
            raise OSError("uv is not on the path, and a Python sender is run by it")
        deps, py = _win_deps(script)
        out = [uv, "run", "--quiet", "--no-project"]
        # A host's UV_PYTHON names the interpreter that runs in an ssh
        # session; asking for a version instead has uv look through its
        # own installs, whose junctions Windows will not follow there.
        if py and not os.environ.get("UV_PYTHON"):
            out += ["--python", py]
        for d in deps:
            out += ["--with", d]
        return out + ["python", script] + argv
    if os.path.splitext(script)[1].lower() in (".exe", ".cmd", ".bat", ".com"):
        return [script] + argv
    raise OSError("Windows runs a .py by uv, or a .exe, .cmd or .bat, and this is neither")


def _win_env(rec, path):
    env = env_for(rec, path)
    if os.path.isdir(GIT_SSH):
        # First, not merely there: Windows' own ssh ahead of it hangs.
        parts = [p for p in env.get("PATH", "").split(os.pathsep)
                 if p and p.lower().rstrip("\\") != GIT_SSH.lower()]
        env["PATH"] = os.pathsep.join([GIT_SSH] + parts)
    return env


def _wmi_create(command, cwd):
    """Start `command` through WMI, outside any job of ours: its pid, or
    OSError."""
    def q(s):
        return "'" + s.replace("'", "''") + "'"
    ps = ("$ProgressPreference = 'SilentlyContinue'; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{ CommandLine = %s; CurrentDirectory = %s }; "
          "\"$($r.ReturnValue) $($r.ProcessId)\"" % (q(command), q(cwd)))
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(ps.encode("utf-16-le")).decode("ascii")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    words = out.stdout.split()
    if out.returncode != 0 or len(words) != 2 or words[0] != "0" or not words[1].isdigit():
        # Win32_Process.Create's ReturnValue says why (8: the program
        # could not be run, as an app alias cannot); PowerShell's progress
        # records on stderr (#< CLIXML) say nothing.
        err = [ln for ln in out.stderr.strip().splitlines()
               if ln and not ln.startswith("#< CLIXML") and not ln.startswith("<Objs")]
        said = (f"ReturnValue {words[0]}" if words and words[0].isdigit()
                else out.stdout.strip() or (err[-1] if err else "no answer"))
        raise OSError(f"WMI would not start it: {said}")
    return int(words[1])


def _win_start(a, rec, path):
    launcher = path[:-5] + ".launch.py"
    try:
        spec = {"argv": _win_argv(a["start"], a["argv"]),
                "cwd": os.path.dirname(a["start"]), "env": _win_env(rec, path),
                "log": rec["log"]}
        with open(launcher, "w") as fh:
            fh.write(LAUNCHER % json.dumps(spec))
        python = [sys.executable]
        if "WindowsApps" in sys.executable:
            # The Store's Python, an app alias WMI cannot start (what
            # `python3` is over ssh): uv's, by uv, which it can.
            uv = shutil.which("uv")
            if not uv:
                raise OSError(f"{sys.executable} is an app alias, which WMI cannot "
                              "start, and there is no uv to start one with")
            python = [uv, "run", "--quiet", "--no-project", "python"]
        pid = _wmi_create(subprocess.list2cmdline(python + [launcher]),
                          os.path.dirname(a["start"]))
    except (OSError, subprocess.TimeoutExpired) as e:
        for p in (path, launcher):
            try:
                os.remove(p)
            except OSError:
                pass
        return {"error": f"could not start {a['start']}: {e}"}
    rec["pid"], rec["pid_started"], rec["launcher"] = pid, started_at(pid), launcher
    write(path, rec)
    return {"id": rec["id"], "pid": pid, "record": path}


#: What `started_at` says of a process that runs but may not be looked at:
#: one started elevated (as WMI does for an administrator's ssh login),
#: asked about from a console that is not. Taken as the one in the record.
HIDDEN = "hidden"


def _win_started_at(pid):
    """When the process with this pid started, while it runs, HIDDEN where
    it runs and this caller may not ask, or None."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # Access denied is a process that is there; no such pid is not.
        return HIDDEN if ctypes.get_last_error() == 5 else None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
            return None                                 # not STILL_ACTIVE
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return str((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime)
    finally:
        k32.CloseHandle(h)


def _win_stop(rec, path):
    """Ask the sender to stop through its record, and after STOP_WAIT end
    the launcher and everything under it: whether it had to."""
    try:
        tell(path, "stop")
    except (OSError, ValueError):
        pass
    until = time.monotonic() + STOP_WAIT
    while time.monotonic() < until and up(rec, path):
        time.sleep(0.1)
    if up(rec, path):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(rec["pid"])],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        until = time.monotonic() + 2.0
        while time.monotonic() < until and up(rec, path):
            time.sleep(0.1)
        return True
    return False


def lock_path(wall):
    return os.path.join(RUNNING, ".lock-" + re.sub(r"[^A-Za-z0-9._-]", "_", wall))


def lock(a):
    os.makedirs(RUNNING, exist_ok=True)
    path = lock_path(a["wall"])
    try:
        os.mkdir(path)
        return {"locked": True}
    except FileExistsError:
        try:
            if time.time() - os.stat(path).st_mtime > STALE_LOCK:
                os.utime(path)
                return {"locked": True}
        except OSError:
            pass
        return {"locked": False}


def unlock(a):
    try:
        os.rmdir(lock_path(a["wall"]))
    except OSError:
        pass
    return {"unlocked": True}


if __name__ == "__main__":
    verbs = {"start": start, "stop": stop, "status": status, "repoint": repoint,
             "tail": tail, "lock": lock, "unlock": unlock}
    try:
        said = sys.argv[2]
        if not said.startswith("{"):
            said = base64.urlsafe_b64decode(said.encode("ascii")).decode("utf-8")
        answer = verbs[sys.argv[1]](json.loads(said))
    except (KeyError, IndexError, ValueError) as e:
        answer = {"error": f"bad request: {e}"}
    print(json.dumps(answer))
