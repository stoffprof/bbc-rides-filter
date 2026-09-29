# bbc-rides-filter

A GitHub Actions job that scrapes the
[Bloomington Bicycle Club](https://www.bloomingtonbicycleclub.org/content.aspx?page_id=4001&club_id=144345&action=cira&vm=MonthView&sif=0)
ride calendar every 3 hours and publishes it on GitHub Pages as subscribable
ICS feeds, filtered by ride type.

The club's calendar moved from a public Google Calendar to ClubExpress,
which has no calendar feed (only a one-off "Add to calendar" download per
event). So `scripts/build_feed.py` rebuilds a feed from the calendar's web
pages.

## How it works

1. The calendar's **Future** list view lists every upcoming ride with its
   date and start time.
2. The **month** list view (plus the previous month's, early in a month)
   adds the past 7 days, so recent rides stay on subscribers' calendars.
3. Each event's **detail page** gives the end time, full address, ClubExpress
   category, ride host, route link, and description. (Recurring events share
   one detail page that always shows the series' first date, so their dates
   come from the list and only their duration from the detail page.
   Members-only events redirect to a login page; those use the list data.)
4. The ClubExpress category decides the ride type, and the script writes one
   ICS file per combination of types.

A run makes about 60 requests to the club's site, paced 0.25 s apart. If the
event data is unchanged since the last deploy (compared by hash against the
live `last-updated.json`), the deploy is skipped.

## Published files

| File | Contents |
| --- | --- |
| `index.html` | Page for picking ride types and subscribing |
| `rides-<mask>.ics` | Feeds for masks 1–255; bit *i* selects `RIDE_TYPES[i]` |
| `bbc-rides-<mask>.ics`, `bbc-rides.ics` | Legacy URLs from the Google-based version (see below) |
| `last-updated.json` | Content hash and when the calendar last changed |

Ride type bits: metric 1, easy 2, early 4, iride 8, growlers 16, other 32,
owls 64, epic 128.

**Legacy URLs.** The old version had six types (bits 1–32), and OWLS and
Saturday Epic rides fell under "other". `bbc-rides-<mask>.ics` keeps those
URLs working with the old meaning: a legacy mask that includes "other" also
gets OWLS and Saturday Epic.

## Running locally

```bash
pip install -r requirements.txt
OUTPUT_DIR=/tmp/bbc-site python scripts/build_feed.py
```

Environment variables: `OUTPUT_DIR` (default `_site`), `LOOKBACK_DAYS`
(default 7), `REQUEST_DELAY` (default 0.25), `PREVIOUS_STATE_URL`,
`FORCE_REBUILD`.
