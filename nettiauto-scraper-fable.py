#!/usr/bin/env python3
"""
Nettiauto full-inventory tracker (v6)
=====================================
Crawl order: the whole inventory (https://www.nettiauto.com/uusimmat).
Cars already recorded in the current database are skipped, so nothing is
double-counted or double-printed within one sweep.

Every sighting is printed and logged to CSV with:

    timestamp | NEW/SEEN | brand | model | year | km | fuel | price
              | city | seen count [| real views] | id

Counters (do not confuse):
  * seen_count = in how many of YOUR sweeps the car has been observed
  * views      = Nettiauto's real visitor counter, fetched from the
                 listing's own page only with --fetch-views (1 extra
                 request per new car)

CSV files:
  * nettiauto_sightings.csv - append-only log of every sighting
  * nettiauto_listings.csv  - full database snapshot after every sweep

Requirements:
    pip install curl_cffi beautifulsoup4

Usage:
    python3 nettiauto_scraper_v5.py
    python3 nettiauto_scraper_v5.py --once --max-pages 5   # quick test
    python3 nettiauto_scraper_v5.py --fetch-views
"""

import argparse
import csv
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime

from curl_cffi import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# Crawl phase. Kept as a one-item list so the rest of the sweep logic stays unchanged.
PHASES = [
    ("Kaikki merkit", "https://www.nettiauto.com/uusimmat?page={page}"),
]

DELAY_MIN = 1.6
DELAY_MAX = 2.6
SWEEP_PAUSE = 1800
EMPTY_PAGE_LIMIT = 3
HARD_PAGE_CAP = 4000

SIGHTINGS_CSV = "nettiauto_sightings.csv"
LISTINGS_CSV = "nettiauto_listings.csv"

HEADERS = {"Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8"}

LISTING_RE = re.compile(
    r"^https?://(?:www\.)?nettiauto\.com/([a-z0-9\-]+)/([a-z0-9\-]+)/(\d{5,})/?$"
)
PRICE_RE = re.compile(
    r"\b(\d{1,3}(?:[\s\u00a0\u202f]\d{3})*)\s*€"
)
PAGECOUNT_RE = re.compile(r"Sivu\s+\d+\s*/\s*(\d+)")
VIEWS_RE = re.compile(r"katsottu\D{0,15}([\d\s\u00a0\u202f]+)\s*kertaa",
                      re.IGNORECASE)

# "2011 ● 290 300 km" / "2025, 30 tkm" / "1983 · 87 300 km"
YEAR_KM_RE = re.compile(
    r"\b(19[2-9]\d|20[0-2]\d)\b\W{0,4}([\d][\d\s\u00a0\u202f]*)\s*(tkm|km)\b"
)
YEAR_RE = re.compile(r"\b(19[2-9]\d|20[0-2]\d)\b")
FUEL_RE = re.compile(
    r"\b(Hybridi(?:\s*\([^)]*\))?|Bensiini|Diesel|Sähkö|Kaasu|"
    r"E85/bensiini|Vety)"
)
# Seller line looks like "Lohja, Rami Heinonen ... Ota yhteyttä" or
# "Kuopio, Saka Finland Oy ... Ota yhteyttä". The city is the last
# capitalised word followed by a comma before the contact link.
CITY_RE = re.compile(r"([A-ZÄÖÅ][A-Za-zÄÖÅäöå\-]{1,30})\s*,")
CONTACT_MARK = "Ota yhteyttä"

IGNORED_FIRST_SEGMENTS = {
    "pikalinkit", "artikkeli", "artikkelit", "arvostelut", "oppaat", "yritys",
}

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def init_db(path):
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS listings (
               id         INTEGER PRIMARY KEY,
               brand      TEXT,
               model      TEXT,
               price      TEXT,
               url        TEXT,
               first_seen TEXT,
               last_seen  TEXT,
               seen_count INTEGER DEFAULT 0
           )"""
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(listings)")]
    for col, ctype in (("views", "INTEGER"), ("views_at", "TEXT"),
                       ("year", "INTEGER"), ("km", "INTEGER"),
                       ("fuel", "TEXT"), ("city", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {ctype}")
    con.commit()
    return con


# ----------------------------------------------------------------------------
# CSV helpers
# ----------------------------------------------------------------------------

SIGHTING_FIELDS = ["timestamp", "status", "brand", "model", "year", "km",
                   "fuel", "price", "city", "seen_count", "views", "id",
                   "url"]

def append_sighting_csv(row):
    new_file = not os.path.exists(SIGHTINGS_CSV)
    with open(SIGHTINGS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIGHTING_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def export_listings_csv(con):
    with open(LISTINGS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "brand", "model", "year", "km", "fuel", "price",
                    "city", "views", "views_at", "first_seen", "last_seen",
                    "seen_count", "url"])
        for r in con.execute(
            "SELECT id, brand, model, year, km, fuel, price, city, views,"
            " views_at, first_seen, last_seen, seen_count, url FROM listings"
            " ORDER BY brand, model, id"):
            w.writerow(r)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

def slug_to_name(slug):
    return "-".join(p.capitalize() for p in slug.split("-"))


def _digits(s):
    return int(re.sub(r"\D", "", s))


def extract_card_details(a_tag):
    """Scan text after the listing link for price, year, km and fuel.
    Stops at the next listing link so fields never bleed between cards."""
    from bs4 import Tag
    texts = []
    own_href = a_tag.get("href")
    count = 0
    for el in a_tag.next_elements:
        if isinstance(el, Tag) and el.name == "a":
            href = el.get("href") or ""
            if href != own_href and LISTING_RE.match(href):
                break  # next car's card begins here
        elif isinstance(el, str):
            t = el.strip()
            if t:
                texts.append(t)
                count += 1
                if count >= 60:
                    break
    blob = " ".join(texts)

    price = None
    m = PRICE_RE.search(blob)
    if m:
        price = re.sub(r"[\s\u00a0\u202f]+", " ", m.group(0)).strip()

    year = km = None
    m = YEAR_KM_RE.search(blob)
    if m:
        year = int(m.group(1))
        km = _digits(m.group(2))
        if m.group(3) == "tkm":
            km *= 1000
    else:
        m = YEAR_RE.search(blob)
        if m:
            year = int(m.group(1))

    fuel = None
    m = FUEL_RE.search(blob)
    if m:
        fuel = m.group(1)

    city = None
    idx = blob.find(CONTACT_MARK)
    scope = blob[:idx] if idx != -1 else blob
    matches = CITY_RE.findall(scope)
    if matches:
        city = matches[-1]  # the one right before "Ota yhteyttä"

    return price, year, km, fuel, city


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        m = LISTING_RE.match(a["href"])
        if not m:
            continue
        brand_slug, model_slug, lid = m.group(1), m.group(2), int(m.group(3))
        if brand_slug in IGNORED_FIRST_SEGMENTS:
            continue
        if lid in found and found[lid]["price"]:
            continue
        price, year, km, fuel, city = extract_card_details(a)
        found[lid] = {
            "brand": slug_to_name(brand_slug),
            "model": slug_to_name(model_slug),
            "price": price, "year": year, "km": km, "fuel": fuel,
            "city": city,
            "url": m.group(0),
        }
    total = None
    mm = PAGECOUNT_RE.search(soup.get_text(" ", strip=True))
    if mm:
        total = int(mm.group(1))
    return found, total


def parse_views(html):
    m = VIEWS_RE.search(html)
    if m:
        return int(re.sub(r"\D", "", m.group(1)))
    return None


# ----------------------------------------------------------------------------
# Sweep logic
# ----------------------------------------------------------------------------

def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def fetch_real_views(session, url):
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return parse_views(resp.text)
    except requests.RequestsError:
        return None
    finally:
        polite_sleep()


def record_sighting(con, lid, info, counted):
    now = datetime.now().isoformat(timespec="seconds")
    row = con.execute(
        "SELECT seen_count, price FROM listings WHERE id=?", (lid,)
    ).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO listings (id, brand, model, year, km, fuel, price,"
            " city, url, first_seen, last_seen, seen_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (lid, info["brand"], info["model"], info["year"], info["km"],
             info["fuel"], info["price"], info["city"], info["url"],
             now, now),
        )
        counted.add(lid)
        return 1, True
    seen = row[0] + 1
    con.execute(
        "UPDATE listings SET price=?, year=COALESCE(?, year),"
        " km=COALESCE(?, km), fuel=COALESCE(?, fuel),"
        " city=COALESCE(?, city), last_seen=?,"
        " seen_count=? WHERE id=?",
        (info["price"] or row[1], info["year"], info["km"], info["fuel"],
         info["city"], now, seen, lid),
    )
    counted.add(lid)
    return seen, False


def fmt_km(km):
    return f"{km:,}".replace(",", " ") + " km" if km else "? km"


def sweep_phase(con, session, args, verbose_repeats, phase_name,
                url_template, counted):
    empty_streak = 0
    total_pages = None
    page = args.start_page
    print(f"\n### Vaihe: {phase_name} ###")

    while page <= HARD_PAGE_CAP:
        if args.max_pages and page > args.start_page + args.max_pages - 1:
            break
        if total_pages and page > total_pages:
            break
        try:
            resp = session.get(url_template.format(page=page),
                               headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestsError as exc:
            print(f"[{datetime.now():%H:%M:%S}] FETCH ERROR "
                  f"{phase_name} p.{page}: {exc}", file=sys.stderr)
            polite_sleep()
            page += 1
            continue

        listings, tp = parse_page(resp.text)
        if tp:
            total_pages = tp

        fresh = {lid: i for lid, i in listings.items() if lid not in counted}
        for lid, info in sorted(fresh.items(), reverse=True):
            seen, is_new = record_sighting(con, lid, info, counted)

            views = None
            if is_new and args.fetch_views:
                views = fetch_real_views(session, info["url"])
                if views is not None:
                    con.execute(
                        "UPDATE listings SET views=?, views_at=? WHERE id=?",
                        (views, datetime.now().isoformat(timespec="seconds"),
                         lid),
                    )

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tag = "NEW " if is_new else "SEEN"
            price = info["price"] or "hinta ?"
            if is_new or verbose_repeats:
                vtxt = f" | katsottu {views}x" if views is not None else ""
                print(f"{ts} | {tag} | {info['brand']:15s} | "
                      f"{info['model']:18s} | {info['year'] or '????'} | "
                      f"{fmt_km(info['km']):>12s} | "
                      f"{(info['fuel'] or '?'):8s} | {price:>10s} | "
                      f"{(info['city'] or '?'):15s} | "
                      f"nähty {seen}x{vtxt} | id {lid}")
            append_sighting_csv({
                "timestamp": ts, "status": tag.strip(),
                "brand": info["brand"], "model": info["model"],
                "year": info["year"] or "", "km": info["km"] or "",
                "fuel": info["fuel"] or "", "price": price,
                "city": info["city"] or "",
                "seen_count": seen,
                "views": views if views is not None else "",
                "id": lid, "url": info["url"],
            })
        con.commit()

        if not listings:
            empty_streak += 1
            if empty_streak >= EMPTY_PAGE_LIMIT:
                print(f"{EMPTY_PAGE_LIMIT} tyhjää sivua, vaihe "
                      f"'{phase_name}' päättyy sivulla {page}.")
                break
        else:
            empty_streak = 0

        if page % 25 == 0:
            tot = f"/{total_pages}" if total_pages else ""
            print(f"--- {phase_name}: sivu {page}{tot}, "
                  f"{len(counted)} autoa kierroksella ---")
        polite_sleep()
        page += 1


def full_sweep(con, session, args, verbose_repeats):
    counted = set()
    for phase_name, url_template in PHASES:
        sweep_phase(con, session, args, verbose_repeats,
                    phase_name, url_template, counted)
    export_listings_csv(con)
    return len(counted)


def main():
    ap = argparse.ArgumentParser(description="Nettiauto full-inventory tracker")
    ap.add_argument("--db", default="nettiauto.db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--max-pages", type=int, default=0,
                    help="page limit PER PHASE (0 = all)")
    ap.add_argument("--sweep-pause", type=int, default=SWEEP_PAUSE)
    ap.add_argument("--quiet-repeats", action="store_true")
    ap.add_argument("--fetch-views", action="store_true",
                    help="fetch Nettiauto's real view counter per NEW car")
    args = ap.parse_args()

    con = init_db(args.db)
    session = requests.Session(impersonate="chrome")
    verbose = not args.quiet_repeats

    print(f"Nettiauto sweep: ensin Toyotat, sitten muut merkit. Viive "
          f"{DELAY_MIN}-{DELAY_MAX} s. CSV: {SIGHTINGS_CSV} + {LISTINGS_CSV}. "
          f"Ctrl+C lopettaa.")
    if args.fetch_views:
        print("HUOM: --fetch-views tekee yhden lisäpyynnön joka UUTTA autoa "
              "kohden. Ensimmäisellä kierroksella kaikki ~78 000 autoa ovat "
              "uusia (~45 h). Aja mieluummin 1. kierros ilman lippua.")
    try:
        while True:
            n = full_sweep(con, session, args, verbose)
            print(f"\n=== Kierros valmis: {n} autoa. CSV päivitetty. ===\n")
            if args.once:
                break
            time.sleep(args.sweep_pause)
    except KeyboardInterrupt:
        pass
    finally:
        export_listings_csv(con)
        total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        toyotas = con.execute("SELECT COUNT(*) FROM listings WHERE "
                              "brand='Toyota'").fetchone()[0]
        print(f"\nTietokannassa {total} ilmoitusta (joista {toyotas} "
              f"Toyotaa). Snapshot: {LISTINGS_CSV}.")
        con.close()


if __name__ == "__main__":
    main()

