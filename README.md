# Nettiauto scraper / watcher

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

It also appends results to `nettiauto_sightings.csv` and stores processed ad
IDs in `nettiauto_seen.sqlite3`.

## Installation

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Windows Command Prompt:

```bat
.venv\Scripts\activate.bat
pip install -r requirements.txt
python -m playwright install chromium
```

## Small test

```bash
python nettiauto_scraper.py --pages 1 --max-ads 3 --headful
```

## Scan all currently listed cars

```bash
python nettiauto_scraper.py
```

To limit the crawl for testing, pass `--pages` with a page count.

```bash
python nettiauto_scraper.py --pages 5
```

The scraper rotates between Chrome, Edge and Firefox user-agent/browser
profiles when possible. Request delays are randomized between 1.79 and 2.30
seconds.

## Watch for new cars every 10 minutes

```bash
python nettiauto_scraper.py --pages 5 --watch 600
```

Only ads not already present in the SQLite database are printed, unless
`--refresh-existing` is used.

## Resume a full crawl

The SQLite database prevents duplicate output. If a scan stops, run the same
command again. Because result pages change while a long scan is running, run
one additional pass after completion to catch cars that moved between pages.

## Important

Use this only where Nettiauto's terms and applicable law permit it. The
program deliberately stops on HTTP 403/429 and does not bypass CAPTCHAs or
other access controls. Visiting an ad may itself increase that ad's view count.
