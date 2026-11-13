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


def _collect(name: str):
    mod = _SOURCES[name]
    try:
        return True, mod.collect(config.SOURCES[name], config.FETCH_TIMEOUT)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _store(s: dict, ts: float) -> None:
    _cache["snapshot"] = s
    _cache["ts"] = ts
