"""
Options data fetch — step 2b of the trading agent.

Primary source: Massive.com paid chain snapshot (exchange-computed greeks/IV,
multi-expiry GEX, expected move, skew/term structure) when MASSIVE_API_KEY is
set — see fetch_options_massive.py.

Fallback: yfinance nearest-expiry chain with Black-Scholes greeks computed
here (no API key required).

GEX convention (SpotGamma-style):
  GEX per strike = (call_gamma * call_OI - put_gamma * put_OI) * 100 * spot² * 0.01
  Positive net GEX → dealers long gamma → they dampen intraday moves (sell rips, buy dips)
  Negative net GEX → dealers short gamma → they amplify intraday moves (buy rips, sell dips)
"""

import os
import sys
import math
import json
from datetime import datetime, date

from .market_calendar import market_today
import yfinance as yf

WATCHLIST = ["SPY", "QQQ", "IWM"]
STRIKE_RANGE_PCT = 0.08   # only compute Greeks for strikes within ±8% of spot
TOP_GEX_COUNT = 6         # number of key gamma levels to surface


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> dict | None:
    """
    Returns delta, gamma, theta (per calendar day), vega (per 1% IV move).
    Returns None when inputs are degenerate.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
    except (ValueError, ZeroDivisionError):
        return None

    gamma = _npdf(d1) / (S * sigma * math.sqrt(T))
    vega  = S * _npdf(d1) * math.sqrt(T) / 100

    if kind == "call":
        delta = _ncdf(d1)
        theta = (-(S * _npdf(d1) * sigma) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * _ncdf(d2)) / 365
    else:
        delta = _ncdf(d1) - 1
        theta = (-(S * _npdf(d1) * sigma) / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365

    return {
        "delta": round(delta, 4),
        "gamma": gamma,              # keep full precision for GEX math
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
    }


# ---------------------------------------------------------------------------
# Max pain
# ---------------------------------------------------------------------------

def _max_pain(calls, puts) -> float | None:
    """Strike that minimises total option-holder value at expiry (dealer-favoured pin)."""
    all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
    if not all_strikes:
        return None
    best_strike, best_pain = all_strikes[0], float("inf")
    for test_k in all_strikes:
        call_pain = sum(max(test_k - k, 0) * oi
                        for k, oi in zip(calls["strike"], calls["openInterest"].fillna(0)))
        put_pain  = sum(max(k - test_k, 0) * oi
                        for k, oi in zip(puts["strike"],  puts["openInterest"].fillna(0)))
        total = call_pain + put_pain
        if total < best_pain:
            best_pain, best_strike = total, test_k
    return float(best_strike)


# ---------------------------------------------------------------------------
# Per-ticker options snapshot
# ---------------------------------------------------------------------------

def fetch_ticker_options(symbol: str, risk_free_rate: float = 0.05) -> dict:
    t = yf.Ticker(symbol)
    spot = t.fast_info.last_price
    if not spot:
        return {"symbol": symbol, "error": "no spot price"}

    expiries = t.options
    if not expiries:
        return {"symbol": symbol, "error": "no options data"}

    today = market_today()
    # Pick nearest expiry that hasn't already passed
    future_expiries = [e for e in expiries if e >= today.isoformat()]
    if not future_expiries:
        return {"symbol": symbol, "error": "no future expiries"}
    expiry = future_expiries[0]

    chain  = t.option_chain(expiry)
    calls  = chain.calls.copy()
    puts   = chain.puts.copy()

    expiry_date = date.fromisoformat(expiry)
    days_out = (expiry_date - today).days
    # On expiry morning the full trading day remains; use 0.5 days as floor
    T = max(days_out, 0.5) / 365.0

    # Restrict to strikes near spot
    lo = spot * (1 - STRIKE_RANGE_PCT)
    hi = spot * (1 + STRIKE_RANGE_PCT)
    calls = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)].copy()
    puts  = puts[ (puts["strike"]  >= lo) & (puts["strike"]  <= hi)].copy()

    # GEX per strike
    gex_by_strike: dict[float, float] = {}

    for _, row in calls.iterrows():
        g = bs_greeks(spot, row["strike"], T, risk_free_rate,
                      row.get("impliedVolatility", 0) or 0, "call")
        if not g:
            continue
        oi = int(row["openInterest"]) if not math.isnan(row["openInterest"] or float("nan")) else 0
        k = round(float(row["strike"]), 2)
        gex_by_strike[k] = gex_by_strike.get(k, 0.0) + g["gamma"] * oi * 100 * spot**2 * 0.01

    for _, row in puts.iterrows():
        g = bs_greeks(spot, row["strike"], T, risk_free_rate,
                      row.get("impliedVolatility", 0) or 0, "put")
        if not g:
            continue
        oi = int(row["openInterest"]) if not math.isnan(row["openInterest"] or float("nan")) else 0
        k = round(float(row["strike"]), 2)
        gex_by_strike[k] = gex_by_strike.get(k, 0.0) - g["gamma"] * oi * 100 * spot**2 * 0.01

    net_gex = sum(gex_by_strike.values())

    # Key gamma levels: strikes with highest absolute GEX
    top_levels = sorted(gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)[:TOP_GEX_COUNT]
    gex_levels = sorted(
        [{"strike": k, "gex": round(v, 2),
          "role": "call_wall" if v > 0 else "put_wall"}
         for k, v in top_levels],
        key=lambda x: x["strike"]
    )

    # Gamma flip: nearest strike above and below spot where GEX sign changes
    strikes_sorted = sorted(gex_by_strike.keys())
    cumulative = 0.0
    flip_level = None
    for k in strikes_sorted:
        prev = cumulative
        cumulative += gex_by_strike[k]
        if prev * cumulative < 0:   # sign change
            flip_level = k
            break

    # Put/call aggregate stats
    call_oi  = int(calls["openInterest"].fillna(0).sum())
    put_oi   = int(puts["openInterest"].fillna(0).sum())
    call_vol = int(calls["volume"].fillna(0).sum())
    put_vol  = int(puts["volume"].fillna(0).sum())

    # Unusual activity: strikes where today's vol exceeds 40% of OI
    unusual = []
    for kind_df, kind_label in [(calls, "call"), (puts, "put")]:
        for _, row in kind_df.iterrows():
            vol = row.get("volume", 0) or 0
            oi  = row.get("openInterest", 0) or 0
            if oi > 0 and vol / oi >= 0.40 and vol >= 500:
                unusual.append({
                    "strike":    round(float(row["strike"]), 2),
                    "kind":      kind_label,
                    "volume":    int(vol),
                    "open_interest": int(oi),
                    "vol_oi_ratio":  round(vol / oi, 2),
                    "iv":        round(float(row.get("impliedVolatility", 0) or 0), 4),
                })
    unusual.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
    unusual_strikes = unusual[:8]

    return {
        "symbol":              symbol,
        "source":              "yfinance",
        "spot":                round(spot, 2),
        "expiry":              expiry,
        "days_to_expiry":      (expiry_date - today).days,
        "net_gex":             round(net_gex, 2),
        "gamma_environment":   "positive" if net_gex > 0 else "negative",
        "gamma_flip_level":    round(flip_level, 2) if flip_level else None,
        "max_pain":            round(_max_pain(calls, puts), 2) if not calls.empty else None,
        "put_call_oi_ratio":   round(put_oi / call_oi, 3) if call_oi else None,
        "put_call_vol_ratio":  round(put_vol / call_vol, 3) if call_vol else None,
        "top_gex_levels":      gex_levels,
        "unusual_strikes":     unusual_strikes,
    }


def fetch_options_snapshot(symbols: list = None, risk_free_rate: float = 0.05) -> dict:
    if symbols is None:
        symbols = WATCHLIST
    massive_key = os.environ.get("MASSIVE_API_KEY")
    results = {}
    for sym in symbols:
        snap = None
        if massive_key:
            try:
                from .fetch_options_massive import fetch_ticker_options_massive
                snap = fetch_ticker_options_massive(sym, massive_key)
            except Exception as e:
                print(f"Massive fetch failed for {sym} ({e}); falling back to yfinance",
                      file=sys.stderr)
        if snap is None:
            try:
                snap = fetch_ticker_options(sym, risk_free_rate)
            except Exception as e:
                snap = {"symbol": sym, "error": str(e)}
        results[sym] = snap
    return results


if __name__ == "__main__":
    data = fetch_options_snapshot()
    print(json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "options": data}, indent=2))
