"""The scorecard: every loop's cohorts, judged by one method, in one vocabulary.

Built on the ledger (what happened) and the stats module (how sure we are). Each
loop contributes one or more *families* — a set of related questions asked of the
same data. Verdicts are corrected for the number of tests within a family, so a
family is the unit to read: "of these six setup types, which pay?".

The first family of each loop is its headline: the question the loop exists to
answer. Later families are diagnostics (does the score rank? does conviction
mean anything?) that the feedback phases act on.

Output is plain JSON-able dicts; see `build`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import ledger
from . import stats as st


def _pct(x) -> float | None:
    return None if x is None else round(100.0 * x, 1)


def _hits(rows: list) -> dict:
    """Descriptive hit rate (with a naive Wilson interval) for the graded rows."""
    graded = [r["hit"] for r in rows if r.get("hit") is not None]
    if not graded:
        return {}
    k = sum(graded)
    lo, hi = st.wilson(k, len(graded))
    return {"hit_rate": _pct(k / len(graded)), "hits": k, "graded": len(graded),
            "hit_ci": [_pct(lo), _pct(hi)]}


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _family(key: str, question: str, unit: str, items: list, note: str | None = None) -> dict:
    """items: [(cohort name, Effect, extras dict)]. Judges the family as a whole."""
    verdicts = st.judge([e for _, e, _ in items])
    cohorts = []
    for (name, e, extra), (verdict, q) in zip(items, verdicts):
        cohort = {"cohort": name, **e.as_dict(),
                  "q": None if q is None else round(q, 6),
                  "verdict": verdict, **extra}
        need = st.shortfall(e)
        if need:
            cohort["needs"] = need
        cohorts.append(cohort)
    out = {"family": key, "question": question, "unit": unit,
           "tests": sum(1 for _, q in verdicts if q is not None), "cohorts": cohorts}
    if note:
        out["note"] = note
    return out


def _dates(rows: list) -> list:
    return [r["date"] for r in rows]


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def _brief(rows: list) -> dict:
    directional = [r for r in rows if r["edge"] is not None]

    def timing(name: str, sub: list):
        e = st.mean_effect([r["edge"] for r in sub], _dates(sub))
        return (name, e, {**_hits(sub),
                          "avg_called_move": _mean(r["direction"] * r["ret"] for r in sub)})

    direction = [timing("all directional calls", directional),
                 timing("bullish", [r for r in directional if r["direction"] > 0]),
                 timing("bearish", [r for r in directional if r["direction"] < 0])]
    for tk in sorted({r["ticker"] for r in directional}):
        direction.append(timing(tk, [r for r in directional if r["ticker"] == tk]))
    for g in sorted({r["gamma"] for r in directional if r["gamma"]}):
        direction.append(timing(f"{g} gamma", [r for r in directional if r["gamma"] == g]))

    # Range calls: a neutral call is graded right when the day stays inside a band.
    # The bar it has to clear is how often ANY day stays inside that band.
    def range_call(bias: str, band_key: str):
        banded = [r[band_key] for r in rows if r[band_key] is not None]
        base = sum(banded) / len(banded) if banded else None
        sub = [r for r in rows if r["bias"] == bias and r["hit"] is not None]
        vals = [100.0 * (r["hit"] - base) for r in sub] if base is not None else []
        return (bias, st.mean_effect(vals, _dates(sub)),
                {**_hits(sub), "base_rate": _pct(base)})

    ranged = [range_call("neutral", "in_neutral_band"),
              range_call("cautious", "in_cautious_band")]

    conv = [r for r in rows if r["hit"] is not None and r["conviction"] is not None]
    thr, mask = st.median_split([r["conviction"] for r in conv])
    conviction = [("high minus low conviction",
                   st.gap_effect([100.0 * r["hit"] for r in conv], mask, _dates(conv)),
                   {"threshold": thr, "n_high": sum(mask), "n_low": len(mask) - sum(mask),
                    "brier": (lambda b: None if b is None else round(b, 3))(
                        st.brier([r["conviction"] for r in conv], [r["hit"] for r in conv]))})]

    return {
        "loop": "brief", "label": "Morning Brief", "store": ledger.STORES["brief"],
        "observation": "a bias call on one index ETF, graded at that day's close",
        "coverage": ledger.coverage(rows),
        "families": [
            _family("direction", "Do bullish and bearish calls time the day's move?",
                    "% move in the called direction, net of the ticker's average day",
                    direction,
                    "Drift is removed, so a call scores for timing only — not for "
                    "being long an index that rose over the sample."),
            _family("range", "Do neutral and cautious calls pick out quiet days?",
                    "percentage points above the rate at which any day stays in the band",
                    ranged),
            _family("conviction", "Are high-conviction calls right more often than low?",
                    "percentage-point gap in hit rate", conviction,
                    "Brier 0.25 is what always saying 50/50 scores."),
        ],
    }


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def _structure(rows: list) -> dict:
    calendar = sorted({r["date"] for r in rows})
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["type"], r["horizon"]), []).append(r)
    keys = sorted(groups, key=lambda k: (k[0], k[1]))
    no_bench = sum(1 for r in rows if r["edge"] is None)

    def scored(sub: list) -> list:
        return [r for r in sub if r["edge"] is not None]

    setup, score, features = [], [], []
    for typ, h in keys:
        sub = scored(groups[(typ, h)])
        name = f"{typ} · {h}d"
        dates, edges = _dates(sub), [r["edge"] for r in sub]
        setup.append((name, st.mean_effect(edges, dates, overlap=h - 1, order=calendar),
                      {**_hits(sub), "avg_signed_return": _mean(r["signed_return"] for r in sub)}))

        thr, mask = st.median_split([r["score"] for r in sub])
        score.append((name, st.gap_effect(edges, mask, dates, overlap=h - 1, order=calendar),
                      {"threshold": thr, "n_high": sum(mask), "n_low": len(mask) - sum(mask)}))

        for feat in ledger.STRUCTURE_FLOW_FEATURES:
            have = [r for r in sub if r["features"].get(feat) is not None]
            thr, mask = st.median_split([r["features"][feat] for r in have])
            features.append((f"{feat} · {name}",
                             st.gap_effect([r["edge"] for r in have], mask, _dates(have),
                                           overlap=h - 1, order=calendar),
                             {"feature": feat, "setup": name, "threshold": thr,
                              "n_high": sum(mask), "n_low": len(mask) - sum(mask)}))

    return {
        "loop": "structure", "label": "Structure setups", "store": ledger.STORES["structure"],
        "observation": "a thin-zone or flag setup, labeled 5 and 10 trading days on",
        "coverage": ledger.coverage(rows),
        "families": [
            _family("setup", "Does the setup beat its benchmark over the horizon?",
                    "% return net of the benchmark, favourable = positive", setup,
                    "Win rate is shown but not judged: a setup can win most of the "
                    "time and still trail the benchmark."
                    + (f" {no_bench} labeled rows have no benchmark and are left out."
                       if no_bench else "")),
            _family("score", "Do higher-scored setups do better than lower-scored ones?",
                    "% excess return, top half by score minus bottom half", score),
            _family("flow_feature",
                    "Does an options-flow feature separate good setups from bad?",
                    "% excess return, top half by the feature minus bottom half", features,
                    "Logged-only features. With this many tests, a few will look "
                    "strong by chance; verdicts are corrected for that."),
        ],
    }


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------

_CUTOFFS = {"10:30": 0, "12:00": 1, "14:00": 2, "eod": 3}
_HORIZONS = {"to_close": 0, "open_close": 1, "next_day": 2}


def _flow(rows: list) -> dict:
    groups: dict = {}
    for r in rows:
        if r["edge"] is not None:
            groups.setdefault((r["horizon"], r["cutoff"]), []).append(r)
    items = []
    for horizon, cutoff in sorted(groups, key=lambda k: (_HORIZONS.get(k[0], 9),
                                                         _CUTOFFS.get(k[1], 9))):
        sub = groups[(horizon, cutoff)]
        items.append((f"{cutoff} → {horizon}",
                      st.mean_effect([r["edge"] for r in sub], _dates(sub)), _hits(sub)))
    return {
        "loop": "flow", "label": "Index ETF flow", "store": ledger.STORES["flow"],
        "observation": "a day's call/put flow lean on one index ETF at a cutoff",
        "coverage": ledger.coverage(rows),
        "families": [
            _family("direction", "Does the day's flow lean precede the ETF's move?",
                    "% move in the implied direction, net of the ticker's average move",
                    items, "Flat leans are not calls and are left out."),
        ],
    }


# ---------------------------------------------------------------------------
# 0DTE
# ---------------------------------------------------------------------------

def _zerodte(rows: list) -> dict:
    """Only RELATIVE questions are judged here. The absolute hold rate has no
    zero-information baseline: the engine calls a level broken after a few
    one-minute closes just past a narrow band, but held only after a clean single
    touch that stays away for the whole re-entry window (engine/touches.py). Those
    are not mirror-image events, so "held less than half the time" says as much
    about the definitions as about the level. It is reported, not judged."""
    resolved = [r for r in rows if r["hit"] is not None]
    held = sum(r["hit"] for r in resolved)
    churn = len(rows) - len(resolved)
    hold = [100.0 * r["hit"] for r in resolved]
    dates = _dates(resolved)

    def versus(name: str, mask: list, lo_name: str):
        return (name, st.gap_effect(hold, mask, dates),
                {"n_high": sum(mask), "n_low": len(mask) - sum(mask),
                 "hit_rate": _hits([r for r, m in zip(resolved, mask) if m]).get("hit_rate"),
                 "other_hit_rate": _hits([r for r, m in zip(resolved, mask) if not m]).get("hit_rate"),
                 "versus": lo_name})

    separates = []
    kinds = sorted({r["kind"] for r in resolved if r["kind"]})
    if len(kinds) == 2:
        separates.append(versus(f"{kinds[0]} minus {kinds[1]}",
                                [r["kind"] == kinds[0] for r in resolved], kinds[1]))
    tickers = sorted({r["ticker"] for r in resolved})
    if len(tickers) > 1:
        separates += [versus(f"{tk} minus the other tickers",
                             [r["ticker"] == tk for r in resolved], "other tickers")
                      for tk in tickers]
    for k in kinds:
        sub = [r for r in resolved if r["kind"] == k and r["wall_size"] is not None]
        thr, mask = st.median_split([r["wall_size"] for r in sub])
        separates.append((f"bigger minus smaller walls · {k}",
                          st.gap_effect([100.0 * r["hit"] for r in sub], mask, _dates(sub)),
                          {"threshold": thr, "n_high": sum(mask),
                           "n_low": len(mask) - sum(mask), "versus": "smaller walls"}))

    # The readings the DEFENDED / SLIPPED labels were built from, tested directly.
    # Each is fixed while price is inside the band, before the outcome resolves.
    def sign_split(name: str, key: str, hi_name: str, lo_name: str, hi_positive: bool,
                   reading: str):
        sub = [r for r in resolved if r[key]]                 # drop missing and exact zero
        mask = [(r[key] > 0) == hi_positive for r in sub]
        return (name, st.gap_effect([100.0 * r["hit"] for r in sub], mask, _dates(sub)),
                {"n_high": sum(mask), "n_low": len(mask) - sum(mask),
                 "hit_rate": _hits([r for r, m in zip(sub, mask) if m]).get("hit_rate"),
                 "other_hit_rate": _hits([r for r, m in zip(sub, mask) if not m]).get("hit_rate"),
                 "reading": reading, "high": hi_name, "versus": lo_name})

    def median_gap(name: str, key: str, reading: str):
        sub = [r for r in resolved if r[key] is not None]
        thr, mask = st.median_split([r[key] for r in sub])
        return (name, st.gap_effect([100.0 * r["hit"] for r in sub], mask, _dates(sub)),
                {"threshold": thr, "n_high": sum(mask), "n_low": len(mask) - sum(mask),
                 "hit_rate": _hits([r for r, m in zip(sub, mask) if m]).get("hit_rate"),
                 "other_hit_rate": _hits([r for r, m in zip(sub, mask) if not m]).get("hit_rate"),
                 "reading": reading, "high": "higher half", "versus": "lower half"})

    during = [
        median_gap("more minus less buy-aggressive option volume", "aggression",
                   "Buy-aggressive option volume"),
        median_gap("heavier minus lighter volume at the level", "rel_volume",
                   "Volume at the level vs normal"),
        sign_split("customers net selling minus net buying", "net_customer",
                   "net selling", "net buying", False, "Customer option flow"),
        sign_split("wall grew minus wall shrank", "wall_change",
                   "wall grew", "wall shrank", True, "Dealer gamma at the level"),
    ]
    low_conf = sum(1 for r in resolved if r["low_conf"])

    return {
        "loop": "zerodte", "label": "0DTE levels", "store": ledger.STORES["zerodte"],
        "observation": "a touch of a 0DTE gamma level, resolved the same session",
        "coverage": ledger.coverage(rows),
        "descriptive": {
            "touches": len(rows), "held": held, "broke": len(resolved) - held,
            "churned": churn,
            "hold_rate": _pct(held / len(resolved)) if resolved else None,
            "churn_share": _pct(churn / len(rows)) if rows else None,
            "note": "Share of resolved touches that held. Not judged: held and broke "
                    "are defined asymmetrically, so there is no coin-flip baseline.",
        },
        "families": [
            _family("separates",
                    "Does anything known before the touch tell levels that hold "
                    "from levels that break?",
                    "percentage-point gap in hold rate", separates,
                    "The touch's own DEFENDED / SLIPPED label is not used: it is "
                    "assigned after the outcome and always agrees with it."),
            _family("during_touch",
                    "Does option flow while price is at the level tell holds from breaks?",
                    "percentage-point gap in hold rate", during,
                    "These are the readings the DEFENDED / SLIPPED labels are built from, "
                    "tested directly; each is fixed before the outcome resolves. "
                    + (f"{low_conf:,} of {len(resolved):,} resolved touches have "
                       "low-confidence flow (inferred from snapshot changes, not prints). "
                       if resolved else "")
                    + "Time inside the band and excursion are left out: a long, deep "
                      "touch is already breaking."),
        ],
    }


# ---------------------------------------------------------------------------
# Watchlist (whale alerts)
# ---------------------------------------------------------------------------

# horizon label -> (cohort name, sessions held = dates of overlap between entries)
_WATCH_HORIZONS = {"0d": ("to that session's close", 0), "1d": ("next session", 1),
                   "3d": ("3 sessions on", 3), "5d": ("5 sessions on", 5)}


def _watch(rows: list) -> dict:
    calendar = sorted({r["date"] for r in rows})
    direction, score = [], []
    for h, (name, overlap) in _WATCH_HORIZONS.items():
        sub = [r for r in rows if r["horizon"] == h and r["edge"] is not None]
        if not sub:
            continue
        dates, edges = _dates(sub), [r["edge"] for r in sub]
        direction.append((name, st.mean_effect(edges, dates, overlap=overlap, order=calendar),
                          {**_hits(sub), "avg_option_pnl": _mean(r["option_pnl"] for r in sub)}))
        have = [r for r in sub if r["score"] is not None]
        thr, mask = st.median_split([r["score"] for r in have])
        score.append((name, st.gap_effect([r["edge"] for r in have], mask, _dates(have),
                                          overlap=overlap, order=calendar),
                      {"threshold": thr, "n_high": sum(mask), "n_low": len(mask) - sum(mask)}))
    return {
        "loop": "watch", "label": "Whale alerts", "store": ledger.STORES["watch"],
        "observation": "an auto-watched whale alert, followed from entry for up to 5 sessions",
        "coverage": ledger.coverage(rows),
        "families": [
            _family("direction", "Does the underlying move the way the alert pointed?",
                    "% move in the alert's direction, net of the average watched name",
                    direction,
                    "Hit rate is the share that moved the alert's way at all. The option's "
                    "own P&L is recorded but not judged: time decay gives it no "
                    "zero-information baseline."),
            _family("score", "Do higher-scored alerts do better than lower-scored ones?",
                    "% directional move, top half by alert score minus bottom half", score,
                    "This is the first check the alert score has ever had against an "
                    "outcome; its weights should not be tuned until a horizon is judged."),
        ],
    }


_BUILDERS = {"brief": _brief, "structure": _structure, "flow": _flow, "zerodte": _zerodte,
             "watch": _watch}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _tally(cohorts: list) -> dict:
    out = {v: 0 for v in st.VERDICTS}
    for c in cohorts:
        out[c["verdict"]] += 1
    return out


def build(data_dir: str | None = None) -> dict:
    """The full scorecard. Each loop is built independently; one unreadable store
    yields an empty loop, not a failed scorecard."""
    loops = []
    for name in ledger.LOOPS:
        try:
            loop = _BUILDERS[name](ledger.observations(name, data_dir))
        except Exception as exc:  # noqa: BLE001
            ledger.log.exception("learning scorecard: %s failed", name)
            loop = {"loop": name, "store": ledger.STORES[name], "families": [],
                    "coverage": ledger.coverage([]), "error": f"{type(exc).__name__}: {exc}"}
        fams = loop["families"]
        loop["headline"] = _tally(fams[0]["cohorts"]) if fams else _tally([])
        loop["verdicts"] = _tally([c for f in fams for c in f["cohorts"]])
        loops.append(loop)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vocabulary": st.VERDICTS,
        "method": {
            "min_observations": st.MIN_N, "min_dates": st.MIN_CLUSTERS,
            "min_independent_windows": st.MIN_EFF_CLUSTERS,
            "false_discovery_rate": st.FDR, "confidence_level": st.CI_LEVEL,
            "error_bars": "clustered by date; overlapping horizons corrected "
                          "(Hansen-Hodrick); q = Benjamini-Hochberg within a family",
        },
        "loops": loops,
    }


def summary(card: dict) -> dict:
    """The scorecard in a few lines per loop: how much history, and how the
    headline cohorts are judged. Small enough for /healthz."""
    return {lp["loop"]: {"dates": lp["coverage"]["dates"],
                         "observations": lp["coverage"]["observations"],
                         "last": lp["coverage"]["last"],
                         "headline": lp["headline"],
                         **({"error": lp["error"]} if lp.get("error") else {})}
            for lp in card["loops"]}


_cache: dict = {"sig": None, "dir": None, "card": None, "at": 0.0}
MIN_REBUILD_S = 300.0


def cached(data_dir: str | None = None, max_age: float = MIN_REBUILD_S) -> dict:
    """`build`, recomputed only when a store has been written since the last call
    — and at most once every `max_age` seconds, because the 0DTE store is written
    continuously through the session and outcomes only change the picture daily."""
    now = time.monotonic()
    fresh = _cache["card"] is not None and _cache["dir"] == data_dir
    if fresh and now - _cache["at"] < max_age:
        return _cache["card"]
    sig = ledger.signature(data_dir)
    if not fresh or _cache["sig"] != sig:
        _cache.update({"sig": sig, "dir": data_dir, "card": build(data_dir)})
    _cache["at"] = now
    return _cache["card"]
