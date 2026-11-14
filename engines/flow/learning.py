"""Flow predictiveness learning loop — closes the flow scanner's open loop.

The scanner scores contracts (signals.score_alert) but never learns whether a
day's index-ETF flow actually *preceded* a move. This module mirrors the
structure engine's oplog (log signal X -> label outcome y -> report -> tune),
scoped to the index ETFs and the same-session / overnight horizons:

  Phase A (capture, here) — after the close, aggregate the day's stored
      sweep_alerts per ETF into a directional flow signal at several intraday
      cutoffs plus an end-of-day reading (the X), written to flow_signals.
  Phase B (label, label.py) — attach the realized ETF price move from Massive
      aggs (the y), per horizon.

EOD-only, no realtime feed: sweep_alerts persist with detected_at timestamps, so
the cumulative directional skew "as of 10:30" is reconstructable after the close.

Storage is a SEPARATE stdlib-sqlite3 DB (flow_learning.db) on the shared volume —
isolated from the scanner's churny ORM writes to flow.db, no new deps, the same
proven shape as engines/structure/oplog.py.

Aggregation is dedup-by-contract: estimated_premium is vol*price*100 on the
*cumulative* day volume, and one contract can be re-flagged several times across
the session, so summing every alert row double-counts. Per cutoff we keep each
contract's LATEST row at or before the cutoff (its cumulative reading there) and
sum those — the true cumulative call/put skew as of that wall-clock.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from .. import config as econf

# Intraday detection-time cutoffs (ET) + the end-of-session reading. Because the
# Massive feed is T-15 delayed, a cutoff is "what the scanner had flagged by this
# wall-clock" — which is exactly the honest predictive test on a delayed feed.
# "eod" extends to 16:20 ET to catch the delayed prints of the closing auction.
CUTOFFS: list[tuple[str, time]] = [
    ("10:30", time(10, 30)),
    ("12:00", time(12, 0)),
    ("14:00", time(14, 0)),
    ("eod",   time(16, 20)),
]

# Directional deadband on premium skew: inside this, the day is "flat" (no call
# or put lean worth testing), so it isn't scored as a wrong/right directional call.
_FLAT_BAND = 0.03

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_signals (
    id            TEXT PRIMARY KEY,   -- "{run_date}:{ticker}:{cutoff}"
    run_date      TEXT NOT NULL,      -- ET trading date YYYY-MM-DD
    ticker        TEXT NOT NULL,
    cutoff        TEXT NOT NULL,      -- "10:30" | "12:00" | "14:00" | "eod"
    cutoff_ts     TEXT NOT NULL,      -- ISO8601 UTC instant of the cutoff
    captured_at   TEXT NOT NULL,      -- when this row was written
    n_alerts            INTEGER NOT NULL,  -- distinct contracts at/under the cutoff
    call_premium        REAL NOT NULL,
    put_premium         REAL NOT NULL,
    call_volume         INTEGER NOT NULL,
    put_volume          INTEGER NOT NULL,
    net_premium_skew    REAL,         -- (call-put)/(call+put) premium, -1..1
    net_volume_skew     REAL,         -- same for volume
    score_weighted_skew REAL,         -- premium*score weighted directional lean
    notable_premium     REAL NOT NULL,  -- premium from is_notable contracts
    notable_contracts   INTEGER NOT NULL,
    zerodte_share       REAL,         -- premium fraction from 0DTE contracts
    implied_dir         TEXT NOT NULL -- "up" | "down" | "flat"
);
CREATE INDEX IF NOT EXISTS ix_flow_signals_run_date ON flow_signals (run_date);

CREATE TABLE IF NOT EXISTS flow_outcomes (
    signal_id         TEXT NOT NULL,
    horizon           TEXT NOT NULL,  -- "to_close" | "open_close" | "next_day"
    labeled_at        TEXT NOT NULL,
    ref_price         REAL NOT NULL,  -- ETF price at the signal's reference time
    exit_price        REAL NOT NULL,  -- ETF price at the horizon end
    raw_return        REAL NOT NULL,  -- (exit/ref - 1) * 100
    signed_return     REAL NOT NULL,  -- raw_return * implied-direction sign
    high_excursion    REAL,           -- max favorable %, ref -> horizon
    low_excursion     REAL,           -- max adverse %
    direction_correct INTEGER NOT NULL,  -- 1 if signed_return > 0
    PRIMARY KEY (signal_id, horizon),
    FOREIGN KEY (signal_id) REFERENCES flow_signals (id)
);
"""


# ── Connection ──────────────────────────────────────────────────────────────────

def default_db_path() -> str:
    return econf.db_path("flow_learning.db")


@contextmanager
def _db(path: str | None = None):
    """Open the learning DB, ensure schema, commit on clean exit, always close.

    sqlite3's `with conn` commits but does NOT close — on Windows that leaves the
    file locked — so we close explicitly."""
    p = path or default_db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Time helpers ─────────────────────────────────────────────────────────────────

def _naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC (SQLAlchemy stores sweep_alerts naive UTC)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _et_to_naive_utc(d: date, t: time) -> datetime:
    """An ET wall-clock on date d, as a naive-UTC instant for comparison."""
    return datetime.combine(d, t, tzinfo=econf.ET).astimezone(timezone.utc).replace(tzinfo=None)


def _today_et_date() -> str:
    return datetime.now(econf.ET).date().isoformat()


# ── Aggregation (pure: testable without a DB) ────────────────────────────────────

def _contract_key(a) -> str:
    occ = getattr(a, "option_ticker", None)
    if occ:
        return occ
    return f"{getattr(a, 'strike', '')}:{getattr(a, 'expiry', '')}:{getattr(a, 'option_type', '')}"


def _aggregate(alerts: list, cutoff_ts: datetime, run_date: str) -> dict | None:
    """Cumulative directional flow over the contracts flagged at/under cutoff_ts.

    Dedups by contract, keeping each one's latest row at or before the cutoff
    (its cumulative premium/volume there). `alerts` is any iterable of objects
    carrying detected_at, option_type, estimated_premium, volume, expiry, plus
    whatever signals.score_alert / is_notable read."""
    from .signals import is_notable, score_alert

    latest: dict[str, object] = {}
    for a in alerts:
        dt = _naive_utc(a.detected_at)
        if dt > cutoff_ts:
            continue
        key = _contract_key(a)
        prev = latest.get(key)
        if prev is None or dt >= _naive_utc(prev.detected_at):
            latest[key] = a

    kept = list(latest.values())
    if not kept:
        return None

    call_prem = put_prem = 0.0
    call_vol = put_vol = 0
    notable_prem = 0.0
    notable_n = 0
    zerodte_prem = 0.0
    sw_num = sw_den = 0.0  # score-weighted directional lean

    for a in kept:
        prem = float(getattr(a, "estimated_premium", 0) or 0)
        vol = int(getattr(a, "volume", 0) or 0)
        is_call = getattr(a, "option_type", "") == "call"
        sign = 1.0 if is_call else -1.0
        if is_call:
            call_prem += prem
            call_vol += vol
        else:
            put_prem += prem
            put_vol += vol
        if getattr(a, "expiry", None) == run_date:
            zerodte_prem += prem
        try:
            if is_notable(a):
                notable_prem += prem
                notable_n += 1
        except Exception:
            pass
        try:
            sc = float(score_alert(a))
        except Exception:
            sc = 0.0
        sw_num += sc * sign * prem
        sw_den += sc * prem

    tot_prem = call_prem + put_prem
    tot_vol = call_vol + put_vol
    prem_skew = (call_prem - put_prem) / tot_prem if tot_prem > 0 else None
    vol_skew = (call_vol - put_vol) / tot_vol if tot_vol > 0 else None
    sw_skew = (sw_num / sw_den) if sw_den else None

    if prem_skew is None:
        implied = "flat"
    elif prem_skew > _FLAT_BAND:
        implied = "up"
    elif prem_skew < -_FLAT_BAND:
        implied = "down"
    else:
        implied = "flat"

    return {
        "n_alerts": len(kept),
        "call_premium": round(call_prem, 2),
        "put_premium": round(put_prem, 2),
        "call_volume": call_vol,
        "put_volume": put_vol,
        "net_premium_skew": None if prem_skew is None else round(prem_skew, 4),
        "net_volume_skew": None if vol_skew is None else round(vol_skew, 4),
        "score_weighted_skew": None if sw_skew is None else round(sw_skew, 4),
        "notable_premium": round(notable_prem, 2),
        "notable_contracts": notable_n,
        "zerodte_share": round(zerodte_prem / tot_prem, 4) if tot_prem > 0 else None,
        "implied_dir": implied,
    }


# ── Phase A: capture ─────────────────────────────────────────────────────────────

_SIGNAL_COLS = (
    "id", "run_date", "ticker", "cutoff", "cutoff_ts", "captured_at",
    "n_alerts", "call_premium", "put_premium", "call_volume", "put_volume",
    "net_premium_skew", "net_volume_skew", "score_weighted_skew",
    "notable_premium", "notable_contracts", "zerodte_share", "implied_dir",
)


def _signal_row(run_date: str, ticker: str, cutoff: str, cutoff_ts: datetime,
                agg: dict) -> dict:
    return {
        "id": f"{run_date}:{ticker}:{cutoff}",
        "run_date": run_date,
        "ticker": ticker,
        "cutoff": cutoff,
        "cutoff_ts": cutoff_ts.replace(tzinfo=timezone.utc).isoformat(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **agg,
    }


def _upsert_signals(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = (f"INSERT OR REPLACE INTO flow_signals ({', '.join(_SIGNAL_COLS)}) "
           f"VALUES ({', '.join('?' for _ in _SIGNAL_COLS)})")
    conn.executemany(sql, [tuple(r[c] for c in _SIGNAL_COLS) for r in rows])
    return len(rows)


def record_signals(run_date: str | None = None, db_path: str | None = None) -> int:
    """Capture per-ETF flow signals at every cutoff for one trading day.

    Reads the day's sweep_alerts from flow.db (via the engine's ORM Session) and
    writes one flow_signals row per (ticker, cutoff). Idempotent per id, so a
    re-run overwrites the day's rows rather than duplicating. Returns rows written."""
    rd = run_date or _today_et_date()
    d = date.fromisoformat(rd)
    day_start = _et_to_naive_utc(d, time(0, 0))
    day_end = _et_to_naive_utc(d + timedelta(days=1), time(0, 0))
    points = [(label, _et_to_naive_utc(d, t)) for label, t in CUTOFFS]

    from .db import Session, SweepAlert

    rows: list[dict] = []
    sess = Session()
    try:
        for ticker in econf.TICKERS:
            alerts = (sess.query(SweepAlert)
                      .filter(SweepAlert.ticker == ticker)
                      .filter(SweepAlert.detected_at >= day_start)
                      .filter(SweepAlert.detected_at < day_end)
                      .order_by(SweepAlert.detected_at.asc())
                      .all())
            if not alerts:
                continue
            for label, ts in points:
                agg = _aggregate(alerts, ts, rd)
                if agg is None:
                    continue
                rows.append(_signal_row(rd, ticker, label, ts, agg))
    finally:
        sess.close()

    with _db(db_path) as conn:
        return _upsert_signals(conn, rows)


# ── Health probe ─────────────────────────────────────────────────────────────────

def counts(db_path: str | None = None) -> tuple[int, int]:
    """(signals, outcomes) row counts — a cheap health probe for /healthz."""
    with _db(db_path) as conn:
        n_sig = conn.execute("SELECT COUNT(*) FROM flow_signals").fetchone()[0]
        n_out = conn.execute("SELECT COUNT(*) FROM flow_outcomes").fetchone()[0]
    return int(n_sig), int(n_out)
