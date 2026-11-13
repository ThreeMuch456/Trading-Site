"""Opportunity log + outcome labeling — the data foundation for self-calibration.

This closes the screener's feedback loop. The live screen is *open-loop*: it emits
scores but never learns whether they predicted anything. Here we:

  Phase 1 (log)   — every screen run, append each detected zone/flag to an
                    append-only SQLite log with its raw features and score (the X).
  Phase 2 (label) — once an opportunity's evaluation horizon has elapsed, revisit
                    it, pull the bars *since* detection, and compute objective
                    outcomes (the y): forward return, MFE/MAE in ATRs, and a
                    win/loss per a fixed rule.

Nothing here changes the screener's behavior — it only records and measures, so a
labeled history accumulates in wall-clock time. The thesis being tested, per
direction:
  support  (LVN below price)  — price breaks DOWN through the air pocket → favorable = down
  resistance (LVN above price)— price breaks UP through the air pocket   → favorable = up
  bull flag                   — breakout continues UP                    → favorable = up
  bear flag                   — breakdown continues DOWN                 → favorable = down

Storage is stdlib sqlite3 (no new deps). Outcomes are keyed (opp_id, horizon_days)
so several horizons can be evaluated independently.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .data import DataFeed
from .flow import pocket_share as flow_pocket_share
from .live import read_daily_summary
from .schedule import market_tz

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    id             TEXT PRIMARY KEY,   -- deterministic: "{run_date}:{ticker}:{type}"
    detected_at    TEXT NOT NULL,      -- ISO8601 UTC, the screen timestamp
    run_date       TEXT NOT NULL,      -- market-tz date (YYYY-MM-DD)
    ticker         TEXT NOT NULL,
    type           TEXT NOT NULL,      -- 'support' | 'resistance' | 'flag'
    direction      TEXT NOT NULL,      -- 'up' | 'down' (thesis-favorable direction)
    entry_price    REAL NOT NULL,      -- price at detection (reference for outcomes)
    atr            REAL NOT NULL,      -- ATR at detection (normalizes MFE/MAE)
    target_price   REAL,               -- LVN price (zones); NULL for flags
    next_hvn_price REAL,               -- far wall of the air pocket (zones); NULL for flags
    score          REAL NOT NULL,      -- the opportunity's own score
    combined_score REAL NOT NULL,      -- the Result-level combined score
    features       TEXT NOT NULL       -- JSON blob of all raw features
);
CREATE INDEX IF NOT EXISTS ix_opp_detected ON opportunities (detected_at);

CREATE TABLE IF NOT EXISTS outcomes (
    opp_id         TEXT NOT NULL,
    horizon_days   INTEGER NOT NULL,
    labeled_at     TEXT NOT NULL,      -- ISO8601 UTC
    n_bars         INTEGER NOT NULL,   -- forward bars used
    fwd_return     REAL NOT NULL,      -- raw % return entry -> horizon close
    signed_return  REAL NOT NULL,      -- fwd_return * directional sign (favorable = +)
    mfe_atr        REAL NOT NULL,      -- max favorable excursion, in ATRs
    mae_atr        REAL NOT NULL,      -- max adverse excursion, in ATRs
    reached_target INTEGER,            -- 1 if price touched target_price (zones); NULL flags
    bars_to_target INTEGER,            -- forward bars until the first touch (NULL if never/flag)
    days_to_target REAL,               -- calendar days from detection to the first touch
    reached_next_hvn INTEGER,          -- 1 if price traversed the pocket to the far wall
    days_to_next_hvn REAL,             -- calendar days to that far-wall touch
    bench_return   REAL,               -- benchmark % return over the same window (NULL if off)
    excess_return  REAL,               -- (fwd_return - bench_return) * sign: edge net of drift
    win            INTEGER NOT NULL,   -- 1 if the setup "worked" per the rule
    PRIMARY KEY (opp_id, horizon_days),
    FOREIGN KEY (opp_id) REFERENCES opportunities (id)
);

-- Per-ticker, per-session call share of options premium (direction-free), the
-- history behind the flow_*_vs_base features. Written for EVERY screened ticker
-- with flow data, not only those with a detection; sessions logged before this
-- table existed are back-derived from opportunities' flow_aligned_share.
CREATE TABLE IF NOT EXISTS flow_daily (
    ticker             TEXT NOT NULL,
    run_date           TEXT NOT NULL,
    call_share         REAL,           -- snapshot: call $ / (call $ + put $)
    scanner_call_share REAL,           -- Options Flow Scanner rollup, same ratio
    PRIMARY KEY (ticker, run_date)
);
"""

# Columns added after the tables first shipped; CREATE TABLE IF NOT EXISTS won't
# add them to an existing DB, so patch with ALTER TABLE on open. Old rows keep
# NULL (they were labeled before these metrics existed).
_MIGRATIONS = {
    "opportunities": {
        "next_hvn_price": "REAL",
    },
    "outcomes": {
        "bars_to_target": "INTEGER",
        "days_to_target": "REAL",
        "reached_next_hvn": "INTEGER",
        "days_to_next_hvn": "REAL",
        "bench_return": "REAL",
        "excess_return": "REAL",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


_BOOL_STRINGS = {"True": True, "False": False}


def _repair_stringified_bools(conn: sqlite3.Connection) -> int:
    """Rewrite feature values serialized as "True"/"False" strings to booleans.

    Before _json_default existed, a numpy bool reaching json.dumps fell through
    to `str` and was stored as the string "False" (see that function).

    This mutates rows in an otherwise append-only log, which is worth being
    explicit about: it repairs a *serialization* defect, not a measurement.
    "False" -> false is lossless and unambiguous, and no legitimate feature
    carries "True"/"False" as a meaningful string (`kind` is bull/bear,
    `earnings_date` is an ISO date), so the coercion cannot catch a real value.

    Deliberately NOT gated on a run-once flag. A gate would save one scan per
    open, but it can only ever lock in a failure: if the repair completed having
    matched nothing — a format the prefilter missed, a partially-written row —
    the flag would suppress every retry and the breakage would be permanent and
    silent, which is the exact failure mode this function exists to undo. The
    scan is self-healing instead: once clean it matches zero rows and does no
    JSON work.

    Cost is O(rows) per DB open, on a TEXT column that can't be indexed for a
    substring match. At the current ~1.8k opportunities that is well under a
    millisecond. It grows with the log (~20k rows/year), so if this ever shows
    up in request latency, the fix is to bound the scan by run_date rather than
    to reintroduce a flag.

    Never raises — a log that cannot be repaired is still a log worth writing to.
    """
    fixed = 0
    try:
        # Cheap prefilter so a clean DB costs one indexless scan and no JSON work.
        candidates = conn.execute(
            "SELECT id, features FROM opportunities "
            "WHERE features LIKE '%: \"True\"%' OR features LIKE '%: \"False\"%'"
        ).fetchall()

        for row in candidates:
            try:
                feats = json.loads(row["features"])
            except (ValueError, TypeError):
                continue
            if not isinstance(feats, dict):
                continue
            touched = {k: _BOOL_STRINGS[v] for k, v in feats.items()
                       if isinstance(v, str) and v in _BOOL_STRINGS}
            if not touched:
                continue
            feats.update(touched)
            conn.execute("UPDATE opportunities SET features = ? WHERE id = ?",
                         (json.dumps(feats, default=_json_default), row["id"]))
            fixed += 1
    except sqlite3.Error as exc:
        print(f"[oplog] stringified-bool repair skipped ({exc}); log still usable")
        return 0

    if fixed:
        print(f"[oplog] repaired stringified booleans in {fixed} opportunity rows")
    return fixed


# ── Connection ──────────────────────────────────────────────────────────────────

def db_path(cfg: Config) -> Path:
    explicit = cfg.get("learn.db_path")
    if explicit:
        return Path(explicit)
    return Path(cfg.get("data.cache_dir", "data")) / "opportunities.db"


def _dataset_version(cfg: Config) -> str:
    return str(cfg.get("learn.dataset_version", "1"))


def _ensure_dataset(path: Path, version: str) -> None:
    """Archive the log and start fresh when learn.dataset_version changes.

    The labeled history is only meaningful if every row was produced by the same
    methodology — when the scoring changes materially (e.g. the GEX bonus), bump
    the version in config and the old log is renamed to *.bak (never deleted) at
    the next open, wherever the DB lives (locally or on the Railway volume)."""
    if not path.exists():
        return
    stored: str | None
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'dataset_version'"
            ).fetchone()
            stored = row[0] if row else "1"      # pre-versioning DBs count as "1"
        except sqlite3.OperationalError:          # no meta table -> legacy DB
            stored = "1"
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - unreadable file -> archive it too
        stored = None

    if stored != version:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        archive = path.with_name(f"{path.stem}_v{stored or 'unknown'}_{stamp}{path.suffix}.bak")
        path.rename(archive)
        print(f"[oplog] dataset version changed ({stored!r} -> {version!r}); "
              f"archived old log to {archive.name} and starting fresh")


@contextmanager
def _db(cfg: Config):
    """Open the log DB, ensure schema, commit on clean exit, and always close.

    Note: sqlite3's own `with conn` context commits but does NOT close the
    connection — on Windows that leaves the file locked. We close explicitly."""
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    version = _dataset_version(cfg)
    _ensure_dataset(path, version)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    _repair_stringified_bools(conn)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('dataset_version', ?)",
                 (version,))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Phase 1: log opportunities ──────────────────────────────────────────────────

def _json_default(o):
    """Fallback encoder for feature values json can't serialize natively.

    This used to be plain `str`, which was safe (a weird value never killed a
    run's logging) but silently lossy: `crosses_flip` is derived from a numpy
    scalar, so the comparison yields np.bool_ — not a bool subclass, therefore
    not serializable — and 427 rows went into the log as the string "False"
    instead of the boolean. Nothing errored; the feature was simply unusable for
    any numeric analysis, and would have mis-bucketed rather than failed if a
    json_extract query had reached it.

    The numeric siblings were unaffected only by accident: np.float64 IS a float
    subclass, so it never reaches this function at all. Coercing the numpy
    scalar types here closes the whole class of bug rather than the one instance.
    """
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)          # genuinely exotic — keep the old lossy-but-safe behavior


def _rows_for_result(r, detected_at: str, run_date: str, live: dict | None = None) -> list[dict]:
    """Emit one log row per detected component (support / resistance / flag) on a Result.

    `live`: that ticker's peak-of-day summary from the intraday monitor (see
    src/live.py), or None when the monitor was off / this ticker wasn't in that
    day's top-N. Folded into features as the live_* fields."""
    rows: list[dict] = []
    price = float(r.profile.current_price)
    atr   = float(r.profile.atr)
    gex   = getattr(r, "gex", None)   # None when GEX disabled/unavailable
    neg_gamma = (gex.net_gex < 0) if gex is not None else None

    # Option-market context from the chain snapshot (all None on the free
    # fallbacks / pre-upgrade Results — getattr throughout). These are the X
    # the tuner will eventually test, so log generously.
    adv      = getattr(r, "adv_dollar", None)
    net_gex  = gex.net_gex if gex is not None else None
    gex_adv_ratio = (net_gex / adv) if (net_gex is not None and adv) else None
    atm_iv   = getattr(gex, "atm_iv", None) if gex is not None else None
    # Option-implied ±move over 5 calendar days, in % — distances divided by
    # this are in "market-expected move" units, comparable across tickers.
    implied_move_5d_pct = (atm_iv * (5.0 / 365.0) ** 0.5 * 100.0) if atm_iv else None
    flip     = getattr(gex, "flip_price", None) if gex is not None else None
    flip_dist_pct = (100.0 * (flip - price) / price) if (flip and price > 0) else None
    walls    = getattr(gex, "top_gamma_strikes", None) if gex is not None else None
    call_vol = getattr(gex, "call_volume", None) if gex is not None else None
    put_vol  = getattr(gex, "put_volume", None) if gex is not None else None
    total_oi = getattr(gex, "total_oi", None) if gex is not None else None
    opt_flow_ratio = ((call_vol - put_vol) / (call_vol + put_vol)
                      if call_vol is not None and put_vol is not None
                      and (call_vol + put_vol) > 0 else None)
    vol_oi_ratio = ((call_vol + put_vol) / total_oi
                    if call_vol is not None and put_vol is not None
                    and total_oi else None)

    # Options flow (src/flow.py): logged only, never scored. None throughout
    # when the snapshot had no fresh premium / the scanner is off or silent.
    flow = getattr(r, "flow", None)
    scan = getattr(r, "scanner_flow", None)
    flow_premium = flow.premium if flow is not None else None
    scan_premium = ((scan["call_premium"] + scan["put_premium"])
                    if scan is not None else None)

    def _side_share(call_p, put_p, direction: str) -> float | None:
        if call_p is None or put_p is None or (call_p + put_p) <= 0:
            return None
        return round((call_p if direction == "up" else put_p) / (call_p + put_p), 4)

    def flow_features(direction: str, target, next_hvn) -> dict:
        edge = next_hvn or target            # far wall of the pocket, else the LVN
        return {
            "flow_premium": flow_premium,
            "flow_premium_adv_ratio": (round(flow_premium / adv, 6)
                                       if flow_premium is not None and adv else None),
            "flow_aligned_share": flow.aligned_share(direction) if flow else None,
            "flow_aligned_short_share": flow.aligned_short_share(direction) if flow else None,
            "flow_aligned_vol_oi": flow.aligned_vol_oi(direction) if flow else None,
            "flow_pocket_share": flow.pocket_share(direction, price, edge) if flow else None,
            "flow_stale_share": flow.stale_share if flow else None,
            "flow_snapshot_at": getattr(flow, "snapshot_at", None) if flow else None,
            "iv_skew": flow.iv_skew if flow else None,
            "scanner_premium": scan_premium,
            "scanner_contracts": scan["contracts"] if scan else None,
            "scanner_aligned_share": (_side_share(scan["call_premium"], scan["put_premium"],
                                                  direction) if scan else None),
            "scanner_pocket_share": (flow_pocket_share(scan["strikes"], direction, price, edge)
                                     if scan else None),
            "scanner_notable_contracts": scan["notable_contracts"] if scan else None,
            "scanner_notable_aligned_share": (
                _side_share(scan["notable_call_premium"], scan["notable_put_premium"], direction)
                if scan else None),
        }

    def _wall_dist_pct(level) -> float | None:
        """Distance from a price level to the nearest gamma wall, % of spot."""
        if not walls or not level or price <= 0:
            return None
        return round(100.0 * min(abs(float(w) - float(level)) for w in walls) / price, 4)

    def base(kind: str, direction: str, target, score: float, features: dict,
             next_hvn=None) -> dict:
        features = {
            **features,
            "neg_gamma": neg_gamma,
            # Result-level context (getattr: absent on pre-upgrade Results).
            "rvol": getattr(r, "rvol", None),
            "earnings_date": getattr(r, "next_earnings", None),
            "days_to_earnings": getattr(r, "days_to_earnings", None),
            # GEX magnitude + scoring amps (are the knobs calibrated?)
            "net_gex": net_gex,
            "gex_adv_ratio": gex_adv_ratio,
            "gex_amp": getattr(r, "gex_amp", None),
            "rvol_amp": getattr(r, "rvol_amp", None),
            # Option-implied expectations + dealer structure
            "atm_iv": atm_iv,
            "implied_move_5d_pct": implied_move_5d_pct,
            "flip_dist_pct": flip_dist_pct,
            "opt_flow_ratio": opt_flow_ratio,
            "vol_oi_ratio": vol_oi_ratio,
            # Live intraday confirmation (src/live.py), peak-of-day as of the
            # screen. None on every field when the monitor was off or this
            # ticker wasn't being watched that day.
            "live_ever_activated": (live or {}).get("ever_activated"),
            "live_peak_aligned_rvol": (live or {}).get("peak_aligned_rvol"),
            "live_ever_flip_pressure": (live or {}).get("ever_flip_pressure"),
            "live_peak_flip_share": (live or {}).get("peak_flip_share"),
            **flow_features(direction, target, next_hvn),
        }
        return {
            "id": f"{run_date}:{r.ticker}:{kind}",
            "detected_at": detected_at,
            "run_date": run_date,
            "ticker": r.ticker,
            "type": kind,
            "direction": direction,
            "entry_price": price,
            "atr": atr,
            "target_price": None if target is None else float(target),
            "next_hvn_price": None if next_hvn is None else float(next_hvn),
            "score": float(score),
            "combined_score": float(r.combined_score),
            # _json_default: coerce numpy scalars to their Python equivalents,
            # and only fall back to a repr for genuinely exotic values.
            "features": json.dumps(features, default=_json_default),
        }

    def zone_features(z) -> dict:
        next_hvn = getattr(z, "next_hvn_price", None)
        pocket_edge = next_hvn or z.lvn_price   # far wall, else the LVN itself
        return {
            "distance_pct": z.distance_pct, "distance_atr": z.distance_atr,
            "depth": z.depth, "width_pct": z.width_pct, "clearance": bool(z.clearance),
            # Far wall of the air pocket (getattr: absent on pre-upgrade pickles).
            "next_hvn_price": next_hvn,
            "next_hvn_distance_pct": getattr(z, "next_hvn_distance_pct", None),
            # Distance in option-implied-move units: ~1 means the market prices
            # a typical 5-day move that just reaches the LVN.
            "dist_vs_im5": (round(z.distance_pct / implied_move_5d_pct, 4)
                            if implied_move_5d_pct else None),
            # Does the pocket cross the zero-gamma flip (amplification regime
            # would flip to dampening mid-traversal)?
            # bool(): `flip` is a numpy scalar, so the chained comparison returns
            # np.bool_, which is NOT a bool subclass and so cannot be serialized —
            # it used to reach _json_default and land in the log as the STRING
            # "False". (np.float64 IS a float subclass, which is why the numeric
            # siblings were unaffected and the breakage stayed invisible.)
            "crosses_flip": (bool(min(price, pocket_edge) < flip < max(price, pocket_edge))
                             if flip and price > 0 else None),
            # Do the options gamma walls agree with the volume-profile levels?
            "lvn_wall_dist_pct": _wall_dist_pct(z.lvn_price),
            "hvn_wall_dist_pct": _wall_dist_pct(next_hvn),
        }

    if r.support is not None:
        z = r.support
        rows.append(base("support", "down", z.lvn_price, z.score, zone_features(z),
                         next_hvn=getattr(z, "next_hvn_price", None)))
    if r.resistance is not None:
        z = r.resistance
        rows.append(base("resistance", "up", z.lvn_price, z.score, zone_features(z),
                         next_hvn=getattr(z, "next_hvn_price", None)))
    if r.flag is not None:
        f = r.flag
        direction = "up" if f.kind == "bull" else "down"
        rows.append(base("flag", direction, None, f.score, {
            "kind": f.kind, "pole_pct": f.pole_pct,
            "flag_retrace_pct": f.flag_retrace_pct, "channel_pct": f.channel_pct,
            "volume_declining": bool(f.volume_declining),
        }))
    return rows


# ── Flow baselines (per-ticker normal call/put mix) ─────────────────────────────
#
# A day's aligned share is mostly the stock's STRUCTURAL call/put mix, not the
# day's signal: most names trade more calls than puts, so put-aligned (support)
# setups sat in the bottom tercile of flow_aligned_share almost by construction
# (39 of 69 labeled support rows, 2026-10-01..09). The vs_base features subtract
# the ticker's own trailing call share, so "unusual for this name" is what gets
# tested. Strictly prior sessions — never today's — so there's no lookahead.

_BASELINE_KEYS = ("flow_call_share", "flow_call_share_base", "flow_base_sessions",
                  "flow_aligned_share_vs_base", "scanner_call_share",
                  "scanner_base_sessions", "scanner_aligned_share_vs_base")


def _call_share_from_aligned(aligned, direction: str) -> float | None:
    """Undo the direction: aligned share is calls for 'up', puts for 'down'."""
    if aligned is None:
        return None
    try:
        a = float(aligned)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(a):
        return None
    return a if direction == "up" else 1.0 - a


def _backfill_flow_daily(conn: sqlite3.Connection) -> None:
    """Derive flow_daily rows for sessions logged before the table existed.
    INSERT OR IGNORE: a session already recorded directly (which also covers
    tickers with no detection) is never overwritten by the derived value. One
    ticker-day can carry several rows (support/resistance/flag) over the same
    chain, so they agree on the call share — MIN() just picks one."""
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO flow_daily (ticker, run_date, call_share, scanner_call_share)
            SELECT ticker, run_date,
                   MIN(CASE WHEN json_extract(features, '$.flow_aligned_share') IS NULL THEN NULL
                            WHEN direction = 'up' THEN json_extract(features, '$.flow_aligned_share')
                            ELSE 1.0 - json_extract(features, '$.flow_aligned_share') END),
                   MIN(CASE WHEN json_extract(features, '$.scanner_aligned_share') IS NULL THEN NULL
                            WHEN direction = 'up' THEN json_extract(features, '$.scanner_aligned_share')
                            ELSE 1.0 - json_extract(features, '$.scanner_aligned_share') END)
            FROM opportunities
            WHERE json_extract(features, '$.flow_aligned_share') IS NOT NULL
               OR json_extract(features, '$.scanner_aligned_share') IS NOT NULL
            GROUP BY ticker, run_date
            """
        )
    except sqlite3.OperationalError as exc:      # no json_extract on ancient SQLite
        print(f"[oplog] flow_daily backfill skipped ({exc})")


def _record_flow_daily(conn: sqlite3.Connection, results: list, run_date: str) -> None:
    """Today's call share for every screened ticker that has flow data."""
    rows = []
    for r in results:
        flow = getattr(r, "flow", None)
        scan = getattr(r, "scanner_flow", None)
        cs = (round(flow.call_premium / flow.premium, 4)
              if flow is not None and flow.premium > 0 else None)
        ss = None
        if scan is not None:
            tot = (scan.get("call_premium") or 0.0) + (scan.get("put_premium") or 0.0)
            ss = round(scan["call_premium"] / tot, 4) if tot > 0 else None
        if cs is not None or ss is not None:
            rows.append((r.ticker, run_date, cs, ss))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO flow_daily (ticker, run_date, call_share, scanner_call_share) "
            "VALUES (?, ?, ?, ?)", rows)


def _flow_baseline_features(conn: sqlite3.Connection, ticker: str, run_date: str,
                            direction: str, feats: dict, n_sessions: int,
                            min_sessions: int) -> dict:
    """The vs_base features for one opportunity row, from its own logged flow
    features plus that ticker's prior sessions in flow_daily."""
    prior = conn.execute(
        "SELECT call_share, scanner_call_share FROM flow_daily "
        "WHERE ticker = ? AND run_date < ? ORDER BY run_date DESC LIMIT ?",
        (ticker, run_date, int(n_sessions))).fetchall()
    sign = 1.0 if direction == "up" else -1.0
    out: dict = {k: None for k in _BASELINE_KEYS}

    def one(today, col):
        hist = [float(p[col]) for p in prior if p[col] is not None]
        base = float(np.mean(hist)) if len(hist) >= min_sessions else None
        vs = round(sign * (today - base), 4) if today is not None and base is not None else None
        return (None if base is None else round(base, 4)), len(hist), vs

    cs = _call_share_from_aligned(feats.get("flow_aligned_share"), direction)
    out["flow_call_share"] = None if cs is None else round(cs, 4)
    (out["flow_call_share_base"], out["flow_base_sessions"],
     out["flow_aligned_share_vs_base"]) = one(cs, "call_share")
    ss = _call_share_from_aligned(feats.get("scanner_aligned_share"), direction)
    out["scanner_call_share"] = None if ss is None else round(ss, 4)
    _, out["scanner_base_sessions"], out["scanner_aligned_share_vs_base"] = \
        one(ss, "scanner_call_share")
    return out


def _backfill_flow_baselines(conn: sqlite3.Connection, n_sessions: int,
                             min_sessions: int) -> int:
    """Add the vs_base features to rows logged before they existed.

    Like _repair_stringified_bools this writes into the append-only log, and it
    is safe for a similar reason: it only adds DERIVED fields, computed from
    data already logged on strictly earlier sessions — no re-measurement, no
    lookahead — so a backfilled row holds what it would have held had the
    feature shipped on its run_date (except that flow_daily back then only
    knew tickers that had a detection). Existing keys are never overwritten.
    Every scanned row gets the keys (possibly null) once, so the prefilter
    makes later runs a no-op."""
    rows = conn.execute(
        "SELECT id, ticker, run_date, direction, features FROM opportunities "
        "WHERE features NOT LIKE '%\"flow_aligned_share_vs_base\"%'").fetchall()
    n = 0
    for row in rows:
        try:
            feats = json.loads(row["features"])
        except (ValueError, TypeError):
            continue
        if not isinstance(feats, dict):
            continue
        add = _flow_baseline_features(conn, row["ticker"], row["run_date"], row["direction"],
                                      feats, n_sessions, min_sessions)
        feats.update({k: v for k, v in add.items() if k not in feats})
        conn.execute("UPDATE opportunities SET features = ? WHERE id = ?",
                     (json.dumps(feats, default=_json_default), row["id"]))
        n += 1
    if n:
        print(f"[oplog] backfilled flow baseline features on {n} opportunity rows")
    return n


# ── Partial-run guard ───────────────────────────────────────────────────────────

def _has_detection(r) -> bool:
    return any(getattr(r, k, None) is not None for k in ("support", "resistance", "flag"))


def run_coverage_shortfall(cfg: Config, results: list, n_universe: int | None = None) -> str | None:
    """Why this screen looks partial (a reason string), or None when it looks whole.

    On 2026-10-08 a run logged 9 tickers / 12 opportunities against a normal
    ~70 / ~90, and finished 40s early — a provider hiccup logged as a full
    session. With only a handful of detection dates in the sample, one truncated
    date carries a full date's weight in every date-clustered error bar. Checks:

      - screened / universe below learn.min_run_coverage (bars failed outright);
      - tickers WITH a detection below min_run_coverage × the median of the last
        learn.coverage_lookback_runs logged sessions — catches a shrunken
        universe or empty profiles, which the first check can't see. Needs 3
        prior sessions, and compares like with like (detected tickers both sides).
    """
    frac = float(cfg.get("learn.min_run_coverage", 0.5))
    if frac <= 0:
        return None
    n_screened = len(results)
    if n_universe and n_screened < frac * n_universe:
        return (f"only {n_screened}/{n_universe} universe tickers screened "
                f"(< {frac:.0%})")
    n_detected = sum(1 for r in results if _has_detection(r))
    lookback = int(cfg.get("learn.coverage_lookback_runs", 10))
    with _db(cfg) as conn:
        hist = [row[0] for row in conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM opportunities GROUP BY run_date "
            "ORDER BY run_date DESC LIMIT ?", (lookback,)).fetchall()]
    if len(hist) >= 3:
        ref = float(np.median(hist))
        if n_detected < frac * ref:
            return (f"only {n_detected} tickers with a detection vs a median of "
                    f"{ref:.0f} over the last {len(hist)} sessions (< {frac:.0%})")
    return None


def log_opportunities(cfg: Config, results: list, detected_at: datetime | None = None) -> int:
    """Append every detected zone/flag from a screen to the log. Idempotent per
    (run_date, ticker, type) — re-running the same day won't duplicate. Returns
    the number of new rows inserted."""
    if not results:
        return 0

    dt = detected_at or datetime.now(timezone.utc)
    detected_iso = dt.astimezone(timezone.utc).isoformat()
    run_date = dt.astimezone(market_tz(cfg)).date().isoformat()

    # Best-effort: today's peak-of-day live confirmation, keyed by ticker. {}
    # when the monitor is off or hasn't observed anything for this run_date yet.
    try:
        live_summary = read_daily_summary(cfg, run_date)
    except Exception:  # noqa: BLE001 - never let this block logging
        live_summary = {}
    # Make the live hand-off visible in the deploy logs: how many of this run's
    # tickers actually had a peak-of-day summary to fold in. 0 here (with the
    # monitor streaming) means the summary file's run_date didn't match — see
    # live.daily_summary_status(), surfaced at /healthz.
    n_live = sum(1 for r in results if live_summary.get(getattr(r, "ticker", None)))
    print(f"[oplog] live confirmation available for {n_live}/{len(results)} "
          f"tickers this run (run_date={run_date})")

    # One bad result must not cost the whole run's history: feature building is
    # isolated per result, and rows whose NOT NULL numerics aren't finite are
    # dropped individually (SQLite stores NaN as NULL, which INSERT OR IGNORE
    # would silently skip — make the drop loud instead).
    rows: list[dict] = []
    for r in results:
        try:
            rows.extend(_rows_for_result(r, detected_iso, run_date, live_summary.get(r.ticker)))
        except Exception as exc:  # noqa: BLE001
            print(f"[oplog] skipping {getattr(r, 'ticker', '?')}: "
                  f"could not build log rows ({type(exc).__name__}: {exc})")

    def _finite(x) -> bool:
        try:
            return math.isfinite(float(x))
        except (TypeError, ValueError):
            return False

    bad = [row["id"] for row in rows
           if not all(_finite(row[k]) for k in ("entry_price", "atr", "score", "combined_score"))]
    if bad:
        print(f"[oplog] dropping {len(bad)} rows with non-finite fields: {', '.join(bad)}")
        rows = [row for row in rows if row["id"] not in bad]
    if not rows:
        return 0

    cols = ("id", "detected_at", "run_date", "ticker", "type", "direction",
            "entry_price", "atr", "target_price", "next_hvn_price",
            "score", "combined_score", "features")
    sql = (f"INSERT OR IGNORE INTO opportunities ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")

    n_sess = int(cfg.get("flow.baseline_sessions", 10))
    min_sess = int(cfg.get("flow.baseline_min_sessions", 3))
    with _db(cfg) as conn:
        # Flow baselines: history first (derived + this run's direct record),
        # then the vs_base features on this run's rows, then any older rows still
        # missing them. Best-effort — a baseline failure must not cost the log.
        try:
            _backfill_flow_daily(conn)
            _record_flow_daily(conn, results, run_date)
            for row in rows:
                feats = json.loads(row["features"])
                feats.update(_flow_baseline_features(conn, row["ticker"], run_date,
                                                     row["direction"], feats, n_sess, min_sess))
                row["features"] = json.dumps(feats, default=_json_default)
        except Exception as exc:  # noqa: BLE001
            print(f"[oplog] flow baselines skipped ({type(exc).__name__}: {exc})")
        before = conn.total_changes
        conn.executemany(sql, [tuple(row[c] for c in cols) for row in rows])
        inserted = conn.total_changes - before
        try:
            _backfill_flow_baselines(conn, n_sess, min_sess)
        except Exception as exc:  # noqa: BLE001
            print(f"[oplog] flow baseline backfill skipped ({type(exc).__name__}: {exc})")
    print(f"[oplog] logged {inserted} new opportunities ({len(rows)} detected this run)")
    return inserted


# ── Phase 2: label outcomes ─────────────────────────────────────────────────────

def _first_touch(forward: pd.DataFrame, highs: np.ndarray, lows: np.ndarray,
                 level: float | None, sign: int,
                 detected: pd.Timestamp | None) -> tuple[int | None, int | None, float | None]:
    """(reached, bars_to, days_to) for the first forward bar touching `level` in
    the thesis direction. All None when there's no level to test."""
    if level is None:
        return None, None, None
    touched = (highs >= level) if sign > 0 else (lows <= level)
    if not np.any(touched):
        return 0, None, None
    hit_i = int(np.argmax(touched))
    days = None
    if detected is not None:
        fidx = forward.index
        fidx = fidx.tz_localize("UTC") if fidx.tz is None else fidx.tz_convert("UTC")
        days = round(float((fidx[hit_i] - detected).total_seconds()) / 86400.0, 3)
    return 1, hit_i + 1, days


def _forward_metrics(forward: pd.DataFrame, entry: float, atr: float, sign: int,
                     barrier_atr: float, target_price: float | None,
                     next_hvn_price: float | None = None,
                     detected: pd.Timestamp | None = None) -> dict:
    """Objective outcome of one opportunity over its forward window.

    sign: +1 if the thesis-favorable direction is up, -1 if down.
    All excursions are signed so favorable is always positive, then normalized to ATRs.
    `detected` (UTC) enables days-to-touch timing. Two touch levels are labeled:
    target_price (the LVN — did price enter the air pocket?) and next_hvn_price
    (the pocket's far wall — did the move traverse the whole gap?).

    `barrier_atr`: the flag win/loss barrier in ATRs, already scaled for the
    horizon by the caller (see _barrier_atr)."""
    highs  = forward["high"].to_numpy(dtype=float)
    lows   = forward["low"].to_numpy(dtype=float)
    closes = forward["close"].to_numpy(dtype=float)

    # Per-bar favorable / adverse price travel from the entry.
    if sign > 0:                       # favorable = up
        fav = highs - entry
        adv = entry - lows
    else:                              # favorable = down
        fav = entry - lows
        adv = highs - entry

    mfe = float(np.max(fav)) if fav.size else 0.0
    mae = float(np.max(adv)) if adv.size else 0.0
    mfe_atr = mfe / atr if atr > 0 else 0.0
    mae_atr = mae / atr if atr > 0 else 0.0

    fwd_return    = (float(closes[-1]) / entry - 1.0) * 100.0 if entry > 0 else 0.0
    signed_return = fwd_return * sign

    # Breakout-before-stop (flags): did favorable cross the barrier before adverse?
    # Ties (both barriers breached inside the SAME bar) are losses, not wins:
    # intrabar we cannot know which came first, and counting them as wins put a
    # systematic upward bias on every flag win-rate on the /learning page.
    thr = barrier_atr * atr
    fav_idx = int(np.argmax(fav >= thr)) if np.any(fav >= thr) else None
    adv_idx = int(np.argmax(adv >= thr)) if np.any(adv >= thr) else None
    breakout_win = int(fav_idx is not None and (adv_idx is None or fav_idx < adv_idx))
    barrier_ambiguous = int(fav_idx is not None and adv_idx is not None and fav_idx == adv_idx)

    # Did price reach the thin zone in the predicted direction — and how fast?
    # (zones only). bars_to_target is 1-based: 1 = the very next forward bar.
    # Same question for the pocket's far wall (the next HVN beyond the LVN).
    reached, bars_to_target, days_to_target = _first_touch(
        forward, highs, lows, target_price, sign, detected)
    reached_next_hvn, _, days_to_next_hvn = _first_touch(
        forward, highs, lows, next_hvn_price, sign, detected)

    return {
        "n_bars": int(len(forward)),
        "fwd_return": round(fwd_return, 4),
        "signed_return": round(signed_return, 4),
        "mfe_atr": round(mfe_atr, 4),
        "mae_atr": round(mae_atr, 4),
        "reached_target": reached,
        "bars_to_target": bars_to_target,
        "days_to_target": days_to_target,
        "reached_next_hvn": reached_next_hvn,
        "days_to_next_hvn": days_to_next_hvn,
        "breakout_win": breakout_win,
        "barrier_ambiguous": barrier_ambiguous,
    }


def _window_return_pct(bars: pd.DataFrame, detected: pd.Timestamp,
                       horizon_days: int) -> float | None:
    """Plain % close-to-close return of `bars` over one opportunity's window.

    Used for the benchmark leg of `excess_return`. The entry reference is the last
    close at or before detection (the opportunity's own entry_price has no
    benchmark equivalent), and the exit is the last close inside the horizon —
    the same slice `_forward_metrics` uses for the stock."""
    if bars is None or bars.empty:
        return None
    idx = bars.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    prior = bars[idx <= detected]
    forward = bars[(idx > detected) & (idx <= detected + pd.Timedelta(days=horizon_days))]
    if prior.empty or forward.empty:
        return None
    entry = float(prior["close"].iloc[-1])
    if not np.isfinite(entry) or entry <= 0:
        return None
    exit_px = float(forward["close"].iloc[-1])
    if not np.isfinite(exit_px):
        return None
    return (exit_px / entry - 1.0) * 100.0


def _barrier_atr(cfg: Config, horizon_days: int) -> float:
    """The flag win/loss barrier for one horizon, in ATRs.

    ATR here is the *hourly* ATR (profile.atr_period bars of the 1h series), so a
    flat barrier is not comparable across horizons: the old `learn.atr_target: 1.0`
    put a one-14-hour-range barrier on a 5–10 *day* window, and both sides were
    breached almost immediately (observed avg MFE/MAE 2.7/3.8 ATR at 5d and
    5.2/5.8 at 10d — every sample cleared it). The race was decided in the first
    bar or two, so the label measured the next hour rather than the horizon.

    Diffusion scales with √t, so the barrier does too: coefficient × √horizon_days."""
    coef = float(cfg.get("learn.barrier_atr_per_sqrt_day", 0.9))
    return coef * math.sqrt(max(int(horizon_days), 1))


def clear_outcomes(cfg: Config, setup_type: str = "all") -> int:
    """Drop labeled outcomes for one setup type so label_due recomputes them.

    Outcomes are *derived* data — recoverable from the bars — while the
    opportunity rows are not, so a rule change (e.g. the flag barrier) doesn't
    need the dataset_version sledgehammer that archives the whole log and throws
    away a zone history the change didn't touch. Only rows whose detection is
    still inside the bar cache's reach can be relabeled; older ones simply stay
    unlabeled, which is the honest outcome — better an absent row than one
    produced by a rule that no longer exists.

    Returns the number of outcome rows dropped."""
    with _db(cfg) as conn:
        if setup_type == "all":
            cur = conn.execute("DELETE FROM outcomes")
        else:
            cur = conn.execute(
                "DELETE FROM outcomes WHERE opp_id IN "
                "(SELECT id FROM opportunities WHERE type = ?)", (setup_type,))
        return int(cur.rowcount or 0)


def _due_opportunities(conn: sqlite3.Connection, horizon_days: int, now: datetime) -> list[sqlite3.Row]:
    """Opportunities old enough to evaluate at this horizon and not yet labeled for it."""
    cutoff = (now - timedelta(days=horizon_days)).astimezone(timezone.utc).isoformat()
    return conn.execute(
        """
        SELECT o.* FROM opportunities o
        LEFT JOIN outcomes oc
          ON oc.opp_id = o.id AND oc.horizon_days = ?
        WHERE oc.opp_id IS NULL          -- not yet labeled at this horizon
          AND o.detected_at <= ?         -- horizon has elapsed
        ORDER BY o.ticker
        """,
        (horizon_days, cutoff),
    ).fetchall()


def label_due(cfg: Config, feed: DataFeed | None = None, now: datetime | None = None) -> int:
    """Label every opportunity whose horizon has elapsed but isn't yet scored.

    Reuses the daily-refreshed bar cache, so running this right after the screen
    is cheap. Returns the number of (opportunity, horizon) outcomes written."""
    if not cfg.get("learn.enabled", True):
        return 0

    horizons   = [int(h) for h in (cfg.get("learn.horizons_days") or [5, 10])]
    feed       = feed or DataFeed(cfg)
    now        = now or datetime.now(timezone.utc)

    # Benchmark leg for excess_return. Resistance (↑) and support (↓) setups have
    # been posting near-mirrored signed returns (+1.5% / -1.4% at 5d), which is the
    # signature of market drift rather than of thin-zone structure — a real
    # structural edge would be positive in BOTH directions. Subtracting the
    # benchmark over the identical window separates the two. Assumes beta = 1;
    # it is a drift adjustment, not a risk model.
    bench_ticker = cfg.get("learn.benchmark") or None
    bench_bars: pd.DataFrame | None = None
    if bench_ticker:
        try:
            bench_bars = feed.get_bars(str(bench_ticker))
        except Exception:  # noqa: BLE001 - benchmark is optional, never fatal
            bench_bars = None
        if bench_bars is None or bench_bars.empty:
            print(f"[oplog] benchmark {bench_ticker} unavailable — "
                  f"excess_return will be NULL for this pass")

    written = 0
    with _db(cfg) as conn:
        # Cache bars per ticker across horizons within this pass.
        bars_cache: dict[str, pd.DataFrame] = {}

        for horizon in horizons:
            due = _due_opportunities(conn, horizon, now)
            if not due:
                continue
            barrier = _barrier_atr(cfg, horizon)
            print(f"[oplog] labeling {len(due)} opportunities at {horizon}d horizon "
                  f"(flag barrier {barrier:.2f} ATR)...")

            for opp in due:
                ticker = opp["ticker"]
                if ticker not in bars_cache:
                    try:
                        bars_cache[ticker] = feed.get_bars(ticker)
                    except Exception:
                        bars_cache[ticker] = pd.DataFrame()
                bars = bars_cache[ticker]
                if bars is None or bars.empty:
                    continue

                idx = bars.index
                idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                detected = pd.Timestamp(opp["detected_at"])
                if detected.tz is None:
                    detected = detected.tz_localize("UTC")
                end = detected + pd.Timedelta(days=horizon)

                forward = bars[(idx > detected) & (idx <= end)]
                if forward.empty:
                    continue  # no forward data yet (e.g. delisted / gap) — try again later

                sign = 1 if opp["direction"] == "up" else -1
                next_hvn = opp["next_hvn_price"] if "next_hvn_price" in opp.keys() else None
                m = _forward_metrics(
                    forward, float(opp["entry_price"]), float(opp["atr"]),
                    sign, barrier, opp["target_price"],
                    next_hvn_price=next_hvn, detected=detected,
                )
                win = m["breakout_win"] if opp["type"] == "flag" else (m["reached_target"] or 0)

                # Drift-adjusted return: same window, same sign convention, so a
                # setup only shows positive excess if it beat simply being long
                # (or short) the market for those days.
                bench_ret = _window_return_pct(bench_bars, detected, horizon) if bench_ticker else None
                excess = (round((m["fwd_return"] - bench_ret) * sign, 4)
                          if bench_ret is not None else None)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO outcomes
                      (opp_id, horizon_days, labeled_at, n_bars, fwd_return,
                       signed_return, mfe_atr, mae_atr, reached_target,
                       bars_to_target, days_to_target,
                       reached_next_hvn, days_to_next_hvn,
                       bench_return, excess_return, win)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (opp["id"], horizon, now.isoformat(), m["n_bars"], m["fwd_return"],
                     m["signed_return"], m["mfe_atr"], m["mae_atr"], m["reached_target"],
                     m["bars_to_target"], m["days_to_target"],
                     m["reached_next_hvn"], m["days_to_next_hvn"],
                     (round(bench_ret, 4) if bench_ret is not None else None), excess,
                     int(win)),
                )
                written += 1

    print(f"[oplog] wrote {written} outcome rows")
    return written


# ── Lightweight calibration peek (Phase 3 preview) ──────────────────────────────

def counts(cfg: Config) -> tuple[int, int]:
    """(opportunities, outcomes) row counts — a cheap health probe for /healthz,
    so an unexpectedly empty learning DB is visible without shell access."""
    with _db(cfg) as conn:
        n_opp = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        n_out = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    return int(n_opp), int(n_out)


def clustered_mean_se(x, clusters) -> float | None:
    """Standard error of mean(x) with rows clustered by `clusters` (CR1 sandwich).

    Rows detected on the same date are not independent draws: the whole market
    moves them together (on 2026-09-11 nearly every up-flag lost at once), and one
    ticker-day emits a support, a resistance and a flag row over the same window.
    Clustering by run_date is the honest error bar. NaNs are dropped; None with
    fewer than two clusters, where no error bar can be formed."""
    x = np.asarray(x, dtype=float)
    c = np.asarray(clusters, dtype=object)
    ok = np.isfinite(x)
    x, c = x[ok], c[ok]
    n = len(x)
    if n == 0:
        return None
    sums = pd.Series(x - x.mean()).groupby(c).sum()
    g = len(sums)
    if g < 2:
        return None
    return float(np.sqrt((sums ** 2).sum() * g / (g - 1)) / n)


def clustered_gap_se(x, hi_mask, clusters) -> float | None:
    """Date-clustered standard error of mean(x[hi]) − mean(x[~hi]).

    The two halves of a score median-split share detection dates, so their means
    are correlated and the naive √(se_hi² + se_lo²) is wrong in either direction.
    This is the influence-function form: each row contributes its deviation from
    its own half's mean over that half's size, summed within each date."""
    x = np.asarray(x, dtype=float)
    hi = np.asarray(hi_mask, dtype=bool)
    c = np.asarray(clusters, dtype=object)
    ok = np.isfinite(x)
    x, hi, c = x[ok], hi[ok], c[ok]
    n_hi, n_lo = int(hi.sum()), int((~hi).sum())
    if n_hi == 0 or n_lo == 0:
        return None
    infl = np.where(hi, (x - x[hi].mean()) / n_hi, -(x - x[~hi].mean()) / n_lo)
    sums = pd.Series(infl).groupby(c).sum()
    g = len(sums)
    if g < 2:
        return None
    return float(np.sqrt((sums ** 2).sum() * g / (g - 1)))


# Options-flow features judged on /learning and by the tuner. All logged-only
# (see src/flow.py); this is the table that decides whether any earns a weight.
FLOW_SPLIT_FEATURES = (
    "flow_aligned_share",
    "flow_aligned_share_vs_base",
    "flow_aligned_short_share",
    "flow_pocket_share",
    "flow_premium_adv_ratio",
    "iv_skew",
    "scanner_aligned_share",
    "scanner_aligned_share_vs_base",
    "scanner_notable_aligned_share",
)


def flow_splits(conn: sqlite3.Connection, features=FLOW_SPLIT_FEATURES) -> list[dict]:
    """Per (feature, type, horizon): labeled rows split into terciles of the
    feature, each with avg excess return ± its date-clustered SE, plus the
    top-minus-bottom tercile gap with a date-clustered SE and t.

    Terciles, not fixed cuts: these features have no natural thresholds yet and
    their scales differ (shares, $ ratios, vol points), so rank-based buckets
    keep every table comparable. Cut within each (type, horizon), since the
    distributions differ by setup (put-aligned support skews low). Rows with
    the feature NULL are counted (`n_missing`), never bucketed."""
    rows = conn.execute(
        """
        SELECT o.type AS type, oc.horizon_days AS horizon, o.run_date AS run_date,
               o.features AS features, oc.excess_return AS exc, oc.win AS win
        FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
        """
    ).fetchall()
    by_group: dict[tuple, list] = {}
    for r in rows:
        try:
            feats = json.loads(r["features"])
        except (ValueError, TypeError):
            continue
        if not isinstance(feats, dict):
            continue
        by_group.setdefault((r["type"], int(r["horizon"])), []).append(
            (r["run_date"], np.nan if r["exc"] is None else float(r["exc"]), r["win"], feats))

    def num(v):
        if isinstance(v, bool) or v is None:
            return np.nan
        try:
            return float(v)
        except (TypeError, ValueError):
            return np.nan

    out: list[dict] = []
    for (typ, h), grp in sorted(by_group.items()):
        exc_all = np.array([g[1] for g in grp], dtype=float)
        for feat in features:
            x = np.array([num(g[3].get(feat)) for g in grp], dtype=float)
            ok = np.isfinite(x) & np.isfinite(exc_all)
            n = int(ok.sum())
            entry = {"feature": feat, "type": typ, "horizon": h, "n": n,
                     "n_missing": int((~np.isfinite(x)).sum()), "buckets": []}
            if n < 9:
                continue                      # not even 3 rows per tercile
            xv, ev = x[ok], exc_all[ok]
            dv = [g[0] for g, keep in zip(grp, ok) if keep]
            wv = np.array([g[2] for g, keep in zip(grp, ok) if keep], dtype=float)
            q1, q2 = np.quantile(xv, [1 / 3, 2 / 3])
            if q1 == q2:
                continue                      # degenerate: mostly one value
            masks = (("low", xv <= q1), ("mid", (xv > q1) & (xv <= q2)), ("high", xv > q2))
            for label, m in masks:
                if not m.any():
                    continue
                d = [dd for dd, keep in zip(dv, m) if keep]
                entry["buckets"].append({
                    "bucket": label, "n": int(m.sum()), "n_dates": len(set(d)),
                    "lo": float(xv[m].min()), "hi": float(xv[m].max()),
                    "win_pct": 100.0 * float(wv[m].mean()),
                    "avg_excess": float(ev[m].mean()),
                    "excess_se": clustered_mean_se(ev[m], d),
                })
            lo_m, hi_m = masks[0][1], masks[2][1]
            both = lo_m | hi_m
            entry["n_dates"] = len(set(dv))
            if lo_m.any() and hi_m.any():
                gap = float(ev[hi_m].mean() - ev[lo_m].mean())
                se = clustered_gap_se(ev[both], hi_m[both],
                                      [dd for dd, keep in zip(dv, both) if keep])
                entry.update({"gap": round(gap, 3),
                              "gap_se": None if se is None else round(se, 3),
                              "gap_t": None if not se else round(gap / se, 2)})
            out.append(entry)
    return out


def stats(cfg: Config, recent_limit: int = 250) -> dict:
    """Structured win-rate / return breakdown for the CLI summary and /learning page.

    Returns:
      n_opportunities, n_outcomes — totals
      groups — per (type, horizon): n, win%, hit% (zones), median days-to-hit,
               far-wall traversal % and its median days, avg signed return,
               avg MFE/MAE (ATRs), plus n_dates (distinct detection dates —
               the real count of independent draws) and excess_se (the
               date-clustered standard error of avg_excess)
      eras   — the same per (type, horizon), split before / since
               learn.regime_start; empty when that knob is unset
      gamma  — per (type, dealer-gamma bucket, horizon): n, win%, avg signed and
               excess return —
               does the −γ amplification thesis actually show up in outcomes?
      live   — per (type, live-confirmed?, horizon): n, win%, avg signed and
               excess return —
               did the /live monitor's intraday option-flow confirmation (see
               src/live.py) actually predict better outcomes?
      rvol   — per (type, RVOL-at-detection bucket, horizon): n, win%, avg signed and
               excess return — is score.combined.w_rvol earning its weight?
      flow   — per (options-flow feature, type, horizon): tercile buckets with avg
               excess ± date-clustered SE, and the high−low gap with its t
               (see flow_splits) — does any logged-only flow feature pay?
      recent — up to `recent_limit` labeled outcomes PER HORIZON, most recent labeling pass
               first, then by detection date (NOT alphabetical — see the note on
               the query below)
    """
    with _db(cfg) as conn:
        n_opp = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        n_out = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]

        groups = [dict(r) for r in conn.execute(
            """
            SELECT o.type AS type, oc.horizon_days AS horizon,
                   COUNT(*)                       AS n,
                   AVG(oc.win) * 100.0            AS win_pct,
                   AVG(oc.reached_target)*100.0   AS hit_pct,        -- NULL for flags
                   AVG(oc.reached_next_hvn)*100.0 AS far_pct,        -- traversed to the far wall
                   AVG(oc.signed_return)          AS avg_ret,
                   AVG(oc.excess_return)          AS avg_excess,     -- net of benchmark drift
                   COUNT(oc.excess_return)        AS n_excess,       -- rows with a benchmark leg
                   AVG(oc.mfe_atr)                AS mfe,
                   AVG(oc.mae_atr)                AS mae
            FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
            GROUP BY o.type, oc.horizon_days
            ORDER BY o.type, oc.horizon_days
            """
        ).fetchall()]

        # Medians (SQLite has no MEDIAN): time-to-hit distributions are
        # right-skewed and signed returns are fat-tailed (a single -6% / 15-ATR
        # blow-off drags the mean), so the median is the honest "typical" figure.
        med_hit: dict[tuple, list] = {}
        med_far: dict[tuple, list] = {}
        med_ret: dict[tuple, list] = {}
        # Per-row (run_date, excess, win) for the date-clustered error bars and the
        # regime split below — neither can be expressed in SQLite aggregates.
        per_row: dict[tuple, list] = {}
        for row in conn.execute(
            """
            SELECT o.type AS type, oc.horizon_days AS horizon,
                   oc.days_to_target AS d_hit, oc.days_to_next_hvn AS d_far,
                   oc.signed_return AS sret, oc.excess_return AS exc,
                   oc.win AS win, o.run_date AS run_date
            FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
            """
        ):
            key = (row["type"], row["horizon"])
            if row["d_hit"] is not None:
                med_hit.setdefault(key, []).append(row["d_hit"])
            if row["d_far"] is not None:
                med_far.setdefault(key, []).append(row["d_far"])
            if row["sret"] is not None:
                med_ret.setdefault(key, []).append(row["sret"])
            per_row.setdefault(key, []).append(
                (row["run_date"], np.nan if row["exc"] is None else row["exc"], row["win"]))
        for g in groups:
            key = (g["type"], g["horizon"])
            g["med_days_to_hit"] = float(np.median(med_hit[key])) if key in med_hit else None
            g["med_days_to_far"] = float(np.median(med_far[key])) if key in med_far else None
            g["med_ret"] = float(np.median(med_ret[key])) if key in med_ret else None
            g.update(_date_clustered(per_row.get(key, [])))

        # Regime split around learn.regime_start (the date the rank weights last
        # changed). Detection and the logged per-opportunity score are NOT changed
        # by a reweight — every zone is logged, undamped, whatever its rank — so
        # this is an out-of-sample check on the edge the reweight was based on,
        # not an A/B of the new ranking.
        regime_start = cfg.get("learn.regime_start")
        eras: list[dict] = []
        if regime_start:
            regime_start = str(regime_start)
            for (typ, h), rows in sorted(per_row.items()):
                for label, keep in (("before", lambda d: d < regime_start),
                                    ("since", lambda d: d >= regime_start)):
                    sub = [r for r in rows if keep(r[0])]
                    if not sub:
                        continue
                    eras.append({"type": typ, "horizon": h, "era": label, "n": len(sub),
                                 "win_pct": 100.0 * float(np.mean([r[2] for r in sub])),
                                 **_date_clustered(sub)})

        # Dealer-gamma split, by sign AND raw strength. Buckets are cut on
        # gex_adv_ratio (|net GEX| / avg daily $ volume) rather than gex_amp
        # because gex_amp is the *knob's own output*: it saturates at
        # score.combined.gex_full_at_adv_frac, so re-tuning that knob would move
        # the very buckets meant to evaluate it, and its ADV-unknown fallback
        # returns a full 1.0 (see screen.gex_amp) — which put "we could not
        # measure the scale" in the same bucket as "the exposure is enormous".
        # gex_adv_ratio is NULL exactly when the scale is unknown, so those rows
        # now get their own bucket and stop contaminating the strong one.
        # neg_gamma lives in the features JSON: JSON true/false extract as 1/0,
        # missing/null as NULL. Guarded — very old SQLite builds lack json_extract.
        gamma: list[dict] = []
        try:
            gamma = [dict(r) for r in conn.execute(
                """
                SELECT CASE
                         WHEN json_extract(o.features, '$.neg_gamma') IS NULL
                           THEN 'no options data'
                         WHEN json_extract(o.features, '$.neg_gamma') = 0
                           THEN '+γ dampening'
                         WHEN json_extract(o.features, '$.gex_adv_ratio') IS NULL
                           THEN '−γ (scale unknown)'
                         WHEN ABS(json_extract(o.features, '$.gex_adv_ratio')) >= 0.05
                           THEN '−γ ≥ 5% of ADV$'
                         WHEN ABS(json_extract(o.features, '$.gex_adv_ratio')) >= 0.01
                           THEN '−γ 1–5% of ADV$'
                         ELSE '−γ < 1% of ADV$'
                       END                       AS bucket,
                       o.type                    AS type,
                       oc.horizon_days           AS horizon,
                       COUNT(*)                  AS n,
                       AVG(oc.win) * 100.0       AS win_pct,
                       AVG(oc.signed_return)     AS avg_ret,
                       AVG(oc.excess_return)     AS avg_excess,
                       COUNT(oc.excess_return)   AS n_excess,
                       AVG(oc.days_to_target)    AS avg_days_to_hit
                FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
                GROUP BY o.type, bucket, oc.horizon_days
                ORDER BY o.type, bucket, oc.horizon_days
                """
            ).fetchall()]
        except sqlite3.OperationalError:
            pass

        # RVOL split. score.combined.w_rvol has carried the same weight as the GEX
        # term with no calibration table behind it at all — this is that table.
        # Bucketed on the RAW rvol feature, not on rvol_amp, for the same reason
        # the gamma table uses gex_adv_ratio: rvol_amp is the knob's own output
        # (it saturates at score.combined.rvol_full_at), so re-tuning the knob
        # would move the buckets meant to judge it.
        rvol: list[dict] = []
        try:
            rvol = [dict(r) for r in conn.execute(
                """
                SELECT CASE
                         WHEN json_extract(o.features, '$.rvol') IS NULL
                           THEN 'no volume data'
                         WHEN json_extract(o.features, '$.rvol') < 1.0
                           THEN 'RVOL < 1× (quiet)'
                         WHEN json_extract(o.features, '$.rvol') < 1.5
                           THEN 'RVOL 1–1.5×'
                         WHEN json_extract(o.features, '$.rvol') < 2.0
                           THEN 'RVOL 1.5–2×'
                         ELSE 'RVOL ≥ 2× (full credit)'
                       END                       AS bucket,
                       CASE
                         WHEN json_extract(o.features, '$.rvol') IS NULL THEN 9
                         WHEN json_extract(o.features, '$.rvol') < 1.0 THEN 0
                         WHEN json_extract(o.features, '$.rvol') < 1.5 THEN 1
                         WHEN json_extract(o.features, '$.rvol') < 2.0 THEN 2
                         ELSE 3
                       END                       AS sort_key,
                       o.type                    AS type,
                       oc.horizon_days           AS horizon,
                       COUNT(*)                  AS n,
                       AVG(oc.win) * 100.0       AS win_pct,
                       AVG(oc.signed_return)     AS avg_ret,
                       AVG(oc.excess_return)     AS avg_excess,
                       COUNT(oc.excess_return)   AS n_excess
                FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
                GROUP BY o.type, bucket, sort_key, oc.horizon_days
                ORDER BY o.type, sort_key, oc.horizon_days
                """
            ).fetchall()]
        except sqlite3.OperationalError:
            pass

        # Live intraday confirmation split: did the /live monitor ever see
        # option flow surging in-direction (or clustering at the gamma flip)
        # that day? Only rows where the monitor actually observed the ticker
        # are bucketed — "no live data" (monitor off, or outside that day's
        # top-N) is reported separately so it never dilutes the comparison.
        live: list[dict] = []
        try:
            live = [dict(r) for r in conn.execute(
                """
                SELECT CASE
                         WHEN json_extract(o.features, '$.live_ever_activated') IS NULL
                           THEN 'no live data'
                         WHEN json_extract(o.features, '$.live_ever_activated') = 1
                           THEN 'live-confirmed'
                         ELSE 'not confirmed'
                       END                       AS bucket,
                       o.type                    AS type,
                       oc.horizon_days           AS horizon,
                       COUNT(*)                  AS n,
                       AVG(oc.win) * 100.0       AS win_pct,
                       AVG(oc.signed_return)     AS avg_ret,
                       AVG(oc.excess_return)     AS avg_excess,
                       COUNT(oc.excess_return)   AS n_excess
                FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
                GROUP BY o.type, bucket, oc.horizon_days
                ORDER BY o.type, bucket, oc.horizon_days
                """
            ).fetchall()]
        except sqlite3.OperationalError:
            pass

        # days_to_earnings via json_extract when available (older SQLite: skip).
        earn_expr = "json_extract(o.features, '$.days_to_earnings')"
        try:
            conn.execute(f"SELECT {earn_expr} FROM opportunities o LIMIT 1")
        except sqlite3.OperationalError:
            earn_expr = "NULL"

        # `labeled_at` is stamped ONCE per labeling pass (see label_due), so it can
        # only order batches, never rows within one — sort by detection date inside
        # a batch (an o.ticker tiebreak turns the table alphabetical). The cap is
        # applied PER HORIZON: one shared cap sorted by detection date fills up with
        # the newest 5d rows and pushes every 10d row (older detections) off the end.
        recent: list[dict] = []
        for (h,) in conn.execute("SELECT DISTINCT horizon_days FROM outcomes").fetchall():
            recent += [dict(r) for r in conn.execute(
                f"""
                SELECT o.ticker, o.type, o.direction, o.run_date, o.score,
                       oc.horizon_days AS horizon, oc.signed_return, oc.mfe_atr,
                       oc.mae_atr, oc.reached_target, oc.days_to_target,
                       oc.reached_next_hvn, oc.days_to_next_hvn,
                       oc.bench_return, oc.excess_return,
                       {earn_expr} AS days_to_earnings,
                       oc.win, oc.labeled_at
                FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
                WHERE oc.horizon_days = ?
                ORDER BY oc.labeled_at DESC, o.run_date DESC, o.ticker
                LIMIT ?
                """,
                (h, int(recent_limit)),
            ).fetchall()]
        recent.sort(key=lambda r: (r["ticker"]))
        recent.sort(key=lambda r: r["horizon"])
        recent.sort(key=lambda r: (r["labeled_at"] or "", r["run_date"]), reverse=True)

        # Options-flow tercile splits (logged-only features). Guarded like the
        # json_extract tables: a breakdown failing must not cost the page.
        try:
            flow = flow_splits(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"[oplog] flow splits unavailable ({type(exc).__name__}: {exc})")
            flow = []

    # Each breakdown row carries its (type, horizon) base win rate, so the page can
    # colour a bucket by whether it beats its own group rather than a flat 50% —
    # zone "win" (touched the LVN) and flag "win" (barrier) have different bases.
    base = {(g["type"], g["horizon"]): g["win_pct"] for g in groups}
    for rows in (gamma, rvol, live):
        for g in rows:
            g["base_win_pct"] = base.get((g["type"], g["horizon"]))

    return {"n_opportunities": n_opp, "n_outcomes": n_out,
            "groups": groups, "gamma": gamma, "rvol": rvol, "live": live, "flow": flow,
            "regime_start": regime_start, "eras": eras,
            "recent": recent}


def _date_clustered(rows: list[tuple]) -> dict:
    """rows: (run_date, excess_or_nan, win). Distinct detection dates, and mean
    excess return with its date-clustered standard error."""
    dates = [r[0] for r in rows]
    exc = np.array([r[1] for r in rows], dtype=float)
    has = np.isfinite(exc)
    return {
        "n_dates": len(set(dates)),
        "n_excess_dates": len({d for d, ok in zip(dates, has) if ok}),
        "avg_excess": float(exc[has].mean()) if has.any() else None,
        "n_excess": int(has.sum()),
        "excess_se": clustered_mean_se(exc, dates),
    }


# ── Raw per-row export ──────────────────────────────────────────────────────────

# Hard ceiling on one export response. The log grows without bound, and the whole
# point of this endpoint is bulk reads, so the cap is generous — it exists to stop
# a single request from materialising an unbounded list in memory, not to ration.
EXPORT_MAX_ROWS = 50_000

_EXPORT_COLUMNS = """
    o.id, o.detected_at, o.run_date, o.ticker, o.type, o.direction,
    o.entry_price, o.atr, o.target_price, o.next_hvn_price,
    o.score, o.combined_score, o.features,
    oc.horizon_days, oc.labeled_at, oc.n_bars, oc.fwd_return, oc.signed_return,
    oc.mfe_atr, oc.mae_atr, oc.reached_target, oc.bars_to_target,
    oc.days_to_target, oc.reached_next_hvn, oc.days_to_next_hvn,
    oc.bench_return, oc.excess_return, oc.win
"""


def export_rows(cfg: Config, limit: int | None = None, offset: int = 0,
                setup_type: str | None = None, horizon: int | None = None,
                since: str | None = None, labeled: bool | None = None) -> list[dict]:
    """Every logged opportunity LEFT JOINed to its outcomes, as plain dicts.

    This is the raw (X, y) the /learning aggregates are computed from. `stats()`
    answers fixed questions and caps its row view at `recent_limit`; this answers
    arbitrary ones, which is what any out-of-band calibration work needs (e.g.
    decomposing a move into its overnight and intraday legs — impossible from the
    aggregates, and impossible from a 250-row window that covers three days).

    One row per (opportunity, horizon). LEFT JOIN, so an opportunity whose horizon
    hasn't elapsed yet appears once with every outcome field None — the caller can
    tell "not yet resolved" from "resolved as a loss", which an INNER JOIN hides.

    `features` is returned as a parsed object, not the stored JSON string, so
    consumers don't have to double-decode. A row whose blob somehow won't parse
    keeps its raw string rather than failing the whole export.
    """
    where: list[str] = []
    params: list = []
    if setup_type:
        where.append("o.type = ?")
        params.append(str(setup_type))
    if horizon is not None:
        where.append("oc.horizon_days = ?")
        params.append(int(horizon))
    if since:
        where.append("o.run_date >= ?")
        params.append(str(since))
    if labeled is True:
        where.append("oc.opp_id IS NOT NULL")
    elif labeled is False:
        where.append("oc.opp_id IS NULL")

    sql = (f"SELECT {_EXPORT_COLUMNS} "
           "FROM opportunities o "
           "LEFT JOIN outcomes oc ON oc.opp_id = o.id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Deterministic and paging-safe: run_date/ticker/type is the log's natural key,
    # so a caller walking offsets never sees a row twice or skips one.
    sql += " ORDER BY o.run_date, o.ticker, o.type, oc.horizon_days"

    n = EXPORT_MAX_ROWS if limit is None else max(0, min(int(limit), EXPORT_MAX_ROWS))
    sql += " LIMIT ? OFFSET ?"
    params.extend([n, max(0, int(offset))])

    with _db(cfg) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]

    for row in rows:
        raw = row.get("features")
        if isinstance(raw, str):
            try:
                row["features"] = json.loads(raw)
            except (ValueError, TypeError):
                pass          # keep the raw string; a bad blob shouldn't sink the export
    return rows


def export_total(cfg: Config) -> int:
    """Total (opportunity, horizon) rows an unfiltered export would yield — lets a
    caller page without guessing when it has reached the end."""
    with _db(cfg) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM opportunities o "
            "LEFT JOIN outcomes oc ON oc.opp_id = o.id"
        ).fetchone()[0])


def summary(cfg: Config) -> None:
    """Print a quick win-rate / mean-signed-return breakdown by type and horizon.
    Descriptive only — not the model, just a sanity check that signal exists."""
    s = stats(cfg)
    print(f"\nopportunities logged: {s['n_opportunities']}   outcomes labeled: {s['n_outcomes']}")
    if not s["n_outcomes"]:
        print("(no labeled outcomes yet — they accrue as horizons elapse)")
        return

    print(f"\n{'type':<11}{'horizon':>8}{'n':>7}{'win%':>8}{'avg signed ret%':>18}"
          f"{'avg excess ret%':>18}{'avg MFE/MAE atr':>18}")
    for r in s["groups"]:
        mm = f"{r['mfe']:.2f}/{r['mae']:.2f}"
        # excess is NULL until a benchmark-aware labeling pass has run
        ex = f"{r['avg_excess']:.2f}%" if r.get("avg_excess") is not None else "—"
        print(f"{r['type']:<11}{r['horizon']:>8}{r['n']:>7}{r['win_pct']:>7.1f}%"
              f"{r['avg_ret']:>17.2f}%{ex:>18}{mm:>18}")
