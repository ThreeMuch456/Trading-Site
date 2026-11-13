"""Desk (compose-layer) configuration.

Unified single-deploy model: the desk no longer reads six sibling services over
HTTP — every source is now produced in-process by a vendored engine (see the
``engines`` package). A source is "live" when its engine is registered AND its
prerequisites are met (chiefly ``MASSIVE_API_KEY``). With nothing available the
desk renders sample data, exactly as the old no-URLs path did.

The ``SOURCES`` dict keeps its original shape (``url`` / ``token`` / ``header``) so
the source adapters and the whole test-suite are unchanged; ``url`` is just a
``local://<name>`` sentinel the transport shim routes to the engine instead of a
network address. Tests still override ``SOURCES[name]["url"]`` directly.
"""

import os

TICKERS = [t.strip().upper() for t in os.environ.get("TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()]

FETCH_TIMEOUT = float(os.environ.get("DESK_FETCH_TIMEOUT", "4"))   # seconds per source produce()
CACHE_TTL = float(os.environ.get("DESK_CACHE_TTL", "60"))          # seconds to serve a composed snapshot

# Which engines can produce right now (empty until engines are ported/available).
# Guarded so a missing/broken engines package simply yields sample mode.
try:
    import engines as _engines
    _AVAILABLE = _engines.available_sources()
except Exception:
    _AVAILABLE = set()

# Header stays per-source for adapter compatibility; it is unused in-process (the
# engine router ignores token/header) but a few adapters still read cfg["header"].
_HEADERS = {"agent": "X-App-Token", "zerodte": "X-App-Token", "flow": "Authorization",
            "structure": "Authorization", "stock": "Authorization"}

SOURCES = {
    name: {"url": f"local://{name}" if name in _AVAILABLE else "", "token": "", "header": hdr}
    for name, hdr in _HEADERS.items()
}


def enabled(name: str) -> bool:
    return bool(SOURCES.get(name, {}).get("url"))


# Sources that render a page/section. "stock" is excluded — it only enriches the
# Structure recs table's quant column, so it must never flip the site out of sample
# mode on its own (that would show live banners over otherwise-sample pages).
PAGE_SOURCES = ("agent", "zerodte", "flow", "structure")


def any_live() -> bool:
    return any(SOURCES[name]["url"] for name in PAGE_SOURCES)


def refresh() -> None:
    """Recompute source availability from the engines registry (e.g. after a key
    is set at runtime, or in a test). Cheap; safe to call on each composition."""
    global _AVAILABLE
    try:
        import engines as eng
        _AVAILABLE = eng.available_sources()
    except Exception:
        _AVAILABLE = set()
    for name in SOURCES:
        SOURCES[name]["url"] = f"local://{name}" if name in _AVAILABLE else ""
