#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The time and the weather on a band across the top of a wall, over
whatever it shows.

    senders/ticker/main.py pi.local
    senders/ticker/main.py --at bottom --speed 6 pi.local
    senders/ticker/main.py --place 3 --lat 48.86 --lon 2.35 --where Paris pi.local
    senders/ticker/main.py --font 20 pi.local

A cells layer above the base: a grid over the wall, every cell it does
not draw transparent, so a film, a dashboard or a sprite carries on
beneath it. The grid is the base's unless `--font` gives it one of its
own, which over a film is the only way to choose the size of the text. The band is three rows, a half-cell edge above and below and
the text between, on a translucent dark ground, so it reads over a bright
picture without hiding it. The clock stands still at the left and the
weather scrolls past it, a cell at a time.

The weather is Open-Meteo's (open-meteo.com), which needs no key: now,
the rest of today and tomorrow, asked every fifteen minutes and not at
all while nobody is watching the wall.

Where the weather is for is the host's place: `~/.bitwall/place.json`, or
the file `BITWALL_PLACE_FILE` names, `{"name": "Paris", "lat": 48.86,
"lon": 2.35}` with an optional `"tz"` for the clock, and `--where`,
`--lat` and `--lon` over it. With no place the band is the clock alone.

`--place` is where it goes among the layers: `above` the lowest free,
`top` the highest, or a number; a controller says it in `BITWALL_PLACE`.
A wall whose `caps` do not offer a cells layer above the base refuses it,
and the ticker says so and stops.
"""
import argparse
import datetime
import json
import os
import signal
import sys
import threading
import time
import urllib.request
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import bitwall                                                  # noqa: E402

#: Where the host is, kept for every sender that wants it rather than in
#: one sender's options.
PLACE = os.path.expanduser("~/.bitwall/place.json")
#: How often the weather is asked, and how soon again after a failure.
EVERY = 15 * 60
RETRY = 60
API = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
       "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
       "weather_code,wind_speed_10m,wind_direction_10m"
       "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
       "precipitation_probability_max,sunrise,sunset"
       "&timezone=auto&forecast_days=2")

#: The band: a dark ground at three quarters, so a picture shows through it.
BAND = "101418c0"
CLOCK = "ffffff"
TEXT = "e8e4da"
DIM = "9aa3ad"
WARM, COOL = "ffb454", "7fc8ff"
GAP = "   ·   "

#: WMO weather codes, as words.
WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
       45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
       55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
       67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Heavy showers",
       85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms",
       96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


def place():
    """The host's place as a dict, name, lat, lon and maybe tz; empty
    where none is kept."""
    path = os.environ.get("BITWALL_PLACE_FILE") or PLACE
    try:
        with open(path) as fh:
            got = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"{path}: unreadable, no place: {e}")
        return {}
    return got if isinstance(got, dict) else {}


def fetch(lat, lon):
    """Open-Meteo's answer for the place, as JSON."""
    req = urllib.request.Request(API.format(lat=lat, lon=lon),
                                 headers={"User-Agent": "bitwall-ticker"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def hhmm(iso):
    """`2026-10-07T18:52` as `18:52`."""
    return iso[11:16] if iso and len(iso) >= 16 else "?"


def pieces(where, got):
    """The weather as the runs of text the band scrolls: (text, colour)."""
    now, day = got["current"], got["daily"]
    out = [(where, CLOCK), ("  ", TEXT),
           (f"{now['temperature_2m']:.0f}°", WARM),
           (f"  feels {now['apparent_temperature']:.0f}°", DIM),
           ("  " + WMO.get(now["weather_code"], "—"), TEXT),
           (f"  wind {now['wind_speed_10m']:.0f} km/h "
            + COMPASS[round(now["wind_direction_10m"] / 45) % 8], DIM),
           (f"  humidity {now['relative_humidity_2m']:.0f}%", DIM),
           (GAP, DIM),
           ("Today ", TEXT),
           (f"{day['temperature_2m_max'][0]:.0f}°", WARM), (" / ", DIM),
           (f"{day['temperature_2m_min'][0]:.0f}°", COOL),
           ("  " + WMO.get(day["weather_code"][0], "—"), TEXT),
           (f"  rain {day['precipitation_probability_max'][0] or 0:.0f}%", DIM),
           (f"  sunset {hhmm(day['sunset'][0])}", DIM),
           (GAP, DIM),
           ("Tomorrow ", TEXT),
           (f"{day['temperature_2m_max'][1]:.0f}°", WARM), (" / ", DIM),
           (f"{day['temperature_2m_min'][1]:.0f}°", COOL),
           ("  " + WMO.get(day["weather_code"][1], "—"), TEXT),
           (f"  rain {day['precipitation_probability_max'][1] or 0:.0f}%", DIM),
           (f"  sunrise {hhmm(day['sunrise'][1])}", DIM),
           (GAP, DIM)]
    return out


class Ticker:
    """The band, as a source: `render(cols, rows)` is the whole grid, every
    cell transparent but the band's three rows."""

    def __init__(self, where, at, speed, tz=None, weather=True):
        self.where, self.at, self.speed, self.tz = where, at, speed, tz
        self.runs = [(f"{where}  fetching the weather", DIM), (GAP, DIM)] \
            if weather else []
        self.start = time.monotonic()
        self.lock = threading.Lock()

    def weather(self, got):
        with self.lock:
            self.runs = pieces(self.where, got)

    def failed(self, why):
        with self.lock:
            if self.runs and self.runs[0][1] == DIM:
                self.runs = [(f"{self.where}  no weather: {why}", DIM), (GAP, DIM)]

    def strip(self):
        """The scrolling text as cells, one lap of it."""
        with self.lock:
            runs = list(self.runs)
        return [bitwall.cell(ch, colour, BAND) for text, colour in runs for ch in text]

    def render(self, cols, rows):
        clear = bitwall.cell(" ")
        grid = [[clear] * cols for _ in range(rows)]
        if rows < 3 or cols < 12:
            return grid
        top = 0 if self.at == "top" else rows - 3
        # The band's edges are half a cell: the band's colour as the ink of
        # a half block over nothing, so the band is two rows deep, not three.
        grid[top] = [bitwall.cell("▄", BAND)] * cols
        grid[top + 2] = [bitwall.cell("▀", BAND)] * cols
        clock = datetime.datetime.now(self.tz).strftime(" %H:%M ")
        left = [bitwall.cell(ch, CLOCK, BAND, bold=True) for ch in clock] \
            + [bitwall.cell("│", DIM, BAND)]
        lap = self.strip()
        if not lap:
            grid[top + 1] = (left + [bitwall.cell(" ", TEXT, BAND)] * cols)[:cols]
            return grid
        room = cols - len(left)
        step = int((time.monotonic() - self.start) * self.speed) % max(1, len(lap))
        moving = [lap[(step + i) % len(lap)] for i in range(room)]
        grid[top + 1] = (left + moving + [bitwall.cell(" ", TEXT, BAND)] * cols)[:cols]
        return grid


def parse(argv):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--at", choices=("top", "bottom"), default="top",
                    help="the edge of the wall the band runs along (default top)")
    ap.add_argument("--place", default=os.environ.get("BITWALL_PLACE") or "above",
                    help="above, top, or a layer's number (default above)")
    ap.add_argument("--font", default="base",
                    help="the band's font size, or base to use the base's grid "
                         "(default base)")
    ap.add_argument("--speed", type=float, default=6.0,
                    help="cells a second the weather moves (default 6)")
    ap.add_argument("--lat", type=float, default=None,
                    help="the weather's latitude (default the host's place)")
    ap.add_argument("--lon", type=float, default=None,
                    help="the weather's longitude (default the host's place)")
    ap.add_argument("--where", default=None,
                    help="the place's name, as the band says it "
                         "(default the host's place)")
    ap.add_argument("target", help="the wall, host:wall")
    a = ap.parse_args(argv)
    a.speed = min(30.0, max(1.0, a.speed))
    # A place given on the command line is a different place from the one
    # kept, so none of the kept one's name or zone goes with it.
    kept = place() if a.lat is None and a.lon is None else {}
    a.lat = kept.get("lat") if a.lat is None else a.lat
    a.lon = kept.get("lon") if a.lon is None else a.lon
    if a.where is None:
        a.where = kept.get("name") or ""
    a.tz = None
    if kept.get("tz"):
        try:
            a.tz = zoneinfo.ZoneInfo(kept["tz"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
            log(f"place tz {kept['tz']!r} unknown, the host's clock instead: {e}")
    if a.place not in ("above", "top") and not a.place.isdigit():
        ap.error(f"--place {a.place!r}: above, top or a number")
    if a.font != "base" and not (a.font.isdigit() and 4 <= int(a.font) <= 400):
        ap.error(f"--font {a.font!r}: base, or a size from 4 to 400")
    return a


def main():
    a = parse(sys.argv[1:])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    located = a.lat is not None and a.lon is not None
    if not located:
        log(f"no place: the clock alone, no weather (give --lat and --lon, "
            f"or keep one in {PLACE})")
    ticker = Ticker(a.where, a.at, a.speed, a.tz, weather=located)
    place = int(a.place) if a.place.isdigit() else a.place
    w = bitwall.CellsLayer(a.target, place=place, min_frame=1.0 / a.speed).attach()
    if w.z is None:
        log(f"{a.target}: no cells layer {a.place} the base: "
            f"{getattr(w, '_refused', None) or 'the wall did not open one'}")
        w.detach()
        return 1
    about = (f"The time and the weather in {a.where or 'one place'}, scrolling"
             if located else "The time")
    w.meta(name="Ticker", description=f"{about} along the {a.at} of the wall")
    if a.font != "base":
        w.set_font_size(int(a.font))
    w.set_source(ticker.render)
    cols, rows = w.area()
    log(f"ticker -> {a.target} layer {w.z}, {cols}x{rows}, band at the {a.at}, "
        + (f"{a.where or 'unnamed'} ({a.lat}, {a.lon}), " if located else "no place, ")
        + f"{a.speed:g} cells a second, font {a.font}")

    def weather():
        due = 0.0
        while not stop.is_set():
            if time.monotonic() >= due and w.watching() != 0:
                try:
                    ticker.weather(fetch(a.lat, a.lon))
                    log("weather fetched")
                    due = time.monotonic() + EVERY
                except Exception as e:                          # noqa: BLE001
                    why = getattr(e, "reason", None) or e
                    log(f"weather failed: {why}")
                    ticker.failed(str(why))
                    due = time.monotonic() + RETRY
            stop.wait(5)

    if located:
        threading.Thread(target=weather, daemon=True).start()
    seen = (cols, rows)
    while not stop.wait(1):
        if w.area() != seen:
            seen = w.area()
            log(f"area {seen[0]}x{seen[1]}")
    log("stopping")
    w.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
