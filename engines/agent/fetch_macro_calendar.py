"""
Macro calendar fetch — step 2 of the trading agent.
Pulls upcoming economic release dates from FRED and earnings dates from yfinance.

Requires a free FRED API key: https://fredaccount.stlouisfed.org/login/secure/
Set it as the FRED_API_KEY environment variable or pass it directly.
"""

import os
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
from datetime import datetime, date, timedelta
import yfinance as yf

from .market_calendar import market_today

# Release names (substrings) to keep from the FRED bulk calendar.
# FRED returns hundreds of daily-updating series; we only want market-moving events.
FRED_RELEASE_FILTERS = [
    "Employment Situation",       # NFP
    "JOLTS",
    "Consumer Price Index",       # CPI
    "Producer Price Index",       # PPI
    "Gross Domestic Product",     # GDP (top-level only)
    "Retail Sales",
    "Personal Income and Outlays",  # PCE — exact name avoids state-level noise
    # NOT "FOMC Press Release" — FRED's release-dates API for that release_id
    # returns a bogus entry for nearly every calendar day (see market_calendar.py's
    # FOMC_DATES for the real, hardcoded schedule sourced from federalreserve.gov).
    "New Residential Construction",
    "Consumer Sentiment",         # U of M
    "ISM Manufacturing",
    "ISM Services",
    "Import and Export Price",
]

# Sub-releases that match a filter above but are low-signal noise
FRED_RELEASE_EXCLUSIONS = [
    "by State",
    "by Industry",
    "Monthly State",
    "Selected Real",
    "Debt to Gross",
]

# ETFs don't report earnings — only individual equities here
EARNINGS_WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "JPM", "GS"]

LOOKAHEAD_DAYS = 14


def _fred_get(path: str, api_key: str, params: dict = None,
              retries: int = 3, timeout: int = 30) -> dict:
    base = "https://api.stlouisfed.org/fred"
    p = {"api_key": api_key, "file_type": "json"}
    if params:
        p.update(params)
    url = f"{base}/{path}?{urllib.parse.urlencode(p)}"
    # FRED occasionally stalls; a single un-retried timeout here used to fail the
    # whole morning brief. Retry with exponential backoff before giving up.
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
    raise last_err


def fetch_economic_releases(api_key: str, lookahead: int = LOOKAHEAD_DAYS) -> list[dict]:
    """Return market-moving economic releases scheduled in the next `lookahead` days."""
    today = market_today()
    end = today + timedelta(days=lookahead)

    data = _fred_get(
        "releases/dates",
        api_key,
        {
            "realtime_start": today.isoformat(),
            "realtime_end": end.isoformat(),
            "include_release_dates_with_no_data": "true",
        },
    )

    # Collect earliest scheduled date per release_id to avoid daily-update spam
    earliest: dict[int, dict] = {}
    for entry in data.get("release_dates", []):
        name = entry.get("release_name", "")
        if not any(f.lower() in name.lower() for f in FRED_RELEASE_FILTERS):
            continue
        if any(ex.lower() in name.lower() for ex in FRED_RELEASE_EXCLUSIONS):
            continue
        rid = entry["release_id"]
        release_date = entry["date"]
        if rid not in earliest or release_date < earliest[rid]["date"]:
            earliest[rid] = {
                "date": release_date,
                "event": name,
                "release_id": rid,
                "days_away": (date.fromisoformat(release_date) - today).days,
            }

    return sorted(earliest.values(), key=lambda x: (x["date"], x["event"]))


def fomc_events(start: date | None = None, lookahead: int = LOOKAHEAD_DAYS) -> list[dict]:
    """FOMC decision dates from the hardcoded Fed schedule (see market_calendar).

    Deliberately network-free and kept OUT of fetch_economic_releases: merging it
    there once meant a transient FRED outage silently erased the rate decision too
    (a 5am FRED timeout on 2026-07-29 produced a brief that reported an empty
    calendar on an FOMC day). A missed rate decision is the worst possible calendar
    failure, so it must not share a failure domain with the FRED HTTP call.
    """
    from .market_calendar import upcoming_fomc_dates
    return [{**f, "release_id": None}
            for f in upcoming_fomc_dates(start or market_today(), lookahead)]


def fetch_earnings_dates(symbols: list[str]) -> list[dict]:
    """Return upcoming earnings dates for a list of tickers."""
    today = market_today()
    end = today + timedelta(days=LOOKAHEAD_DAYS)
    events = []

    for sym in symbols:
        try:
            cal = yf.Ticker(sym).calendar
            if not cal:
                continue
            earnings_dates = cal.get("Earnings Date")
            if not earnings_dates:
                continue
            if not isinstance(earnings_dates, list):
                earnings_dates = [earnings_dates]
            for ed in earnings_dates:
                # yfinance returns plain datetime.date here (older versions gave
                # pandas Timestamp / datetime). A `hasattr(ed, "date")` guard used
                # to silently coerce every plain date to None — dropping the entire
                # earnings calendar without raising. Check datetime first: it
                # subclasses date, so the order matters.
                if isinstance(ed, datetime):
                    ed_date = ed.date()
                elif isinstance(ed, date):
                    ed_date = ed
                else:
                    ed_date = None
                if ed_date and today <= ed_date <= end:
                    events.append({
                        "date": ed_date.isoformat(),
                        "event": f"{sym} earnings",
                        "symbol": sym,
                        "days_away": (ed_date - today).days,
                    })
        except Exception:
            pass  # ticker unavailable or no earnings calendar

    events.sort(key=lambda x: x["date"])
    return events


def fetch_macro_calendar(api_key: str | None = None) -> dict:
    api_key = api_key or os.environ.get("FRED_API_KEY")
    if not api_key:
        raise ValueError(
            "FRED API key required. Set FRED_API_KEY env var or pass api_key=. "
            "Get a free key at https://fredaccount.stlouisfed.org/login/secure/"
        )

    today = market_today()

    # A brief without the FRED releases is far better than no brief at all, so a
    # persistent FRED failure degrades to a partial calendar (with an error note)
    # rather than sinking the whole run.
    economic_error = None
    try:
        fred_releases = fetch_economic_releases(api_key)
    except Exception as e:
        fred_releases = []
        economic_error = str(e)

    # Merged AFTER the try/except so FOMC survives a FRED outage — see fomc_events.
    economic = sorted(fred_releases + fomc_events(today, LOOKAHEAD_DAYS),
                      key=lambda x: (x["date"], x["event"]))

    earnings = fetch_earnings_dates(EARNINGS_WATCHLIST)

    # Market-calendar awareness: today's session status + upcoming NYSE closures,
    # folded into the unified event list so holidays/half-days show in the calendar.
    from .market_calendar import market_status, upcoming_closures
    session = market_status(today)
    closures = [
        {"date": c["date"], "event": c["event"], "days_away": c["days_away"]}
        for c in upcoming_closures(today, LOOKAHEAD_DAYS)
    ]

    all_events = sorted(economic + earnings + closures,
                        key=lambda x: (x["date"], x["event"]))

    result = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "lookahead_days": LOOKAHEAD_DAYS,
        "market_session": session,
        "economic_releases": economic,
        "earnings": earnings,
        "all_events_sorted": all_events,
    }
    if economic_error:
        result["economic_error"] = economic_error
    return result


if __name__ == "__main__":
    data = fetch_macro_calendar()
    print(json.dumps(data, indent=2))
