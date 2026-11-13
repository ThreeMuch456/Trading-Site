"""Calibration + tuning *recommendations* (recommend-only, human-approved).

Reads the labeled outcomes accumulated by oplog and asks the one question that
matters for tuning: **does a higher score actually predict a better outcome?**
For each (type, horizon) it median-splits opportunities into a low-score and a
high-score half and compares them on EXCESS return (net of the benchmark) whenever
the group has enough benchmark-backed rows, falling back to win rate otherwise.

Why excess and not win rate: for zones "win" means "price touched the LVN", and the
2026-08-26 read showed that can be high while the setup loses money (support hit
its pocket 67% of the time and still trailed SPY). A score that ranks touches well
can rank payoffs backwards, and the win-rate verdict can't see it.

It then emits advisory notes — e.g. "support score isn't discriminating, candidate
to down-weight". It is strictly read-only: it proposes, it never changes
config.yaml or the live screener. A human reviews and applies.

Everything is gated on `learn.min_label_samples`: until a (type, horizon) group has
that many labeled outcomes, it reports "insufficient data" rather than tuning on
noise. Designed to be wired into the daily job (best-effort) so a fresh proposal
regenerates automatically once the history is deep enough.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import Config
from .oplog import _db, clustered_gap_se, flow_splits

# Margins below which a score is judged "not discriminating" (high half ~ low half).
_WIN_MARGIN = 5.0      # percentage points
_RET_MARGIN = 0.10     # percent signed return
_EXC_MARGIN = 0.25     # percentage points of excess return between the halves
# A date-clustered SE from 3-4 clusters is itself too noisy to trust (it put
# t = -3.9 on a 3-date flow split), so flow verdicts wait for this many dates.
_MIN_FLOW_DATES = 5


def _group_rows(cfg: Config) -> dict[tuple[str, int], list[dict]]:
    """All labeled outcomes joined to their opportunity score, keyed by (type, horizon)."""
    groups: dict[tuple[str, int], list[dict]] = {}
    with _db(cfg) as conn:
        rows = conn.execute(
            """
            SELECT o.type AS type, oc.horizon_days AS h, o.score AS score,
                   oc.win AS win, oc.signed_return AS signed_return,
                   oc.excess_return AS excess_return,
                   oc.reached_next_hvn AS far_wall,
                   o.ticker AS ticker, o.run_date AS run_date
            FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
            """
        ).fetchall()
    for r in rows:
        groups.setdefault((r["type"], int(r["h"])), []).append(dict(r))
    return groups


def _live_bucket_rows(cfg: Config) -> dict[tuple[int, bool], list[dict]]:
    """Labeled outcomes split by whether the /live monitor ever confirmed the
    setup that day, keyed by (horizon, confirmed). Opportunities with NO live
    reading at all (monitor off, or the ticker wasn't in that day's top-N) are
    excluded entirely — this compares confirmed vs NOT confirmed, not confirmed
    vs unknown, which would understate any real effect.

    Zones only: w_flag has been 0 since 2026-08-12, so flag rows no longer move
    the rank — and pooled in, they were ~70% of the sample and decided the
    verdict for setups the dashboard doesn't rank on."""
    groups: dict[tuple[int, bool], list[dict]] = {}
    try:
        with _db(cfg) as conn:
            rows = conn.execute(
                """
                SELECT oc.horizon_days AS h, oc.win AS win, oc.signed_return AS signed_return,
                       oc.excess_return AS excess_return, o.run_date AS run_date,
                       json_extract(o.features, '$.live_ever_activated') AS confirmed
                FROM outcomes oc JOIN opportunities o ON o.id = oc.opp_id
                WHERE json_extract(o.features, '$.live_ever_activated') IS NOT NULL
                  AND o.type IN ('support', 'resistance')
                """
            ).fetchall()
    except Exception:  # noqa: BLE001 - old SQLite w/o json_extract, or no rows yet
        return groups
    for r in rows:
        groups.setdefault((int(r["h"]), bool(r["confirmed"])), []).append(dict(r))
    return groups


def _split_stats(rows: list[dict]) -> dict:
    """Median-split by score; compare win-rate and mean signed return across halves."""
    scores = np.array([r["score"] for r in rows], dtype=float)
    wins   = np.array([r["win"] for r in rows], dtype=float)
    rets   = np.array([r["signed_return"] for r in rows], dtype=float)

    med = float(np.median(scores))
    hi = scores >= med
    lo = ~hi
    # Guard degenerate splits (all equal scores) — fall back to no discrimination.
    if hi.sum() == 0 or lo.sum() == 0:
        hi = lo = np.ones_like(scores, dtype=bool)

    def winp(mask): return 100.0 * float(wins[mask].mean()) if mask.any() else 0.0
    def avgr(mask): return float(rets[mask].mean()) if mask.any() else 0.0

    dates = [r.get("run_date") for r in rows]
    out = {
        "n": int(len(rows)),
        # Rows are NOT independent draws. Everything detected on one date shares
        # that date's market move (on 2026-09-11 nearly every up-flag lost at
        # once), so the count of distinct detection dates is the honest
        # denominator. (This used to count ticker-days, but within one
        # (type, horizon) a ticker-day emits at most one row, so it always equalled
        # n and looked like reassurance while measuring nothing.)
        "n_dates": len(set(dates)),
        "win_pct": round(winp(np.ones_like(scores, dtype=bool)), 1),
        "avg_signed_return": round(avgr(np.ones_like(scores, dtype=bool)), 3),
        "low_win_pct": round(winp(lo), 1),
        "high_win_pct": round(winp(hi), 1),
        "low_avg_return": round(avgr(lo), 3),
        "high_avg_return": round(avgr(hi), 3),
    }

    # Excess return (net of the benchmark) — the payoff axis the verdict is judged
    # on. NaN where a row has no benchmark leg; masks stay aligned with the split.
    exc = np.array([np.nan if r.get("excess_return") is None else r["excess_return"]
                    for r in rows], dtype=float)
    has = np.isfinite(exc)
    out["n_excess"] = int(has.sum())
    if (has & lo).any() and (has & hi).any():
        lo_exc = float(exc[has & lo].mean())
        hi_exc = float(exc[has & hi].mean())
        gap = hi_exc - lo_exc
        se = clustered_gap_se(exc, hi, dates)
        out.update({
            "low_avg_excess": round(lo_exc, 3),
            "high_avg_excess": round(hi_exc, 3),
            "excess_gap": round(gap, 3),
            "excess_gap_se": None if se is None else round(se, 3),
            "excess_gap_t": None if not se else round(gap / se, 2),
            "n_excess_dates": len({d for d, ok in zip(dates, has) if ok}),
        })

    # Far-wall traversal, for zones: the win label ("price touched the LVN") is
    # partly tautological, because closeness-to-price is the single heaviest term
    # in the zone score (score.w_closeness) and a nearer target is mechanically
    # easier to touch. Whether the move crossed the whole pocket to the far wall
    # is not rewarded by the score, so it is the independent check on the split.
    # Keep the array the SAME LENGTH as rows (NaN where unlabeled) so the lo/hi
    # score masks still line up. The previous version filtered the Nones out and
    # then required `far.size == len(rows)`, which meant a SINGLE null blanked the
    # column for the whole group — and flag rows are null by construction (no far
    # wall), so in practice the check never fired anywhere on the page. That
    # silently disabled the only non-tautological validation of the score.
    far = np.array([np.nan if r.get("far_wall") is None else r["far_wall"] for r in rows],
                   dtype=float)
    lo_far, hi_far = far[lo], far[hi]
    if np.isfinite(lo_far).any() and np.isfinite(hi_far).any():
        out["low_far_pct"]  = round(100.0 * float(np.nanmean(lo_far)), 1)
        out["high_far_pct"] = round(100.0 * float(np.nanmean(hi_far)), 1)
        # How much of the group actually carries a far-wall label, so a split
        # resting on a handful of rows can be discounted on sight.
        out["n_far"] = int(np.isfinite(far).sum())
    return out


def recommend(cfg: Config) -> dict:
    """Build a recommend-only tuning proposal from the labeled history."""
    min_n = int(cfg.get("learn.min_label_samples", 50))
    groups = _group_rows(cfg)
    generated_at = datetime.now(timezone.utc).isoformat()

    if not groups:
        return {"generated_at": generated_at, "status": "no_data",
                "min_label_samples": min_n, "groups": [], "notes": [
                    "No labeled outcomes yet — recommendations begin once horizons elapse."]}

    group_reports: list[dict] = []
    notes: list[str] = []
    ready = False

    for (typ, horizon), rows in sorted(groups.items()):
        if len(rows) < min_n:
            group_reports.append({"type": typ, "horizon": horizon, "n": len(rows),
                                  "status": "insufficient_data"})
            continue

        ready = True
        s = _split_stats(rows)
        win_gap = s["high_win_pct"] - s["low_win_pct"]
        ret_gap = s["high_avg_return"] - s["low_avg_return"]

        # Judge on excess return once enough rows carry a benchmark leg. The
        # win-rate logic below is the fallback for groups (or older histories)
        # that don't have one yet.
        on_excess = s["n_excess"] >= min_n and "excess_gap" in s
        if on_excess:
            exc_gap = s["excess_gap"]
            inverted = exc_gap < -_EXC_MARGIN
            discriminating = exc_gap > _EXC_MARGIN
            verdict = ("score_inverted" if inverted else
                       "score_discriminates" if discriminating else
                       "score_not_discriminating")
            group_reports.append({"type": typ, "horizon": horizon, **s,
                                  "win_gap": round(win_gap, 1), "return_gap": round(ret_gap, 3),
                                  "basis": "excess", "status": verdict})
            t = s.get("excess_gap_t")
            noise = (f"gap {exc_gap:+.2f}pp ± {s['excess_gap_se']:.2f}, t={t:+.1f} over "
                     f"{s['n_excess_dates']} detection dates" if t is not None
                     else f"gap {exc_gap:+.2f}pp, too few dates for an error bar")
            weak = "" if t is not None and abs(t) >= 2 else " Within noise — watch it, don't act on it yet."
            if inverted:
                # Only call out the touch-vs-payoff split when win rate actually
                # moved the other way; if both fall, it's just a bad score.
                split = (" The score is ranking setups that reach their target, not "
                         "ones that pay." if win_gap > 0 else "")
                notes.append(
                    f"{typ}@{horizon}d: on excess return the score is INVERTED — the "
                    f"high-scoring half returns {s['high_avg_excess']:+.2f}% vs "
                    f"{s['low_avg_excess']:+.2f}% for the low half ({noise}), while win "
                    f"rate goes {s['low_win_pct']} → {s['high_win_pct']}%.{split}{weak}")
            elif not discriminating:
                notes.append(
                    f"{typ}@{horizon}d: higher scores don't return more on excess "
                    f"({s['high_avg_excess']:+.2f}% vs {s['low_avg_excess']:+.2f}%, {noise}). "
                    f"Candidate to re-weight or tighten the {typ} thresholds.")
            if s["win_pct"] < 50.0 and typ in ("support", "resistance"):
                notes.append(
                    f"{typ}@{horizon}d: only {s['win_pct']}% reach the gap (n={s['n']}); "
                    f"consider a stricter clearance/distance gate.")
            continue

        # An INVERTED score (high half wins materially LESS) is its own verdict,
        # and the most actionable one there is — you can zero or flip that weight.
        # It used to hide inside "not discriminating", or, worse, be reported as
        # "discriminates": the old rule OR'd the two gaps, so a +0.1pp return gap
        # could outvote a -10.6pp win-rate inversion (flag@5d did exactly that,
        # while its near-identical flag@10d twin was called non-discriminating).
        # Returns may still carry a verdict on their own, but they can no longer
        # overrule a contradicting win rate.
        # ...and a win rate that merely LEANS the wrong way still can't be talked
        # out of by returns. The 2026-08-12 fix only held outside the +/-5pp band:
        # inside it `inverted` is False, so a lone ret_gap > 0.10 could still buy a
        # "discriminates" verdict over a negative win_gap. flag@5d did exactly that
        # on 2026-08-26 — reported as "score discriminates" while its high-scoring
        # half won 47.2% vs the low half's 50.1%. A negative win gap now caps the
        # verdict at "not discriminating" whatever the returns say.
        inverted = win_gap < -_WIN_MARGIN
        discriminating = (win_gap > _WIN_MARGIN
                          or (ret_gap > _RET_MARGIN and win_gap >= 0))

        verdict = ("score_inverted" if inverted else
                   "score_discriminates" if discriminating else
                   "score_not_discriminating")
        group_reports.append({"type": typ, "horizon": horizon, **s,
                              "win_gap": round(win_gap, 1), "return_gap": round(ret_gap, 3),
                              "basis": "win", "status": verdict})

        if inverted:
            notes.append(
                f"{typ}@{horizon}d: score is INVERTED — the high-scoring half wins "
                f"{abs(win_gap):.1f}pp LESS ({s['high_win_pct']}% vs {s['low_win_pct']}%, "
                f"n={s['n']}). Candidate to zero the {typ} weight rather than re-tune it.")
        elif not discriminating:
            notes.append(
                f"{typ}@{horizon}d: higher scores aren't winning more "
                f"(high {s['high_win_pct']}% vs low {s['low_win_pct']}%, n={s['n']}). "
                f"Candidate to re-weight or tighten the {typ} thresholds.")
        if s["win_pct"] < 50.0 and typ in ("support", "resistance"):
            notes.append(
                f"{typ}@{horizon}d: only {s['win_pct']}% reach the gap (n={s['n']}); "
                f"consider a stricter clearance/distance gate.")

        # Zone scores can "discriminate" purely by ranking nearer targets higher —
        # touching a closer LVN is easier, and closeness is the heaviest weight in
        # the score. Far-wall traversal isn't rewarded by the score, so when the
        # two disagree the win-rate verdict is the suspect one.
        far_gap = (s["high_far_pct"] - s["low_far_pct"]
                   if "high_far_pct" in s and typ in ("support", "resistance") else None)
        if far_gap is not None and discriminating and far_gap <= 0:
            notes.append(
                f"{typ}@{horizon}d: the score separates gap-touches "
                f"(+{win_gap:.1f}pp) but NOT full traversals to the far wall "
                f"({far_gap:+.1f}pp, {s['high_far_pct']}% vs {s['low_far_pct']}%, "
                f"n={s.get('n_far', s['n'])} with a far-wall label). "
                f"Touching a nearer LVN is easier by construction — "
                f"treat the win-rate edge here as partly an artifact of "
                f"score.w_closeness, not confirmation.")

    # Does live intraday option-flow confirmation (src/live.py) actually predict
    # better outcomes? Same min_n gate, compared per horizon: confirmed vs NOT
    # confirmed (rows with no live reading at all are excluded, not lumped in).
    live_reports: list[dict] = []
    live_groups = _live_bucket_rows(cfg)
    for horizon in sorted({h for h, _ in live_groups}):
        confirmed   = live_groups.get((horizon, True), [])
        unconfirmed = live_groups.get((horizon, False), [])
        if len(confirmed) < min_n or len(unconfirmed) < min_n:
            live_reports.append({"horizon": horizon, "n_confirmed": len(confirmed),
                                 "n_unconfirmed": len(unconfirmed), "status": "insufficient_data"})
            continue

        ready = True
        c_win = 100.0 * float(np.mean([r["win"] for r in confirmed]))
        u_win = 100.0 * float(np.mean([r["win"] for r in unconfirmed]))
        c_ret = float(np.mean([r["signed_return"] for r in confirmed]))
        u_ret = float(np.mean([r["signed_return"] for r in unconfirmed]))
        win_gap = c_win - u_win
        report = {
            "horizon": horizon, "n_confirmed": len(confirmed), "n_unconfirmed": len(unconfirmed),
            "confirmed_win_pct": round(c_win, 1), "unconfirmed_win_pct": round(u_win, 1),
            "confirmed_avg_return": round(c_ret, 3), "unconfirmed_avg_return": round(u_ret, 3),
            "win_gap": round(win_gap, 1),
        }

        # Same rule as the score split: judge on excess return once both sides
        # carry enough benchmark legs. On 2026-09-18 confirmed support@10d won 82%
        # and returned -1.03% net of SPY — a win-rate lift is not a payoff.
        both = confirmed + unconfirmed
        exc = np.array([np.nan if r["excess_return"] is None else r["excess_return"]
                        for r in both], dtype=float)
        is_c = np.array([True] * len(confirmed) + [False] * len(unconfirmed))
        has = np.isfinite(exc)
        if (has & is_c).sum() >= min_n and (has & ~is_c).sum() >= min_n:
            c_exc, u_exc = float(exc[has & is_c].mean()), float(exc[has & ~is_c].mean())
            gap = c_exc - u_exc
            se = clustered_gap_se(exc, is_c, [r["run_date"] for r in both])
            helps = gap > _EXC_MARGIN
            report.update({
                "basis": "excess",
                "confirmed_avg_excess": round(c_exc, 3), "unconfirmed_avg_excess": round(u_exc, 3),
                "excess_gap": round(gap, 3),
                "excess_gap_se": None if se is None else round(se, 3),
                "excess_gap_t": None if not se else round(gap / se, 2),
            })
            evidence = (f"excess {c_exc:+.2f}% vs {u_exc:+.2f}%"
                        + (f", t={gap / se:+.1f}" if se else ""))
        else:
            helps = win_gap > _WIN_MARGIN
            report["basis"] = "win"
            evidence = f"win {c_win:.1f}% vs {u_win:.1f}%"
        report["status"] = "live_confirmation_helps" if helps else "no_clear_effect"
        live_reports.append(report)
        if helps:
            # Live confirmation is only known intraday, after the detection-time
            # score is fixed, so it can't be a Combined weight — it's a /live re-rank.
            notes.append(
                f"zones @ {horizon}d: live-confirmed setups beat unconfirmed ones "
                f"({evidence}; n={len(confirmed)}/{len(unconfirmed)}). It's known only "
                f"intraday, so it belongs as a /live re-rank, not a Combined weight.")

    # Options-flow features (src/flow.py) are logged, never scored. Each is
    # judged the way a weight would be: does the top tercile beat the bottom on
    # excess return, by more than the date-clustered noise? Same min_n gate.
    # Only a |t| >= 2 read gets a note — with a dozen features × types ×
    # horizons, a few 1.5-sigma gaps are expected from noise alone.
    flow_reports: list[dict] = []
    try:
        with _db(cfg) as conn:
            splits = flow_splits(conn)
    except Exception:  # noqa: BLE001 - advisory section; never fail the report
        splits = []
    for sp in splits:
        rep = {k: sp.get(k) for k in ("feature", "type", "horizon", "n", "n_dates",
                                      "gap", "gap_se", "gap_t")}
        b = {x["bucket"]: x for x in sp["buckets"]}
        rep["low_avg_excess"] = round(b["low"]["avg_excess"], 3) if "low" in b else None
        rep["high_avg_excess"] = round(b["high"]["avg_excess"], 3) if "high" in b else None
        if sp["n"] < min_n or sp.get("gap") is None:
            rep["status"] = "insufficient_data"
            flow_reports.append(rep)
            continue
        t = sp.get("gap_t")
        if (sp.get("n_dates") or 0) < _MIN_FLOW_DATES:
            rep["status"] = "too_few_dates"
        elif t is None:
            rep["status"] = "no_error_bar"
        elif t >= 2:
            rep["status"] = "high_pays"
        elif t <= -2:
            rep["status"] = "high_underperforms"
        else:
            rep["status"] = "within_noise"
        flow_reports.append(rep)
        if rep["status"] in ("high_pays", "high_underperforms"):
            ready = True
            what = ("the top tercile out-earns the bottom" if t > 0
                    else "the top tercile UNDER-earns the bottom")
            act = ("If it holds as dates accrue, a candidate for a score term or filter."
                   if t > 0 else
                   "Do not add it as a positive weight; if it persists, it's a fade/filter "
                   "candidate.")
            notes.append(
                f"flow {sp['feature']} on {sp['type']}@{sp['horizon']}d: {what} on excess "
                f"({rep['low_avg_excess']:+.2f}% → {rep['high_avg_excess']:+.2f}%, gap "
                f"{sp['gap']:+.2f}pp ± {sp['gap_se']:.2f}, t={t:+.1f}, {sp['n_dates']} dates, "
                f"n={sp['n']}). Logged-only today. {act}")

    if not notes and ready:
        notes.append("Scores look reasonably calibrated — no changes recommended.")

    return {"generated_at": generated_at,
            "status": "ready" if ready else "insufficient_data",
            "min_label_samples": min_n, "groups": group_reports,
            "live": live_reports, "flow": flow_reports, "notes": notes}


def format_report(rec: dict) -> str:
    lines = [f"Tuning recommendation  ({rec['status']}, generated {rec['generated_at'][:19]}Z)",
             f"min labeled samples per group: {rec['min_label_samples']}", ""]
    # "dates" is the un-inflated denominator: everything detected on one date
    # shares that date's market move, so n overstates the independent evidence.
    header = (f"{'type':<11}{'horizon':>8}{'n':>7}{'dates':>7}{'win%':>8}"
              f"{'low→high win%':>16}{'low→high excess%':>20}{'t':>7}  verdict")
    lines.append(header)
    lines.append("-" * len(header))
    for g in rec["groups"]:
        days = g.get("n_dates", "—")
        if g.get("status") == "insufficient_data":
            lines.append(f"{g['type']:<11}{g['horizon']:>8}{g['n']:>7}{days:>7}{'—':>8}"
                         f"{'(need more)':>16}")
        else:
            exc = (f"{g['low_avg_excess']:+.2f}→{g['high_avg_excess']:+.2f}"
                   if "excess_gap" in g else "—")
            t = g.get("excess_gap_t")
            lines.append(f"{g['type']:<11}{g['horizon']:>8}{g['n']:>7}{days:>7}"
                         f"{g['win_pct']:>7.1f}%"
                         f"{g['low_win_pct']:>7.1f}→{g['high_win_pct']:<7.1f}"
                         f"{exc:>20}{('—' if t is None else f'{t:+.1f}'):>7}"
                         f"  {g['status']} (on {g.get('basis', 'win')})")
    if rec.get("live"):
        lines.append("")
        lines.append("Live confirmation (src/live.py) — confirmed vs NOT confirmed that day:")
        lhead = f"{'horizon':>8}{'confirmed n':>13}{'unconf. n':>11}{'confirmed win%':>16}{'unconf. win%':>14}"
        lines.append(lhead)
        lines.append("-" * len(lhead))
        for g in rec["live"]:
            if g.get("status") == "insufficient_data":
                lines.append(f"{g['horizon']:>8}{g['n_confirmed']:>13}{g['n_unconfirmed']:>11}"
                             f"{'(need more)':>16}{'':>14}")
            else:
                lines.append(f"{g['horizon']:>8}{g['n_confirmed']:>13}{g['n_unconfirmed']:>11}"
                             f"{g['confirmed_win_pct']:>15.1f}%{g['unconfirmed_win_pct']:>13.1f}%")

    judged = [g for g in rec.get("flow", []) if g.get("status") != "insufficient_data"]
    if judged:
        lines.append("")
        lines.append("Options-flow features (logged only) — top vs bottom tercile on excess:")
        fhead = (f"{'feature':<32}{'type':<11}{'horizon':>8}{'n':>6}{'dates':>7}"
                 f"{'low→high excess%':>20}{'t':>7}  verdict")
        lines.append(fhead)
        lines.append("-" * len(fhead))
        for g in judged:
            t = g.get("gap_t")
            exc = f"{g['low_avg_excess']:+.2f}→{g['high_avg_excess']:+.2f}"
            lines.append(f"{g['feature']:<32}{g['type']:<11}{g['horizon']:>8}{g['n']:>6}"
                         f"{g.get('n_dates') or '—':>7}{exc:>20}"
                         f"{('—' if t is None else f'{t:+.1f}'):>7}  {g['status']}")

    lines.append("")
    lines.append("Notes (advisory — review, then apply by hand in config.yaml):")
    for n in rec["notes"]:
        lines.append(f"  • {n}")
    return "\n".join(lines)


def write_report(cfg: Config, rec: dict | None = None) -> Path:
    """Persist the latest recommendation next to the log (best-effort, for the daily job)."""
    rec = rec or recommend(cfg)
    out = Path(cfg.get("data.cache_dir", "data")) / "tuning_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2))
    return out
