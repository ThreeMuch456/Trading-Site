"""Phase C of the flow predictiveness loop: turn labeled signals into a report.

Reads flow_signals JOIN flow_outcomes and, per (cutoff, horizon) bucket, measures
how predictive the day's directional flow was:

  - hit_rate      — share of non-flat signals whose implied direction was right
  - avg_signed    — mean signed return (correct call = positive)
  - avg_abs_move  — mean |move|, the magnitude the bucket is calling
  - skew_corr     — Pearson corr of net premium skew vs realized raw return
  - sw_corr       — same for the score-weighted skew
  - terciles      — avg raw return in low/mid/high skew thirds (monotonic = signal)

Pure stdlib (no numpy/pandas on the flow path). A bucket is only called "ready"
once it clears min_samples labeled, non-flat rows — the same "earn it with
evidence" posture as structure's /learning. This is what the EOD surface renders
and the tuner (Phase D) consumes.
"""
from __future__ import annotations

import math
import os

from .learning import _db

MIN_SAMPLES = int(os.getenv("FLOW_LEARN_MIN_SAMPLES", "20"))

# Display/scan order.
_CUTOFF_ORDER = {"10:30": 0, "12:00": 1, "14:00": 2, "eod": 3}
_HORIZON_ORDER = {"to_close": 0, "open_close": 1, "next_day": 2}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / math.sqrt(sxx * syy), 4)


def _terciles(pairs: list[tuple[float, float]]) -> list[dict] | None:
    """pairs = (skew, raw_return); avg raw_return in low/mid/high skew thirds."""
    if len(pairs) < 9:
        return None
    pairs = sorted(pairs, key=lambda p: p[0])
    k = len(pairs) // 3
    thirds = [pairs[:k], pairs[k:2 * k], pairs[2 * k:]]
    out = []
    for label, grp in zip(("low", "mid", "high"), thirds):
        if not grp:
            continue
        rets = [r for _, r in grp]
        out.append({"bucket": label, "n": len(grp),
                    "skew_lo": round(grp[0][0], 4), "skew_hi": round(grp[-1][0], 4),
                    "avg_return": round(sum(rets) / len(rets), 4)})
    return out


def _bucket_stats(rows: list) -> dict:
    n_all = len(rows)
    directional = [r for r in rows if r["implied_dir"] in ("up", "down")]
    n_dir = len(directional)
    hit = (round(100.0 * sum(r["direction_correct"] for r in directional) / n_dir, 1)
           if n_dir else None)
    avg_signed = (round(sum(r["signed_return"] for r in directional) / n_dir, 4)
                  if n_dir else None)
    avg_abs = round(sum(abs(r["raw_return"]) for r in rows) / n_all, 4) if n_all else None

    skew = [(r["net_premium_skew"], r["raw_return"]) for r in rows
            if r["net_premium_skew"] is not None]
    sw = [(r["score_weighted_skew"], r["raw_return"]) for r in rows
          if r["score_weighted_skew"] is not None]

    return {
        "n": n_all,
        "n_directional": n_dir,
        "ready": n_dir >= MIN_SAMPLES,
        "hit_rate": hit,
        "avg_signed": avg_signed,
        "avg_abs_move": avg_abs,
        "skew_corr": _pearson([s for s, _ in skew], [r for _, r in skew]) if skew else None,
        "sw_corr": _pearson([s for s, _ in sw], [r for _, r in sw]) if sw else None,
        "terciles": _terciles(skew),
    }


def summary(db_path: str | None = None) -> dict:
    """Per-(cutoff, horizon) predictiveness, plus a one-line headline."""
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.cutoff AS cutoff, s.implied_dir AS implied_dir,
                   s.net_premium_skew AS net_premium_skew,
                   s.score_weighted_skew AS score_weighted_skew,
                   o.horizon AS horizon, o.raw_return AS raw_return,
                   o.signed_return AS signed_return,
                   o.direction_correct AS direction_correct
            FROM flow_outcomes o JOIN flow_signals s ON s.id = o.signal_id
            """
        ).fetchall()
        n_sessions = conn.execute(
            "SELECT COUNT(DISTINCT run_date) FROM flow_signals").fetchone()[0]

    groups: dict[tuple[str, str], list] = {}
    for r in rows:
        groups.setdefault((r["cutoff"], r["horizon"]), []).append(r)

    buckets = []
    for (cutoff, horizon), grp in groups.items():
        buckets.append({"cutoff": cutoff, "horizon": horizon, **_bucket_stats(grp)})
    buckets.sort(key=lambda b: (_HORIZON_ORDER.get(b["horizon"], 9),
                                _CUTOFF_ORDER.get(b["cutoff"], 9)))

    ready = [b for b in buckets if b["ready"] and b["hit_rate"] is not None]
    if ready:
        best = max(ready, key=lambda b: b["hit_rate"])
        headline = (f"Most predictive: {best['cutoff']} → {best['horizon']} "
                    f"{best['hit_rate']}% over {best['n_directional']} calls")
    else:
        headline = (f"Accumulating — {int(n_sessions)} session(s); need "
                    f"{MIN_SAMPLES} directional calls per bucket to rate predictiveness")

    return {
        "n_sessions": int(n_sessions),
        "min_samples": MIN_SAMPLES,
        "headline": headline,
        "buckets": buckets,
    }
