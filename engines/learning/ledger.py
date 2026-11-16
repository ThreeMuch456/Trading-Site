"""The learning ledger: one read-only view over every loop's own store.

Each engine keeps its learning record in its own SQLite file with its own shape
(see the table below). Nothing here writes, migrates or copies — every store is
opened ``mode=ro`` and its rows are *projected* into one common observation:

    loop      which learning loop the row belongs to
    date      the session the call was made on (the clustering unit everywhere)
    ticker
    direction +1 / -1 for a directional call, 0 for none
    horizon   how far ahead the outcome was measured
    ret       the raw outcome in % (the underlying's move over the horizon)
    edge      the outcome as an *edge*: zero in expectation when the call carries
              no information (benchmark- or drift-adjusted, favourable = positive)
    hit       1 / 0 when the loop grades right/wrong, else None

plus the loop's own tags for cohorting. Because every loop shares `date` and
`ticker`, rows from different loops can be lined up against each other — which is
what cross-signal work needs and what four separate databases could not do.

  loop       store               one observation is
  brief      agent.sqlite3       a morning bias call on one index ETF, graded at the close
  structure  opportunities.db    a thin-zone / flag setup, labeled at a 5- or 10-day horizon
  flow       flow_learning.db    a day's index-ETF flow lean at a cutoff, labeled per horizon
  zerodte    zerodte.sqlite3     a touch of a 0DTE gamma level, resolved the same session

A missing store or table yields no rows; a loader never raises into its caller.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from urllib.parse import quote

from .. import config as econf

log = logging.getLogger("engines.learning")

LOOPS = ("brief", "structure", "flow", "zerodte")

STORES = {
    "brief": "agent.sqlite3",
    "structure": "opportunities.db",
    "flow": "flow_learning.db",
    "zerodte": "zerodte.sqlite3",
}

# The Brief's grading bands (engines/agent/grade_outcomes.py). Mirrored, not
# imported: that module pulls in the fetchers. A test pins them to the source.
NEUTRAL_EM_FRACTION, NEUTRAL_PCT_FALLBACK = 0.25, 0.3
CAUTIOUS_EM_FRACTION, CAUTIOUS_PCT_FALLBACK = 0.5, 0.5

# Options-flow features the structure screener logs on every setup
# (engines/structure/oplog.py FLOW_SPLIT_FEATURES; pinned by a test for the same
# reason — importing oplog loads pandas).
STRUCTURE_FLOW_FEATURES = (
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


def store_path(loop: str, data_dir: str | None = None) -> str:
    return os.path.join(data_dir or econf.DATA_DIR, STORES[loop])


def _ro(path: str) -> sqlite3.Connection | None:
    """Open a store strictly read-only, or None if it is not there."""
    if not os.path.exists(path):
        return None
    uri = "file:" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _query(loop: str, sql: str, data_dir: str | None) -> list:
    con = None
    try:
        con = _ro(store_path(loop, data_dir))
        return con.execute(sql).fetchall() if con is not None else []
    except sqlite3.Error:
        log.warning("learning ledger: %s store unreadable", loop, exc_info=True)
        return []
    finally:
        if con is not None:
            con.close()


def _num(v) -> float | None:
    """A real number or None. Booleans are not numbers here; neither is NaN."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return None if v != v else float(v)


def _demean(rows: list, key) -> dict:
    """Mean raw return per `key(row)` — the drift a direction-free caller earns.
    Subtracting it means a call only scores for *timing*, not for being long a
    market that went up."""
    acc: dict = {}
    for r in rows:
        if r["ret"] is not None:
            s = acc.setdefault(key(r), [0.0, 0])
            s[0] += r["ret"]
            s[1] += 1
    return {k: s / n for k, (s, n) in acc.items()}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def brief(data_dir: str | None = None) -> list:
    """Morning bias calls. `edge` (directional calls only) is the day's move in
    the called direction, net of that ticker's average day over the sample.
    `in_neutral_band` / `in_cautious_band` say whether a neutral / cautious call
    WOULD have been graded right that day whatever was actually called — the base
    rate those calls have to beat."""
    raw = _query("brief", """
        SELECT s.date AS date, o.ticker AS ticker, o.day_pct AS day_pct,
               o.bias_correct AS bias_correct,
               json_extract(s.gameplan, '$.gameplan.' || o.ticker || '.bias') AS bias,
               json_extract(s.gameplan, '$.gameplan.' || o.ticker || '.conviction') AS conviction,
               json_extract(s.gameplan, '$.overall_risk_rating') AS risk,
               json_extract(s.options_data, '$.options.' || o.ticker || '.gamma_environment') AS gamma,
               json_extract(s.options_data, '$.options.' || o.ticker || '.expected_move.move_pct') AS em
        FROM outcomes o JOIN sessions s ON s.id = o.session_id
        ORDER BY s.date, o.ticker""", data_dir)
    rows = []
    for r in raw:
        ret, em = _num(r["day_pct"]), _num(r["em"])
        bias = (r["bias"] or "").strip().lower()
        nb = em * NEUTRAL_EM_FRACTION if em else NEUTRAL_PCT_FALLBACK
        cb = em * CAUTIOUS_EM_FRACTION if em else CAUTIOUS_PCT_FALLBACK
        rows.append({
            "loop": "brief", "date": r["date"], "ticker": r["ticker"], "horizon": "day",
            "bias": bias or None,
            "direction": 1 if bias == "bullish" else -1 if bias == "bearish" else 0,
            "conviction": _num(r["conviction"]),
            "gamma": r["gamma"], "risk": r["risk"],
            "ret": ret, "edge": None,
            "hit": r["bias_correct"] if r["bias_correct"] in (0, 1) else None,
            "in_neutral_band": None if ret is None else int(abs(ret) <= nb),
            "in_cautious_band": None if ret is None else int(abs(ret) <= cb),
        })
    drift = _demean(rows, lambda r: r["ticker"])
    for r in rows:
        if r["direction"] and r["ret"] is not None:
            r["edge"] = r["direction"] * (r["ret"] - drift[r["ticker"]])
    return rows


def structure(data_dir: str | None = None) -> list:
    """Labeled setups, one row per (setup, horizon). `edge` is the logged excess
    return: the move net of the benchmark over the same window, signed so that
    favourable is positive. Rows labeled without a benchmark have no edge."""
    raw = _query("structure", """
        SELECT o.opp_id AS opp_id, o.horizon_days AS horizon, o.fwd_return AS fwd_return,
               o.signed_return AS signed_return, o.excess_return AS excess_return,
               o.win AS win, p.run_date AS date, p.ticker AS ticker, p.type AS type,
               p.direction AS direction, p.score AS score,
               p.combined_score AS combined_score, p.features AS features
        FROM outcomes o JOIN opportunities p ON p.id = o.opp_id
        ORDER BY p.run_date""", data_dir)
    feats: dict = {}
    rows = []
    for r in raw:
        f = feats.get(r["opp_id"])
        if f is None:
            try:
                blob = json.loads(r["features"] or "{}")
            except ValueError:
                blob = {}
            f = feats[r["opp_id"]] = {k: _num(blob.get(k)) for k in STRUCTURE_FLOW_FEATURES}
        rows.append({
            "loop": "structure", "date": r["date"], "ticker": r["ticker"],
            "horizon": int(r["horizon"]), "type": r["type"],
            "direction": 1 if r["direction"] == "up" else -1,
            "score": _num(r["score"]), "combined_score": _num(r["combined_score"]),
            "ret": _num(r["fwd_return"]), "signed_return": _num(r["signed_return"]),
            "edge": _num(r["excess_return"]),
            "hit": r["win"] if r["win"] in (0, 1) else None,
            "features": f,
        })
    return rows


def flow(data_dir: str | None = None) -> list:
    """Index-ETF flow leans. `edge` (directional leans only) is the ETF's move in
    the implied direction, net of that ticker's average move over the same
    horizon."""
    raw = _query("flow", """
        SELECT s.run_date AS date, s.ticker AS ticker, s.cutoff AS cutoff,
               s.implied_dir AS implied_dir, s.net_premium_skew AS net_premium_skew,
               s.score_weighted_skew AS score_weighted_skew,
               o.horizon AS horizon, o.raw_return AS raw_return,
               o.direction_correct AS direction_correct
        FROM flow_outcomes o JOIN flow_signals s ON s.id = o.signal_id
        ORDER BY s.run_date""", data_dir)
    rows = [{
        "loop": "flow", "date": r["date"], "ticker": r["ticker"],
        "horizon": r["horizon"], "cutoff": r["cutoff"],
        "direction": {"up": 1, "down": -1}.get(r["implied_dir"], 0),
        "net_premium_skew": _num(r["net_premium_skew"]),
        "score_weighted_skew": _num(r["score_weighted_skew"]),
        "ret": _num(r["raw_return"]), "edge": None, "hit": None,
    } for r in raw]
    drift = _demean(rows, lambda r: (r["ticker"], r["horizon"]))
    for r, src in zip(rows, raw):
        if r["direction"] and r["ret"] is not None:
            r["edge"] = r["direction"] * (r["ret"] - drift[(r["ticker"], r["horizon"])])
            r["hit"] = src["direction_correct"] if src["direction_correct"] in (0, 1) else None
    return rows


def zerodte(data_dir: str | None = None) -> list:
    """Touches of 0DTE gamma levels. `hit` is 1 when the level held (REJECTED),
    0 when it broke (BREACHED), None when price just churned through it.

    The touch's own `label` (DEFENDED / SLIPPED / ...) is carried for reference
    but must never be used as a signal: it is assigned after the outcome and maps
    one-to-one onto it. What CAN be tested are the readings the label was built
    from, all fixed before the outcome resolves: `wall_size` (dealer gamma at the
    level on entry) and the option flow while price was inside the band —
    `net_customer` (signed contracts), `aggression` (share of volume at the ask),
    `wall_change` (dealer gamma on exit vs entry) and `rel_volume`.

    Deliberately NOT carried: time inside the band and maximum excursion. A long,
    deep touch is one that is already breaking, so they describe the outcome
    rather than precede it."""
    raw = _query("zerodte", "SELECT session_date, ticker, payload FROM touches "
                            "ORDER BY session_date", data_dir)
    rows = []
    for r in raw:
        try:
            t = json.loads(r["payload"])
        except ValueError:
            continue
        outcome = (t.get("price_outcome") or "").strip().upper()
        if not outcome:
            continue
        fl = t.get("flow") or {}
        mag = _num(fl.get("mag_before"))
        rows.append({
            "loop": "zerodte", "date": r["session_date"], "ticker": r["ticker"],
            "horizon": "session", "kind": t.get("kind"), "strike": _num(t.get("strike")),
            "direction": 0, "outcome": outcome,
            "wall_size": abs(mag) if mag is not None else None,
            "net_customer": _num(fl.get("net_customer_contracts")),
            "aggression": _num(fl.get("aggression_ratio")),
            "wall_change": _num(fl.get("mag_change")),
            "rel_volume": _num(t.get("rel_volume")),
            "low_conf": bool(fl.get("low_conf")),
            "label_after_outcome": t.get("label"),
            "ret": None, "edge": None,
            "hit": 1 if outcome == "REJECTED" else 0 if outcome == "BREACHED" else None,
        })
    return rows


LOADERS = {"brief": brief, "structure": structure, "flow": flow, "zerodte": zerodte}


def observations(loop: str, data_dir: str | None = None) -> list:
    """Every observation for one loop, in date order."""
    return LOADERS[loop](data_dir)


def coverage(rows: list) -> dict:
    """How much history a loop holds, counted in the unit that matters (dates)."""
    dates = sorted({r["date"] for r in rows if r.get("date")})
    return {"observations": len(rows), "dates": len(dates),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "tickers": len({r["ticker"] for r in rows if r.get("ticker")})}


def signature(data_dir: str | None = None) -> tuple:
    """Changes whenever any store is written — the cache key for anything built
    on the ledger. WAL sidecars count: a store in WAL mode can take writes without
    its main file's mtime moving."""
    sig = []
    for loop in LOOPS:
        base = store_path(loop, data_dir)
        for suffix in ("", "-wal"):
            try:
                st = os.stat(base + suffix)
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
    return tuple(sig)
