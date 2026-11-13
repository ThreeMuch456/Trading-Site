"""
Morning market data fetch — step 1 of the trading agent.
Pulls current/last-close levels, after-hours/pre-market change, and recent
volume for a watchlist of tickers using yfinance (free, no API key).
"""

import yfinance as yf
from datetime import datetime
import json

WATCHLIST = ["SPY", "QQQ", "IWM"]

# Index futures trade nearly around the clock, so pre-open they show where the
# market is actually headed in a way yesterday's ETF close cannot. Still worth
# pulling once the ETFs trade 23/5 themselves: futures carry far more overnight
# liquidity, so where the two disagree before the open, the futures are usually
# the better read of the two.
FUTURES = [
    ("ES=F",  "S&P 500 E-mini"),
    ("NQ=F",  "Nasdaq 100 E-mini"),
    ("RTY=F", "Russell 2000 E-mini"),
]


def fetch_ticker_snapshot(symbol: str) -> dict:
    """Pull a snapshot of price/volume data for one ticker."""
    t = yf.Ticker(symbol)
    info = t.fast_info  # lightweight, fast endpoint

    # 5-day daily history gives us last close + simple trend context
    hist = t.history(period="5d", interval="1d")

    last_close = float(hist["Close"].iloc[-1]) if not hist.empty else None
    prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
    day_change_pct = (
        round((last_close - prev_close) / prev_close * 100, 2)
        if last_close and prev_close
        else None
    )

    def _fi(attr):
        v = getattr(info, attr, None)
        return v if v is not None else None

    snapshot = {
        "symbol": symbol,
        "last_close": round(last_close, 2) if last_close else None,
        "prev_close": round(prev_close, 2) if prev_close else None,
        "day_change_pct": day_change_pct,
        "last_price": round(float(_fi("last_price")), 2) if _fi("last_price") else None,
        "day_high": round(float(_fi("day_high")), 2) if _fi("day_high") else None,
        "day_low": round(float(_fi("day_low")), 2) if _fi("day_low") else None,
        # These two are only comparable while both are scoped the same way.
        # Overnight prints count toward consolidated volume from 2026-12-06, so
        # for the ~10 sessions it takes the trailing average to roll over, the
        # ratio moves for reasons unrelated to conviction — see
        # market_calendar.volume_baseline_note, which warns the model directly.
        "volume": int(_fi("last_volume")) if _fi("last_volume") else None,
        "ten_day_avg_volume": int(_fi("ten_day_average_volume")) if _fi("ten_day_average_volume") else None,
        "fifty_day_avg": round(float(_fi("fifty_day_average")), 2) if _fi("fifty_day_average") else None,
        "two_hundred_day_avg": round(float(_fi("two_hundred_day_average")), 2) if _fi("two_hundred_day_average") else None,
        "year_high": round(float(_fi("year_high")), 2) if _fi("year_high") else None,
        "year_low": round(float(_fi("year_low")), 2) if _fi("year_low") else None,
    }

    # Pre-market tape: the latest extended-hours print vs. yesterday's close.
    # (During the regular session this is simply the live price.)
    #
    # From 2026-12-06 the window this reaches back into is a full 23/5 overnight
    # session rather than a few hours of thin pre-market, so the last print is a
    # real trade in the ETF itself instead of a futures-implied level. Still the
    # right number to take — it is the most recent one — but a much larger move
    # can now sit between it and yesterday's close. market_calendar's notes on
    # the schedule cover what that does to the comparison, and the synthesis
    # prompt tells the model to read overnight prints as thin.
    try:
        pre = t.history(period="1d", interval="1m", prepost=True)
        if not pre.empty:
            pm_last = round(float(pre["Close"].iloc[-1]), 2)
            snapshot["premarket_last"] = pm_last
            if last_close:
                snapshot["premarket_change_pct"] = round(
                    (pm_last - last_close) / last_close * 100, 2)
    except Exception:
        pass

    return snapshot


def fetch_futures() -> list[dict]:
    """Overnight index futures — last print and change vs. prior settle."""
    results = []
    for sym, name in FUTURES:
        try:
            info = yf.Ticker(sym).fast_info
            last = getattr(info, "last_price", None)
            prev = getattr(info, "previous_close", None)
            results.append({
                "symbol": sym,
                "name": name,
                "last": round(float(last), 2) if last else None,
                "prev_close": round(float(prev), 2) if prev else None,
                "change_pct": round((last - prev) / prev * 100, 2) if last and prev else None,
            })
        except Exception as e:
            results.append({"symbol": sym, "name": name, "error": str(e)})
    return results


def fetch_watchlist(symbols: list[str]) -> dict:
    results = {}
    for sym in symbols:
        try:
            results[sym] = fetch_ticker_snapshot(sym)
        except Exception as e:
            results[sym] = {"symbol": sym, "error": str(e)}
    return results


if __name__ == "__main__":
    data = fetch_watchlist(WATCHLIST)
    output = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "watchlist": data,
    }
    print(json.dumps(output, indent=2))
