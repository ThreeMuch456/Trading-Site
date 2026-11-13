"""
Global / overnight market recap — extends step 1 of the trading agent.

Pulls the major non-US equity indices that trade ahead of the US session
(Asia closes overnight, Europe is open into the US pre-market) so the brief
can frame the US open against global risk sentiment. Uses yfinance (free).
"""

import yfinance as yf
from datetime import datetime
import json

# Ordered roughly by local close time, Asia → Europe — i.e. the sequence the
# tape prints leading up to the US cash open.
GLOBAL_INDICES = [
    {"symbol": "^N225",     "name": "Nikkei 225",    "country": "Japan",       "region": "Asia"},
    {"symbol": "^KS11",     "name": "KOSPI",         "country": "South Korea", "region": "Asia"},
    {"symbol": "^TWII",     "name": "TAIEX",         "country": "Taiwan",      "region": "Asia"},
    {"symbol": "000001.SS", "name": "Shanghai Comp", "country": "China",       "region": "Asia"},
    {"symbol": "^HSI",      "name": "Hang Seng",     "country": "Hong Kong",   "region": "Asia"},
    {"symbol": "^GDAXI",    "name": "DAX",           "country": "Germany",     "region": "Europe"},
    {"symbol": "^FCHI",     "name": "CAC 40",        "country": "France",      "region": "Europe"},
    {"symbol": "^FTSE",     "name": "FTSE 100",      "country": "UK",          "region": "Europe"},
    {"symbol": "^STOXX50E", "name": "Euro Stoxx 50", "country": "Eurozone",    "region": "Europe"},
]

_META_KEYS = ("symbol", "name", "country", "region")


def fetch_index_snapshot(meta: dict) -> dict:
    """Pull last close + day change for one global index."""
    t = yf.Ticker(meta["symbol"])
    # 5-day window tolerates local holidays where a market was closed.
    hist = t.history(period="5d", interval="1d")
    # Drop NaN closes — yfinance often returns a placeholder row for the current
    # in-progress session with no close yet, which would otherwise emit invalid
    # `NaN` JSON and break the dashboard.
    closes = hist["Close"].dropna()

    last = float(closes.iloc[-1]) if not closes.empty else None
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    change_pct = round((last - prev) / prev * 100, 2) if last and prev else None
    as_of = closes.index[-1].date().isoformat() if not closes.empty else None

    return {
        **{k: meta[k] for k in _META_KEYS},
        "last": round(last, 2) if last else None,
        "change_pct": change_pct,
        "as_of": as_of,  # date of the most recent close (their session, not ours)
    }


def fetch_global_markets(indices: list[dict] | None = None) -> dict:
    indices = indices or GLOBAL_INDICES
    results = []
    for meta in indices:
        try:
            results.append(fetch_index_snapshot(meta))
        except Exception as e:
            results.append({**{k: meta[k] for k in _META_KEYS}, "error": str(e)})
    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "indices": results,
    }


if __name__ == "__main__":
    print(json.dumps(fetch_global_markets(), indent=2))
