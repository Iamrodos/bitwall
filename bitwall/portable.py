"""What a sender needs from the machine it runs on, said once for every one.

A sender runs on a Mac, a Pi or Windows. Most of what it does is the same
on all three; what is not is here, so the rest of the library never asks
which it is on:

    Pipe(proc)          a child's stdin, written by a thread of its own
    detached()          Popen arguments for a child that outlives a signal
                        to its caller's process group
    SSH_SHARES          whether this machine's ssh shares one connection
                        between sessions

A pipe is written by a thread because waiting for room in a pipe is the
one thing the three do differently: `select` takes a pipe on a Mac and a
Pi and only a socket on Windows. A thread blocked in `write` is the same
everywhere, and wakes the moment the pipe drains.
"""
import collections
import os
import subprocess
import threading
import time

WINDOWS = os.name == "nt"

#: Whether this machine's ssh multiplexes sessions over one connection
#: (`ControlMaster`). Windows' own OpenSSH has no multiplexing, so there
#: every session makes a connection of its own.
SSH_SHARES = not WINDOWS

#: The ssh a link runs. On Windows, Git's: Windows' own, started from a
#: program whose stdout is a pipe, does the whole job and never exits
#: (AGENTS.md, Traps), and which is first on a PATH depends on who
#: started the sender.
SSH = "ssh"
if WINDOWS and os.path.isfile(r"C:\Program Files\Git\usr\bin\ssh.exe"):
    SSH = r"C:\Program Files\Git\usr\bin\ssh.exe"

#: How much one `write` hands the pipe: small enough that a write given up
#: on stops soon, large enough that a frame is a few calls.
CHUNK = 65536


def detached():
    """Popen arguments that put a child in a process group of its own, so
    a signal to its caller's group leaves it to finish what it carries."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class _Job:
    __slots__ = ("view", "sent", "done", "dropped")

    def __init__(self, data):
        self.view = memoryview(data)
        self.sent = 0
        self.done = threading.Event()
        self.dropped = False


class Pipe:
    """A child's stdin, written whole or not at all by a thread of its own.

    A caller hands it bytes and waits as long as it chooses: until they
    are all in the pipe, until `stop` is set, or until `timeout` passes.
    What it gives up on is dropped at the next chunk, so the pipe carries
    at most one piece of a frame nobody waits for any more. The thread
    owns the pipe's end and is the one that closes it, so a close can
    never pull the descriptor from under a write.
    """

    def __init__(self, proc, name="bitwall-pipe"):
        self.proc = proc
        self._fd = proc.stdin.fileno()
        self._jobs = collections.deque()
        self._busy = None
        self._closing = False
        #: True once the pipe can take nothing more: closed, or broken.
        self.closed = False
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True, name=name).start()

    @property
    def idle(self):
        """Whether everything handed over is in the pipe already."""
        with self._cv:
            return not self.closed and not self._jobs and self._busy is None

    def write(self, data, stop=None, timeout=None):
        """Put `data` in the pipe; how many bytes went, all of them unless
        `timeout` passed first, or None when the pipe is closed or `stop`
        was set."""
        job = _Job(data)
        with self._cv:
            if self.closed or self._closing:
                return None
            self._jobs.append(job)
            self._cv.notify()
        until = None if timeout is None else time.monotonic() + timeout
        while not job.done.wait(0.1 if until is None else
                                max(0.0, min(0.1, until - time.monotonic()))):
            if (stop is not None and stop.is_set()) or (
                    until is not None and time.monotonic() >= until):
                job.dropped = True
                return None if stop is not None and stop.is_set() else job.sent
        if job.dropped:                   # the pipe broke under it
            return None
        return job.sent

    def close(self):
        """Close the pipe once what it was handed is in it."""
        with self._cv:
            self._closing = True
            self._cv.notify()

    def _run(self):
        try:
            while True:
                with self._cv:
                    while not self._jobs and not self._closing:
                        self._cv.wait()
                    if not self._jobs:
                        return
                    job = self._busy = self._jobs.popleft()
                try:
                    while job.view and not job.dropped:
                        n = os.write(self._fd, job.view[:CHUNK])
                        job.sent += n
                        job.view = job.view[n:]
                except (OSError, ValueError):
                    job.dropped = True
                    return
                finally:
                    with self._cv:
                        self._busy = None
                    job.done.set()
        finally:
            with self._cv:
                self.closed = True
                left = list(self._jobs)
                self._jobs.clear()
            for job in left:
                job.dropped = True
                job.done.set()
            try:
                self.proc.stdin.close()
            except Exception:
                pass

