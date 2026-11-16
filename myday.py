"""My Day: a private plan of the owner's calendar, published encrypted.

The site is public. Anyone with the address can read every file on it, and
the build log is public too. A calendar is not something to put in either
place, so this module is built around three rules:

1. **Nothing readable is published.** The plan and its spoken rundown are
   encrypted with AES-256-GCM before they touch ./public. The key lives in a
   repository secret and on the owner's phone, nowhere else. What the public
   sees is ciphertext and a date.

2. **Nothing about the calendar is logged.** Not a title, not a name, not a
   count. Exceptions are reported by class name only, because an error
   message from an HTTP library contains the URL it failed on, and the URL
   *is* the credential -- Google's private calendar address is a secret link.

3. **Facts come from the calendar, judgment comes from the model.** Times,
   titles and places are copied from the feed verbatim. Claude contributes
   only the overview and the prep notes, keyed to events by number, so a
   model that misreads something cannot move a meeting.

Configuration, all optional. With either secret missing the feature is off
and the build carries on without it:

    CALENDAR_ICS_URL   secret   Google Calendar's "secret address in iCal format"
    MYDAY_KEY          secret   32 random bytes, base64url (the app can make one)
    MYDAY_TZ           variable IANA timezone, default America/Denver
    MYDAY_EMAIL        variable your address, to hide meetings you declined
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# Longest the spoken rundown may run, in seconds.
RUNDOWN_LIMIT = 45.0

PLAN_FILE = "myday/day.json.enc"
AUDIO_FILE = "myday/day.mp3.enc"
INDEX_FILE = "myday/index.json"


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------

def load_key(raw: str) -> bytes:
    """Decode MYDAY_KEY. Raises ValueError without echoing the key."""
    text = (raw or "").strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:  # noqa: BLE001
        raise ValueError("MYDAY_KEY is not valid base64url") from None
    if len(key) != 32:
        raise ValueError(f"MYDAY_KEY must decode to 32 bytes, got {len(key)}")
    return key


def encrypt(key: bytes, plaintext: bytes, aad: str) -> bytes:
    """AES-256-GCM. Output is nonce (12 bytes) followed by ciphertext+tag.

    `aad` ties a file to its date and its role, so a stale or swapped file
    fails to decrypt rather than showing yesterday's plan as today's. The
    browser side (crypto.subtle) reads exactly this layout.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad.encode("utf-8"))


def decrypt(key: bytes, blob: bytes, aad: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(blob[:12], blob[12:], aad.encode("utf-8"))


def aad_for(date_str: str, kind: str) -> str:
    return f"whatsup-myday-v1|{date_str}|{kind}"


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def _clean(text, limit: int) -> str:
    text = str(text or "")
    text = re.sub(r"<[^>]+>", " ", text)            # invites are often HTML
    text = re.sub(r"https?://\S+", " ", text)       # links are noise to a model
    return " ".join(text.split())[:limit]


def _attendee_name(att) -> str:
    cn = ""
    try:
        cn = str(att.params.get("CN", ""))
    except Exception:  # noqa: BLE001
        pass
    addr = str(att).replace("mailto:", "").replace("MAILTO:", "")
    return cn or addr


def fetch_calendar(url: str):
    """Download and parse the private feed. Raises on any failure."""
    import icalendar
    import requests

    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    resp = requests.get(url, timeout=25, headers={"User-Agent": "whatsup-myday/1"})
    resp.raise_for_status()
    return icalendar.Calendar.from_ical(resp.content)


def events_between(cal, start: datetime, end: datetime, tz: ZoneInfo,
                   my_email: str = "") -> list[dict]:
    """Events overlapping [start, end), recurring ones expanded, in local time."""
    import recurring_ical_events

    owner = (my_email or "").strip().lower()
    if not owner:
        # A primary Google calendar is named after its owner's address.
        name = str(cal.get("X-WR-CALNAME", "")).strip().lower()
        if "@" in name:
            owner = name

    out: list[dict] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue

        attendees = ev.get("ATTENDEE") or []
        if not isinstance(attendees, list):
            attendees = [attendees]

        declined = False
        names: list[str] = []
        for att in attendees:
            addr = str(att).lower().replace("mailto:", "")
            partstat = ""
            try:
                partstat = str(att.params.get("PARTSTAT", "")).upper()
            except Exception:  # noqa: BLE001
                pass
            if owner and addr == owner:
                declined = declined or partstat == "DECLINED"
                continue
            names.append(_attendee_name(att))
        if declined:
            continue

        raw_start = ev.get("DTSTART").dt
        raw_end = ev.get("DTEND").dt if ev.get("DTEND") else None
        all_day = not isinstance(raw_start, datetime)

        if all_day:
            s = datetime.combine(raw_start, time.min, tz)
            e = datetime.combine(raw_end or raw_start + timedelta(days=1), time.min, tz)
        else:
            s = raw_start if raw_start.tzinfo else raw_start.replace(tzinfo=tz)
            s = s.astimezone(tz)
            if raw_end is None:
                e = s
            else:
                e = raw_end if raw_end.tzinfo else raw_end.replace(tzinfo=tz)
                e = e.astimezone(tz)

        description = str(ev.get("DESCRIPTION", "") or "")
        location = _clean(ev.get("LOCATION", ""), 120)
        online = bool(re.search(r"zoom\.us|meet\.google|teams\.microsoft|webex",
                                description + " " + str(ev.get("LOCATION", "")), re.I))

        out.append({
            "start": s.isoformat(),
            "end": e.isoformat(),
            "all_day": all_day,
            "title": _clean(ev.get("SUMMARY", ""), 140) or "(no title)",
            "location": location,
            "online": online,
            "who": names[:8],
            "who_count": len(names),
            "description": _clean(description, 500),
        })

    out.sort(key=lambda x: (not x["all_day"], x["start"]))
    return out


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

DAY_TOOL = {
    "name": "publish_day",
    "description": "Publish the plan for the listener's day.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overview": {
                "type": "string",
                "description": "Two or three sentences on the shape of the day.",
            },
            "notes": {
                "type": "array",
                "description": "One entry per event that deserves a prep note.",
                "items": {
                    "type": "object",
                    "properties": {
                        "event": {"type": "integer",
                                  "description": "The event's number from the list."},
                        "prep": {"type": "string",
                                 "description": "One or two sentences: what to have ready."},
                        "priority": {"type": "string", "enum": ["high", "normal"]},
                    },
                    "required": ["event", "prep", "priority"],
                },
            },
            "heads_up": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("Zero to three short flags about the day as a "
                                "whole: back-to-backs, no lunch gap, travel time, "
                                "something tomorrow that needs work today."),
            },
            "script": {
                "type": "string",
                "description": ("The spoken rundown, 80 to 100 words, hard maximum "
                                "105. Plain spoken prose only."),
            },
        },
        "required": ["overview", "notes", "heads_up", "script"],
    },
}


def _fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M %p")


def _event_lines(events: list[dict]) -> str:
    lines = []
    for i, ev in enumerate(events):
        when = ("all day" if ev["all_day"]
                else f"{_fmt_time(ev['start'])} to {_fmt_time(ev['end'])}")
        bits = [f"[{i}] {when}: {ev['title']}"]
        if ev["location"]:
            bits.append(f"    where: {ev['location']}")
        elif ev["online"]:
            bits.append("    where: video call")
        if ev["who"]:
            extra = ev["who_count"] - len(ev["who"])
            bits.append("    with: " + ", ".join(ev["who"])
                        + (f" and {extra} more" if extra > 0 else ""))
        if ev["description"]:
            bits.append(f"    notes on the invite: {ev['description']}")
        lines.append("\n".join(bits))
    return "\n".join(lines) if lines else "(nothing scheduled)"


def write_plan(today: list[dict], tomorrow: list[dict], date_label: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(timeout=120.0, max_retries=2)
    system = (ROOT / "myday_prompt.md").read_text(encoding="utf-8")
    user = (
        f"Today is {date_label}.\n\n"
        f"TODAY'S CALENDAR:\n{_event_lines(today)}\n\n"
        f"TOMORROW, for context only:\n{_event_lines(tomorrow)}\n\n"
        "Write the plan and publish it with the publish_day tool."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[DAY_TOOL],
        tool_choice={"type": "tool", "name": "publish_day"},
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "publish_day":
            return dict(block.input)
    raise RuntimeError("model did not call publish_day")


def assemble(today: list[dict], tomorrow: list[dict], plan: dict,
             date_str: str, tz_name: str) -> dict:
    """Merge calendar facts with the model's notes into what the app shows."""
    notes: dict[int, dict] = {}
    raw_notes = plan.get("notes")
    if isinstance(raw_notes, list):
        for n in raw_notes:
            if not isinstance(n, dict):
                continue
            try:
                idx = int(n.get("event"))
            except (TypeError, ValueError):
                continue
            # A note pointing at an event that does not exist is dropped, not
            # attached to a neighbour.
            if 0 <= idx < len(today):
                notes[idx] = n

    events = []
    for i, ev in enumerate(today):
        note = notes.get(i, {})
        events.append({
            "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"],
            "title": ev["title"], "location": ev["location"],
            "online": ev["online"], "who": ev["who"], "who_count": ev["who_count"],
            "prep": str(note.get("prep", "")).strip(),
            "priority": "high" if note.get("priority") == "high" else "normal",
        })

    heads = plan.get("heads_up")
    if isinstance(heads, str):
        heads = [heads]
    heads = [str(h).strip() for h in (heads or []) if str(h).strip()][:3]

    return {
        "v": 1,
        "date": date_str,
        "tz": tz_name,
        "generated": datetime.now(timezone.utc).isoformat(),
        "overview": str(plan.get("overview", "")).strip(),
        "heads_up": heads,
        "events": events,
        "tomorrow": [{"start": ev["start"], "all_day": ev["all_day"],
                      "title": ev["title"]} for ev in tomorrow[:6]],
    }


SAMPLE_TODAY = [
    {"start": "T09:00", "end": "T09:30", "all_day": False,
     "title": "Team standup", "location": "", "online": True,
     "who": ["Priya Raman", "Marcus Lee"], "who_count": 2, "description": ""},
    {"start": "T11:00", "end": "T12:00", "all_day": False,
     "title": "Quarterly review with Northfield", "location": "Conference room B",
     "online": False, "who": ["Dana Whitlock", "Chris Okafor", "Elena Ruiz"],
     "who_count": 3, "description": "Bring the Q3 numbers."},
    {"start": "T12:00", "end": "T12:45", "all_day": False,
     "title": "Lunch with Jordan", "location": "Table Six", "online": False,
     "who": ["Jordan Avery"], "who_count": 1, "description": ""},
    {"start": "T15:30", "end": "T16:00", "all_day": False,
     "title": "Dentist", "location": "Aspen Dental", "online": False,
     "who": [], "who_count": 0, "description": ""},
]


def _sample(date_str: str, tz: ZoneInfo) -> tuple[list[dict], list[dict], dict]:
    """Made-up events for --dry-run, so the app can be tested without a
    calendar, an API key, or anyone's real schedule."""
    def at(day: str, hhmm: str) -> str:
        h, m = hhmm[1:].split(":")
        d = date.fromisoformat(day)
        return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=tz).isoformat()

    today = [dict(ev, start=at(date_str, ev["start"]), end=at(date_str, ev["end"]))
             for ev in SAMPLE_TODAY]
    nxt = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    tomorrow = [{"start": at(nxt, "T08:30"), "end": at(nxt, "T09:30"),
                 "all_day": False, "title": "Board prep", "location": "",
                 "online": True, "who": [], "who_count": 0, "description": ""}]
    plan = {
        "overview": ("A front-loaded day. The Northfield review at eleven is the "
                     "one that matters, and it runs straight into lunch."),
        "notes": [
            {"event": 1, "priority": "high",
             "prep": "Have the Q3 numbers open and know the two lines you expect "
                     "questions on."},
            {"event": 2, "priority": "normal",
             "prep": "The review ends as lunch starts; leave five minutes early "
                     "or warn Jordan."},
        ],
        "heads_up": ["No gap between the review and lunch.",
                     "Board prep is first thing tomorrow."],
        "script": ("Here's your day. Standup at nine, then the one that matters: "
                   "the Northfield quarterly review at eleven. Have the Q3 numbers "
                   "ready. It runs right into lunch with Jordan, so watch the "
                   "clock. Dentist at three thirty. Tomorrow starts with board "
                   "prep, so leave some time this afternoon. " * 1),
    }
    return today, tomorrow, plan


# --------------------------------------------------------------------------

def build(public_dir: Path, dry_run: bool = False, skip_audio: bool = False) -> bool:
    """Build and publish today's encrypted plan. Returns True if published.

    Never raises and never prints calendar content. Clears any previous
    plan first, so a failure leaves "not ready" rather than yesterday's day
    dressed up as today's.
    """
    out_dir = public_dir / "myday"
    shutil.rmtree(out_dir, ignore_errors=True)

    url = os.getenv("CALENDAR_ICS_URL", "").strip()
    raw_key = os.getenv("MYDAY_KEY", "").strip()
    tz_name = os.getenv("MYDAY_TZ", "").strip() or "America/Denver"

    if not raw_key or (not url and not dry_run):
        missing = [n for n, v in (("CALENDAR_ICS_URL", url), ("MYDAY_KEY", raw_key))
                   if not v and not (dry_run and n == "CALENDAR_ICS_URL")]
        print(f"  · My Day is off ({', '.join(missing)} not set)")
        return False

    try:
        key = load_key(raw_key)
    except ValueError as exc:
        print(f"  ! My Day: {exc}")
        return False

    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        print(f"  ! My Day: unknown timezone in MYDAY_TZ, using America/Denver")
        tz_name, tz = "America/Denver", ZoneInfo("America/Denver")

    now_local = datetime.now(tz)
    date_str = now_local.strftime("%Y-%m-%d")
    date_label = now_local.strftime("%A, %B %-d, %Y")
    day_start = datetime.combine(now_local.date(), time.min, tz)
    day_end = day_start + timedelta(days=1)

    # Each stage reports its own failure by class name only. See rule 2 above:
    # the text of these exceptions can contain the feed URL or event details.
    stage = "start"
    try:
        if dry_run:
            today, tomorrow, plan = _sample(date_str, tz)
        else:
            stage = "fetching the calendar"
            cal = fetch_calendar(url)
            stage = "reading the calendar"
            my_email = os.getenv("MYDAY_EMAIL", "")
            today = events_between(cal, day_start, day_end, tz, my_email)
            tomorrow = events_between(cal, day_end, day_end + timedelta(days=1),
                                      tz, my_email)
            if today:
                stage = "writing the plan"
                plan = write_plan(today, tomorrow, date_label)
            else:
                plan = {"overview": "Nothing on your calendar today.",
                        "notes": [], "heads_up": [], "script": ""}

        stage = "assembling the plan"
        doc = assemble(today, tomorrow, plan, date_str, tz_name)

        out_dir.mkdir(parents=True, exist_ok=True)
        has_audio = False
        script = str(plan.get("script", "")).strip()
        if script and not skip_audio and len(script.split()) >= 20:
            stage = "recording the rundown"
            has_audio = _record(script, key, date_str, public_dir, dry_run, doc)

        stage = "encrypting the plan"
        blob = encrypt(key, json.dumps(doc).encode("utf-8"), aad_for(date_str, "plan"))
        (public_dir / PLAN_FILE).write_bytes(blob)
        (public_dir / INDEX_FILE).write_text(json.dumps({
            "v": 1, "date": date_str, "updated": doc["generated"],
            "plan": PLAN_FILE, "audio": AUDIO_FILE if has_audio else None,
        }), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"  ! My Day failed while {stage} ({exc.__class__.__name__}). "
              f"Details are withheld from this public log on purpose.")
        return False

    print(f"  + My Day published, encrypted"
          f"{' with spoken rundown' if has_audio else ''}")
    return True


def _record(script: str, key: bytes, date_str: str, public_dir: Path,
            dry_run: bool, doc: dict) -> bool:
    """Speak the rundown, encrypt it, and never leave the MP3 in ./public."""
    import contextlib
    import io
    import subprocess

    import tts

    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "rundown.mp3"
        # tts prints progress lines that are harmless, but swallow them
        # anyway: this module's contract is that it logs nothing it did not
        # write itself.
        sink = io.StringIO()
        try:
            if dry_run and os.getenv("DRY_RUN_SILENT_AUDIO") == "1":
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "sine=frequency=330:sample_rate=24000", "-t", "20",
                     "-ac", "1", "-b:a", "32k", str(mp3)], check=True)
            else:
                with contextlib.redirect_stdout(sink):
                    tts.synthesize(script, mp3)
            with contextlib.redirect_stdout(sink):
                tts.fit_duration(mp3, RUNDOWN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! My Day: no spoken rundown today ({exc.__class__.__name__})")
            return False

        doc["audio_seconds"] = tts.duration_seconds(mp3)
        blob = encrypt(key, mp3.read_bytes(), aad_for(date_str, "audio"))
        (public_dir / AUDIO_FILE).write_bytes(blob)
    return True
