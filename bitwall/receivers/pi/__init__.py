"""Puts a wall on each screen attached to a Linux machine.

The sender drives it over ssh: it opens a session at the wall's door, asks
for a font size, reads back the area the screen can offer in cells, and
streams what to paint. Everything the screen shows is decided by the
sender, so this side holds no credentials and no content.

The package is named `pi` because a Raspberry Pi driving an HDMI panel is
the usual case.

How it fits together
--------------------
Deployed to `~/bitwall` on the receiver:

    bitwall-paint.c          the painter: KMS straight to the glass, and
                             the whole receiver -- door, sessions, status
                             page, blank and font
    platform.h               the seams between the painter and the machine:
                             display, GPU context, child processes, decoders,
                             the machine, and the pipes a wall is made of
    outputs.c                what is made of the glass once composed: the
                             raw streams, each at its own size and rate
    pool.c                   the worker pool: pictures decoded and snapshots
                             encoded off the main thread
    display-kms.c, gpu-gles.c, child-posix.c, probe-v4l2.c, machine-linux.c,
    ipc-posix.c              a Pi's side of each seam
    display-sdl.c, gpu-gl.c, probe-mac.c, machine-mac.c
                             a Mac's, with the Pi's children and fifos
    gpu-win.c, child-win.c, probe-win.c, machine-win.c, ipc-win.c,
    compat-win.h             Windows', with the Mac's display; a Pi builds
                             neither the Mac's nor these, but their text is
                             part of the version
    qoi.h                    the QOI decoder
    Makefile                 builds bitwall-paint from all of these
    bitwall-screen           keeps the painter running, one wall per
                             connected output
    bitwall-screen.service   the user unit that starts it at boot
    screen.py                the painter's design in readable Python

Each wall is `bitwall/<name>/` in the run directory: its door and a
directory of sessions, `s/<name>/in` and `s/<name>/out`. PROTOCOL.md is
the contract.
"""
import getpass
import os

#: ssh user on the receiver. Defaults to whoever runs the sender, which
#: suits machines that share a username. Set BITWALL_PI_USER or pass
#: `user=` to attach() to choose another.
USER = os.environ.get("BITWALL_PI_USER") or getpass.getuser()
