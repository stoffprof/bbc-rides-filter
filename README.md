# bbc-rides-filter

A tiny GitHub Actions job that fetches the public
[Bloomington Bicycle Club](https://bloomingtonbicycleclub.org/) Google
Calendar feed every 12 hours, strips out events older than a week, and
publishes the result as a static ICS file on GitHub Pages.

The upstream feed is ~6 MB with 17 years of history and ~5,000 events, which
is enough to make macOS Calendar (and plenty of other clients) silently fail
on subscription. This repo produces a smaller, client-friendly version.

## What it does

- Downloads the upstream ICS once per scheduled run.
- Keeps every `VTIMEZONE` block and all `VCALENDAR`-level metadata verbatim.
- Keeps non-recurring events whose `DTSTART` is within the last 7 days or in
  the future.
- Keeps recurring "master" events unless their `RRULE` has an `UNTIL` that's
  already past.
- Keeps per-instance overrides (`RECURRENCE-ID`) only if that instance is
  recent or future.
- Writes the result to `_site/bbc-rides.ics` and deploys `_site/` to
  GitHub Pages.
