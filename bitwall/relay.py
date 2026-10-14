"""What runs on a wall's host to reach its pipes: the far end of a session.

A caller on another host reaches a wall's door and pipes through ssh, and
something on the wall's host has to open them. On a Mac or a Pi that is
the shell, `cat > in`, and the opener; on Windows a pipe is opened by a
program, so it is this, the same on every host:

    relay run                          the host's run directory
    relay walls <run>                  its walls with a live door, a line each
    relay find <run>                   the same as a control page asks it:
                                       `wall <door>` a line each, and `web <run>`
                                       where a web receiver keeps its control pair
    relay web <run> <command>          a command to that web receiver, and its answer
    relay open <wall> <name> <request> <wait>
                                       knock, wait for the session's `out`,
                                       then copy it to stdout
    relay write <path>                 copy stdin into `in` or `data`
    relay read <path>                  copy `glass` to stdout
    relay clock                        the host's Unix time, to the microsecond,
                                       and again for each line on stdin

`open` exits 3 when nothing reads the door, 4 when no `out` came within
the wait, and 5 when the session's name was taken before the knock,
which are `session.py`'s NO_WALL, DROPPED and IN_USE.

`clock` answers at once and then once a line until stdin ends, so a
caller times its question and answer on a process already running: the
first answer comes after ssh, the shell and Python have started, late in
its round trip, and taking its time as the middle of that trip is a
quarter second out from a Windows host.

It is carried, never installed: `command()` makes it one `python3 -c`
with `local.py` and this file compressed inside, and its arguments in
base64, which has no quote, no `$` and no space, so sh, PowerShell and
cmd all hand it on untouched. No host keeps a copy that can go stale.
"""
import ast
import base64
import json
import os
import sys
import time
import zlib

NO_WALL, DROPPED, IN_USE = 3, 4, 5


def main(local, args):
    """Run one verb with its arguments; the process's exit code."""
    verb, rest = args[0], args[1:]
    local.binary_stdio()
    if verb == "run":
        print(local.run_dir(), flush=True)
        return 0
    if verb == "clock":
        while True:
            print(f"{time.time():.6f}", flush=True)
            if not sys.stdin.buffer.readline():
                return 0
    if verb == "walls":
        for wall in local.walls(rest[0]):
            print(wall)
        sys.stdout.flush()
        return 0
    if verb == "find":
        for said in find(local, rest[0]):
            print(said)
        sys.stdout.flush()
        return 0
    if verb == "web":
        code, answer = web(local, rest[0], rest[1])
        print("\n".join(answer), flush=True)
        return code
    if verb == "open":
        wall, name, request, wait = rest[0], rest[1], rest[2], float(rest[3])
        sdir = wall.rstrip("/") + "/s/" + name
        if local.taken(sdir):
            return IN_USE
        try:
            local.knock(wall.rstrip("/") + "/door.in", request)
        except local.NoDoor:
            return NO_WALL
        try:
            fd = local.open_read(sdir + "/out", wait)
        except local.NoDoor:
            return DROPPED
        local.copy(fd, 1)
        return 0
    if verb == "write":
        try:
            fd = local.open_write(rest[0], wait=2.0)
        except local.NoDoor:
            return NO_WALL
        local.copy(0, fd)
        os.close(fd)
        return 0
    if verb == "read":
        try:
            fd = local.open_read(rest[0], 2.0)
        except local.NoDoor:
            return NO_WALL
        local.copy(fd, 1)
        return 0
    print(f"relay: no verb {verb!r}", file=sys.stderr)
    return 2


def find(local, run):
    """A host's walls as `bitwall-control` lists them: `wall <door>` for
    each with a live door, and `web <run>` where a web receiver's control
    pair is there. The pair is looked for in a listing, never at its path."""
    run = run.rstrip("/")
    said = [f"wall {run}/bitwall/{wall}/door.in" for wall in local.walls(run)]
    if local.door_live(run + "/bitwall-web.in"):
        said.append(f"web {run}")
    return said


def web(local, run, words, wait=5.0):
    """A command to a web receiver on its control pair, `bitwall-web.in`
    and `bitwall-web.out` in its run directory: the exit code, 0 unless
    the answer is an error, and the lines that answered. `out` is opened
    before the command goes, since a pipe holds nothing for a reader that
    has not come; every line carrying the command's id is its answer, the
    last of them anything but a `wall` line."""
    run = run.rstrip("/")
    cid = "ctl" + os.urandom(6).hex()
    mark = " id=" + cid
    try:
        out = local.open_read(run + "/bitwall-web.out", 3.0)
    except OSError:
        return 1, [f"error no web receiver at {run}"]
    got = []
    try:
        try:
            fd = local.open_write(run + "/bitwall-web.in", 3.0)
        except OSError:
            return 1, [f"error no web receiver at {run}"]
        try:
            os.write(fd, (words + mark + "\n").encode())
        finally:
            os.close(fd)
        end, buf = time.monotonic() + wait, b""
        while time.monotonic() < end:
            if not local.ready(out, min(0.25, max(0.0, end - time.monotonic()))):
                continue
            data = os.read(out, 65536)
            if not data:
                break
            *lines, buf = (buf + data).split(b"\n")
            for line in lines:
                text = line.decode(errors="replace").rstrip("\r")
                if text.endswith(mark):
                    got.append(text[:-len(mark)])
                    if not text.startswith("wall "):
                        return (1 if text.startswith("error ") else 0), got
    finally:
        os.close(out)
    return 1, got + ["error no answer from the web receiver"]


def _source(module):
    """A module's code without its docstrings and comments: what runs,
    in as few bytes as the command line can carry."""
    with open(module.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


#: The program that runs on the far end, worked out once: local.py made a
#: module, then this file's `main` run on the arguments.
_PAYLOAD = None


def payload():
    global _PAYLOAD
    if _PAYLOAD is None:
        from . import local
        me = sys.modules[__name__]
        code = (
            "import sys,types,base64,json\n"
            f"L=types.ModuleType('bitwall_local');exec({_source(local)!r},L.__dict__)\n"
            f"R={{'__name__':'bitwall_relay'}};exec({_source(me)!r},R)\n"
            "sys.exit(R['main'](L,json.loads(base64.b64decode(sys.argv[2]))))\n")
        _PAYLOAD = base64.b64encode(zlib.compress(code.encode(), 9)).decode()
    return _PAYLOAD


#: The program as the command line says it: a few words that unpack the
#: rest from the next argument. The rest goes as an argument rather than
#: in the program because PowerShell looks hard at a long program before
#: it runs it, and took six seconds over one of six kilobytes.
BOOT = "import base64,zlib,sys;exec(zlib.decompress(base64.b64decode(sys.argv[1])))"


#: The Python a Windows host runs the relay with over ssh. Its `python3`
#: is the Store's app alias, which an ssh login may not run, and uv's own
#: shims and managed interpreter fail there too (an untrusted mount
#: point); uv itself runs, and starts its Python for us.
WINDOWS_PYTHON = "uv run --quiet --no-project python"


def command(verb, *args, python="python3"):
    """The relay as one command line for a host's shell, whatever shell
    that is."""
    return [python, "-c", BOOT, payload(),
            base64.b64encode(json.dumps([verb, *map(str, args)]).encode()).decode()]


def line(verb, *args, python="python3"):
    """`command` as one string, for ssh to hand to the far shell. Only
    BOOT is quoted, with double quotes, which sh, PowerShell and cmd all
    take; it holds no double quote, `$` or backquote of its own."""
    py, c, boot, code, arg = command(verb, *args, python=python)
    return f'{py} {c} "{boot}" {code} {arg}'
