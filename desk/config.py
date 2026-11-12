"""Desk configuration — all env-driven, with safe defaults.

If no upstream URLs are set, the desk runs in **sample mode** (renders the mock
snapshot, exactly like Phase 0). Set a source's URL to make it live; each source
is independent and fails soft on its own.
"""

import os

TICKERS = [t.strip().upper() for t in os.environ.get("TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()]

FETCH_TIMEOUT = float(os.environ.get("DESK_FETCH_TIMEOUT", "4"))   # seconds per upstream call
CACHE_TTL = float(os.environ.get("DESK_CACHE_TTL", "60"))          # seconds to serve a composed snapshot

# One entry per upstream service the desk reads. `header` is how the token is sent
# (0DTE uses an X-App-Token header; the Morning Brief's JSON is public).
SOURCES = {
    "agent":   {"url": os.environ.get("AGENT_URL", "").rstrip("/"),
                "token": os.environ.get("AGENT_TOKEN", ""), "header": "Authorization"},
    "zerodte": {"url": os.environ.get("ZERO_DTE_URL", "").rstrip("/"),
                "token": os.environ.get("ZERO_DTE_TOKEN", ""), "header": "X-App-Token"},
    "flow":    {"url": os.environ.get("FLOW_URL", "").rstrip("/"),
                "token": os.environ.get("FLOW_TOKEN", ""), "header": "Authorization"},
}


def enabled(name: str) -> bool:
    return bool(SOURCES.get(name, {}).get("url"))


def any_live() -> bool:
    return any(s["url"] for s in SOURCES.values())
