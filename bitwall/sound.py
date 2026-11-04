"""A film's sound on the wall clock: each sample out of the speakers at its time.

    sound = bitwall.sound.play("clip.mp4", frm=12.0, at=start / 1000)
    ...
    sound.failed()                                # why it stopped, or None
    sound.stop()

`play` starts the file's sound so that the sample `frm` seconds into it is
heard at Unix time `at`, and every sample after it at its own time. That
is the clock a wall plays a timed stream on (PROTOCOL.md, *Timed
playback*): a sender that opens a layer with `start` and plays the same
file's sound with `at` the same moment has the two in step, on a machine
whose clock agrees with the wall's. A sender on another machine measures
how far the wall's clock is from its own and moves `at` by that, as
`senders/film` does.

GStreamer plays it, on the system's realtime clock, the one NTP keeps. The
sink allows for the sound card's own latency, and takes up the drift
between the card's clock and the system's, so the sound holds to the
picture for the length of a film and not only at its start. A sound held
up -- a busy machine, a stalled process -- is late, and GStreamer drops
what is late rather than playing it: it comes back into step by itself.
`at` already past starts it that much further into the file.

GStreamer comes from the `sound` extra, `gstreamer-bundle`, whose wheels
carry GStreamer, its plugins and its Python bindings for macOS and
Windows; on Linux the system's GStreamer and PyGObject do as well.
`missing()` says why there is no sound, or None where there is.

GStreamer's first start in an environment opens every plugin it has to
learn what each does: the bundle's 258, which on a Mac, where each
library is checked as it is first opened, takes minutes. So it is shown
only the plugins a film's sound needs (`PLUGINS`), through a directory of
links to them of its own, and its first start takes seconds. Where that
directory cannot be made, it is shown them all, as it would be anyway. A
sender that will play sound calls `warm()` as it starts, so even that is
done by the time the first sound is wanted; until it is, a `play` waits.
"""
import hashlib
import os
import shutil
import sys
import threading
import time

__all__ = ["Sound", "SoundError", "missing", "play", "warm"]

#: Seconds a pipeline may take to open its file and be ready to play.
READY = 10

#: The plugins a film's sound is played with: reading the file, finding
#: what it is, taking it apart (MP4 and QuickTime, Matroska, Ogg),
#: decoding its sound (libav; the Mac's own decoders), and putting it out
#: on whatever this machine has.
PLUGINS = ("coreelements", "typefindfunctions", "playback", "isomp4", "matroska",
           "ogg", "audioparsers", "libav", "applemedia", "vorbis", "opus",
           "audioconvert", "audioresample", "autodetect", "volume",
           "osxaudio", "wasapi2", "wasapi", "directsound", "pulseaudio", "alsa")
#: The elements it cannot play without.
NEEDS = ("filesrc", "decodebin", "audioconvert", "audioresample", "autoaudiosink")


class SoundError(RuntimeError):
    """The sound would not start: no GStreamer, or a file it cannot play."""


_gst = None
_loading = threading.Lock()     # one start of GStreamer, the others waiting on it


def _cache_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "bitwall", "gst-plugins")


def _plugin(name, dirs):
    """The file of plugin `name` in one of `dirs`, or None."""
    for d in dirs:
        for f in (f"libgst{name}.dylib", f"libgst{name}.so", f"gst{name}.dll", f"libgst{name}.dll"):
            if os.path.isfile(os.path.join(d, f)):
                return os.path.join(d, f)
    return None


def _narrow():
    """Point GStreamer at a directory holding only `PLUGINS`, linked (or,
    where a link cannot be made, copied) from where the bundle keeps them,
    with a registry of its own. Done only where the bundle has said where
    its plugins are; a GStreamer of the system's keeps its own."""
    told = os.environ.get("GST_PLUGIN_SYSTEM_PATH_1_0") or ""
    dirs = [d for d in (told + os.pathsep + (os.environ.get("GST_PLUGIN_PATH_1_0") or "")).split(os.pathsep)
            if d and os.path.isdir(d)]
    if not dirs:
        return
    found = {n: _plugin(n, dirs) for n in PLUGINS}
    found = {n: f for n, f in found.items() if f}
    key = hashlib.sha256("\n".join(sorted(found.values())).encode()).hexdigest()[:16]
    here = os.path.join(_cache_dir(), key)
    try:
        os.makedirs(here, exist_ok=True)
        for f in found.values():
            to = os.path.join(here, os.path.basename(f))
            if os.path.lexists(to):
                continue
            try:
                os.symlink(f, to)
            except OSError:
                try:
                    os.link(f, to)
                except OSError:
                    shutil.copy2(f, to)
    except OSError:
        return
    os.environ["GST_PLUGIN_SYSTEM_PATH_1_0"] = here
    os.environ["GST_PLUGIN_PATH_1_0"] = ""
    os.environ["GST_REGISTRY_1_0"] = os.path.join(here, "registry.bin")


def _load():
    """GStreamer, initialised once, or SoundError saying why not."""
    global _gst
    with _loading:
        if _gst is None:
            saved = {k: os.environ.get(k) for k in
                     ("GST_PLUGIN_SYSTEM_PATH_1_0", "GST_PLUGIN_PATH_1_0", "GST_REGISTRY_1_0")}
            _narrow()
            try:
                import gi
                gi.require_version("Gst", "1.0")
                from gi.repository import Gst
            except (ImportError, ValueError) as e:
                raise SoundError(f"no GStreamer ({e}): install bitwall[sound]") from e
            Gst.init(None)
            lacking = [n for n in NEEDS if not Gst.ElementFactory.find(n)]
            if lacking and os.environ.get("GST_PLUGIN_SYSTEM_PATH_1_0") != saved["GST_PLUGIN_SYSTEM_PATH_1_0"]:
                # The narrowed set lacks something here: every plugin, as
                # GStreamer would have had them, at the cost of the long scan.
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                Gst.update_registry()
                lacking = [n for n in NEEDS if not Gst.ElementFactory.find(n)]
            if lacking:
                raise SoundError(f"GStreamer has no {', '.join(lacking)}")
            _gst = Gst
    return _gst


def warm(said=None):
    """Start GStreamer now, on a thread of its own, so the first sound is
    not kept waiting by its first start. `said(text)`, where given, is
    told when that start is a long one and when it is done, or why there
    is no sound. The thread, started."""
    def go():
        t0 = time.monotonic()
        slow = threading.Timer(3.0, lambda: said and said(
            "sound: GStreamer is starting for the first time here, which scans its plugins; "
            "a sound wanted before it is done waits for it"))
        slow.daemon = True
        slow.start()
        try:
            _load()
        except SoundError as e:
            if said:
                said(f"no sound: {e}")
            return
        finally:
            slow.cancel()
        took = time.monotonic() - t0
        if said and took >= 3.0:
            said(f"sound: GStreamer ready after {took:.0f}s")
    th = threading.Thread(target=go, name="bitwall-sound-warm", daemon=True)
    th.start()
    return th


def missing():
    """Why this machine cannot play sound, or None where it can."""
    try:
        _load()
    except SoundError as e:
        return str(e)
    return None


class Sound:
    """The sound of `path` from `frm` seconds in, heard from Unix time
    `at`. Started when made; raises SoundError if it cannot be."""

    def __init__(self, path, frm=0.0, at=None):
        Gst = self._Gst = _load()
        self.path, self.frm = path, float(frm)
        self.at = time.time() if at is None else float(at)
        pipe = self._pipe = Gst.parse_launch(
            "filesrc name=src ! decodebin ! audioconvert ! audioresample ! autoaudiosink")
        pipe.get_by_name("src").set_property("location", str(path))
        clock = Gst.SystemClock.obtain()
        clock.set_property("clock-type", Gst.ClockType.REALTIME)
        pipe.use_clock(clock)
        # The base time is said here, not taken when it starts playing.
        pipe.set_start_time(Gst.CLOCK_TIME_NONE)
        pipe.set_state(Gst.State.PAUSED)
        if pipe.get_state(READY * Gst.SECOND)[0] == Gst.StateChangeReturn.FAILURE:
            why = self._error() or "it would not play"
            pipe.set_state(Gst.State.NULL)
            raise SoundError(f"{path}: {why}")
        # Started late, it starts that much further in, so the sample at
        # running time 0 is still the one due at its base time.
        late = max(0.0, time.time() - self.at)
        into = self.frm + late
        if into > 0:
            pipe.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                             int(into * Gst.SECOND))
            pipe.get_state(READY * Gst.SECOND)
        # Running time 0 is the sample at `into`, due at `at` + `late`; the
        # sink adds the pipeline's latency so that it is heard then.
        pipe.set_base_time(int((self.at + late) * Gst.SECOND))
        pipe.set_state(Gst.State.PLAYING)
        self.playing = True

    def _error(self):
        msg = self._pipe.get_bus().pop_filtered(self._Gst.MessageType.ERROR)
        return msg.parse_error()[0].message if msg else None

    def failed(self):
        """Why it stopped with an error, said once, or None."""
        why = self._error() if self.playing else None
        if why:
            self.stop()
        return why

    def ended(self):
        """Whether it has played to the end of the file."""
        if not self.playing:
            return True
        return self._pipe.get_bus().pop_filtered(self._Gst.MessageType.EOS) is not None

    def stop(self):
        """Stop it now. Stopping one stopped is nothing."""
        if self.playing:
            self._pipe.set_state(self._Gst.State.NULL)
            self.playing = False


def play(path, frm=0.0, at=None):
    """The sound of `path` from `frm` seconds in, heard from Unix time `at`
    (now, where None): a started `Sound`. Raises SoundError."""
    return Sound(path, frm, at)
