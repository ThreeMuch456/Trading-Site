"""Phase B of the flow predictiveness loop: label signal outcomes from price.

For each captured flow_signal missing an outcome whose required ETF price bars
are now available, compute the realized move (the y) from the shared Massive
minute aggs (T-15 delayed, but complete by the EOD / next-day label pass):

  intraday cutoffs (10:30/12:00/14:00) -> horizon "to_close":
      ref = ETF price at the cutoff, exit = that session's close.
  eod signal -> horizon "open_close": ref = session open,  exit = session close.
  eod signal -> horizon "next_day":   ref = session close, exit = next session's close.

signed_return = raw_return * implied-direction sign, so a correct directional
call is always positive; high/low excursion bracket the path ref -> exit.

Gracefully degrades: if the plan lacks stock aggs (401/403/404), minute bars come
back empty and outcomes simply stay unlabeled (retried each pass) rather than
erroring — the same entitlement posture as the rest of the engine.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .learning import _db

# Which horizons each cutoff is expected to produce.
_EXPECTED: dict[str, tuple[str, ...]] = {
    "10:30": ("to_close",),
    "12:00": ("to_close",),
    "14:00": ("to_close",),
    "eod":   ("open_close", "next_day"),
}

_OUTCOME_COLS = (
    "signal_id", "horizon", "labeled_at", "ref_price", "exit_price",
    "raw_return", "signed_return", "high_excursion", "low_excursion",
    "direction_correct",
)


# ── bar helpers ──────────────────────────────────────────────────────────────────

def _ts(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["ts"])


def _get_bars(client, ticker: str, d: date, cache: dict) -> list[dict]:
    key = (ticker, d.isoformat())
    if key not in cache:
        try:
            cache[key] = client.fetch_minute_bars(ticker, d) or []
        except Exception:
            cache[key] = []
    return cache[key]


def _next_session_bars(client, ticker: str, rd: date, cache: dict):
    """(date, bars) of the first weekday after rd that has bars (skips holidays)."""
    for off in range(1, 6):
        d = rd + timedelta(days=off)
        if d.weekday() >= 5:            # Sat/Sun
            continue
        bars = _get_bars(client, ticker, d, cache)
        if bars:
            return d, bars
    return None, []


def _price_at(bars: list[dict], cutoff: datetime):
    """Close of the last bar at or before `cutoff` (bars are ascending)."""
    last = None
    for b in bars:
        if _ts(b) <= cutoff:
            last = b.get("c")
        else:
            break
    return last


def _excursion(bars: list[dict], start: datetime | None, end: datetime, ref: float):
    """(high%, low%) excursion of price vs ref over (start, end]; start=None = all."""
    hi = lo = None
    for b in bars:
        t = _ts(b)
        if (start is not None and t <= start) or t > end:
            continue
        h, l = b.get("h"), b.get("l")
        if h is not None:
            hi = h if hi is None else max(hi, h)
        if l is not None:
            lo = l if lo is None else min(lo, l)
    if not ref:
        return None, None
    high_exc = round((hi / ref - 1) * 100, 4) if hi is not None else None
    low_exc = round((lo / ref - 1) * 100, 4) if lo is not None else None
    return high_exc, low_exc


# ── labeling ─────────────────────────────────────────────────────────────────────

def _finalize(ref, exit_px, implied_dir, high_exc, low_exc) -> dict | None:
    if not ref or exit_px is None:
        return None
    raw = (exit_px / ref - 1) * 100
    sign = {"up": 1.0, "down": -1.0}.get(implied_dir, 0.0)
    signed = raw * sign
    return {
        "ref_price": round(float(ref), 4),
        "exit_price": round(float(exit_px), 4),
        "raw_return": round(raw, 4),
        "signed_return": round(signed, 4),
        "high_excursion": high_exc,
        "low_excursion": low_exc,
        "direction_correct": 1 if signed > 0 else 0,
    }


def _label_one(sig: sqlite3.Row, horizon: str, client, cache: dict) -> dict | None:
    ticker = sig["ticker"]
    rd = date.fromisoformat(sig["run_date"])
    implied = sig["implied_dir"]

    if horizon == "to_close":
        bars = _get_bars(client, ticker, rd, cache)
        if not bars:
            return None
        cutoff = datetime.fromisoformat(sig["cutoff_ts"])
        ref = _price_at(bars, cutoff)
        if ref is None:
            return None
        exit_px = bars[-1].get("c")
        high, low = _excursion(bars, cutoff, _ts(bars[-1]), ref)
        return _finalize(ref, exit_px, implied, high, low)

    if horizon == "open_close":
        bars = _get_bars(client, ticker, rd, cache)
        if not bars:
            return None
        ref = bars[0].get("o")
        exit_px = bars[-1].get("c")
        high, low = _excursion(bars, None, _ts(bars[-1]), ref)
        return _finalize(ref, exit_px, implied, high, low)

    if horizon == "next_day":
        today = _get_bars(client, ticker, rd, cache)
        if not today:
            return None
        ref = today[-1].get("c")
        _, nd_bars = _next_session_bars(client, ticker, rd, cache)
        if not nd_bars:
            return None                # next session not available yet — retry later
        exit_px = nd_bars[-1].get("c")
        high, low = _excursion(nd_bars, None, _ts(nd_bars[-1]), ref)
        return _finalize(ref, exit_px, implied, high, low)

    return None


def _due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Signals that are still missing at least one of their expected outcomes."""
    return conn.execute(
        """
        SELECT s.* FROM flow_signals s
        LEFT JOIN (
            SELECT signal_id, COUNT(*) AS n FROM flow_outcomes GROUP BY signal_id
        ) o ON o.signal_id = s.id
        WHERE COALESCE(o.n, 0) < CASE s.cutoff WHEN 'eod' THEN 2 ELSE 1 END
        ORDER BY s.run_date, s.ticker, s.cutoff
        """
    ).fetchall()


def label_due(db_path: str | None = None, client=None, now: datetime | None = None) -> int:
    """Label every captured signal whose outcome bars are now available.

    Idempotent per (signal_id, horizon). Returns the number of outcomes written.
    Safe to run every EOD: same-day horizons label on the capture day, next_day
    the following session once that close exists."""
    if client is None:
        from .. import massive as _massive
        client = _massive.client()

    cache: dict = {}
    written = 0
    with _db(db_path) as conn:
        due = _due(conn)
        if not due:
            return 0
        have = {(r["signal_id"], r["horizon"]) for r in conn.execute(
            "SELECT signal_id, horizon FROM flow_outcomes").fetchall()}
        labeled_at = (now or datetime.now(timezone.utc)).isoformat()
        for sig in due:
            for horizon in _EXPECTED.get(sig["cutoff"], ()):
                if (sig["id"], horizon) in have:
                    continue
                out = _label_one(sig, horizon, client, cache)
                if out is None:
                    continue
                conn.execute(
                    f"INSERT OR REPLACE INTO flow_outcomes ({', '.join(_OUTCOME_COLS)}) "
                    f"VALUES ({', '.join('?' for _ in _OUTCOME_COLS)})",
                    (sig["id"], horizon, labeled_at, out["ref_price"], out["exit_price"],
                     out["raw_return"], out["signed_return"], out["high_excursion"],
                     out["low_excursion"], out["direction_correct"]),
                )
                written += 1
    return written
