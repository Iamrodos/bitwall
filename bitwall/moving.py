"""A running sender moved to another wall by its controller.

A controller moves a sender it started by writing the new wall into the
sender's record, the file `BITWALL_RECORD` names, as `target`, and sending
the sender's process SIGUSR1: the pid alone, never its group, since only
the library catches it and a child of the sender's would end.
The library catches the signal and moves every Wall and Layer of this
process that says the controller's instance to the wall the record names,
so a sender built on them moves with all its state and needs no code of
its own. Whether a sender may be moved this way is said by its catalogue
entry, `"moves": true`, since SIGUSR1 ends a process that does not catch
it; the controller signals no other.

The handler is set by the first Wall or Layer that takes an instance from
the environment, on the main thread, and only where nothing else has
claimed SIGUSR1.

**Windows has no SIGUSR1 and no SIGTERM to send**, so there the record
is the signal. The controller writes what it asks into the record as
`told`, `{"n": <count>, "what": "move" | "stop"}`, and a thread of every
process that imports bitwall with `BITWALL_RECORD` set watches the file
and does what the newest one says: `move` as SIGUSR1 does, and `stop` as
a SIGTERM would, by raising the signal the sender stops on -- its own
SIGINT handler where it has one, since only SIGINT wakes a main thread
waiting on Windows, else its SIGTERM handler, else Ctrl+C's
KeyboardInterrupt -- so it detaches and leaves as it does on a Mac or a
Pi. The watch starts on import, since a sender may take the record out
of its environment before it opens anything.
"""
import json
import os
import signal
import sys
import threading
import time
import weakref

#: Every Wall and Layer of this process under the controller's instance.
_held = weakref.WeakSet()
_record = None
_lock = threading.Lock()
#: One move at a time: a second signal waits for the first to be done.
_moving = threading.Lock()
#: The record each watching thread watches, where there is no SIGUSR1.
_watched = set()
#: How often a watching thread looks at the record.
WATCH_EVERY = 0.5


def hold(obj, record):
    """Move `obj` with the rest when the controller says, reading the new
    wall from `record`."""
    global _record
    with _lock:
        _held.add(obj)
        if _record is not None or not record:
            return
        _record = record
    if not hasattr(signal, "SIGUSR1"):
        watch(record)
        return
    if threading.current_thread() is not threading.main_thread():
        print("bitwall: attached off the main thread, so a controller "
              "cannot move this sender", file=sys.stderr, flush=True)
        return
    if signal.getsignal(signal.SIGUSR1) not in (signal.SIG_DFL, None):
        print("bitwall: SIGUSR1 is the sender's own, so a controller "
              "cannot move it", file=sys.stderr, flush=True)
        return
    signal.signal(signal.SIGUSR1, _signalled)


def _signalled(*_):
    # A handler interrupts the main thread wherever it is, locks and all,
    # so the move is made on a thread of its own.
    threading.Thread(target=move, daemon=True, name="bitwall-move").start()


def move(record=None):
    """Move everything held to the wall the record names now."""
    with _moving:
        try:
            with open(record or _record) as fh:
                target = json.load(fh).get("target")
        except (OSError, TypeError, ValueError) as e:
            print(f"bitwall: told to move, and the record could not be read: {e}",
                  file=sys.stderr, flush=True)
            return
        if not isinstance(target, str) or not target:
            print("bitwall: told to move, and the record names no target",
                  file=sys.stderr, flush=True)
            return
        for obj in list(_held):
            try:
                obj.retarget(target)
            except Exception as e:                              # noqa: BLE001
                print(f"bitwall: {obj.target}: could not move to {target}: {e}",
                      file=sys.stderr, flush=True)


def watch(record):
    """Do what the controller writes into `record` as `told`, on a thread
    of its own, once for each record."""
    with _lock:
        if not record or record in _watched:
            return
        _watched.add(record)
    threading.Thread(target=_watching, args=(record,), daemon=True,
                     name="bitwall-told").start()


def _watching(record):
    done, seen = 0, None
    while True:
        time.sleep(WATCH_EVERY)
        try:
            st = os.stat(record)
            if (st.st_mtime_ns, st.st_size) == seen:
                continue
            seen = (st.st_mtime_ns, st.st_size)
            with open(record) as fh:
                told = json.load(fh).get("told")
        except (OSError, ValueError):
            continue
        if not isinstance(told, dict) or not isinstance(told.get("n"), int) \
                or told["n"] <= done:
            continue
        done = told["n"]
        if told.get("what") == "move":
            if _held:
                threading.Thread(target=move, args=(record,), daemon=True,
                                 name="bitwall-move").start()
        elif told.get("what") == "stop":
            print("bitwall: told to stop", file=sys.stderr, flush=True)
            signal.raise_signal(stop_signal())


def stop_signal():
    """The signal this process stops on as a controller would have it
    stop: its own SIGINT handler, which is the one a waiting main thread
    wakes for on Windows, else its own SIGTERM handler, else SIGINT, which
    Python ends the main thread on."""
    own = signal.getsignal(signal.SIGINT)
    if callable(own) and own is not signal.default_int_handler:
        return signal.SIGINT
    if callable(signal.getsignal(signal.SIGTERM)):
        return signal.SIGTERM
    return signal.SIGINT


if not hasattr(signal, "SIGUSR1"):
    watch(os.environ.get("BITWALL_RECORD"))
