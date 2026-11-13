"""
Massive.com (formerly Polygon.io) options data fetch — paid upgrade over the
yfinance chain in fetch_options_data.py.

Uses the Option Chain Snapshot endpoint (GET /v3/snapshot/options/{ticker}),
which returns exchange-computed greeks, implied volatility, open interest, and
day volume for every contract — no homemade Black-Scholes needed. Fetches all
expiries out to EXPIRY_WINDOW_DAYS so dealer positioning reflects the full
near-term chain, not just the front (0DTE) expiry.

Requires MASSIVE_API_KEY. fetch_options_data.fetch_options_snapshot() calls
this first and falls back to yfinance when the key is missing or a request
fails.

Plan-tier notes:
  - last_quote / last_trade only appear on plans that include quotes/trades.
    Expected-move prices the ATM straddle from the best source available:
    quote midpoint > last trade > day-bar close (aggregates are on every
    options tier), and only falls back to an ATM-IV formula estimate when no
    contract prices exist at all.
  - Starter/Developer data is 15-minute delayed — fine for a pre-market brief.
  - open_interest is as of the prior session's close (updated each morning).

GEX convention matches fetch_options_data.py (SpotGamma-style):
  GEX per strike = (call_gamma * call_OI - put_gamma * put_OI) * 100 * spot² * 0.01
"""

import json
import math
import os
import threading
import time
import urllib.request
import urllib.parse
from collections import deque
from datetime import date, timedelta

from .market_calendar import market_today

MASSIVE_BASE       = "https://api.massive.com"
EXPIRY_WINDOW_DAYS = 30      # aggregate dealer positioning over expiries within this window
STRIKE_RANGE_PCT   = 0.08    # only analyze strikes within ±8% of spot
TOP_GEX_COUNT      = 6       # number of key gamma levels to surface
SKEW_MIN_DTE       = 5       # 25Δ skew / back-month IV measured on first expiry ≥ this many days out
PAGE_LIMIT         = 250     # API maximum per page
REQUEST_TIMEOUT    = 20

# Requests allowed per rolling minute. The plan this runs on answers 5 and then
# 429s, which is enough to blow through a paginated chain fetch or a review's
# per-contract bar pulls — and every one of those callers falls back to yfinance
# on error, so an un-throttled burst silently downgrades paid data to free data.
# Raise via MASSIVE_RPM if the subscription tier allows more.
MASSIVE_RPM  = int(os.environ.get("MASSIVE_RPM", 5))
MAX_RETRIES  = 4
MAX_BACKOFF  = 30


class MassiveError(RuntimeError):
    pass


class _RateLimiter:
    """Sliding-window limiter shared by every Massive caller in the process —
    the quota is per account, not per endpoint, so the brief pipeline and the
    previous-day review have to draw from the same budget."""

    def __init__(self, per_minute: int):
        self.per_minute = max(per_minute, 1)
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= 60:
                    self._hits.popleft()
                if len(self._hits) < self.per_minute:
                    self._hits.append(now)
                    return
                wait = 60 - (now - self._hits[0]) + 0.25
            time.sleep(wait)


_limiter = _RateLimiter(MASSIVE_RPM)


def _get_json(url: str, api_key: str) -> dict:
    """Throttled GET with bounded retry on 429 / 5xx / transient network errors."""
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        _limiter.acquire()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_error = MassiveError(f"HTTP {e.code} from {url.split('?')[0]}")
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable or attempt == MAX_RETRIES:
                raise last_error from e
            try:
                delay = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                delay = 0
            time.sleep(min(delay or 2 ** attempt * 2, MAX_BACKOFF))
        except Exception as e:
            last_error = MassiveError(str(e))
            if attempt == MAX_RETRIES:
                raise last_error from e
            time.sleep(min(2 ** attempt, 8))

    raise last_error or MassiveError(f"exhausted retries for {url.split('?')[0]}")


def _parse_contract(c: dict) -> dict | None:
    det = c.get("details") or {}
    kind, strike, expiry = det.get("contract_type"), det.get("strike_price"), det.get("expiration_date")
    if kind not in ("call", "put") or strike is None or not expiry:
        return None
    greeks = c.get("greeks") or {}
    day = c.get("day") or {}
    return {
        "kind":      kind,
        "strike":    round(float(strike), 2),
        "expiry":    expiry,
        "oi":        int(c.get("open_interest") or 0),
        "vol":       int(day.get("volume") or 0),
        "day_close": day.get("close"),
        "iv":        float(c.get("implied_volatility") or 0),
        "gamma":     greeks.get("gamma"),
        "delta":     greeks.get("delta"),
        "quote":     c.get("last_quote") or {},
        "trade":     c.get("last_trade") or {},
    }


def _fetch_chain(symbol: str, api_key: str) -> tuple[float, list[dict]]:
    """Return (spot, contracts) for strikes near spot across the expiry window."""
    base = f"{MASSIVE_BASE}/v3/snapshot/options/{symbol}"

    # Probe one contract to read the underlying price (needed for strike filters)
    probe = _get_json(f"{base}?limit=1", api_key)
    results = probe.get("results") or []
    spot = (results[0].get("underlying_asset") or {}).get("price") if results else None
    if not spot:
        raise MassiveError(f"{symbol}: no underlying price in snapshot")
    spot = float(spot)

    today = market_today()
    params = {
        "expiration_date.gte": today.isoformat(),
        "expiration_date.lte": (today + timedelta(days=EXPIRY_WINDOW_DAYS)).isoformat(),
        "strike_price.gte":    round(spot * (1 - STRIKE_RANGE_PCT), 2),
        "strike_price.lte":    round(spot * (1 + STRIKE_RANGE_PCT), 2),
        "limit":               PAGE_LIMIT,
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"

    contracts = []
    while url:
        page = _get_json(url, api_key)
        for c in page.get("results") or []:
            parsed = _parse_contract(c)
            if parsed:
                contracts.append(parsed)
        url = page.get("next_url")

    if not contracts:
        raise MassiveError(f"{symbol}: chain snapshot returned no contracts")
    return spot, contracts


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def _leg_price(c: dict) -> tuple[float | None, str | None]:
    """Best available price for one option leg, with its source.

    Preference: live quote midpoint > last trade > day-bar close. The last two
    are the only ones available on plan tiers without quotes; they lag (last
    print / prior-session close) but beat an IV-formula estimate.
    """
    q = c["quote"]
    if q.get("midpoint"):
        return float(q["midpoint"]), "quote"
    bid, ask = q.get("bid"), q.get("ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2, "quote"
    price = c["trade"].get("price")
    if price:
        return float(price), "trade"
    if c.get("day_close"):
        return float(c["day_close"]), "day_close"
    return None, None


def _max_pain(front: list[dict]) -> float | None:
    """Strike that minimises total option-holder value at the front expiry."""
    strikes = sorted({c["strike"] for c in front})
    if not strikes:
        return None
    calls = [(c["strike"], c["oi"]) for c in front if c["kind"] == "call"]
    puts  = [(c["strike"], c["oi"]) for c in front if c["kind"] == "put"]
    best_strike, best_pain = strikes[0], float("inf")
    for test_k in strikes:
        pain = (sum(max(test_k - k, 0) * oi for k, oi in calls)
                + sum(max(k - test_k, 0) * oi for k, oi in puts))
        if pain < best_pain:
            best_pain, best_strike = pain, test_k
    return best_strike


def _atm_iv(contracts: list[dict], spot: float) -> float | None:
    """Average call/put IV at the strike closest to spot."""
    ivs = [c["iv"] for c in contracts if c["iv"] > 0]
    if not ivs:
        return None
    by_dist = sorted((c for c in contracts if c["iv"] > 0), key=lambda c: abs(c["strike"] - spot))
    atm_strike = by_dist[0]["strike"]
    at_strike = [c["iv"] for c in contracts if c["strike"] == atm_strike and c["iv"] > 0]
    return sum(at_strike) / len(at_strike)


def _expected_move(front: list[dict], spot: float, dte: float) -> dict | None:
    """±$ band for the front expiry from the ATM straddle, else from ATM IV."""
    strikes_both = sorted(
        {c["strike"] for c in front if c["kind"] == "call"}
        & {c["strike"] for c in front if c["kind"] == "put"},
        key=lambda k: abs(k - spot),
    )
    move, method = None, None
    if strikes_both:
        atm = strikes_both[0]
        call_px, call_src = next(
            (_leg_price(c) for c in front if c["kind"] == "call" and c["strike"] == atm), (None, None))
        put_px, put_src = next(
            (_leg_price(c) for c in front if c["kind"] == "put" and c["strike"] == atm), (None, None))
        if call_px and put_px:
            move = call_px + put_px
            # "indicative" = at least one leg priced off a stale print rather than a live quote
            method = ("atm_straddle" if call_src == put_src == "quote"
                      else "atm_straddle_indicative")
    if move is None:
        iv = _atm_iv(front, spot)
        if not iv:
            return None
        T = max(dte, 0.5) / 365.0
        move, method = spot * iv * math.sqrt(T), "atm_iv"
    return {
        "method":    method,
        "move_usd":  round(move, 2),
        "move_pct":  round(move / spot * 100, 2),
        "band_low":  round(spot - move, 2),
        "band_high": round(spot + move, 2),
    }


def _skew_25d(contracts: list[dict]) -> dict | None:
    """25-delta put IV minus 25-delta call IV (positive = downside hedging demand)."""
    puts  = [c for c in contracts if c["kind"] == "put"  and c["delta"] is not None and c["iv"] > 0]
    calls = [c for c in contracts if c["kind"] == "call" and c["delta"] is not None and c["iv"] > 0]
    if not puts or not calls:
        return None
    p = min(puts,  key=lambda c: abs(c["delta"] + 0.25))
    c = min(calls, key=lambda c: abs(c["delta"] - 0.25))
    # Require deltas reasonably near 25Δ or the reading is meaningless
    if abs(p["delta"] + 0.25) > 0.10 or abs(c["delta"] - 0.25) > 0.10:
        return None
    return {
        "put_25d_iv":  round(p["iv"], 4),
        "call_25d_iv": round(c["iv"], 4),
        "skew":        round(p["iv"] - c["iv"], 4),
    }


def build_snapshot(symbol: str, spot: float, contracts: list[dict],
                   today: date | None = None) -> dict:
    """Turn a parsed chain into the morning-brief options dict (pure, testable)."""
    today = today or market_today()

    expiries = sorted({c["expiry"] for c in contracts})
    front_expiry = expiries[0]
    front = [c for c in contracts if c["expiry"] == front_expiry]
    front_dte = (date.fromisoformat(front_expiry) - today).days

    # GEX aggregated across the whole window, by strike
    gex_by_strike: dict[float, float] = {}
    front_gex = 0.0
    for c in contracts:
        if c["gamma"] is None or not c["oi"]:
            continue
        gex = c["gamma"] * c["oi"] * 100 * spot**2 * 0.01
        if c["kind"] == "put":
            gex = -gex
        gex_by_strike[c["strike"]] = gex_by_strike.get(c["strike"], 0.0) + gex
        if c["expiry"] == front_expiry:
            front_gex += gex
    net_gex = sum(gex_by_strike.values())

    top_levels = sorted(gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)[:TOP_GEX_COUNT]
    gex_levels = sorted(
        [{"strike": k, "gex": round(v, 2), "role": "call_wall" if v > 0 else "put_wall"}
         for k, v in top_levels],
        key=lambda x: x["strike"],
    )

    # Gamma flip: strike where cumulative GEX changes sign
    cumulative, flip_level = 0.0, None
    for k in sorted(gex_by_strike):
        prev = cumulative
        cumulative += gex_by_strike[k]
        if prev * cumulative < 0:
            flip_level = k
            break

    call_oi  = sum(c["oi"]  for c in contracts if c["kind"] == "call")
    put_oi   = sum(c["oi"]  for c in contracts if c["kind"] == "put")
    call_vol = sum(c["vol"] for c in contracts if c["kind"] == "call")
    put_vol  = sum(c["vol"] for c in contracts if c["kind"] == "put")

    # Unusual activity across every fetched expiry
    unusual = [
        {"strike": c["strike"], "expiry": c["expiry"], "kind": c["kind"],
         "volume": c["vol"], "open_interest": c["oi"],
         "vol_oi_ratio": round(c["vol"] / c["oi"], 2), "iv": round(c["iv"], 4)}
        for c in contracts
        if c["oi"] > 0 and c["vol"] >= 500 and c["vol"] / c["oi"] >= 0.40
    ]
    unusual.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)

    # Vol structure on the first expiry ≥ SKEW_MIN_DTE days out (0DTE skew is noise)
    back_expiry = next(
        (e for e in expiries if (date.fromisoformat(e) - today).days >= SKEW_MIN_DTE),
        None,
    )
    back = [c for c in contracts if c["expiry"] == back_expiry] if back_expiry else []

    skew = _skew_25d(back or front)
    if skew:
        skew["expiry"] = back_expiry or front_expiry

    front_atm_iv = _atm_iv(front, spot)
    back_atm_iv  = _atm_iv(back, spot) if back else None
    term = None
    if front_atm_iv and back_atm_iv and back_expiry != front_expiry:
        term = {
            "front_expiry":  front_expiry,
            "front_atm_iv":  round(front_atm_iv, 4),
            "back_expiry":   back_expiry,
            "back_atm_iv":   round(back_atm_iv, 4),
            "spread":        round(front_atm_iv - back_atm_iv, 4),
        }

    return {
        "symbol":               symbol,
        "source":               "massive",
        "spot":                 round(spot, 2),
        "expiry":               front_expiry,
        "days_to_expiry":       front_dte,
        "expiries_analyzed":    len(expiries),
        "expiry_window_days":   EXPIRY_WINDOW_DAYS,
        "net_gex":              round(net_gex, 2),
        "net_gex_front_expiry": round(front_gex, 2),
        "gamma_environment":    "positive" if net_gex > 0 else "negative",
        "gamma_flip_level":     flip_level,
        "max_pain":             _max_pain(front),
        "put_call_oi_ratio":    round(put_oi / call_oi, 3) if call_oi else None,
        "put_call_vol_ratio":   round(put_vol / call_vol, 3) if call_vol else None,
        "top_gex_levels":       gex_levels,
        "unusual_strikes":      unusual[:8],
        "expected_move":        _expected_move(front, spot, front_dte),
        "iv_skew_25d":          skew,
        "iv_term_structure":    term,
    }


def fetch_ticker_options_massive(symbol: str, api_key: str) -> dict:
    spot, contracts = _fetch_chain(symbol, api_key)
    return build_snapshot(symbol, spot, contracts)


if __name__ == "__main__":
    import os
    from datetime import datetime
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).parent / ".env")
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        raise SystemExit("MASSIVE_API_KEY not set")
    out = {sym: fetch_ticker_options_massive(sym, key) for sym in ["SPY", "QQQ", "IWM"]}
    print(json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                      "options": out}, indent=2))
