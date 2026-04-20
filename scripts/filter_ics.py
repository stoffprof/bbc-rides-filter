"""
Fetch an upstream Google Calendar ICS feed and produce a slimmed-down copy
that contains only recent and future events, suitable for subscription from
clients (like macOS Calendar) that choke on large ICS feeds.

Rules:
  - All VCALENDAR-level properties and every VTIMEZONE block are passed
    through unchanged.
  - Single, non-recurring events are kept only if DTSTART >= cutoff.
  - Recurring "master" events (VEVENTs with an RRULE) are kept whenever the
    series could still produce an occurrence at or after cutoff. If the RRULE
    has an UNTIL that is before cutoff, the whole master is dropped.
  - Per-instance overrides (VEVENTs with RECURRENCE-ID) are kept only if
    that instance's RECURRENCE-ID is at or after cutoff.
  - If a master is dropped, any of its overrides are dropped too.

Output is written to _site/<OUTPUT_FILENAME> so it can be served by
GitHub Pages via actions/deploy-pages.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from icalendar import Calendar

UPSTREAM_URL = os.environ.get(
    "UPSTREAM_ICS_URL",
    "https://calendar.google.com/calendar/ical/"
    "cuc66guoam1hhcqq2rnaniinl8%40group.calendar.google.com/public/basic.ics",
)

# How far back to keep non-recurring events. 7 days means "this week's past
# rides still show up on your calendar" which is usually what a subscriber
# wants.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "_site"))
OUTPUT_FILENAME = os.environ.get("OUTPUT_FILENAME", "bbc-rides.ics")


def _as_aware_utc(value) -> datetime:
    """Normalize a DTSTART/RECURRENCE-ID/UNTIL value to a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise TypeError(f"Unsupported temporal type: {type(value)!r}")


def _rrule_until(event) -> datetime | None:
    """Return the UNTIL from an RRULE as tz-aware UTC, or None if absent."""
    rrule = event.get("RRULE")
    if not rrule:
        return None
    until = rrule.get("UNTIL")
    if not until:
        return None
    # icalendar returns UNTIL as a list
    value = until[0] if isinstance(until, list) else until
    return _as_aware_utc(value)


def fetch_upstream(url: str) -> bytes:
    print(f"[fetch] {url}", file=sys.stderr)
    req = urllib.request.Request(
        url, headers={"User-Agent": "bbc-rides-filter/1.0 (+github-actions)"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    print(f"[fetch] received {len(data):,} bytes", file=sys.stderr)
    return data


def filter_calendar(src: Calendar, cutoff: datetime) -> Calendar:
    out = Calendar()
    # Copy top-level VCALENDAR properties (PRODID, VERSION, X-WR-*, etc.)
    for key, value in src.property_items(recursive=False):
        if key in ("BEGIN", "END"):
            continue
        out.add(key, value)

    # Pass through every VTIMEZONE block untouched.
    for vtz in src.walk("VTIMEZONE"):
        out.add_component(vtz)

    # First pass: decide which master UIDs survive and collect overrides by UID.
    kept_masters: set[str] = set()
    dropped_masters: set[str] = set()
    overrides: list = []
    singletons: list = []

    for ev in src.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        rrule = ev.get("RRULE")
        recurrence_id = ev.get("RECURRENCE-ID")

        if recurrence_id is not None:
            overrides.append(ev)
            continue

        if rrule is not None:
            until = _rrule_until(ev)
            if until is not None and until < cutoff:
                dropped_masters.add(uid)
            else:
                kept_masters.add(uid)
            continue

        singletons.append(ev)

    # Keep singletons whose DTSTART is on/after cutoff.
    for ev in singletons:
        dt = ev.get("DTSTART")
        if dt is None:
            continue
        if _as_aware_utc(dt.dt) >= cutoff:
            out.add_component(ev)

    # Keep masters we decided to keep.
    for ev in src.walk("VEVENT"):
        if ev.get("RRULE") is None or ev.get("RECURRENCE-ID") is not None:
            continue
        uid = str(ev.get("UID", ""))
        if uid in kept_masters:
            out.add_component(ev)

    # Keep overrides whose RECURRENCE-ID is on/after cutoff AND whose master
    # wasn't dropped.
    for ev in overrides:
        uid = str(ev.get("UID", ""))
        if uid in dropped_masters:
            continue
        rid = ev.get("RECURRENCE-ID")
        if _as_aware_utc(rid.dt) < cutoff:
            continue
        out.add_component(ev)

    return out


def main() -> int:
    raw = fetch_upstream(UPSTREAM_URL)
    src = Calendar.from_ical(raw)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    print(f"[filter] cutoff = {cutoff.isoformat()}", file=sys.stderr)

    out = filter_calendar(src, cutoff)

    before = len(list(src.walk("VEVENT")))
    after = len(list(out.walk("VEVENT")))
    print(f"[filter] VEVENTs: {before} -> {after}", file=sys.stderr)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / OUTPUT_FILENAME
    out_path.write_bytes(out.to_ical())
    print(f"[write] {out_path} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)

    # Drop a simple index.html so the Pages root isn't a 404.
    index_html = OUTPUT_DIR / "index.html"
    index_html.write_text(
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>BBC Rides (filtered)</title></head>\n"
        "<body style=\"font-family:system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.5\">\n"
        "<h1>BBC Rides (filtered feed)</h1>\n"
        f"<p>Subscribe from a calendar client using this URL:</p>\n"
        f"<p><code>{OUTPUT_FILENAME}</code> &mdash; "
        f"<a href=\"{OUTPUT_FILENAME}\">download / view</a></p>\n"
        "<p>This feed is regenerated from the upstream Google Calendar a few times "
        "a day and contains only recent and upcoming events.</p>\n"
        "</body></html>\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
