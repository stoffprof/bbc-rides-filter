"""
Scrape the Bloomington Bicycle Club's ClubExpress calendar and publish it as
subscribable ICS feeds, one per combination of ride types.

The club's calendar moved from a public Google Calendar (which had an ICS
feed) to ClubExpress, which has no feed; each event only offers a one-off
"Add to calendar" download. So this script rebuilds the feed from the HTML:

  1. The "Future" list view gives every upcoming occurrence in one page,
     each with a full date and start time.
  2. The current month's list view (plus the previous month's, early in a
     month) supplies the recent past, so this week's rides don't vanish
     from subscribers' calendars the moment they start.
  3. Each event's detail page supplies the end time, full address,
     category, ride host, route link, and description.

Recurring events share one detail page (item_id) across dates (distinguished
by event_date_id), and that page always shows the series' first date. So for
those, the date comes from the list and only the duration comes from the
detail page. One-off events take their times from the detail page, which
also handles multi-day events that the list repeats once per day.

Output (in OUTPUT_DIR):
  - rides-<mask>.ics for every non-empty combination of RIDE_TYPES, where
    bit i of mask selects RIDE_TYPES[i]. index.html builds these URLs.
  - bbc-rides-<mask>.ics for masks 1-63, and bbc-rides.ics (everything),
    kept so subscriptions made against the old Google-based feed keep
    working. See legacy_types().
  - last-updated.json, used by the next run to skip unchanged deploys.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from icalendar import Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.bloomingtonbicycleclub.org"
CLUB_ID = "144345"
LIST_URL = f"{BASE_URL}/content.aspx?page_id=4001&club_id={CLUB_ID}&action=cira&sif=0"
PUBLIC_CALENDAR_URL = f"{LIST_URL}&vm=MonthView"

# The site's firewall rejects some generic clients (e.g. python-requests),
# but accepts an honest bot User-Agent.
USER_AGENT = "bbc-rides-filter/2.0 (+https://github.com/stoffprof/bbc-rides-filter)"

# Pause between detail-page requests, to be polite to the club's site.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))

# Keep events that ended within this many days, so recent rides stay on
# subscribers' calendars.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

# Used when an event's detail page can't be read.
DEFAULT_DURATION = timedelta(hours=2)

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "_site"))
LAST_UPDATED_FILENAME = "last-updated.json"

# URL of the previously published last-updated.json (on the live Pages site),
# used to detect whether the calendar changed since the last run. Empty means
# "no previous state available", so the feeds are always rebuilt.
PREVIOUS_STATE_URL = os.environ.get("PREVIOUS_STATE_URL", "")

# Set FORCE_REBUILD=true to publish even when the calendar is unchanged
# (used for pushes and manual workflow runs, so site changes still deploy).
FORCE_REBUILD = os.environ.get("FORCE_REBUILD", "").lower() in ("1", "true", "yes")

CLUB_TZ = ZoneInfo("America/Indiana/Indianapolis")
DISPLAY_TZ = ZoneInfo("America/New_York")

# Ride types in bitmask order (bit 0 = index 0, etc.). The first six keep the
# bit positions of the old Google-based feed.
RIDE_TYPES = ["metric", "easy", "early", "iride", "growlers", "other", "owls", "epic"]

# ClubExpress category -> ride type. Anything unlisted is "other".
CATEGORY_TYPES = {
    "Metric Monday Ride": "metric",
    "Nice and Easy Ride": "easy",
    "Early Birds": "early",
    "iRide": "iride",
    "Growlers Adventure Ride": "growlers",
    "OWLS Ride": "owls",
    "Saturday Epic Ride": "epic",
}

# Boilerplate that opens nearly every event description.
TRIAL_MEMBERSHIP_RE = re.compile(
    r"^Non-members should sign up for a free trial membership.*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Occurrence:
    """One dated entry from a calendar list view."""

    item_id: str
    event_date_id: str | None
    title: str
    location: str
    start: datetime | date  # date means all-day
    url: str


@dataclass
class Details:
    """What an event's detail page says about the event (or its series)."""

    title: str
    start: datetime | date | None
    end: datetime | date | None
    location: str
    category: str
    contacts: str
    links: list[str]
    registration: str
    about: str


@dataclass
class RideEvent:
    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    location: str
    ride_type: str
    category: str
    description: str
    url: str


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_page(session: requests.Session, url: str) -> BeautifulSoup:
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def postback(
    session: requests.Session, url: str, page: BeautifulSoup, target: str
) -> BeautifulSoup:
    """Simulate an ASP.NET __doPostBack(target) from an already-loaded page."""
    form = page.find("form")
    if form is None:
        raise RuntimeError(f"no form on {url} to post back")
    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type", "text") in ("hidden", "text"):
            data[name] = inp.get("value", "")
    data["__EVENTTARGET"] = target
    data["__EVENTARGUMENT"] = ""
    resp = session.post(url, data=data, headers={"Referer": url}, timeout=60)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_list(page: BeautifulSoup) -> list[Occurrence]:
    """Parse the events on a list view (Future or MonthView).

    The Future view shows full dates ("Tue, Sep 29, 2026 at 9:00 AM"). The
    month view shows only a time, so the date comes from the preceding
    "<h2>September, 2026</h2>" month header and the day-number badge.
    """
    occurrences = []
    month_year = None
    for node in page.select(
        "[id*=event_list_repeater_list_month_header] h2, .list-event-container"
    ):
        if node.name == "h2":
            month_year = datetime.strptime(clean(node.get_text()), "%B, %Y")
            continue

        link = node.select_one(".event-list-title a[href]")
        when = node.select_one(".list-date-time")
        if link is None or when is None:
            continue
        href = urljoin(BASE_URL, link["href"])
        query = parse_qs(urlparse(href).query)
        if "item_id" not in query:
            continue
        location = node.select_one(".location-literal")

        when_text = clean(when.get_text())
        full = re.match(r"\w+, (\w+ \d{1,2}, \d{4})(?: at (\d{1,2}:\d{2} [AP]M))?", when_text)
        if full:
            start = combine(parse_date(full.group(1)), full.group(2))
        else:
            day = node.select_one(".big-date-day")
            if month_year is None or day is None:
                print(f"[warn] can't date list entry {when_text!r}", file=sys.stderr)
                continue
            day_date = month_year.replace(day=int(clean(day.get_text()))).date()
            time_match = re.search(r"\d{1,2}:\d{2} [AP]M", when_text)
            start = combine(day_date, time_match.group(0) if time_match else None)

        occurrences.append(Occurrence(
            item_id=query["item_id"][0],
            event_date_id=query.get("event_date_id", [None])[0],
            title=clean(link.get_text()),
            location=clean(location.get_text()) if location else "",
            start=start,
            url=href,
        ))
    return occurrences


def parse_date(text: str) -> date:
    """ "October 4, 2026" (detail pages) or "Oct 4, 2026" (list views)."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date {text!r}")


def combine(day: date, time_text: str | None) -> datetime | date:
    """A local date plus an optional "1:00 PM" time; no time means all-day."""
    if not time_text:
        return day
    t = datetime.strptime(time_text, "%I:%M %p").time()
    return datetime.combine(day, t, tzinfo=CLUB_TZ)


DATE_PART = r"(?:\w+, )?(\w+ \d{1,2}, \d{4})"
TIME_PART = r"(\d{1,2}:\d{2} [AP]M)"


def parse_date_range(text: str) -> tuple[datetime | date | None, datetime | date | None]:
    """Parse a detail page's "Date and Time", e.g.

    "Sunday, October 4, 2026, 1:00 PM until 4:00 PM"
    "Saturday, October 3, 2026, 8:00 AM until Sunday, October 4, 2026, 5:00 PM"
    """
    text = clean(text.replace("More Dates", "").replace("...", ""))
    first, _, second = text.partition(" until ")
    m = re.match(DATE_PART + rf"(?:,? {TIME_PART})?", first)
    if not m:
        return None, None
    start_day = parse_date(m.group(1))
    start = combine(start_day, m.group(2))
    end = None
    if second:
        m2 = re.match(rf"(?:{DATE_PART},? ?)?{TIME_PART}?", second)
        if m2 and (m2.group(1) or m2.group(2)):
            end_day = parse_date(m2.group(1)) if m2.group(1) else start_day
            end = combine(end_day, m2.group(2))
            if not isinstance(end, datetime) and not isinstance(start, datetime):
                end = end + timedelta(days=1)  # all-day ends are exclusive
    return start, end


def section_lines(section: Tag) -> list[str]:
    """Visible lines of a detail-page section, minus its <h3> heading."""
    body = [child for child in section.children if getattr(child, "name", None) != "h3"]
    text = "\n".join(
        child.get_text("\n") if isinstance(child, Tag) else str(child) for child in body
    )
    return [clean(line) for line in text.split("\n") if clean(line)]


def parse_about(section: Tag) -> str:
    """The "About this event" text, with <br>/<p> as line breaks."""
    content = section.find("div") or section
    for br in content.find_all("br"):
        br.replace_with("\n")
    for block in content.find_all(["p", "div", "li"]):
        block.insert_after("\n")
    lines = [clean(line) for line in content.get_text().split("\n")]
    text = "\n".join(lines)
    text = TRIAL_MEMBERSHIP_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def format_contacts(lines: list[str]) -> str:
    """["Ken Dau-Schmidt", "Ride Host", "Gail Morell"] -> "Ken Dau-Schmidt (Ride Host), Gail Morell"."""
    people: list[str] = []
    for line in lines:
        if people and re.search(r"\b(host|leader|contact|coordinator|captain)\b", line, re.I):
            people[-1] += f" ({line})"
        else:
            people.append(line)
    return ", ".join(people)


def parse_details(page: BeautifulSoup) -> Details:
    # Members-only events redirect to the login page.
    if page.select_one(".event-detail-content") is None:
        raise ValueError("not an event page (members-only?)")

    sections: dict[str, Tag] = {}
    for heading in page.select(".event-detail-content .section h3"):
        section = heading.find_parent(class_="section")
        if section is not None:
            sections[clean(heading.get_text())] = section

    title_el = page.select_one(".event-detail-content .title-container h2")
    start = end = None
    if "Date and Time" in sections:
        date_row = sections["Date and Time"].select_one(".date-row") or sections["Date and Time"]
        start, end = parse_date_range(date_row.get_text(" "))

    location = ""
    if "Location" in sections:
        lines = section_lines(sections["Location"])
        location = ", ".join(line for line in lines if line != "USA")

    links = []
    if "Additional Info" in sections:
        for a in sections["Additional Info"].find_all("a", href=True):
            links.append(urljoin(BASE_URL, a["href"]))
        if not links:
            links = section_lines(sections["Additional Info"])

    def text_of(name: str) -> str:
        return " ".join(section_lines(sections[name])) if name in sections else ""

    return Details(
        title=clean(title_el.get_text()) if title_el else "",
        start=start,
        end=end,
        location=location,
        category=text_of("Category"),
        contacts=format_contacts(section_lines(sections["Event Contact(s)"]))
        if "Event Contact(s)" in sections else "",
        links=links,
        registration=text_of("Registration Info"),
        about=parse_about(sections["About this event"]) if "About this event" in sections else "",
    )


# --------------------------------------------------------------------------
# Building events
# --------------------------------------------------------------------------


def ride_type(category: str, title: str) -> str:
    if category in CATEGORY_TYPES:
        return CATEGORY_TYPES[category]
    if category:
        return "other"
    # No category (detail page unreadable): fall back to the title.
    t = title.lower()
    for needle, kind in (
        ("metric monday", "metric"), ("nice 'n' easy", "easy"), ("nice and easy", "easy"),
        ("early bird", "early"), ("iride", "iride"), ("growler", "growlers"),
        ("owls", "owls"), ("saturday epic", "epic"),
    ):
        if needle in t:
            return kind
    return "other"


def build_description(details: Details | None, occurrence: Occurrence) -> str:
    parts = []
    if details:
        if details.links:
            parts.append("Route/info: " + " ".join(details.links))
        if details.contacts:
            parts.append("Contact: " + details.contacts)
        if details.registration and "not required" not in details.registration.lower():
            parts.append(details.registration)
        if details.about:
            parts.append(details.about)
    parts.append(f"Details: {occurrence.url}")
    return "\n\n".join(parts)


def build_events(
    occurrences: list[Occurrence], details_by_item: dict[str, Details | None]
) -> list[RideEvent]:
    events: dict[str, RideEvent] = {}
    for occ in occurrences:
        details = details_by_item.get(occ.item_id)
        recurring = occ.event_date_id is not None

        if recurring:
            uid = f"{occ.item_id}-{occ.event_date_id}"
        else:
            # One-off events (including multi-day ones listed once per day).
            uid = occ.item_id
        if uid in events:
            continue

        start: datetime | date = occ.start
        end: datetime | date | None = None
        if details and details.start is not None and details.end is not None:
            if not recurring:
                start, end = details.start, details.end
            elif type(details.start) is type(occ.start):
                # The detail page shows the series' first date; reuse only
                # its duration.
                end = occ.start + (details.end - details.start)
        if end is None:
            end = start + (DEFAULT_DURATION if isinstance(start, datetime) else timedelta(days=1))

        title = (details.title if details and details.title else occ.title) or "BBC event"
        category = details.category if details else ""
        events[uid] = RideEvent(
            uid=f"{uid}@bloomingtonbicycleclub.org",
            title=title,
            start=start,
            end=end,
            location=(details.location if details and details.location else occ.location),
            ride_type=ride_type(category, title),
            category=category,
            description=build_description(details, occ),
            url=occ.url,
        )
    return sorted(events.values(), key=lambda e: (as_utc(e.start), e.title))


def as_utc(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.combine(value, datetime.min.time(), tzinfo=CLUB_TZ).astimezone(timezone.utc)


# --------------------------------------------------------------------------
# ICS output
# --------------------------------------------------------------------------


def to_ical_event(ev: RideEvent, stamp: datetime) -> Event:
    out = Event()
    out.add("uid", ev.uid)
    out.add("dtstamp", stamp)
    if isinstance(ev.start, datetime):
        out.add("dtstart", ev.start.astimezone(timezone.utc))
        out.add("dtend", ev.end.astimezone(timezone.utc))
    else:
        out.add("dtstart", ev.start)
        out.add("dtend", ev.end)
    out.add("summary", ev.title)
    if ev.location:
        out.add("location", ev.location)
    out.add("description", ev.description)
    out.add("url", ev.url)
    if ev.category:
        out.add("categories", [ev.category])
    return out


def build_calendar(events: list[RideEvent], allowed: set[str], stamp: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//stoffprof//bbc-rides-filter 2.0//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "BBC Rides")
    cal.add("x-wr-caldesc", f"Bloomington Bicycle Club rides, from {PUBLIC_CALENDAR_URL}")
    cal.add("x-wr-timezone", str(CLUB_TZ))
    cal.add("refresh-interval", "PT3H", parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "PT3H")
    for ev in events:
        if ev.ride_type in allowed:
            cal.add_component(to_ical_event(ev, stamp))
    return cal.to_ical()


def types_for_mask(mask: int) -> set[str]:
    return {name for i, name in enumerate(RIDE_TYPES) if mask & (1 << i)}


def legacy_types(mask: int) -> set[str]:
    """Types for an old bbc-rides-<mask>.ics URL (masks 1-63).

    The old feed had no OWLS or Saturday Epic types; those rides fell under
    "other" (shown as "General rides"), so old subscribers who chose "other"
    still get them.
    """
    allowed = types_for_mask(mask)
    if "other" in allowed:
        allowed |= {"owls", "epic"}
    return allowed


# --------------------------------------------------------------------------
# Run state (change detection and "last updated")
# --------------------------------------------------------------------------


def content_hash(events: list[RideEvent]) -> str:
    payload = [
        {**asdict(ev), "start": as_utc(ev.start).isoformat(), "end": as_utc(ev.end).isoformat()}
        for ev in events
    ]
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def fetch_previous_state(session: requests.Session, url: str) -> dict:
    """Fetch the last-updated.json published by the previous run, or {}."""
    if not url:
        return {}
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        state = resp.json()
        return state if isinstance(state, dict) else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"[state] could not fetch previous state ({exc})", file=sys.stderr)
        return {}


def set_github_output(name: str, value: str) -> None:
    """Expose a step output when running under GitHub Actions."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def format_display_timestamp(value: datetime) -> str:
    local = value.astimezone(DISPLAY_TZ)
    hour = local.hour % 12 or 12
    am_pm = "AM" if local.hour < 12 else "PM"
    return f"{local:%B} {local.day}, {local.year}, {hour}:{local.minute:02d} {am_pm} {local.tzname()}"


def write_last_updated(modified_at: datetime | None, digest: str, event_count: int) -> None:
    """modified_at is None when unknown (no previous hash to compare against)."""
    path = OUTPUT_DIR / LAST_UPDATED_FILENAME
    payload = {
        "modified_display": format_display_timestamp(modified_at) if modified_at else None,
        "modified_at_utc": modified_at.isoformat().replace("+00:00", "Z") if modified_at else None,
        "timezone": str(DISPLAY_TZ),
        "content_sha256": digest,
        "event_count": event_count,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[write] {path}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def collect_occurrences(session: requests.Session, now: datetime) -> list[Occurrence]:
    future = parse_list(get_page(session, f"{LIST_URL}&vm=Future"))
    print(f"[list] future view: {len(future)} entries", file=sys.stderr)
    if not future:
        raise RuntimeError("the Future list view had no events; has the page layout changed?")

    # The current month's view includes its past days; early in a month the
    # lookback window also reaches into the previous month.
    month_url = f"{LIST_URL}&vm=MonthView"
    month_page = get_page(session, month_url)
    past = parse_list(month_page)
    if (now.astimezone(CLUB_TZ) - timedelta(days=LOOKBACK_DAYS)).month != now.astimezone(CLUB_TZ).month:
        prev = postback(session, month_url, month_page, "ctl00$ctl00$prevMonth_0")
        past += parse_list(prev)
    print(f"[list] month view(s): {len(past)} entries", file=sys.stderr)

    return future + past


def fetch_all_details(
    session: requests.Session, occurrences: list[Occurrence]
) -> dict[str, Details | None]:
    details: dict[str, Details | None] = {}
    for occ in occurrences:
        if occ.item_id in details:
            continue
        try:
            details[occ.item_id] = parse_details(get_page(session, occ.url))
        except (requests.RequestException, ValueError) as exc:
            print(f"[warn] detail page for {occ.item_id} failed ({exc}); using list data",
                  file=sys.stderr)
            details[occ.item_id] = None
        time.sleep(REQUEST_DELAY)
    print(f"[detail] fetched {len(details)} event pages", file=sys.stderr)
    return details


def main() -> int:
    session = make_session()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    occurrences = collect_occurrences(session, now)
    # Skip detail fetches for list entries that are clearly too old. (A day
    # of slack covers multi-day events that started before the cutoff.)
    occurrences = [o for o in occurrences if as_utc(o.start) >= cutoff - timedelta(days=1)]
    details = fetch_all_details(session, occurrences)
    events = [ev for ev in build_events(occurrences, details) if as_utc(ev.end) >= cutoff]
    print(f"[build] {len(events)} events", file=sys.stderr)

    digest = content_hash(events)
    previous = fetch_previous_state(session, PREVIOUS_STATE_URL)
    prev_hash = previous.get("content_sha256")
    if prev_hash == digest and not FORCE_REBUILD:
        print("[skip] calendar unchanged since last run; nothing to do", file=sys.stderr)
        set_github_output("changed", "false")
        return 0
    set_github_output("changed", "true")

    # When the calendar was last updated: now if it changed since the last
    # run; carried forward on a forced rebuild; unknown when there is no
    # previous state to compare against.
    modified_at = None
    if previous and prev_hash != digest:
        modified_at = now
    elif prev_hash == digest and previous.get("modified_at_utc"):
        try:
            modified_at = datetime.fromisoformat(previous["modified_at_utc"].replace("Z", "+00:00"))
        except ValueError:
            pass

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_last_updated(modified_at, digest, len(events))

    for mask in range(1, 2 ** len(RIDE_TYPES)):
        (OUTPUT_DIR / f"rides-{mask}.ics").write_bytes(
            build_calendar(events, types_for_mask(mask), now))
    for mask in range(1, 64):
        (OUTPUT_DIR / f"bbc-rides-{mask}.ics").write_bytes(
            build_calendar(events, legacy_types(mask), now))
    (OUTPUT_DIR / "bbc-rides.ics").write_bytes(build_calendar(events, set(RIDE_TYPES), now))
    print(f"[write] {2 ** len(RIDE_TYPES) - 1} feeds + 64 legacy feeds to {OUTPUT_DIR}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
