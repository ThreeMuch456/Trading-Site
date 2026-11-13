"""
Automatic EOD grading — feeds the learning loop.
Fetches the actual daily OHLC bar for each ticker (Massive.com, yfinance fallback)
and grades the saved gameplan against it: bias correct, bull/bear case triggered.
Writes straight into journal.outcomes, which get_performance_stats() /
synthesize_gameplan._build_historical_context() read back into the next brief.

Requires MASSIVE_API_KEY for the primary path; falls back to yfinance (no key)
when unset or a request fails, same fallback pattern as fetch_options_massive.py.
"""

import json
import re
from datetime import datetime, timedelta

from .market_calendar import market_today

from .fetch_options_massive import MASSIVE_BASE, MassiveError, _get_json

PRICE_RE = re.compile(r"\d{1,4}(?:\.\d{1,2})?")

# Fallback move-size bands when a session has no options-implied expected move.
NEUTRAL_PCT_FALLBACK  = 0.3
CAUTIOUS_PCT_FALLBACK = 0.5

# Directional calls on a session this flat are a scratch, not a win or loss —
# a ±0.05% day carries no information about whether a bullish/bearish read was right.
DIRECTIONAL_SCRATCH_PCT = 0.15


# ---------------------------------------------------------------------------
# Price bar fetch
# ---------------------------------------------------------------------------

# The daily open/close endpoint is preferred over the plain aggregate (see
# fetch_daily_bar_massive), but it may not be on the subscription. Probing it
# per ticker would burn half the 5-requests-per-minute budget on 404s, so a
# "not available" answer disables it for the rest of the process. Only 403/404
# count: a timeout or a 5xx is transient and must not downgrade the whole run.
_OPEN_CLOSE_AVAILABLE = True


def _note_open_close_failure(err: Exception) -> None:
    global _OPEN_CLOSE_AVAILABLE
    if any(f"HTTP {code}" in str(err) for code in (403, 404)):
        _OPEN_CLOSE_AVAILABLE = False

def fetch_daily_bar_massive(symbol: str, date_str: str, api_key: str) -> dict | None:
    """One day's regular-session OHLC from Massive (Polygon-compatible).

    Deliberately UNADJUSTED: the gameplan's price levels were written that
    morning in raw terms, and options strikes are raw too. A back-adjusted bar
    sits a full dividend below the tape after each ex-date, which silently
    shifts every level comparison (SPY's quarterly distribution is ~$2 — enough
    to flip a gamma-flip or max-pain verdict on its own).

    Tries the daily open/close endpoint first, which reports the regular session
    and breaks pre-market and after-hours out into their own fields, then falls
    back to the plain daily aggregate. That order matters from 2026-12-06: under
    23/5 a daily aggregate can fold the overnight session into high/low and
    volume, and a thin 02:00 wick through a level the regular session never
    approached would grade as a real test (see market_calendar's notes on the
    schedule). `rth_scoped` records which kind of bar came back so callers can
    tighten it — see rth_clamp — or flag it rather than trust it silently."""
    data = {}
    if _OPEN_CLOSE_AVAILABLE:
        try:
            data = _get_json(
                f"{MASSIVE_BASE}/v1/open-close/{symbol}/{date_str}?adjusted=false", api_key)
        except MassiveError as e:
            _note_open_close_failure(e)
    if data.get("open") is not None and data.get("close") is not None:
        return {
            "open":   round(float(data["open"]), 2),
            "high":   round(float(data["high"]), 2),
            "low":    round(float(data["low"]), 2),
            "close":  round(float(data["close"]), 2),
            "volume": int(data.get("volume") or 0),
            "source": "massive (open/close)",
            "rth_scoped": True,
        }

    url = (f"{MASSIVE_BASE}/v2/aggs/ticker/{symbol}/range/1/day/{date_str}/{date_str}"
           f"?adjusted=false")
    data = _get_json(url, api_key)
    results = data.get("results") or []
    if not results:
        return None
    bar = results[0]
    return {
        "open":   round(float(bar["o"]), 2),
        "high":   round(float(bar["h"]), 2),
        "low":    round(float(bar["l"]), 2),
        "close":  round(float(bar["c"]), 2),
        "volume": int(bar.get("v") or 0),
        "source": "massive (daily agg)",
        # The aggregate is not session-scoped: high/low/volume may include
        # extended hours, so a caller holding RTH intraday bars should clamp.
        "rth_scoped": False,
    }


def rth_clamp(bar: dict, path: list[dict], session_open: str = "09:30") -> dict:
    """Tighten a daily bar's high/low to an RTH-filtered intraday path.

    Free accuracy for callers that already paid for intraday bars: the path is
    filtered to the regular session, so its extremes are the session's extremes,
    while an unscoped daily bar's may not be. Narrowing only — a path reaching
    further than the daily bar means the two feeds disagree, which is not
    something to bake into a grade.

    No-ops unless the path plausibly *is* the session: it has to start at the
    open and agree with the daily bar's opening print, otherwise a partial or
    misaligned feed would clip a real high.
    """
    if bar.get("rth_scoped") or not path:
        return bar
    if path[0]["t"] != session_open:
        return bar
    if not bar["open"] or abs(path[0]["o"] - bar["open"]) / bar["open"] > 0.005:
        return bar

    high = min(bar["high"], max(b["h"] for b in path))
    low  = max(bar["low"],  min(b["l"] for b in path))
    if (high, low) == (bar["high"], bar["low"]):
        return {**bar, "rth_scoped": True}
    return {**bar, "high": round(high, 2), "low": round(low, 2),
            "rth_scoped": True, "source": f"{bar['source']}, RTH range from intraday"}


def fetch_daily_bar_yfinance(symbol: str, date_str: str) -> dict | None:
    import yfinance as yf
    d = datetime.fromisoformat(date_str)
    start = d.strftime("%Y-%m-%d")
    end   = (d + timedelta(days=1)).strftime("%Y-%m-%d")
    # auto_adjust=False for the same reason the Massive path passes
    # adjusted=false — raw prices are what the brief's levels refer to.
    hist = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
    if hist.empty:
        return None
    row = hist.iloc[0]
    return {
        "open":   round(float(row["Open"]), 2),
        "high":   round(float(row["High"]), 2),
        "low":    round(float(row["Low"]), 2),
        "close":  round(float(row["Close"]), 2),
        "volume": int(row["Volume"]),
        "source": "yfinance",
        # Daily history is regular-session only (prepost defaults off).
        "rth_scoped": True,
    }


def fetch_daily_bar(symbol: str, date_str: str, massive_api_key: str | None) -> dict | None:
    if massive_api_key:
        try:
            bar = fetch_daily_bar_massive(symbol, date_str, massive_api_key)
            if bar:
                return bar
        except MassiveError:
            pass
    try:
        return fetch_daily_bar_yfinance(symbol, date_str)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _extract_price_levels(text: str, spot: float) -> list[float]:
    """Numbers in `text` that plausibly read as a price near `spot` (±30%)."""
    if not text or not spot:
        return []
    lo, hi = spot * 0.7, spot * 1.3
    return [v for v in (float(m.group()) for m in PRICE_RE.finditer(text)) if lo <= v <= hi]


def _grade_bias(bias: str, day_pct: float, expected_move_pct: float | None) -> int | None:
    """1/0/None. Neutral/cautious bands scale with the options-implied expected
    move when available (a "calm" day at a volatile ticker isn't the same as a
    calm day at a quiet one); otherwise fall back to fixed bands."""
    bias = (bias or "").lower()
    neutral_band  = expected_move_pct * 0.25 if expected_move_pct else NEUTRAL_PCT_FALLBACK
    cautious_band = expected_move_pct * 0.5  if expected_move_pct else CAUTIOUS_PCT_FALLBACK

    if bias in ("bullish", "bearish"):
        if abs(day_pct) < DIRECTIONAL_SCRATCH_PCT:
            return None  # scratch — flat day, leave unscored
        if bias == "bullish":
            return 1 if day_pct > 0 else 0
        return 1 if day_pct < 0 else 0
    if bias == "neutral":
        return 1 if abs(day_pct) <= neutral_band else 0
    if bias == "cautious":
        return 1 if abs(day_pct) <= cautious_band else 0
    return None


def _grade_triggers(plan: dict, bar: dict) -> tuple[int | None, int | None]:
    """Bull/bear case triggered, inferred from price levels named in the case
    text vs. the day's actual range. The lowest number mentioned in the bull
    case is treated as the breakout trigger (targets sit above it); the highest
    number in the bear case as the breakdown trigger (targets sit below it).

    Requires the close to have actually moved in that direction, not just an
    intraday wick through the level — bull and bear cases both cite levels
    close to the day's open (e.g. a put wall doubling as the bear trigger while
    a call wall a few points higher is the bull trigger), so touch-only would
    mark both "triggered" on almost every session regardless of which one
    actually played out."""
    spot = bar["open"]

    bull_levels = _extract_price_levels(plan.get("bull_case", ""), spot)
    bull_hit = (1 if bar["high"] >= min(bull_levels) and bar["close"] > bar["open"] else 0) \
        if bull_levels else None

    bear_levels = _extract_price_levels(plan.get("bear_case", ""), spot)
    bear_hit = (1 if bar["low"] <= max(bear_levels) and bar["close"] < bar["open"] else 0) \
        if bear_levels else None

    return bull_hit, bear_hit


def grade_session(row, massive_api_key: str | None) -> dict:
    """Grade every ticker in a saved session's gameplan against actual price
    action. Returns {ticker: outcome_dict | {"error": ...}}."""
    from .journal import TICKERS

    gameplan     = json.loads(row["gameplan"])
    options_data = json.loads(row["options_data"])
    ticker_plans = gameplan.get("gameplan", {})
    opts         = options_data.get("options", {})

    graded = {}
    for ticker in TICKERS:
        bar = fetch_daily_bar(ticker, row["date"], massive_api_key)
        if not bar:
            graded[ticker] = {"error": "no price bar available for this date"}
            continue

        plan = ticker_plans.get(ticker, {})
        day_pct = round((bar["close"] - bar["open"]) / bar["open"] * 100, 2)
        expected_move_pct = ((opts.get(ticker) or {}).get("expected_move") or {}).get("move_pct")

        bull_hit, bear_hit = _grade_triggers(plan, bar)

        graded[ticker] = {
            "open": bar["open"], "close": bar["close"],
            "high": bar["high"], "low": bar["low"],
            "day_pct": day_pct,
            "bias_correct":   _grade_bias(plan.get("bias"), day_pct, expected_move_pct),
            "bull_triggered": bull_hit,
            "bear_triggered": bear_hit,
            # Flag an unscoped bar in the note rather than hiding it: the
            # trigger grades above read high/low, and under 23/5 those can
            # carry overnight prints the regular session never saw.
            "notes": f"auto-graded ({bar['source']})"
                     + ("" if bar.get("rth_scoped") else " — range not RTH-scoped"),
        }
    return graded


def save_graded_outcomes(session_id: int, graded: dict) -> None:
    from .journal import get_conn
    conn = get_conn()
    try:
        for ticker, o in graded.items():
            if "error" in o:
                continue
            conn.execute(
                """INSERT INTO outcomes
                     (session_id, ticker, open, close, day_pct,
                      bias_correct, bull_triggered, bear_triggered, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, ticker) DO UPDATE SET
                     open=excluded.open, close=excluded.close, day_pct=excluded.day_pct,
                     bias_correct=excluded.bias_correct,
                     bull_triggered=excluded.bull_triggered,
                     bear_triggered=excluded.bear_triggered, notes=excluded.notes""",
                (session_id, ticker, o["open"], o["close"], o["day_pct"],
                 o["bias_correct"], o["bull_triggered"], o["bear_triggered"], o["notes"]),
            )
        conn.commit()
    finally:
        conn.close()


def autograde_date(date_str: str, massive_api_key: str | None = None) -> dict:
    """Grade and save outcomes for one session date. Returns the graded dict.

    No-ops (returns {"skipped": ...}) on days NYSE was closed — those sessions
    hold a forward-looking preview gameplan (see synthesize_gameplan.py), not a
    prediction for that date, so there's nothing to grade against."""
    import os
    from datetime import date as _date
    from .journal import get_conn
    from .market_calendar import market_status

    session = market_status(_date.fromisoformat(date_str))
    if session["status"] == "closed":
        return {"skipped": f"NYSE closed {date_str} ({session.get('reason') or 'holiday/weekend'})"}

    massive_api_key = massive_api_key or os.environ.get("MASSIVE_API_KEY")

    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE date = ?", (date_str,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"No session found for {date_str}")

    graded = grade_session(row, massive_api_key)
    save_graded_outcomes(row["id"], graded)
    return graded


if __name__ == "__main__":
    import os
    import sys
    from datetime import date
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).parent / ".env")
    target = sys.argv[1] if len(sys.argv) > 1 else market_today().isoformat()
    result = autograde_date(target, os.environ.get("MASSIVE_API_KEY"))
    print(json.dumps(result, indent=2))
