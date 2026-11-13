"""Bull/bear flag detection on hourly OHLCV bars.

A flag has two parts:
  Pole — a strong directional move (>= pole_min_pct %) over pole_min_bars..pole_max_bars
  Flag — a tight consolidation channel after the pole, with a slight counter-trend drift
         and ideally declining volume.

We slide a variable-length flag window over the recent tail (optionally ending a
few bars back), and for each flag window try every valid pole length, returning
the best-scoring flag found (or None). The sliding window — rather than a single
fixed-length window pinned to the last bar — lets us catch flags that are younger
than the max window (which a fixed window would dilute with pre-pole bars) and,
if flag_max_age_bars > 0, ones that began breaking out a few bars ago.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config


@dataclass
class Flag:
    kind: str              # "bull" or "bear"
    pole_start: int        # bar index of pole start
    pole_end: int          # bar index of pole end
    flag_start: int        # bar index of flag consolidation start
    flag_end: int          # bar index of flag end (<= len(bars) - 1)
    pole_pct: float        # signed % move of the pole
    flag_retrace_pct: float  # how much the flag retraces the pole (%)
    channel_pct: float     # flag channel height as % of price (lower = tighter)
    volume_declining: bool  # True if avg volume in flag < avg volume in pole
    score: float           # 0..1 composite confidence


def _pct(close: np.ndarray, i: int, j: int) -> float:
    c0 = close[i]
    return 100.0 * (close[j] - c0) / c0 if c0 > 0 else 0.0


def _channel(high: np.ndarray, low: np.ndarray, close: np.ndarray, i: int, j: int) -> float:
    hi  = float(high[i : j + 1].max())
    lo  = float(low[i : j + 1].min())
    mid = float(close[i : j + 1].mean())
    return 100.0 * (hi - lo) / mid if mid > 0 else float("inf")


def detect_flag(bars: pd.DataFrame, cfg: Config) -> Flag | None:
    """Find the best recent bull or bear flag in bars. Returns None if none qualifies."""
    pole_min_bars = int(cfg.get("flags.pole_min_bars", 5))
    pole_max_bars = int(cfg.get("flags.pole_max_bars", 30))
    pole_min_pct  = float(cfg.get("flags.pole_min_pct", 4.0))
    # flag_max_bars supersedes the old fixed flag_bars (kept as a fallback default).
    flag_min_bars = int(cfg.get("flags.flag_min_bars", 10))
    flag_max_bars = int(cfg.get("flags.flag_max_bars", cfg.get("flags.flag_bars", 30)))
    flag_max_age  = int(cfg.get("flags.flag_max_age_bars", 0))
    max_retrace   = float(cfg.get("flags.flag_max_retrace_pct", 50.0))
    max_channel   = float(cfg.get("flags.flag_max_channel_pct", 9.0))

    n = len(bars)
    if n < pole_min_bars + flag_min_bars + 1:
        return None

    close = bars["close"].to_numpy(dtype=float)
    high  = bars["high"].to_numpy(dtype=float)
    low   = bars["low"].to_numpy(dtype=float)
    vol   = bars["volume"].to_numpy(dtype=float)

    best: Flag | None = None

    # Slide the flag's end back over the most recent (flag_max_age + 1) bars.
    for flag_end in range(n - 1, n - 1 - max(0, flag_max_age) - 1, -1):
        if flag_end < flag_min_bars:
            break
        age = (n - 1) - flag_end  # 0 = ends on the latest bar

        for flag_len in range(flag_min_bars, flag_max_bars + 1):
            flag_start = flag_end - flag_len + 1
            if flag_start - pole_min_bars < 0:
                break  # no room left for even a minimal pole

            flag_ch = _channel(high, low, close, flag_start, flag_end)
            if flag_ch > max_channel:
                continue
            flag_drift = _pct(close, flag_start, flag_end)
            flag_vol   = float(vol[flag_start : flag_end + 1].mean())

            for pole_len in range(pole_min_bars, pole_max_bars + 1):
                pole_start = flag_start - pole_len
                if pole_start < 0:
                    break
                pole_end = flag_start - 1
                move = _pct(close, pole_start, pole_end)
                if abs(move) < pole_min_pct:
                    continue

                kind = "bull" if move > 0 else "bear"
                # Flag should drift counter to the pole (or stay flat), not extend it.
                if kind == "bull" and flag_drift > 0.5 * abs(move):
                    continue
                if kind == "bear" and flag_drift < -0.5 * abs(move):
                    continue

                retrace = abs(flag_drift) / abs(move) * 100.0 if move != 0 else 0.0
                if retrace > max_retrace:
                    continue

                pole_vol = float(vol[pole_start : pole_end + 1].mean())
                vol_decl = flag_vol < pole_vol if pole_vol > 0 else False

                score = (
                    0.35 * min(1.0, abs(move) / (pole_min_pct * 3))
                    + 0.35 * max(0.0, 1.0 - flag_ch / max_channel)
                    + 0.20 * max(0.0, 1.0 - retrace / max_retrace)
                    + 0.10 * float(vol_decl)
                )
                # Prefer current flags: gently penalize ones that already aged out.
                if age:
                    score *= max(0.0, 1.0 - 0.1 * age)

                if best is None or score > best.score:
                    best = Flag(
                        kind=kind,
                        pole_start=pole_start,
                        pole_end=pole_end,
                        flag_start=flag_start,
                        flag_end=flag_end,
                        pole_pct=round(move, 2),
                        flag_retrace_pct=round(retrace, 2),
                        channel_pct=round(flag_ch, 3),
                        volume_declining=vol_decl,
                        score=round(score, 4),
                    )

    return best
