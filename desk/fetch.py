"""Compose the desk snapshot from upstream sources — parallel, cached, fail-soft.

Strategy (BUILD-PLAN §4): start from the sample snapshot (full shape + fallback),
fetch each enabled source in parallel with a timeout, then merge successes onto
the base on the main thread. A failed/slow source leaves its section as sample
and is recorded in `s["degraded"]`, so the page always renders.

No upstream URLs configured → sample mode (identical to Phase 0).
"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from . import config
from . import sample_data
from .sources import agent, zerodte, flow, structure

# Page sources fetched in parallel. "stock" is not here — it's read only by the
# structure adapter (the quant column), never on its own.
_SOURCES = {"agent": agent, "zerodte": zerodte, "flow": flow, "structure": structure}
_cache = {"snapshot": None, "ts": 0.0}


def get_snapshot(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["snapshot"] is not None and now - _cache["ts"] < config.CACHE_TTL:
        return _cache["snapshot"]

    s = sample_data.snapshot()

    if not config.any_live():
        s["mock"], s["live"], s["degraded"] = True, False, {}
        _derive_overview_cards(s)
        _attach_scorecard(s)
        _store(s, now)
        return s

    s["mock"], s["live"] = False, True
    active = {name: mod for name, mod in _SOURCES.items() if config.enabled(name)}

    # Overall per-source deadline so one hung upstream can't block the page. The
    # structure source fans out to several quant calls, so give some headroom.
    deadline = max(config.FETCH_TIMEOUT * 4, 15)
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(active))) as ex:
        futures = {name: ex.submit(_collect, name) for name in active}
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=deadline)
            except FutureTimeout:
                results[name] = (False, "timeout (source too slow)")

    degraded = {}
    for name, mod in active.items():
        ok, payload = results[name]
        if not ok:
            degraded[name] = payload
            continue
        try:
            mod.merge(s, payload)
        except Exception as exc:  # a mapping bug must not 500 the page
            degraded[name] = f"merge error: {type(exc).__name__}: {exc}"

    _derive_overview_cards(s)
    s["degraded"] = degraded
    _record_learning(s, degraded)
    _attach_scorecard(s)
    _store(s, now)
    return s


_REGIME_COLOR = {"pin": "blue", "trend": "amber", "transition": "purple"}


def _derive_overview_cards(s: dict) -> None:
    """Recompute the Overview snapshot cards from the live composed snapshot, so the
    home page summarizes real data rather than sample text. Flow/Structure cards are
    filled by their own adapters (which hold the raw numbers); this covers the rest."""
    try:
        cards = {c.get("href"): c for c in s.get("overview_cards", [])}
        tickers = s.get("tickers", [])
        by = {t["sym"]: t for t in tickers}

        def regime_badge(t):
            col = _REGIME_COLOR.get(t.get("regime_class"), "purple")
            return (t.get("regime", "—"), f"var(--{col}-dim)", f"var(--{col})")

        g = cards.get("gameplan")
        if g and tickers:
            obs = [t.get("observed") for t in tickers if isinstance(t.get("observed"), (int, float))]
            obs_avg = round(sum(obs) / len(obs)) if obs else None
            st = []
            for sym in ("SPY", "QQQ"):
                if by.get(sym):
                    st.append({"l": f"{sym} regime", "badge": regime_badge(by[sym])})
            st.append({"l": "Observed", "v": f"{obs_avg}%" if obs_avg is not None else "—",
                       "v_class": "up" if (obs_avg or 0) >= 60 else "flat"})
            g["stats"] = st

        it = cards.get("intraday")
        if it:
            intr = s.get("intraday", {}) or {}
            skew = intr.get("skew", []) or []
            calls = sum(1 for k in skew if (k.get("call_pct") or 0) >= 50)
            lean = "Calls" if (skew and calls > len(skew) / 2) else ("Puts" if skew else "—")
            it["stats"] = [
                {"l": "Plan status", "v": "Tracking", "v_class": "up"},
                {"l": "Touches today", "v": str(len(intr.get("touches", []) or [])), "v_class": ""},
                {"l": "Flow lean", "v": lean, "v_class": "up" if lean == "Calls" else ("down" if lean == "Puts" else "flat")},
            ]

        e = cards.get("eod")
        if e:
            eod = s.get("eod", {}) or {}
            grades = eod.get("grades", []) or []
            correct = sum(1 for x in grades if x.get("correct") == "Yes")
            total = sum(1 for x in grades if x.get("correct") in ("Yes", "No"))
            grade = ("Good" if correct >= total - correct else "Mixed") if total else "—"
            e["stats"] = [
                {"l": "Plan grade", "v": grade, "v_class": "up" if grade == "Good" else "flat"},
                {"l": "Next risk", "badge": (s.get("context", {}).get("risk_rating", "—"),
                                             "var(--surface2)", "var(--muted)")},
                {"l": "Carried", "v": str(len(eod.get("watchlist", []) or [])), "v_class": ""},
            ]
    except Exception:
        pass   # the home cards are cosmetic — never break the page over them


def _record_learning(s: dict, degraded: dict) -> None:
    """Log the live composed signals to the desk journal, ingest any graded
    outcomes (only when the brief source was live), and append the desk's own
    Track Record items. Fully guarded — learning never breaks a page."""
    try:
        from . import store
        store.record_snapshot(s)
        grades = (s.get("eod") or {}).get("grades")
        if grades and "agent" not in degraded:
            store.ingest_grades(grades)
        tr = s.setdefault("eod", {}).setdefault("track_record", {"lines": []})
        tr["lines"] = list(tr.get("lines") or []) + store.track_record_items()
        hist = store.history()
        if hist:
            s["eod"]["history"] = hist
    except Exception:
        pass


def _attach_scorecard(s: dict) -> None:
    """Make the shared learning scorecard the desk's one account of what has been
    learned. Each source adapter still appends its own Track Record line (a win
    rate, a recent-sessions accuracy, a per-label hold rate) — computed three
    different ways and none with an error bar. When the scorecard has history,
    its digest REPLACES those lines, and the Structure page's learned strip shows
    edge over benchmark instead of raw win rate. With no history (a fresh or
    sample-mode desk) everything is left as the adapters wrote it. Fully guarded."""
    try:
        from . import learning_view
        lines = learning_view.digest()
        if not lines:
            return
        s.setdefault("eod", {})["track_record"] = {"lines": lines, "scorecard": True}
        tiles = learning_view.structure_tiles()
        if tiles:
            s.setdefault("structure", {})["scorecard"] = tiles
            # Each rec carries the learned edge of its own kind of setup.
            for r in s["structure"].get("recs") or []:
                r["edge"] = tiles["edges"].get(learning_view.rec_setup(r)) or []
        # Intraday: tested flow readings instead of per-label hold rates, which
        # could only read 100% or 0% (the label is assigned after the outcome).
        levels = learning_view.zerodte_panel()
        if levels:
            s.setdefault("intraday", {})["level_record"] = levels
        _apply_brief_feedback(s, learning_view)
    except Exception:
        pass


def _apply_brief_feedback(s: dict, learning_view) -> None:
    """Apply the Brief's own history to today's Brief, in code.

    Gameplan: each ticker gets `learned` — the calibrated probability its call
    verifies (or "no call" for neutral), which the card shows in place of the
    model's self-reported conviction. EOD: neutral rows are marked "No call" and
    left out of the day's tally while neutral has not beaten its base rate, and
    the performance tiles become full-history figures with verdicts."""
    learned = learning_view.brief_calls(
        [{"sym": t.get("sym"), "bias": t.get("bias"), "conviction": t.get("conviction")}
         for t in s.get("tickers", [])])
    if not learned:
        return
    for t in s.get("tickers", []):
        if t.get("sym") in learned:
            t["learned"] = learned[t["sym"]]

    eod = s.setdefault("eod", {})
    grades = eod.get("grades") or []
    if learning_view.neutral_is_a_call() is False:
        for g in grades:
            if str(g.get("bias", "")).lower() in ("neutral", "cautious"):
                g["correct"], g["correct_class"] = "No call", "nocall"
    right = sum(1 for g in grades if g.get("correct") == "Yes")
    judged = sum(1 for g in grades if g.get("correct") in ("Yes", "No"))
    if not s.get("mock") and any(g.get("correct") == "No call" for g in grades):
        # The adapter's one-line read counted the neutral rows; restate it without them.
        from .sources.agent import _eod_summary
        eod["summary_html"] = _eod_summary(right, judged)
    today = {"label": "Today", "value": f"{right} / {judged}" if judged else "—",
             "value_class": "up" if (judged and right == judged) else "",
             "sub": "directional calls verified"}
    tiles = learning_view.brief_tiles(today)
    if tiles:
        eod["performance"] = tiles
        eod["performance_scorecard"] = True


def _collect(name: str):
    mod = _SOURCES[name]
    try:
        return True, mod.collect(config.SOURCES[name], config.FETCH_TIMEOUT)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _store(s: dict, ts: float) -> None:
    _cache["snapshot"] = s
    _cache["ts"] = ts
