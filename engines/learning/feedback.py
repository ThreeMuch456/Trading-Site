"""Deterministic feedback: what a loop's history licenses saying about a NEW signal.

The scorecard judges the past. This module turns those verdicts into numbers and
sentences that are applied to today's signals — in code, the same way every time,
instead of asking a model to "reduce conviction where the track record is poor".

One rule governs all of it:

    A distinction is used only when the scorecard has judged it real.

If bullish calls have a 61% hit rate and bearish calls 55%, but the scorecard
says neither cohort differs from zero edge, then today's bullish call does NOT
get 61%: it gets the pooled rate for all directional calls. Only a cohort judged
`paying` or `inverted` earns its own number, and even then its rate is shrunk
toward the pooled one in proportion to how little history it has.

That keeps the feedback from chasing noise — which is what feeding raw
per-bucket accuracies back into a prompt does.
"""
from __future__ import annotations

from . import scorecard as _scorecard
from . import stats as st

# Pseudo-observations pulling a cohort's own hit rate toward the pooled rate.
PRIOR_STRENGTH = 20
_REAL = (st.PAYING, st.INVERTED)

POOLED = "all directional calls"


def _loop(card: dict, name: str) -> dict:
    return next((lp for lp in card.get("loops") or [] if lp.get("loop") == name), {})


def _cohorts(loop: dict, family: str) -> dict:
    fam = next((f for f in loop.get("families") or [] if f.get("family") == family), {})
    return {c["cohort"]: c for c in fam.get("cohorts") or []}


def _rate(p: float, n: int, basis: str, own: bool, verdict: str | None) -> dict:
    lo, hi = st.wilson(round(p * n), n) if n else (None, None)
    return {"p": round(p, 4), "low": None if lo is None else round(lo, 4),
            "high": None if hi is None else round(hi, 4),
            "n": n, "basis": basis, "own": own, "verdict": verdict}


def brief(card: dict | None = None) -> dict:
    """Everything the Brief's history says, reduced to what may be applied.

    `by_bias` / `by_ticker` / `by_gamma` each map a cohort to the hit rate a new
    call of that kind should carry: its own (shrunk) rate when the scorecard has
    judged the cohort real, the pooled directional rate otherwise."""
    card = card or _scorecard.cached()
    loop = _loop(card, "brief")
    direction = _cohorts(loop, "direction")
    pooled = direction.get(POOLED) or {}
    graded = pooled.get("graded") or 0
    if not graded:
        return {"available": False}
    p0 = pooled["hits"] / graded

    def rate(name: str) -> dict:
        c = direction.get(name) or {}
        n = c.get("graded") or 0
        if c.get("verdict") in _REAL and n:
            p = (c["hits"] + PRIOR_STRENGTH * p0) / (n + PRIOR_STRENGTH)
            return _rate(p, n, name, True, c["verdict"])
        return _rate(p0, graded, POOLED, False, pooled.get("verdict"))

    names = [n for n in direction if n != POOLED]
    neutral = _cohorts(loop, "range").get("neutral") or {}
    conv = next(iter(_cohorts(loop, "conviction").values()), {})
    return {
        "available": True,
        "sessions": (loop.get("coverage") or {}).get("dates") or 0,
        "directional": {
            "hit_rate": pooled.get("hit_rate"), "hits": pooled.get("hits"), "graded": graded,
            "edge": pooled.get("effect"), "ci_low": pooled.get("ci_low"),
            "ci_high": pooled.get("ci_high"), "verdict": pooled.get("verdict"),
            **_rate(p0, graded, POOLED, False, pooled.get("verdict")),
        },
        "by_bias": {n: rate(n) for n in ("bullish", "bearish")},
        "by_ticker": {n: rate(n) for n in names if n.isupper()},
        "by_gamma": {n.split()[0]: rate(n) for n in names if n.endswith(" gamma")},
        "real": [{"cohort": n, "verdict": direction[n]["verdict"], "edge": direction[n].get("effect"),
                  "hit_rate": direction[n].get("hit_rate"), "graded": direction[n].get("graded")}
                 for n in names if direction[n].get("verdict") in _REAL],
        "neutral": {
            "hit_rate": neutral.get("hit_rate"), "base_rate": neutral.get("base_rate"),
            "graded": neutral.get("graded") or 0, "verdict": neutral.get("verdict"),
            # Neutral is a forecast worth making only once it has beaten its base rate.
            "informative": neutral.get("verdict") == st.PAYING,
        },
        "conviction": {
            "gap": conv.get("effect"), "verdict": conv.get("verdict"),
            "brier": conv.get("brier"), "threshold": conv.get("threshold"),
            "informative": conv.get("verdict") == st.PAYING,
        },
    }


def brief_call(fb: dict, bias: str, ticker: str | None = None) -> dict:
    """The feedback for one new call.

    Directional: the calibrated probability it verifies, from the most specific
    cohort the scorecard has judged real (ticker, then bias), else the pooled rate.
    Neutral / cautious: a no-call unless neutral calls have earned `paying`."""
    if not fb.get("available"):
        return {"kind": "unavailable"}
    bias = (bias or "").strip().lower()
    if bias not in ("bullish", "bearish"):
        if fb["neutral"]["informative"]:
            return {"kind": "range", "p": (fb["neutral"]["hit_rate"] or 0) / 100.0,
                    "n": fb["neutral"]["graded"], "basis": "neutral", "own": True}
        return {"kind": "no_call", **fb["neutral"]}
    own = [r for r in (fb["by_ticker"].get((ticker or "").upper()), fb["by_bias"].get(bias))
           if r and r["own"]]
    chosen = max(own, key=lambda r: r["n"]) if own else fb["directional"]
    return {"kind": "directional", **{k: chosen[k] for k in
                                      ("p", "low", "high", "n", "basis", "own", "verdict")}}


def brief_prompt_block(fb: dict, fomc_today: bool = False) -> str:
    """The HISTORICAL PERFORMANCE CONTEXT block for the Brief's prompt.

    Same header the system prompt already keys on. Where the old block listed raw
    30-session accuracies per bucket and warned on any that dipped below a fixed
    line, this states the full-history verdicts and tells the model only what
    they support. Empty string when there is no graded history."""
    if not fb.get("available"):
        return ""
    d, neu, conv = fb["directional"], fb["neutral"], fb["conviction"]
    pct = lambda p: f"{100 * p:.0f}%"  # noqa: E731
    lines = [
        "=== HISTORICAL PERFORMANCE CONTEXT ===",
        f"Full graded history: {fb['sessions']} sessions. Every figure below was judged with "
        "errors clustered by date and corrected for the number of comparisons; a split is "
        "REAL only where marked.",
        f"Directional calls (bullish or bearish): {d['hits']}/{d['graded']} verified "
        f"({d['hit_rate']:.1f}%). Timing edge {d['edge']:+.2f}% per call after removing market "
        f"drift (90% interval {d['ci_low']:+.2f} to {d['ci_high']:+.2f}) — verdict: "
        f"{str(d['verdict']).upper()}.",
    ]
    if fb["real"]:
        for r in fb["real"]:
            word = ("has reliably ADDED edge" if r["verdict"] == st.PAYING
                    else "has been reliably WRONG")
            lines.append(f"REAL: '{r['cohort']}' {word} ({r['edge']:+.2f}% per call, "
                         f"{r['hit_rate']:.0f}% verified over {r['graded']} graded).")
    else:
        lines.append("No bias, ticker or gamma-regime split differs reliably from the rest. "
                     "Do not raise or lower conviction for any of them.")
    if neu.get("graded"):
        if neu["informative"]:
            lines.append(f"Neutral calls have verified {neu['hit_rate']:.0f}% against a "
                         f"{neu['base_rate']:.0f}% base rate — REAL: neutral is earning its place.")
        else:
            lines.append(f"Neutral calls: verified {neu['hit_rate']:.0f}% over {neu['graded']} graded, "
                         f"against {neu['base_rate']:.0f}% of ALL days staying inside the neutral band. "
                         "Neutral has carried no information. The desk now treats neutral as "
                         "'no call'.")
    if conv.get("gap") is not None:
        brier = f" Brier {conv['brier']:.3f} (0.250 = always saying 50/50)." if conv.get("brier") is not None else ""
        if conv["informative"]:
            lines.append(f"Stated conviction is informative: higher-conviction calls verified "
                         f"{conv['gap']:+.1f} points more often — REAL.{brier}")
        else:
            lines.append(f"Stated conviction has NOT been informative: higher- vs lower-conviction "
                         f"calls differ by {conv['gap']:+.1f} points, not distinguishable from zero.{brier}")
    lines.append("CALIBRATION NOTES (apply directly to today's output):")
    notes = [f"A directional call has verified {pct(d['p'])} of the time. Set conviction near "
             f"{d['p']:.2f} by default and move off it only for confluence you can name."]
    for r in fb["real"]:
        if r["verdict"] == st.INVERTED:
            notes.append(f"'{r['cohort']}' is REAL and negative: avoid it or state it at conviction below 0.50.")
        else:
            notes.append(f"'{r['cohort']}' is REAL and positive: conviction up to "
                         f"{min(0.75, (r['hit_rate'] or 50) / 100):.2f} is supported there.")
    if neu.get("graded") and not neu["informative"]:
        notes.append("Prefer a directional call at honest conviction over neutral. Use neutral "
                     "only when you expect a genuinely tight range AND can name the pin level.")
    if fomc_today:
        notes.append("TODAY IS FOMC — binary event risk makes directional bias unreliable; "
                     "raise overall_risk_rating and widen scenarios")
    lines += [f"  • {n}" for n in notes]
    lines.append("=== END HISTORICAL CONTEXT ===")
    return "\n".join(lines)
