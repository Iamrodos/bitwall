"""Receiver implementations, one package per kind.

A receiver accepts a font size request, publishes the area it can offer in
cells, and paints the stream it is given. That is the whole interface;
anything implementing it is a receiver, on any platform.

Each package here holds its kind's connection defaults, deployment files and
setup, because those are properties of that receiver rather than settings
that belong to the tool.
"""
import hashlib
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

#: Each receiver program and the files that make it, in order. Its version
#: is the first twelve hex digits of the SHA-256 of those files one after
#: another, as `cat <files> | sha256sum` has it, so a machine holding the
#: same files works out the same version, and one that has only the
#: files -- a Pi, building the painter -- works it out with the shell.
SOURCES = {
    "bitwall-paint": ("pi/bitwall-paint.c", "pi/qoi.h", "pi/platform.h", "pi/display-kms.c",
                      "pi/gpu-gles.c", "pi/child-posix.c", "pi/probe-v4l2.c",
                      "pi/machine-linux.c", "pi/display-sdl.c", "pi/gpu-gl.c",
                      "pi/probe-mac.c", "pi/machine-mac.c", "pi/outputs.c", "pi/ipc-posix.c",
                      "pi/ipc-win.c", "pi/compat-win.h", "pi/gpu-win.c", "pi/child-win.c",
                      "pi/probe-win.c", "pi/machine-win.c", "pi/pool.c"),
    "bitwall-web": ("web/bitwall-web", "core.py", "kitty.py", "stream.py",
                    "web/bitwall-screen.js", "web/page.html"),
    "bitwall-mac": ("web/bitwall-web", "core.py", "kitty.py", "stream.py",
                    "web/bitwall-screen.js", "web/page.html", "mac/bitwall-mac"),
    "bitwall-shell": ("shell/bitwall-shell",),
    "bitwall-ps": ("shell/bitwall-ps.ps1",),
    # The same list as term/build.rs, which works out the version it says:
    # the terminal's own files, then the wall core-rs keeps for it.
    "bitwall-term": ("term/Cargo.toml", "term/build.rs", "term/src/main.rs", "term/src/screen.rs",
                     "core-rs/Cargo.toml", "core-rs/src/lib.rs", "core-rs/src/glass.rs",
                     "core-rs/src/grid.rs", "core-rs/src/pipe/mod.rs", "core-rs/src/pipe/unix.rs",
                     "core-rs/src/pipe/windows.rs", "core-rs/src/proto.rs", "core-rs/src/sys.rs",
                     "core-rs/src/video.rs", "core-rs/src/wall.rs"),
    # The same list as eink/build.rs: the panel's wall and its face, then
    # the wall core-rs keeps for it.
    "bitwall-eink": ("eink/Cargo.toml", "eink/build.rs", "eink/src/bin/bitwall-eink/main.rs",
                     "eink/src/bin/bitwall-eink/kitty.rs", "eink/src/lib.rs", "eink/src/compose.rs",
                     "eink/src/face.rs", "eink/src/geom.rs", "eink/src/grey.rs", "eink/src/policy.rs",
                     "eink/src/scene.rs", "eink/src/shapes.rs", "eink/src/tune.rs", "eink/src/wall.rs",
                     "eink/src/panel/mod.rs", "eink/src/panel/it8951.rs",
                     "core-rs/Cargo.toml", "core-rs/src/lib.rs", "core-rs/src/glass.rs",
                     "core-rs/src/grid.rs", "core-rs/src/pipe/mod.rs", "core-rs/src/pipe/unix.rs",
                     "core-rs/src/pipe/windows.rs", "core-rs/src/proto.rs", "core-rs/src/sys.rs",
                     "core-rs/src/video.rs", "core-rs/src/wall.rs", "core-rs/src/decode.rs",
                     "pi/MesloLGSNerdFontMono-Regular.ttf",
                     "eink/fonts/MesloLGSNerdFontMono-Bold.ttf",
                     "eink/fonts/MesloLGSNerdFont-Regular.ttf"),
}


def version(program, root=HERE):
    """A receiver program's version, from its files under `root`."""
    h = hashlib.sha256()
    for rel in SOURCES[program]:
        with open(os.path.join(root, rel), "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def booted():
    """When this machine last started, in Unix seconds, or None where it
    cannot be told."""
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5).stdout
        got = re.search(r"sec = (\d+)", out)
        return int(got.group(1)) if got else None
    except (OSError, subprocess.SubprocessError):
        return None


def receiver_line(program, started):
    """The `receiver` line of a report: the program, its version, when it
    started and when its machine did."""
    up = booted()
    return (f"receiver {program} version={version(program)} started={int(started)}"
            + (f" booted={up}" if up else ""))
