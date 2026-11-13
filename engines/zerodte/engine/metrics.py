"""Supporting metrics computed from the 1-minute underlying series and the
option chain. Numbers only."""
import math
from datetime import datetime

from .. import config


def session_bars(bars: list[dict]) -> list[dict]:
    """Bars from 09:30 ET onward, for the most recent session present.

    Filters on DATE as well as time-of-day. Filtering only on time-of-day
    silently folded a previous session's 09:30-16:00 bars into today's VWAP,
    ATR and relative volume whenever the series carried more than one day —
    which the feed can do around the session rollover, and which the delayed
    bar fallback can do when it backfills."""
    if not bars:
        return []
    session_date = datetime.fromisoformat(bars[-1]["ts"]).date()
    out = []
    for b in bars:
        ts = datetime.fromisoformat(b["ts"])
        if ts.date() == session_date and \
                ts.timetz().replace(tzinfo=None) >= config.SESSION_OPEN:
            out.append(b)
    return out


def vwap_bands(bars: list[dict]) -> dict | None:
    pv = vv = 0.0
    var_acc = 0.0
    if not bars:
        return None
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * b["v"]
        vv += b["v"]
    if vv <= 0:
        return None
    vwap = pv / vv
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        var_acc += b["v"] * (tp - vwap) ** 2
    sd = math.sqrt(var_acc / vv)
    return {"vwap": vwap, "sd": sd,
            "bands": [vwap - 2 * sd, vwap - sd, vwap + sd, vwap + 2 * sd]}


def atr14_1min(bars: list[dict], seed_bars: list[dict] | None = None) -> float | None:
    """ATR(14) on 1-minute bars; seeds from prior-day tail early in session."""
    seq = (seed_bars or [])[-20:] + bars
    if len(seq) < 15:
        return None
    trs = []
    for i in range(1, len(seq)):
        h, l, pc = seq[i]["h"], seq[i]["l"], seq[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tail = trs[-14:]
    return sum(tail) / len(tail)


def relative_volume(bars: list[dict], tod_baseline: dict[str, float],
                    baseline_days: int) -> dict | None:
    """Cumulative session volume vs same-time-of-day baseline average."""
    if not bars or not tod_baseline:
        return None
    cum = sum(b["v"] for b in bars)
    base = 0.0
    for b in bars:
        key = datetime.fromisoformat(b["ts"]).strftime("%H:%M")
        base += tod_baseline.get(key, 0.0)
    if base <= 0:
        return None
    return {"ratio": cum / base, "baseline_days": baseline_days}


def window_relative_volume(bars: list[dict], start_ts: str, end_ts: str,
                           tod_baseline: dict[str, float]) -> float | None:
    """Volume in [start, end] vs the baseline for those same minutes —
    used for touch windows."""
    if not tod_baseline:
        return None
    vol = base = 0.0
    for b in bars:
        if start_ts <= b["ts"] <= end_ts:
            vol += b["v"]
            key = datetime.fromisoformat(b["ts"]).strftime("%H:%M")
            base += tod_baseline.get(key, 0.0)
    return (vol / base) if base > 0 else None


def put_call_ratio(chain: list[dict]) -> float | None:
    pv = sum(c.get("volume") or 0 for c in chain if c["type"] == "P")
    cv = sum(c.get("volume") or 0 for c in chain if c["type"] == "C")
    return (pv / cv) if cv > 0 else None


def total_volume(chain: list[dict]) -> float:
    return float(sum(c.get("volume") or 0 for c in chain))


def atm_iv(chain: list[dict], spot: float) -> float | None:
    with_iv = [c for c in chain if c.get("iv")]
    if not with_iv or spot <= 0:
        return None
    strikes = sorted({c["strike"] for c in with_iv}, key=lambda k: abs(k - spot))
    atm = strikes[0]
    ivs = [c["iv"] for c in with_iv if c["strike"] == atm]
    return sum(ivs) / len(ivs) if ivs else None


def cumulative_customer_delta(flow: dict, meta: dict) -> float:
    """Sum of signed customer contracts x contract delta x 100 (share-equiv)."""
    total = 0.0
    for key, f in flow.items():
        m = meta.get(key)
        if m and m.get("delta") is not None:
            total += f.net_customer * m["delta"] * 100.0
    return total
