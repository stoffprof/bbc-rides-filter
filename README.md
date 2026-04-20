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

## One-time setup

1. Create an **empty, public** repo on GitHub (private repos can serve Pages
   only on a paid plan).
2. Push this directory to that repo (see [Pushing](#pushing) below).
3. In the repo's **Settings → Pages**, set **Source** to
   **GitHub Actions**. You don't need to pick a branch.
4. Go to the **Actions** tab and run **Update filtered ICS** once
   (`Run workflow`). The first run usually takes ~30 seconds.
5. Once it succeeds, your feed is available at:

   ```
   https://<your-username>.github.io/<repo-name>/bbc-rides.ics
   ```

## Subscribing from macOS Calendar

In Calendar, choose **File → New Calendar Subscription…** and paste:

```
webcal://<your-username>.github.io/<repo-name>/bbc-rides.ics
```

(Use `webcal://`, not `https://` — macOS Calendar is fussier than it should
be about the scheme.)

Set **Auto-refresh** to **Every day** or **Every hour**. There's no benefit
to anything faster since the upstream only rebuilds every 12 hours.

## Pushing

From this directory:

```sh
git init -b main
git add .
git commit -m "Initial import"
git remote add origin git@github.com:<your-username>/<repo-name>.git
git push -u origin main
```

## Configuration

The script reads a few optional environment variables, all with sensible
defaults:

| Variable            | Default                                          | Notes                                   |
| ------------------- | ------------------------------------------------ | --------------------------------------- |
| `UPSTREAM_ICS_URL`  | The BBC Rides Google Calendar basic.ics URL      | Point at a different feed to reuse.     |
| `LOOKBACK_DAYS`     | `7`                                              | How many days of recent past to keep.   |
| `OUTPUT_DIR`        | `_site`                                          | Where to write the artifact.            |
| `OUTPUT_FILENAME`   | `bbc-rides.ics`                                  | Final filename served by Pages.         |

To change any of these permanently, edit the
[`env:` block of the workflow](.github/workflows/update.yml) or change the
defaults in `scripts/filter_ics.py`.

## Running locally

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/filter_ics.py
# produces _site/bbc-rides.ics
```

## Files

```
.
├── .github/workflows/update.yml   # Runs on schedule, builds + deploys to Pages
├── scripts/filter_ics.py          # Fetch + filter logic
├── requirements.txt               # icalendar
├── .gitignore
└── README.md
```

## License

No license file included. Add one (MIT is the usual choice) if you plan to
share.
