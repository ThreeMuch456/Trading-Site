"""Volume-by-price profile: binning, POC, value area, and HVN/LVN detection.

This is the foundation of the screener. Everything downstream inherits its
errors, so the math here is deliberately explicit and configurable.

A profile is built from OHLCV bars:
  1. Choose a price range (window min-low .. max-high) and bin it.
  2. Distribute each bar's volume into the bins it touches.
  3. Optionally smooth the volume[bin] array.
  4. Derive POC (argmax), value area (~70% around POC -> VAH/VAL),
     and HVN/LVN nodes via local maxima/minima with percentile gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from .config import Config


@dataclass
class Node:
    """A high- or low-volume node located at a bin."""

    bin_index: int
    price: float          # bin center
    volume: float         # (smoothed) volume at that bin
    kind: str             # "HVN" or "LVN"


@dataclass
class VolumeProfile:
    ticker: str
    bin_edges: np.ndarray            # length n_bins + 1
    bin_centers: np.ndarray          # length n_bins
    volume: np.ndarray               # smoothed volume per bin (used for detection)
    raw_volume: np.ndarray           # pre-smoothing volume per bin
    current_price: float
    atr: float
    poc_index: int
    poc_price: float
    vah: float
    val: float
    hvns: list[Node] = field(default_factory=list)
    lvns: list[Node] = field(default_factory=list)
    # Absolute volume gates used during node detection (on the smoothed array),
    # reused downstream so "thin" means the same thing everywhere.
    lvn_threshold: float = 0.0
    hvn_threshold: float = 0.0

    @property
    def n_bins(self) -> int:
        return len(self.bin_centers)

    def price_to_bin(self, price: float) -> int:
        """Index of the bin containing `price`, clamped to valid range."""
        idx = int(np.searchsorted(self.bin_edges, price, side="right") - 1)
        return int(np.clip(idx, 0, self.n_bins - 1))


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #
def compute_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Average True Range over the last `period` bars (Wilder-style simple mean)."""
    if len(bars) < 2:
        # Fall back to a rough per-bar range.
        if bars.empty:
            return 0.0
        return float((bars["high"] - bars["low"]).mean())

    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    prev_close = np.concatenate([[close[0]], close[:-1]])

    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    period = min(period, len(tr))
    atr = float(np.mean(tr[-period:]))
    return atr


# --------------------------------------------------------------------------- #
# Binning
# --------------------------------------------------------------------------- #
def _make_bins(bars: pd.DataFrame, atr: float, cfg: Config) -> np.ndarray:
    lo = float(bars["low"].min())
    hi = float(bars["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        hi = lo + max(atr, 1e-6)

    method = (cfg.get("profile.bin_method") or "fixed").lower()
    if method == "atr" and atr > 0:
        width = float(cfg.get("profile.atr_mult", 0.25)) * atr
        n_bins = int(round((hi - lo) / width)) if width > 0 else int(cfg.get("profile.bin_count", 100))
    else:
        n_bins = int(cfg.get("profile.bin_count", 100))

    n_bins = int(np.clip(n_bins, int(cfg.get("profile.min_bins", 24)),
                         int(cfg.get("profile.max_bins", 400))))
    n_bins = max(n_bins, 2)
    return np.linspace(lo, hi, n_bins + 1)


# --------------------------------------------------------------------------- #
# Volume distribution
# --------------------------------------------------------------------------- #
def _distribute_volume(bars: pd.DataFrame, edges: np.ndarray, cfg: Config) -> np.ndarray:
    n_bins = len(edges) - 1
    vol = np.zeros(n_bins, dtype=float)
    method = (cfg.get("profile.distribution") or "range").lower()

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    vols = bars["volume"].to_numpy(dtype=float)
    centers = 0.5 * (edges[:-1] + edges[1:])

    if method == "typical":
        typical = (highs + lows + closes) / 3.0
        idx = np.clip(np.searchsorted(edges, typical, side="right") - 1, 0, n_bins - 1)
        np.add.at(vol, idx, np.nan_to_num(vols))
        return vol

    # "range": spread each bar's volume across the bins its [low, high] spans.
    shape = (cfg.get("profile.range_shape") or "uniform").lower()
    for lo, hi, v in zip(lows, highs, np.nan_to_num(vols)):
        if v <= 0:
            continue
        if hi <= lo:
            j = int(np.clip(np.searchsorted(edges, lo, side="right") - 1, 0, n_bins - 1))
            vol[j] += v
            continue

        lo_b = int(np.clip(np.searchsorted(edges, lo, side="right") - 1, 0, n_bins - 1))
        hi_b = int(np.clip(np.searchsorted(edges, hi, side="right") - 1, 0, n_bins - 1))
        span = slice(lo_b, hi_b + 1)

        if shape == "triangular":
            # Peak weight at the bar's mid-price, tapering to the extremes.
            mid = 0.5 * (lo + hi)
            w = np.maximum(0.0, 1.0 - np.abs(centers[span] - mid) / (0.5 * (hi - lo)))
            if w.sum() == 0:
                w = np.ones(hi_b - lo_b + 1)
        else:  # uniform
            w = np.ones(hi_b - lo_b + 1)

        vol[span] += v * (w / w.sum())

    return vol


def _recency_weight(bars: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Exponentially decay each bar's volume by age before binning, so the profile
    reflects *current* structure: volume traded half_life days before the last bar
    counts half as much. half_life <= 0 disables (raw, TradingView-comparable)."""
    half_life = float(cfg.get("profile.recency_half_life_days", 0) or 0)
    if half_life <= 0 or not isinstance(bars.index, pd.DatetimeIndex):
        return bars
    age_days = (bars.index[-1] - bars.index).total_seconds() / 86400.0
    weights = np.power(0.5, np.asarray(age_days, dtype=float) / half_life)
    return bars.assign(volume=bars["volume"].to_numpy(dtype=float) * weights)


def _smooth(vol: np.ndarray, window: int) -> np.ndarray:
    if window is None or window <= 1 or len(vol) < window:
        return vol
    kernel = np.ones(window) / window
    return np.convolve(vol, kernel, mode="same")


# --------------------------------------------------------------------------- #
# POC / value area / nodes
# --------------------------------------------------------------------------- #
def _value_area(vol: np.ndarray, poc_index: int, target_frac: float) -> tuple[int, int]:
    """Expand out from the POC, greedily adding the larger neighbor, until the
    accumulated volume reaches target_frac of the total. Returns (low_idx, high_idx)."""
    total = vol.sum()
    if total <= 0:
        return poc_index, poc_index

    target = target_frac * total
    acc = vol[poc_index]
    lo = hi = poc_index
    n = len(vol)

    while acc < target and (lo > 0 or hi < n - 1):
        left = vol[lo - 1] if lo > 0 else -1.0
        right = vol[hi + 1] if hi < n - 1 else -1.0
        if right >= left:
            hi += 1
            acc += vol[hi]
        else:
            lo -= 1
            acc += vol[lo]
    return lo, hi


def _detect_nodes(
    vol: np.ndarray, centers: np.ndarray, cfg: Config
) -> tuple[list[Node], list[Node], float, float]:
    distance = max(1, int(cfg.get("profile.peak_distance_bins", 2)))
    hvn_pct = float(cfg.get("profile.hvn_percentile", 70))
    lvn_pct = float(cfg.get("profile.lvn_percentile", 30))

    hvn_gate = float(np.percentile(vol, hvn_pct))
    lvn_gate = float(np.percentile(vol, lvn_pct))

    peak_idx, _ = find_peaks(vol, distance=distance)
    trough_idx, _ = find_peaks(-vol, distance=distance)

    hvns = [
        Node(int(i), float(centers[i]), float(vol[i]), "HVN")
        for i in peak_idx if vol[i] >= hvn_gate
    ]
    lvns = [
        Node(int(i), float(centers[i]), float(vol[i]), "LVN")
        for i in trough_idx if vol[i] <= lvn_gate
    ]
    return hvns, lvns, hvn_gate, lvn_gate


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def build_profile(ticker: str, bars: pd.DataFrame, cfg: Config) -> VolumeProfile | None:
    """Build a full VolumeProfile from OHLCV bars. Returns None if unusable."""
    if bars is None or bars.empty or len(bars) < 5:
        return None

    atr = compute_atr(bars, int(cfg.get("profile.atr_period", 14)))
    edges = _make_bins(bars, atr, cfg)
    centers = 0.5 * (edges[:-1] + edges[1:])

    # Recency weighting applies to VOLUME only (ATR/bins/price untouched), so
    # every downstream level — POC, value area, HVN/LVN — leans toward recent
    # structure rather than treating 60-day-old volume as equal to last week's.
    raw_vol = _distribute_volume(_recency_weight(bars, cfg), edges, cfg)
    if raw_vol.sum() <= 0:
        return None
    vol = _smooth(raw_vol, int(cfg.get("profile.smooth_window", 1)))

    # POC and value area come from the UN-smoothed volume — "where the most
    # volume traded" (recency-weighted when recency_half_life_days > 0; set it
    # to 0 to match TradingView's VPVR exactly). Smoothing is only for stable
    # peak/trough (HVN/LVN) detection below.
    poc_index = int(np.argmax(raw_vol))
    poc_price = float(centers[poc_index])

    lo_idx, hi_idx = _value_area(raw_vol, poc_index, float(cfg.get("profile.value_area_pct", 0.70)))
    val = float(edges[lo_idx])         # value-area low  = lower edge of lowest VA bin
    vah = float(edges[hi_idx + 1])     # value-area high = upper edge of highest VA bin

    hvns, lvns, hvn_gate, lvn_gate = _detect_nodes(vol, centers, cfg)
    current_price = float(bars["close"].iloc[-1])

    return VolumeProfile(
        ticker=ticker,
        bin_edges=edges,
        bin_centers=centers,
        volume=vol,
        raw_volume=raw_vol,
        current_price=current_price,
        atr=atr,
        poc_index=poc_index,
        poc_price=poc_price,
        vah=vah,
        val=val,
        hvns=hvns,
        lvns=lvns,
        lvn_threshold=lvn_gate,
        hvn_threshold=hvn_gate,
    )
