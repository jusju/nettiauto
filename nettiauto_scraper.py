#!/usr/bin/env python3
"""
Nettiauto inventory scraper.

Discovers ads from Nettiauto's newest-listing pages, visits each unseen ad,
prints a timestamp, brand, model, year, kilometer reading, price and view count,
and appends one row per unique ad to CSV. Processed ad IDs are stored in SQLite
so later runs skip cars already written to CSV.

Use responsibly and only where permitted by Nettiauto's terms and applicable law.
This script does not bypass login, CAPTCHAs, rate limits, or other access controls.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

LATEST_URL = "https://www.nettiauto.com/uusimmat?page={page}"

# Expected ad URL form:
# https://www.nettiauto.com/en/ford/focus/12345678
AD_PATH_RE = re.compile(
    r"^/(?:en/)?(?P<make>[^/?#]+)/(?P<model>[^/?#]+)/(?P<id>\d+)/?$",
    re.IGNORECASE,
)

PAGE_COUNT_RE = re.compile(r"\b(?:Page|Sivu)\s+\d+\s*/\s*(\d+)\b", re.IGNORECASE)

VIEW_PATTERNS = (
    re.compile(r"\bViews\s*:\s*([\d\s.,\u00a0]+)\s*times\b", re.IGNORECASE),
    re.compile(r"\bKatsottu\s*:\s*([\d\s.,\u00a0]+)\s*kertaa\b", re.IGNORECASE),
)

YEAR_KM_RE = re.compile(
    r"\b(19[2-9]\d|20[0-2]\d)\b\W{0,4}([\d][\d\s\u00a0\u202f]*)\s*(tkm|km)\b"
)
YEAR_RE = re.compile(r"\b(19[2-9]\d|20[0-2]\d)\b")
FUEL_RE = re.compile(
    r"\b(Hybridi(?:\s*\([^)]*\))?|Bensiini|Diesel|Sähkö|Kaasu|E85/bensiini|Vety)"
)
CITY_RE = re.compile(r"([A-ZÄÖÅ][A-Za-zÄÖÅäöå\-]{1,30})\s*,")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")
DENIED_MARKERS = (
    "access denied",
    "forbidden",
    "captcha",
    "robot",
    "too many requests",
    "kielletty",
)

SPECIAL_CASE = {
    "bmw": "BMW",
    "byd": "BYD",
    "citroen": "Citroën",
    "ds": "DS",
    "fso": "FSO",
    "gaz": "GAZ",
    "gmc": "GMC",
    "jac": "JAC",
    "jdm": "JDM",
    "kgm": "KGM",
    "man": "MAN",
    "mg": "MG",
    "mini": "MINI",
    "nsu": "NSU",
    "raf": "RAF",
    "ram": "RAM",
    "saab": "Saab",
    "smz": "SMZ",
    "tvr": "TVR",
    "uaz": "UAZ",
    "xpeng": "XPENG",
}


@dataclass(frozen=True)
class AdLink:
    ad_id: str
    make_slug: str
    model_slug: str
    url: str


@dataclass(frozen=True)
class AdData:
    timestamp: str
    ad_id: str
    make: str
    model: str
    year: int | None
    km: int | None
    fuel: str | None
    price_eur: int | None
    city: str | None
    phone: str | None
    views: int | None
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print newly discovered Nettiauto cars with year, km, price and views."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan every currently available result page.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Number of newest result pages to scan. If omitted, scans all pages.",
    )

    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Start result-page number (default: 1).",
    )
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="Repeat forever, sleeping this many seconds between scans.",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=1.79,
        help="Minimum delay between requests in seconds (default: 1.79).",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=2.30,
        help="Maximum delay between requests in seconds (default: 2.30).",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("nettiauto_seen.sqlite3"),
        help="SQLite checkpoint file.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("nettiauto_sightings.csv"),
        help="CSV output file.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Revisit and print ads already stored in the database.",
    )
    parser.add_argument(
        "--max-ads",
        type=int,
        help="Stop after this many detail pages; useful for testing.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the Chromium browser window.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Navigation timeout in seconds (default: 30).",
    )
    return parser.parse_args()


def clean_count(value: str) -> int | None:
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def slug_to_name(slug: str) -> str:
    decoded = unquote(slug).strip()
    lower = decoded.lower()
    if lower in SPECIAL_CASE:
        return SPECIAL_CASE[lower]

    parts = decoded.replace("_", "-").split("-")
    rendered = []
    for part in parts:
        key = part.lower()
        rendered.append(SPECIAL_CASE.get(key, part[:1].upper() + part[1:]))
    return "-".join(rendered)


def initialise_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_ads (
            ad_id TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            year INTEGER,
            km INTEGER,
            fuel TEXT,
            price_eur INTEGER,
            city TEXT,
            phone TEXT,
            views INTEGER,
            url TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def is_seen(connection: sqlite3.Connection, ad_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM seen_ads WHERE ad_id = ? LIMIT 1", (ad_id,)
    ).fetchone()
    return row is not None


def save_ad(connection: sqlite3.Connection, ad: AdData) -> None:
    connection.execute(
        """
        INSERT INTO seen_ads (
            ad_id, first_seen, last_seen, make, model, year, km, fuel,
            price_eur, city, phone, views, url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ad_id) DO UPDATE SET
            last_seen = excluded.last_seen,
            make = excluded.make,
            model = excluded.model,
            year = excluded.year,
            km = excluded.km,
            fuel = excluded.fuel,
            price_eur = excluded.price_eur,
            city = excluded.city,
            phone = excluded.phone,
            views = excluded.views,
            url = excluded.url
        """,
        (
            ad.ad_id,
            ad.timestamp,
            ad.timestamp,
            ad.make,
            ad.model,
            ad.year,
            ad.km,
            ad.fuel,
            ad.price_eur,
            ad.city,
            ad.phone,
            ad.views,
            ad.url,
        ),
    )
    connection.commit()


def seed_database_from_csv(connection: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        return

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return

        id_field = "id" if "id" in reader.fieldnames else "ad_id" if "ad_id" in reader.fieldnames else None
        if id_field is None:
            return

        rows: list[tuple[str, str, str, str, str]] = []
        for row in reader:
            ad_id = (row.get(id_field) or "").strip()
            if not ad_id:
                continue

            timestamp = (row.get("timestamp") or row.get("first_seen") or timestamp_now()).strip()
            make = (row.get("brand") or row.get("make") or "").strip() or "unknown"
            model = (row.get("model") or "").strip() or "unknown"
            url = (row.get("url") or "").strip()
            if not url:
                url = f"https://www.nettiauto.com/{ad_id}"
            rows.append((ad_id, timestamp, make, model, url))

    if not rows:
        return

    connection.executemany(
        """
        INSERT OR IGNORE INTO seen_ads (
            ad_id, first_seen, last_seen, make, model, url
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(ad_id, ts, ts, make, model, url) for ad_id, ts, make, model, url in rows],
    )
    connection.commit()


def append_csv(path: Path, ad: AdData) -> None:
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(
                [
                    "timestamp",
                    "status",
                    "brand",
                    "model",
                    "year",
                    "km",
                    "fuel",
                    "price",
                    "city",
                    "phone",
                    "seen_count",
                    "views",
                    "id",
                    "url",
                ]
            )
        writer.writerow(
            [
                ad.timestamp,
                "NEW",
                ad.make,
                ad.model,
                "" if ad.year is None else ad.year,
                "" if ad.km is None else ad.km,
                "" if ad.fuel is None else ad.fuel,
                "" if ad.price_eur is None else ad.price_eur,
                "" if ad.city is None else ad.city,
                "" if ad.phone is None else ad.phone,
                1,
                "" if ad.views is None else ad.views,
                ad.ad_id,
                ad.url,
            ]
        )


def print_ad(ad: AdData) -> None:
    price = "unknown" if ad.price_eur is None else f"{ad.price_eur} EUR"
    year = "unknown" if ad.year is None else str(ad.year)
    km = "unknown" if ad.km is None else f"{ad.km} km"
    views = "unknown" if ad.views is None else str(ad.views)
    fuel = "unknown" if ad.fuel is None else ad.fuel
    city = "unknown" if ad.city is None else ad.city
    phone = "unknown" if ad.phone is None else ad.phone
    print(
        f"{ad.timestamp}\t{ad.make}\t{ad.model}\tyear={year}\tkm={km}\t"
        f"price={price}\tfuel={fuel}\tcity={city}\tphone={phone}\tviews={views}\t{ad.url}",
        flush=True,
    )


async def block_heavy_resources(page: Page) -> None:
    async def handler(route: Any) -> None:
        resource_type = route.request.resource_type
        if resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handler)


async def safe_goto(
    page: Page, url: str, timeout_ms: int, attempts: int = 4
) -> None:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )

            if response is not None and response.status in {403, 429}:
                raise RuntimeError(
                    f"Nettiauto returned HTTP {response.status}. "
                    "Stopping rather than bypassing the restriction."
                )

            # Give JavaScript-loaded statistics time to appear.
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass

            return
        except RuntimeError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            await asyncio.sleep(min(60, 2**attempt + random.random()))

    raise RuntimeError(f"Could not load {url}: {last_error}")


async def page_text(page: Page) -> str:
    return await page.locator("body").inner_text(timeout=10_000)


def timestamp_now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H-%M-%S")


def is_denied_text(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in DENIED_MARKERS)


def extract_year_and_km(text: str) -> tuple[int | None, int | None]:
    match = YEAR_KM_RE.search(text)
    if match:
        year = int(match.group(1))
        km_text = re.sub(r"\D", "", match.group(2))
        km = int(km_text) if km_text else None
        if match.group(3).lower() == "tkm" and km is not None:
            km *= 1000
        return year, km

    match = YEAR_RE.search(text)
    return (int(match.group(1)), None) if match else (None, None)


def extract_fuel(text: str) -> str | None:
    match = FUEL_RE.search(text)
    return match.group(1) if match else None


def extract_city(text: str) -> str | None:
    matches = CITY_RE.findall(text)
    if matches:
        return matches[-1]
    return None


def normalize_phone(value: str) -> str | None:
    cleaned = re.sub(r"[^\d+]", "", value)
    if not cleaned:
        return None
    if cleaned.count("+") > 1:
        cleaned = cleaned.replace("+", "")
    if "+" in cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned.replace("+", "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 7:
        return None
    return cleaned


async def extract_phone(page: Page, text: str) -> str | None:
    tel_links = await page.locator('a[href^="tel:"]').evaluate_all(
        """elements => elements.map(element => element.getAttribute('href') || '')"""
    )
    for href in tel_links:
        candidate = normalize_phone(unquote(href.removeprefix("tel:")))
        if candidate:
            return candidate

    text_patterns = [
        re.compile(r"(?:Puhelin|Puh\.|Phone)\s*[:\-]?\s*(\+?[\d\s().-]{7,})", re.IGNORECASE),
        PHONE_RE,
    ]
    for pattern in text_patterns:
        match = pattern.search(text)
        if match:
            candidate = normalize_phone(match.group(1) if match.groups() else match.group(0))
            if candidate:
                return candidate

    return None


async def discover_ad_links(page: Page) -> list[AdLink]:
    hrefs: list[str] = await page.locator("a[href]").evaluate_all(
        """elements => elements.map(element => element.href)"""
    )

    found: dict[str, AdLink] = {}
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.netloc.lower() not in {"nettiauto.com", "www.nettiauto.com"}:
            continue

        match = AD_PATH_RE.match(parsed.path.rstrip("/"))
        if not match:
            continue

        ad_id = match.group("id")
        canonical = (
            f"https://www.nettiauto.com/"
            f"{match.group('make')}/{match.group('model')}/{ad_id}"
        )
        found[ad_id] = AdLink(
            ad_id=ad_id,
            make_slug=match.group("make"),
            model_slug=match.group("model"),
            url=canonical,
        )

    return list(found.values())


async def determine_last_page(page: Page) -> int:
    text = await page_text(page)
    match = PAGE_COUNT_RE.search(text)
    if not match:
        raise RuntimeError("Could not determine the final Nettiauto result page.")
    return int(match.group(1))


def iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


async def structured_price(page: Page, ad_id: str) -> int | None:
    scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
    candidates: list[tuple[bool, int]] = []

    for raw in scripts:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for obj in iter_json_objects(value):
            offers = obj.get("offers")
            if not isinstance(offers, dict):
                continue

            raw_price = offers.get("price")
            if raw_price is None:
                continue

            digits = re.sub(r"[^\d]", "", str(raw_price).split(".")[0])
            if not digits:
                continue

            url = str(obj.get("url", "")) + " " + str(offers.get("url", ""))
            candidates.append((ad_id in url, int(digits)))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def price_from_text(text: str, ad_id: str) -> int | None:
    # Most reliable fallback: the price printed immediately after "Nettiauto ID".
    id_pattern = re.compile(
        rf"Nettiauto\s+ID\s+{re.escape(ad_id)}.*?\n\s*"
        r"([\d\s\u00a0]+)\s*€",
        re.IGNORECASE | re.DOTALL,
    )
    match = id_pattern.search(text)
    if match:
        return clean_count(match.group(1))

    # Last-resort fallback.
    match = re.search(r"\b([\d][\d\s\u00a0]{1,14})\s*€", text)
    return clean_count(match.group(1)) if match else None


def views_from_text(text: str) -> int | None:
    for pattern in VIEW_PATTERNS:
        match = pattern.search(text)
        if match:
            return clean_count(match.group(1))
    return None


async def extract_ad(page: Page, link: AdLink, timeout_ms: int) -> AdData:
    await safe_goto(page, link.url, timeout_ms)

    # The statistics area may be filled asynchronously after DOMContentLoaded.
    await asyncio.sleep(2.0)
    text = await page_text(page)

    if is_denied_text(text):
        raise RuntimeError(f"Nettiauto refused to provide data for ad {link.ad_id}.")

    price = await structured_price(page, link.ad_id)
    if price is None:
        price = price_from_text(text, link.ad_id)

    year, km = extract_year_and_km(text)
    fuel = extract_fuel(text)
    city = extract_city(text)
    phone = await extract_phone(page, text)
    views = views_from_text(text)

    return AdData(
        timestamp=timestamp_now(),
        ad_id=link.ad_id,
        make=slug_to_name(link.make_slug),
        model=slug_to_name(link.model_slug),
        year=year,
        km=km,
        fuel=fuel,
        price_eur=price,
        city=city,
        phone=phone,
        views=views,
        url=link.url,
    )


async def sleep_between_requests(min_delay: float, max_delay: float) -> None:
    await asyncio.sleep(random.uniform(min_delay, max_delay))


async def launch_browser_profile(
    playwright: Any, profile: dict[str, Any], headful: bool
) -> Browser:
    launcher = getattr(playwright, profile["launcher"])
    launch_kwargs: dict[str, Any] = {"headless": not headful}
    if profile.get("channel") is not None:
        launch_kwargs["channel"] = profile["channel"]

    try:
        return await launcher.launch(**launch_kwargs)
    except PlaywrightError:
        fallback_launcher = profile.get("fallback_launcher")
        if fallback_launcher is not None:
            fallback = getattr(playwright, fallback_launcher)
            return await fallback.launch(headless=not headful)
        raise


async def open_page_in_profile(browser: Browser, user_agent: str) -> tuple[Any, Page]:
    context = await browser.new_context(
        locale="fi-FI",
        user_agent=user_agent,
        viewport={"width": 1365, "height": 900},
    )
    page = await context.new_page()
    await block_heavy_resources(page)
    return context, page


async def run_scan(
    browser_pool: list[tuple[dict[str, Any], Browser]],
    connection: sqlite3.Connection,
    args: argparse.Namespace,
) -> int:
    search_profile, search_browser = browser_pool[0]
    search_context = await search_browser.new_context(
        locale="fi-FI",
        user_agent=search_profile["user_agent"],
        viewport={"width": 1365, "height": 900},
    )
    search_page = await search_context.new_page()
    await block_heavy_resources(search_page)

    timeout_ms = int(args.timeout * 1000)
    processed = 0
    detail_index = 0

    try:
        await safe_goto(
            search_page,
            LATEST_URL.format(page=args.start_page),
            timeout_ms,
        )

        if args.all or args.pages is None:
            last_page = await determine_last_page(search_page)
        else:
            last_page = args.start_page + max(args.pages, 1) - 1

        print(
            f"Scanning result pages {args.start_page}..{last_page}",
            file=sys.stderr,
            flush=True,
        )

        for page_number in range(args.start_page, last_page + 1):
            page_url = LATEST_URL.format(page=page_number)
            if page_number != args.start_page:
                await safe_goto(search_page, page_url, timeout_ms)

            links = await discover_ad_links(search_page)
            if not links:
                print(
                    f"No ad links found on result page {page_number}; stopping.",
                    file=sys.stderr,
                )
                break

            print(
                f"Result page {page_number}: {len(links)} unique ad links",
                file=sys.stderr,
                flush=True,
            )

            for link in links:
                if not args.refresh_existing and is_seen(connection, link.ad_id):
                    continue

                profile, browser = browser_pool[detail_index % len(browser_pool)]
                detail_index += 1
                detail_context, detail_page = await open_page_in_profile(
                    browser, profile["user_agent"]
                )
                try:
                    ad = await extract_ad(detail_page, link, timeout_ms)
                except (RuntimeError, PlaywrightError, PlaywrightTimeoutError) as exc:
                    print(f"ERROR: {exc}", file=sys.stderr, flush=True)
                    if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                        return processed
                    continue
                finally:
                    await detail_context.close()

                save_ad(connection, ad)
                append_csv(args.csv, ad)
                print_ad(ad)
                processed += 1

                if args.max_ads is not None and processed >= args.max_ads:
                    return processed

                await sleep_between_requests(args.delay_min, args.delay_max)

            # A small gap between result pages.
            await sleep_between_requests(args.delay_min, args.delay_max)

    finally:
        await search_context.close()

    return processed


async def async_main() -> int:
    args = parse_args()

    if args.start_page < 1:
        print("--start-page must be at least 1", file=sys.stderr)
        return 2
    if args.pages is not None and args.pages < 1:
        print("--pages must be at least 1", file=sys.stderr)
        return 2
    if args.delay_min < 0 or args.delay_max < 0 or args.delay_min > args.delay_max:
        print("--delay-min must be <= --delay-max and both must be non-negative", file=sys.stderr)
        return 2
    if args.watch is not None and args.watch < 60:
        print(
            "--watch must be at least 60 seconds to avoid excessive requests.",
            file=sys.stderr,
        )
        return 2

    connection = initialise_database(args.database)
    seed_database_from_csv(connection, args.csv)

    browser_specs: list[dict[str, Any]] = [
        {
            "name": "chrome",
            "launcher": "chromium",
            "channel": "chrome",
            "fallback_launcher": "chromium",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        },
        {
            "name": "edge",
            "launcher": "chromium",
            "channel": "msedge",
            "fallback_launcher": "chromium",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
            ),
        },
        {
            "name": "firefox",
            "launcher": "firefox",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
                "Gecko/20100101 Firefox/127.0"
            ),
        },
    ]

    try:
        async with async_playwright() as playwright:
            browser_pool: list[tuple[dict[str, Any], Browser]] = []
            for profile in browser_specs:
                try:
                    browser = await launch_browser_profile(playwright, profile, args.headful)
                except PlaywrightError as exc:
                    print(
                        f"WARNING: Could not launch {profile['name']} profile: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                browser_pool.append((profile, browser))

            if not browser_pool:
                print("Could not launch any browser profile.", file=sys.stderr)
                return 2

            try:
                while True:
                    count = await run_scan(browser_pool, connection, args)
                    print(
                        f"Scan finished; processed {count} ads.",
                        file=sys.stderr,
                        flush=True,
                    )

                    if args.watch is None:
                        break

                    await asyncio.sleep(args.watch)
            finally:
                for _, browser in browser_pool:
                    await browser.close()
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
