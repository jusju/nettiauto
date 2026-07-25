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
- price
- number of views
- ad URL

It appends results to `nettiauto_sightings.csv` and stores processed ad IDs in
`nettiauto_seen.sqlite3`.

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

## Run

Small test:

```bash
python nettiauto_scraper.py --pages 1 --max-ads 3 --headful
```

Full scan:

```bash
python nettiauto_scraper.py
```

Limit the crawl for testing:

```bash
python nettiauto_scraper.py --pages 5
```

Watch mode:

```bash
python nettiauto_scraper.py --pages 5 --watch 600
```

The scraper rotates between Chrome, Edge and Firefox user-agent/browser
profiles when possible. Request delays are randomized between 1.79 and 2.30
seconds.

Only ads not already present in the SQLite database are printed, unless
`--refresh-existing` is used.

If a scan stops, run the same command again. Because result pages change while
a long scan is running, run one additional pass after completion to catch cars
that moved between pages.

## Important

Use this only where Nettiauto's terms and applicable law permit it. The
program deliberately stops on HTTP 403/429 and does not bypass CAPTCHAs or
other access controls. Visiting an ad may itself increase that ad's view count.
