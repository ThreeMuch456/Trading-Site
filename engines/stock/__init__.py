"""Stock-Analysis engine (vendored) — the deterministic quant scorecard.

Reproduces the former ``GET /api/analyze?ticker=…&ai=0`` endpoint in-process. The
desk consumes only ``scorecard.overall`` (the Structure recs table's Quant column),
but the full payload is produced so the contract is complete and reusable.

Data source is yfinance (not Massive) — fundamentals, price history, analysts, news,
related instruments — exactly as the standalone tool. The AI narrative half
(``claude_analysis``) is reachable with ``ai=1`` when ANTHROPIC_API_KEY is set, but the
desk always calls with ``ai=0`` so the compose path stays fast and free.

Availability is gated on MASSIVE_API_KEY purely so a keyless checkout stays in sample
mode (this source is enrichment-only and is excluded from the desk's any_live() page
check, so enabling it never flips the site live on its own).
"""

import threading
import time

from .. import route, serve
from .. import config
from .analyze_stock import run_analysis

# yfinance calls are slow (~1-2s/ticker) and the Structure fan-out requests up to 6
# per composition; a short per-symbol TTL keeps repeated/near-simultaneous asks cheap.
_TTL_S = 600.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def produce_analyze(params: dict) -> dict:
    """Return the full analyze payload for params['ticker']. ai=0 (default here)
    skips the Claude narrative. Raises ValueError on an unknown ticker (the caller
    — the Structure quant map — treats any failure as a blank cell)."""
    sym = (params.get("ticker") or "").strip().upper()
    if not sym:
        raise ValueError("missing ticker")
    include_ai = params.get("ai", "0") not in ("0", "", "false", "no")

    if not include_ai:
        now = time.time()
        with _lock:
            hit = _cache.get(sym)
            if hit and now - hit[0] < _TTL_S:
                return hit[1]

    data = run_analysis(sym, include_ai=include_ai)

    if not include_ai:
        with _lock:
            _cache[sym] = (time.time(), data)
    return data


route("/api/analyze")(produce_analyze)
serve("stock", config.has_massive)
