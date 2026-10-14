"""What a sender host can run: its catalogue, walked where it lives.

    python3 - ~/.bitwall/senders < catalogue.py

The catalogue is `~/.bitwall/senders/` on the sender host: `*.json` files,
one per sender, and links to files or to whole directories of them from
elsewhere. This is sent over ssh and run on the host each time it is
asked, so nothing is installed there to go stale, and it answers with one
line of JSON:

    {"root": ..., "senders": [{"id", "label", ...}],
     "unavailable": [{"id", "label", "reason"}],
     "problems": [{"id", "problem"}], "hidden": 2}

every sender it could read and may run, every one that says it cannot
run here and why, every problem it found, and how many were hidden.

**A sender is named by its path under the catalogue**, `bitwall/picture`
for `bitwall/picture.json`, since two trees may each have a `video.json`.

**Links are followed, once**, and so are Windows' directory junctions,
which a user makes there without the Developer Mode a link wants. A directory that is its own ancestor is a
loop and is reported, not entered; one reached a second time by another
link is reported as the name it already has; and nothing deeper than
DEPTH is walked.

**A script is found from where its `.json` really is**, not from the
link, so a sender in another repository can say `"start": "./main.py"`.
`stop` and `status` are optional: left out, the controller uses its
standard ones.

**An entry may say whether it can run here**, with an `available`
script, found as `start` is. It is run while the catalogue is walked,
all of them at once and each given AVAILABLE_WAIT seconds: exit 0 is
available, anything else is not, and the first line it prints is why.
One that takes longer is not available, since a controller is waiting.
A sender that is not available is not offered: it is listed under
`unavailable` with its reason, and a start refuses it. On Windows a
`.py` script, or one whose `#!` runs Python, is run by the walker's own
Python, a `.exe`, `.cmd`, `.bat` or `.com` as it is, and anything else
-- a shell script -- cannot be run there, so its sender is unavailable
there and says so.

**A host may hide what it does not want offered**, in
`~/.bitwall/catalogue.json` beside the catalogue: `{"hide": ["bitwall/hdmi",
"bitwall/games/*"]}`, names as they are listed, with `*`, `?` and `[...]`
as fnmatch reads them. A hidden sender is left out of the walk, its file
not even read, and only counted, as `hidden`. No file hides nothing; a
file that cannot be read as that hides nothing and is a problem.

**An entry says what it sends and where it goes**: `type`, `cells` or
`video`, and `place`, `base`, `above` or `top`, or `controller` for a
sender that holds only a controller session on the wall it is started
on, as the mirror does to read its glass. Left out it is a cells base.
Only the base's font decides the area, so a `font` option on an entry
that is not the base makes the entry wrong.

**An entry says whether it moves**: `"moves": true` is a sender a
controller may move to another wall while it runs, by its record and
SIGUSR1, which the library catches; on Windows by its record alone. Left out it is false, and the sender
is moved by stopping it and starting it again, since SIGUSR1 ends a
process that does not catch it.

**A file that is wrong is a problem, not a failure.** It is left out and
said why, and the rest of the catalogue is listed. A sender is listed
whole or not at all: one bad option leaves out the sender, since a form
missing an option would start it with a default nobody chose.

**An option is one of four types.** `bool` is a flag, given or not;
`choice` is one word of a list; `font` a whole number in a range; and
`walls` a list of walls, `pi.local:0,pi.local:1`, each written as a
target is, so a sender that shows one wall on others is told where. An
empty `walls` is the option not given, and its `min` and `max` bound how
many walls a list that is given may name. Every address is checked here
by the rules a target is, which this file keeps a copy of, since it runs
where the bitwall library may not be; `walls()` is the one check, which
`bitwall-senders` imports rather than writing its own.

Runs on whatever python3 the host has, so it keeps to what 3.9 knows.
"""
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import time

DEPTH = 8
#: How long every `available` script together may take, in seconds.
AVAILABLE_WAIT = 2.0
#: The host's own settings, beside its catalogue.
SETTINGS = "catalogue.json"
WINDOWS = os.name == "nt"
MAX_FONT = 400
MAX_WALLS = 64
TYPES = ("bool", "choice", "font", "walls")
#: What an entry's stream carries, and where it goes on a wall.
KINDS = ("cells", "video")
PLACES = ("base", "above", "top", "controller")
#: An option is passed as `--<name>`, so its name is one a flag can be.
OPTION = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")
#: A choice is passed as a word of argv, and shown on a page.
CHOICE = re.compile(r"^[A-Za-z0-9_.,-]{1,64}\Z")
#: What may follow a target's colon: bitwall.session's WALL_TOKEN, the
#: same pattern, which a test holds them to.
WALL_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
#: A host as a target names it: a name or an IPv4 address, or a bare IPv6
#: address, which is all colons and so is taken whole. Never starting
#: with `-`, since it becomes a word of an ssh command.
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
IPV6 = re.compile(r"^[0-9A-Fa-f]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:]*\Z")
#: What a wall in a `walls` list may say it is sent, after an `@`: a
#: size, a rate, or both (`@1280x720/15`, `@/15`), passed on unread.
SENT = re.compile(r"@(?:[0-9]+x[0-9]+(?:/[0-9]+)?|/[0-9]+)\Z")


class Bad(Exception):
    pass


def text(spec, key, where, required=True):
    value = spec.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise Bad(f"{where}{key}: {'missing' if value is None else 'not text'}")
    return value


def script(spec, key, base, required):
    """A verb's script, as an absolute path on this host."""
    rel = text(spec, key, "", required)
    if rel is None:
        return None
    path = os.path.normpath(os.path.join(base, os.path.expanduser(rel)))
    if not os.path.isfile(path):
        raise Bad(f"{key} {rel}: not found")
    if not os.access(path, os.X_OK):
        raise Bad(f"{key} {rel}: not executable")
    return path


def walls(value, low=1, high=MAX_WALLS):
    """The (host, wall) each address in a comma-separated list names, as
    bitwall.session's `address()` reads a target, or `Bad` saying which
    one is wrong. An address may end in what that wall is sent, `@WxH`,
    `@WxH/fps` or `@/fps`, which is the sender's to read. An empty list
    is the option not given and is always allowed; one that is given
    names from `low` to `high` walls, none twice."""
    if not isinstance(value, str):
        raise Bad("not text")
    if value == "":
        return []
    out = []
    for target in value.split(","):
        target = SENT.sub("", target)
        if target.count(":") == 1:
            host, _, wall = target.partition(":")
            if not (wall.isascii() and WALL_TOKEN.match(wall)):
                raise Bad(f"{target!r}: the wall after ':' must be a number, "
                          f"or a name of lower case letters, digits, _ and -, "
                          f"starting with a letter or digit")
        else:
            host, wall = target, "0"
        if not (host.isascii() and (HOST.match(host) or IPV6.match(host))):
            raise Bad(f"{target!r}: not a host, or host:wall")
        if (host, wall) in out:
            raise Bad(f"{target!r}: named twice")
        out.append((host, wall))
    if not low <= len(out) <= high:
        raise Bad(f"{len(out)} walls: not within {low}..{high}")
    return out


def option(name, spec):
    where = f"option {name}: "
    if not OPTION.match(name):
        raise Bad(f"option {name!r}: not a flag name")
    if not isinstance(spec, dict):
        raise Bad(f"{where}not an object")
    kind = spec.get("type")
    if kind not in TYPES:
        raise Bad(f"{where}type {kind!r}: not one of {', '.join(TYPES)}")
    out = {"type": kind,
           "label": text(spec, "label", where),
           "description": text(spec, "description", where)}
    default = spec.get("default")
    if kind == "bool":
        if not isinstance(default, bool):
            raise Bad(f"{where}default: not true or false")
    elif kind == "choice":
        choices = spec.get("choices")
        if (not isinstance(choices, list) or not choices
                or not all(isinstance(c, str) and CHOICE.match(c) for c in choices)):
            raise Bad(f"{where}choices: not a list of words")
        if default not in choices:
            raise Bad(f"{where}default {default!r}: not one of the choices")
        out["choices"] = choices
    elif kind == "walls":
        low, high = spec.get("min", 1), spec.get("max", MAX_WALLS)
        default = spec.get("default", "")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)):
            raise Bad(f"{where}min and max: not whole numbers")
        if not 1 <= low <= high <= MAX_WALLS:
            raise Bad(f"{where}min {low}, max {high}: not within 1..{MAX_WALLS}")
        try:
            walls(default, low, high)
        except Bad as e:
            raise Bad(f"{where}default: {e}")
        out["min"], out["max"] = low, high
    else:
        low, high = spec.get("min", 1), spec.get("max", MAX_FONT)
        if not all(isinstance(v, int) and not isinstance(v, bool)
                   for v in (low, high, default)):
            raise Bad(f"{where}default, min and max: not whole numbers")
        if not 1 <= low <= default <= high <= MAX_FONT:
            raise Bad(f"{where}default {default}: not within {low}..{high}, "
                      f"inside 1..{MAX_FONT}")
        out["min"], out["max"] = low, high
    out["default"] = default
    return out


def sender(path, name):
    try:
        with open(path) as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as e:
        raise Bad(f"not JSON: {e}")
    if not isinstance(spec, dict):
        raise Bad("not a JSON object")
    base = os.path.dirname(os.path.realpath(path))
    options = spec.get("options", {})
    if not isinstance(options, dict):
        raise Bad("options: not an object")
    kind = spec.get("type", "cells")
    if kind not in KINDS:
        raise Bad(f"type {kind!r}: not one of {', '.join(KINDS)}")
    place = spec.get("place", "base")
    if place not in PLACES:
        raise Bad(f"place {place!r}: not one of {', '.join(PLACES)}")
    moves = spec.get("moves", False)
    if not isinstance(moves, bool):
        raise Bad("moves: not true or false")
    opts = {k: option(k, v) for k, v in options.items()}
    fonts = [k for k, o in opts.items() if k == "font" or o["type"] == "font"]
    if place != "base" and kind != "cells" and fonts:
        raise Bad(f"option {fonts[0]}: a font on a video layer, which has "
                  f"no grid; a cells layer's is its own grid's")
    return {"id": name,
            "label": text(spec, "label", ""),
            "description": text(spec, "description", ""),
            "type": kind,
            "place": place,
            "moves": moves,
            "start": script(spec, "start", base, True),
            "stop": script(spec, "stop", base, False),
            "status": script(spec, "status", base, False),
            "available": script(spec, "available", base, False),
            "options": opts,
            "file": os.path.realpath(path)}


def hiding(path):
    """The names a host hides, from its settings at `path`, and the
    problem with that file, if any."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            spec = json.load(fh)
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as e:
        return [], f"not JSON, so nothing is hidden: {e}"
    hide = spec.get("hide", []) if isinstance(spec, dict) else None
    if not isinstance(hide, list) or not all(isinstance(h, str) for h in hide):
        return [], "hide: not a list of names, so nothing is hidden"
    return hide, None


def _available_argv(path):
    """The argv that runs an `available` script on this host, or None
    where it cannot be run."""
    if not WINDOWS:
        return [path]
    first = ""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace")
    except OSError:
        pass
    if path.lower().endswith(".py") or (first.startswith("#!") and "python" in first):
        return [sys.executable, path]
    if os.path.splitext(path)[1].lower() in (".exe", ".cmd", ".bat", ".com"):
        return [path]
    return None


def availability(senders, wait=AVAILABLE_WAIT):
    """Each sender's `available` script run, all at once: the reason each
    one that cannot run here gives, by its id."""
    running, why = [], {}
    for s in senders:
        if not s.get("available"):
            continue
        argv = _available_argv(s["available"])
        if argv is None:
            why[s["id"]] = (f"available {os.path.basename(s['available'])}: "
                            f"cannot be run on Windows")
            continue
        try:
            running.append((s, subprocess.Popen(
                argv, cwd=os.path.dirname(s["available"]), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=not WINDOWS)))
        except OSError as e:
            why[s["id"]] = f"available could not be run: {e.strerror or e}"
    until = time.monotonic() + wait
    for s, proc in running:
        try:
            out, _ = proc.communicate(timeout=max(0.0, until - time.monotonic()))
        except subprocess.TimeoutExpired:
            # The whole group, since a shell's child keeps the pipe open.
            try:
                if WINDOWS:
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.stdout.close()
            proc.wait()
            why[s["id"]] = "took too long to say"
            continue
        if proc.returncode != 0:
            lines = out.decode("utf-8", "replace").strip().splitlines()
            why[s["id"]] = (lines[0].strip() if lines
                            else f"available exited {proc.returncode}")
    return why


def walk(root, settings=None):
    """Every sender under root that is not hidden, each problem, and each
    sender that says it cannot run here, each named by its path. The
    host's settings are `settings`, by default beside root."""
    senders, problems = [], []
    named = {}                      # a real directory -> the name it was walked as
    hidden = [0]
    if settings is None:
        settings = os.path.join(os.path.dirname(os.path.normpath(root)), SETTINGS)
    hide, bad = hiding(settings)

    def problem(name, why):
        problems.append({"id": name, "problem": why})

    if bad:
        problem(settings, bad)

    def visit(path, name, depth, above):
        real = os.path.realpath(path)
        if real in above:
            return problem(name, "a loop: a link back to a directory above it")
        if real in named:
            return problem(name, f"already listed as {named[real] or 'the top'}")
        if depth > DEPTH:
            return problem(name, f"more than {DEPTH} directories deep")
        named[real] = name
        try:
            entries = sorted(os.listdir(path))
        except OSError as e:
            return problem(name, f"cannot be read: {e.strerror}")
        for entry in entries:
            if entry.startswith("."):
                continue
            full = os.path.join(path, entry)
            sub = f"{name}/{entry}" if name else entry
            if os.path.isdir(full):
                visit(full, sub, depth + 1, above | {real})
            elif entry.endswith(".path"):
                # A directory named in a file, where a link will not do:
                # Windows follows a junction in a console but not in an
                # ssh session, and a symlink needs Developer Mode.
                try:
                    with open(full, encoding="utf-8-sig") as fh:
                        target = os.path.expanduser(fh.read().strip())
                except OSError as e:
                    problem(sub[:-5], f"cannot be read: {e.strerror}")
                    continue
                if not os.path.isdir(target):
                    problem(sub[:-5], f"names {target!r}, which is not a directory")
                else:
                    visit(target, sub[:-5], depth + 1, above | {real})
            elif not entry.endswith(".json"):
                continue
            elif any(fnmatch.fnmatchcase(sub[:-5], h) for h in hide):
                hidden[0] += 1
            elif not os.path.exists(full):
                problem(sub[:-5], "a link to nothing")
            else:
                try:
                    senders.append(sender(full, sub[:-5]))
                except Bad as e:
                    problem(sub[:-5], str(e))

    if os.path.isdir(root):
        visit(root, "", 0, frozenset())
    why = availability(senders)
    unavailable = [{"id": s["id"], "label": s["label"], "reason": why[s["id"]]}
                   for s in senders if s["id"] in why]
    return {"root": root, "senders": [s for s in senders if s["id"] not in why],
            "unavailable": unavailable, "problems": problems, "hidden": hidden[0]}


if __name__ == "__main__":
    print(json.dumps(walk(os.path.expanduser(sys.argv[1]))))
