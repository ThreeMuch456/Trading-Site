"""
Massive.com market-data client.

Massive (https://massive.com) is a Polygon.io-compatible REST API. This module
wraps the handful of endpoints the scanner needs:

  - Option chain snapshot   GET /v3/snapshot/options/{underlying}
  - Single-contract snapshot GET /v3/snapshot/options/{underlying}/{optionTicker}
  - Stock snapshot           GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}

Auth is a Bearer token (the "Options Starter" plan, 15-min delayed, unlimited
calls). Set MASSIVE_API_KEY in the environment.

Every function returns *normalized* flat dicts so the rest of the codebase never
has to know Massive's nested response shape.
"""

import os
import time
import logging
from datetime import date, datetime, timedelta, timezone

import pytz
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

BASE_URL   = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")
API_KEY    = os.getenv("MASSIVE_API_KEY", "").strip()
ET          = pytz.timezone("US/Eastern")
CHAIN_LIMIT = 250          # max contracts per page (API max)
MAX_PAGES   = 20           # safety cap on pagination (250 * 20 = 5000 contracts)
TIMEOUT     = 20           # seconds per request
MAX_RETRIES = 4            # retries on 429 / 5xx


class MassiveError(RuntimeError):
    """Raised when the Massive API cannot be reached or returns an error."""


_session: requests.Session | None = None


def _require_key() -> None:
    if not API_KEY:
        raise MassiveError(
            "MASSIVE_API_KEY is not set. Add it to your .env "
            "(get one at https://massive.com/dashboard)."
        )


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
        })
        _session = s
    return _session


def _request(url: str, params: dict | None = None) -> dict:
    """
    GET a Massive URL (absolute or path) with retry/backoff on 429 + 5xx.
    Returns the parsed JSON body. Raises MassiveError on unrecoverable failure.
    """
    _require_key()
    if url.startswith("/"):
        url = BASE_URL + url

    session = _get_session()
    backoff = 1.0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise MassiveError(f"Request to {url} failed: {e}") from e
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in (401, 403):
            raise MassiveError(
                f"Massive auth failed ({resp.status_code}). Check MASSIVE_API_KEY."
            )

        # 429 or 5xx → retry with backoff; other 4xx → give up
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise MassiveError(
                    f"Massive returned {resp.status_code} for {url} after "
                    f"{MAX_RETRIES} attempts."
                )
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else backoff)
            backoff *= 2
            continue

        raise MassiveError(
            f"Massive returned {resp.status_code} for {url}: {resp.text[:200]}"
        )

    raise MassiveError(f"Exhausted retries for {url}")  # pragma: no cover


# ── Normalizers ─────────────────────────────────────────────────────────────

def _num(val):
    """Coerce to float, or None if missing/unparseable."""
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _bar_date(day: dict):
    """
    The ET trading date the `day` aggregate belongs to, as "YYYY-MM-DD".

    Massive keeps returning the *previous* session's daily bar for a contract
    that hasn't traded yet today — same shape, but `volume`, `close` and the
    matching `last_trade` are all stale (observed on ~44% of SPY/QQQ/IWM
    contracts with non-zero volume). `day.last_updated` is stamped at midnight
    ET of the bar's own session, which is what makes the two cases separable.

    Returns None when the field is absent, so callers can choose to be
    permissive rather than silently discarding the whole chain.
    """
    ns = day.get("last_updated")
    if not ns:
        return None
    try:
        dt = datetime.fromtimestamp(float(ns) / 1e9, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def _normalize_contract(result: dict) -> dict:
    """Flatten one option-chain snapshot result into the scanner's shape."""
    details    = result.get("details", {}) or {}
    day        = result.get("day", {}) or {}
    quote      = result.get("last_quote", {}) or {}
    trade      = result.get("last_trade", {}) or {}
    greeks     = result.get("greeks", {}) or {}
    underlying = result.get("underlying_asset", {}) or {}

    ctype = (details.get("contract_type") or "").lower()
    ctype = "call" if ctype.startswith("c") else "put" if ctype.startswith("p") else ctype

    ask      = _num(quote.get("ask"))
    bid      = _num(quote.get("bid"))
    midpoint = _num(quote.get("midpoint"))
    last_trade_price = _num(trade.get("price"))
    day_close = _num(day.get("close"))
    day_vwap  = _num(day.get("vwap"))

    # Quotes (last_quote) are a higher-tier entitlement. On the current plan they
    # are empty on every contract, so `price` is in practice always the last
    # trade — which is why `side` below stays None and the @ASK/@BID tags never
    # appear. Fall back to the day's close/VWAP so premium is still estimable.
    price = ask or midpoint or last_trade_price or day_close or day_vwap

    # Aggressor-side inference: a trade at/above the ask was lifted (buyer
    # aggressive), at/below the bid was hit (seller aggressive). Requires quote
    # entitlement (Developer tier) — stays None on plans without quotes.
    side = None
    if last_trade_price and (bid or ask):
        if ask and last_trade_price >= ask * 0.99:
            side = "buy"
        elif bid and last_trade_price <= bid * 1.01:
            side = "sell"
        elif bid and ask:
            side = "mid"

    return {
        "option_ticker":    details.get("ticker"),
        "contract_type":    ctype,
        "strike":           _num(details.get("strike_price")),
        "expiry":           details.get("expiration_date"),
        "volume":           _num(day.get("volume")) or 0,
        "open_interest":    _num(result.get("open_interest")) or 0,
        "bid":              bid,
        "ask":              ask,
        "midpoint":         midpoint,
        "last_trade_price": last_trade_price,
        "day_close":        day_close,
        "day_vwap":         day_vwap,
        "day_bar_date":     _bar_date(day),
        "price":            price,
        "side":             side,
        "iv":               _num(result.get("implied_volatility")),
        "delta":            _num(greeks.get("delta")),
        "gamma":            _num(greeks.get("gamma")),
        "theta":            _num(greeks.get("theta")),
        "vega":             _num(greeks.get("vega")),
        "underlying_price": _num(underlying.get("price")),
    }


def _normalize_stock(snap: dict) -> dict:
    """Flatten a stock snapshot ticker object into a simple dict."""
    day       = snap.get("day", {}) or {}
    prev      = snap.get("prevDay", {}) or {}
    last_tr   = snap.get("lastTrade", {}) or {}

    last_price = _num(last_tr.get("p")) or _num(day.get("c"))
    prev_close = _num(prev.get("c"))
    day_change_pct = None
    if last_price and prev_close:
        day_change_pct = round((last_price - prev_close) / prev_close * 100, 2)

    return {
        "symbol":         snap.get("ticker"),
        "last_price":     round(last_price, 2) if last_price else None,
        "prev_close":     round(prev_close, 2) if prev_close else None,
        "day_change_pct": day_change_pct,
        "day_open":       _num(day.get("o")),
        "day_close":      _num(day.get("c")),
        "day_high":       _num(day.get("h")),
        "day_low":        _num(day.get("l")),
        "day_volume":     int(day.get("v")) if day.get("v") else None,
    }


# ── Public API ──────────────────────────────────────────────────────────────

def get_option_chain(underlying: str, max_expiry_days: int = 45) -> list[dict]:
    """
    Return every option contract for `underlying` expiring within
    `max_expiry_days`, as a list of normalized dicts. Follows pagination.
    """
    cutoff = (date.today() + timedelta(days=max_expiry_days)).isoformat()
    params = {
        "expiration_date.lte": cutoff,
        "limit": CHAIN_LIMIT,
        "order": "asc",
        "sort": "expiration_date",
    }

    contracts: list[dict] = []
    url = f"/v3/snapshot/options/{underlying}"
    next_params: dict | None = params
    next_url = None

    for _ in range(MAX_PAGES):
        body = _request(url, next_params)
        for result in body.get("results", []) or []:
            contracts.append(_normalize_contract(result))

        next_url = body.get("next_url")
        if not next_url:
            break
        # next_url is absolute and already carries the cursor; auth is via header.
        url, next_params = next_url, None

    if next_url:
        # Hit MAX_PAGES with more data available. Pages are sorted by nearest
        # expiry, so the truncated tail is the furthest-dated contracts.
        logger.warning(
            "%s: option chain truncated at %d contracts (MAX_PAGES=%d); "
            "furthest expiries dropped", underlying, len(contracts), MAX_PAGES
        )

    return contracts


def get_contract_snapshot(underlying: str, option_ticker: str) -> dict | None:
    """
    Return a single normalized contract snapshot, or None if unavailable.
    Used for cheap watchlist P&L refreshes.
    """
    if not option_ticker:
        return None
    url = f"/v3/snapshot/options/{underlying}/{option_ticker}"
    body = _request(url)
    result = body.get("results")
    if not result:
        return None
    return _normalize_contract(result)


def get_stock_snapshot(ticker: str) -> dict:
    """Return a normalized stock snapshot (price/volume) for one ticker."""
    url = f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    body = _request(url)
    snap = body.get("ticker") or body.get("results") or {}
    return _normalize_stock(snap)


if __name__ == "__main__":
    # Quick manual smoke test:  python massive_client.py AAPL
    import sys, json
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    chain = get_option_chain(sym, 45)
    print(f"{sym}: {len(chain)} contracts within 45 days")
    print(json.dumps(chain[:2], indent=2))
