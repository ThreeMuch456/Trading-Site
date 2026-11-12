"""Compose the desk snapshot from upstream sources — parallel, cached, fail-soft.

Strategy (BUILD-PLAN §4): start from the sample snapshot (full shape + fallback),
fetch each enabled source in parallel with a timeout, then merge successes onto
the base on the main thread. A failed/slow source leaves its section as sample
and is recorded in `s["degraded"]`, so the page always renders.

No upstream URLs configured → sample mode (identical to Phase 0).
"""

import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from . import sample_data
from .sources import agent, zerodte, flow

_SOURCES = {"agent": agent, "zerodte": zerodte, "flow": flow}
_cache = {"snapshot": None, "ts": 0.0}


def get_snapshot(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["snapshot"] is not None and now - _cache["ts"] < config.CACHE_TTL:
        return _cache["snapshot"]

    s = sample_data.snapshot()

    if not config.any_live():
        s["mock"], s["live"], s["degraded"] = True, False, {}
        _store(s, now)
        return s

    s["mock"], s["live"] = False, True
    active = {name: mod for name, mod in _SOURCES.items() if config.enabled(name)}

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(active))) as ex:
        futures = {name: ex.submit(_collect, name) for name in active}
        for name, fut in futures.items():
            results[name] = fut.result()

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

    s["degraded"] = degraded
    _store(s, now)
    return s


def _collect(name: str):
    mod = _SOURCES[name]
    try:
        return True, mod.collect(config.SOURCES[name], config.FETCH_TIMEOUT)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _store(s: dict, ts: float) -> None:
    _cache["snapshot"] = s
    _cache["ts"] = ts
