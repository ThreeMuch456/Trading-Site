"""Phase D of the flow predictiveness loop: auto-tune the directional metric.

Once a (cutoff, horizon) bucket clears FLOW_LEARN_MIN_SAMPLES labeled calls, learn
from the data — never from a hand-set weight — and persist a versioned tuning row
the report + EOD surface read:

  metric     — net_premium_skew vs score_weighted_skew, whichever correlates more
               strongly with the realized return (the better directional signal).
  threshold  — the smallest |metric| cut whose above-threshold hit-rate is highest
               (the magnitude past which a directional call is worth trusting).
  significant— a one-sided binomial z that the above-threshold hit-rate beats 50%.

This tunes WHICH metric and WHAT threshold the desk trusts — grounded in outcomes —
without reinterpreting the significance score (score_alert) as directional. Runs
after labeling; cheap (DB only, no Massive). Mirrors structure/tune.py's posture:
a knob earns its value only once the data shows separation.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from .learning import _db
from .report import MIN_SAMPLES, _pearson

TARGET_HIT = float(os.getenv("FLOW_TUNE_TARGET_HIT", "0.55"))   # confidence bar
MIN_ABOVE = int(os.getenv("FLOW_TUNE_MIN_ABOVE", "10"))          # rows past threshold
Z_BAR = float(os.getenv("FLOW_TUNE_Z_BAR", "1.64"))             # one-sided 95%

_METRICS = ("net_premium_skew", "score_weighted_skew")

_TUNE_COLS = (
    "cutoff", "horizon", "metric", "direction_sign", "threshold",
    "hit_rate_all", "hit_rate_above", "n_all", "n_above", "edge", "z",
    "significant", "updated_at",
)


def _hit(value: float, raw: float, sign: int) -> int | None:
    """1 if the metric (oriented by sign) called the move's direction, else 0.

    None when there's nothing to call: a flat metric (value 0) or a flat move."""
    if value == 0 or raw == 0:
        return None
    pred_up = (value * sign) > 0
    return 1 if (pred_up and raw > 0) or ((not pred_up) and raw < 0) else 0


def _best_threshold(abs_hits: list[tuple[float, int]]) -> tuple[float, float, int]:
    """(threshold, hit_rate_above, n_above): the smallest |metric| cut with the
    highest above-cut hit-rate and at least MIN_ABOVE rows. Falls back to cut 0."""
    cuts = [0.0] + sorted({a for a, _ in abs_hits})
    best = None
    for cut in cuts:
        sub = [h for a, h in abs_hits if a >= cut]
        if len(sub) < MIN_ABOVE:
            continue
        hr = sum(sub) / len(sub)
        # maximize hit-rate, then prefer the LOWER threshold (more coverage).
        cand = (hr, -cut)
        if best is None or cand > best[0]:
            best = (cand, cut, hr, len(sub))
    if best is None:                       # never enough rows past any cut
        if not abs_hits:
            return 0.0, 0.0, 0
        hits = [h for _, h in abs_hits]
        return 0.0, sum(hits) / len(hits), len(hits)
    _, cut, hr, n = best
    return cut, round(hr, 4), n


def _z(hit_rate: float, n: int) -> float | None:
    if n <= 0:
        return None
    k = hit_rate * n
    return round((k - 0.5 * n) / math.sqrt(0.25 * n), 3)


def _tune_bucket(cutoff: str, horizon: str, rows: list) -> dict | None:
    # Pick the metric that correlates more strongly with the realized return.
    best_metric = None
    best_pairs = None
    best_abs_corr = -1.0
    best_corr = 0.0
    for metric in _METRICS:
        pairs = [(r[metric], r["raw_return"]) for r in rows if r[metric] is not None]
        if len(pairs) < MIN_SAMPLES:
            continue
        corr = _pearson([p for p, _ in pairs], [r for _, r in pairs])
        if corr is None:
            continue
        if abs(corr) > best_abs_corr:
            best_abs_corr, best_corr, best_metric, best_pairs = abs(corr), corr, metric, pairs

    if best_metric is None:
        return None                        # not enough data on either metric yet

    sign = 1 if best_corr >= 0 else -1
    abs_hits = [(abs(v), h) for v, r in best_pairs
                if (h := _hit(v, r, sign)) is not None]
    if len(abs_hits) < MIN_SAMPLES:
        return None

    hit_all = round(sum(h for _, h in abs_hits) / len(abs_hits), 4)
    threshold, hit_above, n_above = _best_threshold(abs_hits)
    z = _z(hit_above, n_above)
    significant = 1 if (z is not None and z >= Z_BAR and hit_above >= TARGET_HIT) else 0

    return {
        "cutoff": cutoff, "horizon": horizon, "metric": best_metric,
        "direction_sign": sign, "threshold": round(threshold, 4),
        "hit_rate_all": round(100 * hit_all, 1),
        "hit_rate_above": round(100 * hit_above, 1),
        "n_all": len(abs_hits), "n_above": n_above,
        "edge": round(100 * hit_above - 50, 1), "z": z,
        "significant": significant,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def tune(db_path: str | None = None) -> int:
    """Recompute the tuning artifact for every ready bucket. Returns rows written."""
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.cutoff AS cutoff, o.horizon AS horizon,
                   s.net_premium_skew AS net_premium_skew,
                   s.score_weighted_skew AS score_weighted_skew,
                   o.raw_return AS raw_return
            FROM flow_outcomes o JOIN flow_signals s ON s.id = o.signal_id
            """
        ).fetchall()

        groups: dict[tuple[str, str], list] = {}
        for r in rows:
            groups.setdefault((r["cutoff"], r["horizon"]), []).append(r)

        written = 0
        for (cutoff, horizon), grp in groups.items():
            tuned = _tune_bucket(cutoff, horizon, grp)
            if tuned is None:
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO flow_tuning ({', '.join(_TUNE_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _TUNE_COLS)})",
                tuple(tuned[c] for c in _TUNE_COLS))
            written += 1
    return written


def active(db_path: str | None = None) -> dict[tuple[str, str], dict]:
    """The current tuning rows, keyed by (cutoff, horizon), for report/EOD to read."""
    with _db(db_path) as conn:
        return {(r["cutoff"], r["horizon"]): dict(r)
                for r in conn.execute("SELECT * FROM flow_tuning").fetchall()}
