"""
Price-history analytics for a single ticker.

Produces everything the chart + long-term performance read needs:
  * daily close series (5y) ready for the front-end chart
  * total returns over 1y / 3y / 5y, annualized volatility, max drawdown
  * alpha & beta vs SPY from an OLS regression on aligned daily returns
    (beta = slope, alpha = annualized intercept)
  * "sharp move" days — statistical outliers worth annotating on the chart
"""

from datetime import datetime

import numpy as np
import yfinance as yf

BENCHMARK = "SPY"
TRADING_DAYS = 252


def _download_closes(symbol: str, period: str = "5y"):
    """Return (dates list[str], closes np.array) of daily closes, or ([], [])."""
    hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    if hist is None or hist.empty:
        return [], np.array([])
    closes = hist["Close"].dropna()
    dates = [d.strftime("%Y-%m-%d") for d in closes.index]
    return dates, closes.to_numpy(dtype=float)


def _total_return(closes: np.ndarray, lookback_days: int) -> float | None:
    if closes.size < 2:
        return None
    n = min(lookback_days, closes.size - 1)
    start, end = closes[-1 - n], closes[-1]
    if not start:
        return None
    return round((end - start) / start * 100, 2)


def _max_drawdown(closes: np.ndarray) -> float | None:
    if closes.size < 2:
        return None
    running_max = np.maximum.accumulate(closes)
    drawdowns = (closes - running_max) / running_max
    return round(float(drawdowns.min()) * 100, 2)


def _alpha_beta(symbol_closes: np.ndarray, sym_dates: list[str]):
    """OLS of stock daily returns on SPY daily returns over the overlap window."""
    bench_dates, bench_closes = _download_closes(BENCHMARK, "5y")
    if bench_closes.size < 30 or symbol_closes.size < 30:
        return {"alpha": None, "beta": None, "r_squared": None}

    # Align on common dates
    bench_map = dict(zip(bench_dates, bench_closes))
    pairs = [(c, bench_map[d]) for d, c in zip(sym_dates, symbol_closes) if d in bench_map]
    if len(pairs) < 30:
        return {"alpha": None, "beta": None, "r_squared": None}

    s = np.array([p[0] for p in pairs])
    b = np.array([p[1] for p in pairs])
    s_ret = np.diff(s) / s[:-1]
    b_ret = np.diff(b) / b[:-1]

    # beta (slope) & alpha (intercept) via least squares
    slope, intercept = np.polyfit(b_ret, s_ret, 1)
    # R^2 of the fit
    pred = slope * b_ret + intercept
    ss_res = np.sum((s_ret - pred) ** 2)
    ss_tot = np.sum((s_ret - s_ret.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot else None

    return {
        "beta": round(float(slope), 2),
        # annualize the daily intercept into a % "excess return"
        "alpha": round(float(intercept) * TRADING_DAYS * 100, 2),
        "r_squared": round(float(r2), 2) if r2 is not None else None,
    }


def _sharp_moves(dates: list[str], closes: np.ndarray,
                 abs_floor: float = 0.05, sigma_mult: float = 2.5,
                 max_markers: int = 15) -> list[dict]:
    """
    Flag bars whose return exceeds max(abs_floor, sigma_mult * rolling std).

    The rolling window and the marker cap both adapt to the series length so the
    chart stays readable across very different horizons: a 1-month daily view and
    a 20-year weekly view each surface only their handful of most significant moves
    (largest magnitude wins), rather than crowding the longer window with markers.
    """
    if closes.size < 12 or max_markers <= 0:
        return []
    rets = np.diff(closes) / closes[:-1]
    ret_dates = dates[1:]

    moves = []
    window = max(5, min(30, len(rets) // 4))
    for i in range(window, len(rets)):
        recent = rets[i - window:i]
        sd = float(np.std(recent))
        threshold = max(abs_floor, sigma_mult * sd)
        if abs(rets[i]) >= threshold:
            moves.append({
                "date": ret_dates[i],
                "pct": round(float(rets[i]) * 100, 2),
                "direction": "up" if rets[i] > 0 else "down",
            })

    # Keep the largest-magnitude moves, then sort newest-first for display
    moves.sort(key=lambda m: abs(m["pct"]), reverse=True)
    moves = moves[:max_markers]
    moves.sort(key=lambda m: m["date"], reverse=True)
    return moves


def fetch_price_history(symbol: str) -> dict:
    sym = symbol.upper()
    dates, closes = _download_closes(sym, "5y")
    if closes.size == 0:
        return {"symbol": sym, "error": "no_price_history", "series": []}

    daily_rets = np.diff(closes) / closes[:-1] if closes.size > 1 else np.array([])
    ann_vol = (round(float(np.std(daily_rets)) * np.sqrt(TRADING_DAYS) * 100, 2)
               if daily_rets.size else None)

    ab = _alpha_beta(closes, dates)

    return {
        "symbol": sym,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        # Full series for the chart: [{t: 'YYYY-MM-DD', c: close}, ...]
        "series": [{"t": d, "c": round(float(c), 2)} for d, c in zip(dates, closes)],
        "return_1y": _total_return(closes, TRADING_DAYS),
        "return_3y": _total_return(closes, TRADING_DAYS * 3),
        "return_5y": _total_return(closes, TRADING_DAYS * 5),
        "annualized_vol": ann_vol,
        "max_drawdown": _max_drawdown(closes),
        "alpha": ab["alpha"],
        "beta_regression": ab["beta"],
        "r_squared": ab["r_squared"],
        "sharp_moves": _sharp_moves(dates, closes),
    }


# ---------------------------------------------------------------------------
# On-demand range fetching for the chart (1D … Max)
# ---------------------------------------------------------------------------
# period/interval pairs respect Yahoo's limits (e.g. 1m only ~7d, 1h only ~730d).
# `markers` caps how many sharp-move flags a range may show; `abs_floor` is the
# minimum return magnitude to even be considered, raised for longer windows so
# the cap selects only genuinely large events. Intraday ranges show no markers
# (the news-driven "big day" concept is a daily/weekly notion).

RANGE_CONFIG: dict[str, dict] = {
    "1D":  {"period": "1d",   "interval": "5m",  "intraday": True,  "markers": 0,  "abs_floor": 0.0},
    "1W":  {"period": "5d",   "interval": "30m", "intraday": True,  "markers": 0,  "abs_floor": 0.0},
    "1M":  {"period": "1mo",  "interval": "1d",  "intraday": False, "markers": 5,  "abs_floor": 0.04},
    "3M":  {"period": "3mo",  "interval": "1d",  "intraday": False, "markers": 7,  "abs_floor": 0.04},
    "6M":  {"period": "6mo",  "interval": "1d",  "intraday": False, "markers": 9,  "abs_floor": 0.05},
    "1Y":  {"period": "1y",   "interval": "1d",  "intraday": False, "markers": 11, "abs_floor": 0.05},
    "3Y":  {"period": "3y",   "interval": "1d",  "intraday": False, "markers": 13, "abs_floor": 0.06},
    "5Y":  {"period": "5y",   "interval": "1d",  "intraday": False, "markers": 15, "abs_floor": 0.06},
    "MAX": {"period": "max",  "interval": "1wk", "intraday": False, "markers": 18, "abs_floor": 0.10},
}

DEFAULT_RANGE = "5Y"


def history_for_range(symbol: str, range_key: str) -> dict:
    """Fetch a single chart range at an appropriate resolution + its markers."""
    cfg = RANGE_CONFIG.get((range_key or "").upper(), RANGE_CONFIG[DEFAULT_RANGE])
    sym = symbol.upper()
    try:
        hist = yf.Ticker(sym).history(
            period=cfg["period"], interval=cfg["interval"], auto_adjust=True,
        )
    except Exception:
        hist = None

    if hist is None or hist.empty:
        return {"symbol": sym, "range": range_key, "intraday": cfg["intraday"],
                "series": [], "sharp_moves": []}

    closes_s = hist["Close"].dropna()
    closes = closes_s.to_numpy(dtype=float)

    if cfg["intraday"]:
        # Epoch seconds for intraday so lightweight-charts shows intraday times
        series = [{"t": int(ts.timestamp()), "c": round(float(c), 2)}
                  for ts, c in zip(closes_s.index, closes)]
        sharp = []
    else:
        dates = [d.strftime("%Y-%m-%d") for d in closes_s.index]
        series = [{"t": d, "c": round(float(c), 2)} for d, c in zip(dates, closes)]
        sharp = _sharp_moves(dates, closes,
                             abs_floor=cfg["abs_floor"], max_markers=cfg["markers"])

    return {
        "symbol": sym,
        "range": (range_key or DEFAULT_RANGE).upper(),
        "intraday": cfg["intraday"],
        "interval": cfg["interval"],
        "series": series,
        "sharp_moves": sharp,
    }


if __name__ == "__main__":
    import json
    out = fetch_price_history("NVDA")
    out["series"] = f"<{len(out['series'])} points>"
    print(json.dumps(out, indent=2))
    for rk in ("1D", "1M", "MAX"):
        h = history_for_range("NVDA", rk)
        print(rk, "->", len(h["series"]), "pts,", len(h["sharp_moves"]), "markers,",
              "intraday" if h["intraday"] else "daily/wk")
