# Nettiauto scraper / watcher

Use [nettiauto_scraper.py](nettiauto_scraper.py) for normal use. The
[nettiauto-scraper-fable.py](nettiauto-scraper-fable.py) file is a legacy
variant and is not the recommended entry point.

This program scans Nettiauto's newest-listing pages, opens previously unseen
ads, and prints:

- timestamp
- brand
- model
- year
- kilometer reading
- fuel
- price
- city
- phone
- number of views
- ad URL

It appends results to `nettiauto_sightings.csv` and stores processed ad IDs in
`nettiauto_seen.sqlite3`.

By default it collects **Toyota Hiace only** — see [Find Hiace vans](#find-hiace-vans).

## WSL2 / Linux setup

Install the system packages needed for Python and Playwright:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the Python dependency and the browser runtime:

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

## Find Hiace vans

This is the command to run. Toyota Hiace is the default, so no flags are
needed:

```bash
source .venv/bin/activate
python nettiauto_scraper.py
```

It browses `https://www.nettiauto.com/toyota/hiace`, which lists Hiaces only —
about 7 result pages rather than the ~2500 pages of the full catalogue, and
every ad found is already a Hiace.

Check it works first, without waiting for the whole run:

```bash
python nettiauto_scraper.py --pages 1 --max-ads 3 --headful
```

Keep watching for newly listed Hiaces, rescanning every 10 minutes:

```bash
python nettiauto_scraper.py --watch 600
```

Results land in `nettiauto_sightings.csv`. Ads already in that file are never
fetched again, so the command is safe to re-run as often as you like.

### Collecting something else

```bash
# a different model
python nettiauto_scraper.py --make toyota --model proace

# every make and model
python nettiauto_scraper.py --any-car
```

`--make` and `--model` are used to build the browse URL
(`nettiauto.com/{make}/{model}`), so they must match Nettiauto's own spelling.
With `--make` alone the whole make is browsed; with `--any-car` the scraper
falls back to scanning `/uusimmat`, which is slow.

Every ad is also re-checked after discovery, and `--model` is a prefix match
there, so trim variants such as `hiace-4wd` and `hiace-long` are kept.

## Run options

| Flag | Purpose |
| --- | --- |
| `--make` / `--model` | Restrict which cars are fetched (default `toyota` / `hiace`). |
| `--any-car` | Disable the make/model filter. |
| `--pages N` | Scan only the N newest result pages. Omit to scan all. |
| `--start-page N` | Begin at result page N; useful for resuming. |
| `--watch SECONDS` | Repeat forever, sleeping between scans (minimum 60). |
| `--max-ads N` | Stop after N detail pages; useful for testing. |
| `--delay-min` / `--delay-max` | Request delay range in seconds. |
| `--headful` | Show the browser window. |
| `--csv` / `--database` | Override the output and checkpoint file paths. |

The scraper rotates between Chrome, Edge and Firefox user-agent/browser
profiles when possible. Request delays are randomized between 1.79 and 2.30
seconds.

## Duplicates

A car is fetched at most once. Before any detail page is opened, its ad ID is
checked against both `nettiauto_sightings.csv` and the `nettiauto_seen.sqlite3`
checkpoint. The CSV is re-read at the start of every scan, so `--watch` runs
and hand-edits to the file are both respected.

If a scan stops, run the same command again. Because result pages change while
a long scan is running, run one additional pass after completion to catch cars
that moved between pages.

## Important

Use this only where Nettiauto's terms and applicable law permit it. The
program does not bypass CAPTCHAs or other access controls. Visiting an ad may
itself increase that ad's view count.

On HTTP 403/429 the scraper waits rather than pushing through: it backs off for
90s, 240s, 600s and 900s, retrying the same page after each pause and honouring
a `Retry-After` header when the server sends one. After four consecutive blocks
it ends the scan; in `--watch` mode the next cycle tries again. If you are
blocked often, raise `--delay-min` and `--delay-max` rather than working around
the limit.
