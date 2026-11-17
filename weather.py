"""Spoken weather for a list of towns.

The app has no server, so nothing can be voiced for one listener's exact
position. What it can do is voice the forecast for a short list of towns each
morning and let the app play the one nearest the listener. (On screen, the
app fetches the forecast for the listener's precise location itself.)

The towns are listed one per line in weather_places.txt, so adding a town is
a one-line edit, not a code change. A repository variable can override the
file without a commit:

    WEATHER_PLACES = "Denver, Colorado; Boise, Idaho; Austin, TX"

Data is Open-Meteo: free, no key, and free for non-commercial use. If this
ever becomes a paid product it needs their commercial plan.

The script is written by template from the forecast numbers, not by a model.
A forecast is the one part of the brief where an invented number is both easy
to produce and immediately checkable by looking out of the window.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 20

# How close a listener must be to a listed town to hear its forecast, in km.
# Published in episodes.json so the app and the build agree.
NEARBY_KM = 60

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}

# WMO weather codes -> (how it is right now, what to expect by day, by night,
# severity for picking a period's headline condition, kind of precipitation).
CODES = {
    0:  ("clear", "sunshine", "clear skies", 0, None),
    1:  ("mostly clear", "mostly sunny skies", "mostly clear skies", 1, None),
    2:  ("partly cloudy", "a mix of sun and cloud", "partly cloudy skies", 2, None),
    3:  ("overcast", "cloudy skies", "cloudy skies", 3, None),
    45: ("foggy", "fog", "fog", 4, None),
    48: ("foggy", "freezing fog", "freezing fog", 5, None),
    51: ("drizzling", "light drizzle", "light drizzle", 6, "rain"),
    53: ("drizzling", "drizzle", "drizzle", 7, "rain"),
    55: ("drizzling", "steady drizzle", "steady drizzle", 8, "rain"),
    56: ("drizzling", "freezing drizzle", "freezing drizzle", 12, "rain"),
    57: ("drizzling", "freezing drizzle", "freezing drizzle", 13, "rain"),
    61: ("raining lightly", "light rain", "light rain", 9, "rain"),
    63: ("raining", "rain", "rain", 11, "rain"),
    65: ("raining hard", "heavy rain", "heavy rain", 15, "rain"),
    66: ("raining", "freezing rain", "freezing rain", 16, "rain"),
    67: ("raining hard", "heavy freezing rain", "heavy freezing rain", 17, "rain"),
    71: ("snowing lightly", "light snow", "light snow", 10, "snow"),
    73: ("snowing", "snow", "snow", 14, "snow"),
    75: ("snowing hard", "heavy snow", "heavy snow", 18, "snow"),
    77: ("snowing lightly", "flurries", "flurries", 9, "snow"),
    80: ("showery", "a few showers", "a few showers", 9, "rain"),
    81: ("showery", "showers", "showers", 11, "rain"),
    82: ("pouring", "heavy showers", "heavy showers", 15, "rain"),
    85: ("snowing", "snow showers", "snow showers", 12, "snow"),
    86: ("snowing hard", "heavy snow showers", "heavy snow showers", 17, "snow"),
    95: ("stormy", "thunderstorms", "thunderstorms", 19, "rain"),
    96: ("stormy", "thunderstorms with hail", "thunderstorms with hail", 20, "rain"),
    99: ("stormy", "thunderstorms with hail", "thunderstorms with hail", 21, "rain"),
}
UNKNOWN = ("unsettled", "unsettled weather", "unsettled weather", 3, None)


def parse_places(raw: str) -> list[str]:
    """Split the WEATHER_PLACES variable into town names."""
    parts = re.split(r"[;\n|]+", raw or "")
    return [" ".join(p.split()) for p in parts if p.strip()][:12]


PLACES_FILE = Path(__file__).parent / "weather_places.txt"


def listed_places() -> list[str]:
    """The towns to voice: the WEATHER_PLACES variable if set, else the file."""
    raw = os.getenv("WEATHER_PLACES", "").strip()
    if not raw and PLACES_FILE.exists():
        raw = "\n".join(
            line for line in PLACES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#"))
    return parse_places(raw)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "place"


def geocode(place: str) -> dict:
    """Resolve "Town, State" to coordinates. Raises if nothing matches."""
    bits = [b.strip() for b in place.split(",") if b.strip()]
    town = bits[0]
    region = bits[1] if len(bits) > 1 else ""
    region = US_STATES.get(region.upper(), region)

    resp = requests.get(GEOCODE_URL, timeout=TIMEOUT, params={
        "name": town, "count": 10, "language": "en", "format": "json"})
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise LookupError(f"no town called {town!r} found")

    pick = None
    if region:
        want = region.lower()
        for r in results:
            names = [str(r.get(k, "")).lower()
                     for k in ("admin1", "country", "country_code")]
            if want in names:
                pick = r
                break
        if pick is None:
            raise LookupError(
                f"found {town!r}, but not in {region!r}. Matches were in: "
                + ", ".join(sorted({str(r.get('admin1') or r.get('country'))
                                    for r in results})))
    else:
        pick = results[0]

    return {
        "slug": slugify(f"{pick['name']}-{pick.get('admin1', '')}"),
        "name": pick["name"],
        "region": pick.get("admin1") or pick.get("country") or "",
        "lat": round(float(pick["latitude"]), 4),
        "lon": round(float(pick["longitude"]), 4),
    }


def fetch_forecast(lat: float, lon: float) -> dict:
    resp = requests.get(FORECAST_URL, timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": "auto", "forecast_days": 2,
    })
    resp.raise_for_status()
    return resp.json()


def _code(code) -> tuple:
    try:
        return CODES.get(int(code), UNKNOWN)
    except (TypeError, ValueError):
        return UNKNOWN


def _headline_code(codes: list[int]) -> int:
    """The condition that best describes a stretch of hours.

    The most severe condition that lasts at least two hours wins; a single
    stray hour of drizzle should not turn a sunny afternoon into "rain". If
    nothing lasts two hours, the most common condition does.
    """
    counts = Counter(int(c) for c in codes if c is not None)
    if not counts:
        return -1
    lasting = [c for c, n in counts.items() if n >= 2]
    if lasting:
        return max(lasting, key=lambda c: _code(c)[3])
    return counts.most_common(1)[0][0]


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _clock(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    return f"{hour % 12} {'in the morning' if hour < 12 else 'in the afternoon' if hour < 18 else 'in the evening'}"


def describe(place: dict, forecast: dict) -> dict:
    """Turn forecast data into the spoken script and the numbers behind it.

    Every figure in the script is read straight from `forecast`. Nothing is
    estimated, and a missing value drops its sentence rather than guessing.
    """
    cur = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    hourly = forecast.get("hourly") or {}

    now_iso = cur.get("time") or ""
    try:
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        now = None
    today = now_iso[:10]

    temp = cur.get("temperature_2m")
    high = (daily.get("temperature_2m_max") or [None])[0]
    low = (daily.get("temperature_2m_min") or [None])[0]
    if temp is None or high is None or low is None:
        raise ValueError("forecast is missing the current, high or low temperature")
    temp, high, low = round(temp), round(high), round(low)

    # The rest of today, hour by hour.
    rest = []
    for i, stamp in enumerate(hourly.get("time") or []):
        if not stamp.startswith(today):
            continue
        hour = int(stamp[11:13])
        if now is not None and hour < now.hour:
            continue
        rest.append({
            "hour": hour,
            "code": (hourly.get("weather_code") or [None] * (i + 1))[i],
            "pop": (hourly.get("precipitation_probability") or [None] * (i + 1))[i],
            "wind": (hourly.get("wind_speed_10m") or [None] * (i + 1))[i],
        })

    periods = []
    for label, lo_h, hi_h, night in (("this morning", 0, 12, False),
                                     ("this afternoon", 12, 18, False),
                                     ("this evening", 18, 24, True)):
        hours = [h for h in rest if lo_h <= h["hour"] < hi_h]
        if len(hours) < 2:      # a sliver of a period is not worth a clause
            continue
        code = _headline_code([h["code"] for h in hours])
        periods.append((label, _code(code)[2 if night else 1]))

    sentences = [
        f"In {place['name']}, it's {temp} degrees and {_code(cur.get('weather_code'))[0]}.",
        f"Today's high is {high}, with a low of {low}.",
    ]

    if periods:
        kinds = [p[1] for p in periods]
        if len(set(kinds)) == 1:
            span = ("for the rest of the day" if len(periods) > 1
                    else periods[0][0])
            sentences.append(f"Expect {kinds[0]} {span}.")
        else:
            # Fold neighbours that match: "sunshine this morning and afternoon".
            merged: list[list] = []
            for label, kind in periods:
                if merged and merged[-1][1] == kind:
                    merged[-1][0].append(label.replace("this ", ""))
                else:
                    merged.append([[label], kind])
            clauses = [f"{kind} {' and '.join(labels)}" for labels, kind in merged]
            sentences.append(f"Expect {_join(clauses)}.")

    # Rain or snow: only worth a sentence when it is reasonably likely.
    wet = [h for h in rest if h["pop"] is not None]
    if wet:
        peak = max(wet, key=lambda h: h["pop"])
        if peak["pop"] >= 30:
            kinds = [_code(h["code"])[4] for h in rest if _code(h["code"])[4]]
            kind = "snow" if kinds.count("snow") > kinds.count("rain") else "rain"
            sentences.append(
                f"The chance of {kind} peaks at {round(peak['pop'])} percent "
                f"around {_clock(peak['hour'])}.")

    winds = [h["wind"] for h in rest if h["wind"] is not None]
    if winds and max(winds) >= 20:
        sentences.append(f"It'll be breezy, with winds up to {round(max(winds))} miles an hour.")

    return {
        **place,
        "text": " ".join(sentences),
        "now": temp, "high": high, "low": low,
        "cond": _code(cur.get("weather_code"))[0],
    }


SAMPLE_FORECAST = {
    "current": {"time": "2026-11-16T07:30", "temperature_2m": 41.2,
                "weather_code": 2, "wind_speed_10m": 6.0},
    "daily": {"temperature_2m_max": [58.4, 55.0], "temperature_2m_min": [33.1, 30.0]},
    "hourly": {
        "time": [f"2026-11-16T{h:02d}:00" for h in range(24)],
        "temperature_2m": [36 + h for h in range(24)],
        "precipitation_probability": [0] * 15 + [10, 20, 35, 45, 40, 30, 20, 10, 5],
        "weather_code": [1] * 7 + [2] * 5 + [3] * 5 + [61] * 4 + [3] * 3,
        "wind_speed_10m": [5] * 12 + [9] * 12,
    },
}
SAMPLE_PLACE = {"slug": "denver-colorado", "name": "Denver", "region": "Colorado",
                "lat": 39.7392, "lon": -104.9903}


def build_reports(dry_run: bool = False) -> list[dict]:
    """A report for every listed town. Never raises; failures are named."""
    if dry_run:
        return [describe(SAMPLE_PLACE, SAMPLE_FORECAST)]

    names = listed_places()
    if not names:
        print("  · Weather: no towns listed, so nothing is spoken. Add towns "
              "to weather_places.txt, one per line, e.g. \"Denver, Colorado\". "
              "The on-screen forecast works regardless.")
        return []

    reports, seen = [], set()
    for name in names:
        try:
            place = geocode(name)
            if place["slug"] in seen:
                continue
            report = describe(place, fetch_forecast(place["lat"], place["lon"]))
            seen.add(place["slug"])
            reports.append(report)
            print(f"  + weather: {place['name']}, {place['region']} "
                  f"({report['now']}°, high {report['high']}, low {report['low']})")
        except Exception as exc:  # noqa: BLE001
            # Loud, and specific to the town: one misspelt name must not take
            # the others down, and must not pass unnoticed either.
            print(f"  ! weather: {name!r} FAILED ({exc.__class__.__name__}: "
                  f"{str(exc)[:140]})")
    return reports


def check() -> int:
    """Health check for the weather path, run as part of --check-feeds."""
    names = listed_places()
    print("Weather (Open-Meteo)")
    if not names:
        print("  no towns listed in weather_places.txt -- spoken weather is off\n")
        return 0
    bad = 0
    for name in names:
        try:
            place = geocode(name)
            report = describe(place, fetch_forecast(place["lat"], place["lon"]))
            print(f"  ok    {name:26s} -> {place['name']}, {place['region']} "
                  f"({place['lat']}, {place['lon']}) | {report['text'][:60]}")
        except Exception as exc:  # noqa: BLE001
            bad = 1
            print(f"  DEAD  {name:26s} {exc.__class__.__name__}: {str(exc)[:90]}")
    print()
    return bad
