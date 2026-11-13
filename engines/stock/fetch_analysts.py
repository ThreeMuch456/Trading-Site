"""
Analyst coverage for a single ticker via yfinance.

Pulls the buy/hold/sell distribution, price-target range, recent rating changes,
and derives a consensus label + upside vs the current price. Everything is
best-effort — yfinance analyst endpoints are sparse for smaller names.
"""

import yfinance as yf

# Weighted scoring for the consensus label (1 = most bullish, 5 = most bearish)
_RATING_WEIGHTS = {
    "strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5,
}


def _consensus_label(dist: dict) -> tuple[str, float | None]:
    total = sum(dist.values())
    if not total:
        return ("No coverage", None)
    score = sum(_RATING_WEIGHTS[k] * v for k, v in dist.items()) / total
    if score <= 1.5:
        label = "Strong Buy"
    elif score <= 2.5:
        label = "Buy"
    elif score <= 3.5:
        label = "Hold"
    elif score <= 4.5:
        label = "Sell"
    else:
        label = "Strong Sell"
    return (label, round(score, 2))


def _distribution(t: yf.Ticker) -> dict:
    dist = {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}
    try:
        summ = t.recommendations_summary
        if summ is not None and not summ.empty:
            # Use the most recent period (period == '0m')
            row = summ.iloc[0]
            for k in dist:
                if k in row and row[k] == row[k]:  # not NaN
                    dist[k] = int(row[k])
    except Exception:
        pass
    return dist


def _price_targets(t: yf.Ticker, current: float | None) -> dict:
    out = {"low": None, "mean": None, "median": None, "high": None,
           "num_analysts": None, "upside_pct": None}
    try:
        pt = t.analyst_price_targets  # dict in recent yfinance
        if isinstance(pt, dict):
            out["low"] = pt.get("low")
            out["mean"] = pt.get("mean")
            out["median"] = pt.get("median")
            out["high"] = pt.get("high")
            cur = pt.get("current") or current
            if out["mean"] and cur:
                out["upside_pct"] = round((out["mean"] - cur) / cur * 100, 2)
    except Exception:
        pass
    return out


def _recent_changes(t: yf.Ticker, limit: int = 6) -> list[dict]:
    rows = []
    try:
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            ud = ud.sort_index(ascending=False).head(limit)
            for idx, r in ud.iterrows():
                rows.append({
                    "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                    "firm": r.get("Firm"),
                    "from_grade": r.get("FromGrade") or "",
                    "to_grade": r.get("ToGrade") or "",
                    "action": (r.get("Action") or "").lower(),
                })
    except Exception:
        pass
    return rows


def fetch_analysts(symbol: str, current_price: float | None = None) -> dict:
    t = yf.Ticker(symbol.upper())
    dist = _distribution(t)
    label, score = _consensus_label(dist)
    return {
        "symbol": symbol.upper(),
        "distribution": dist,
        "total_analysts": sum(dist.values()),
        "consensus": label,
        "consensus_score": score,
        "price_targets": _price_targets(t, current_price),
        "recent_changes": _recent_changes(t),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_analysts("NVDA"), indent=2, default=str))
