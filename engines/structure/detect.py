"""Thin-zone detection: find LVNs near price with a clear path to them.

Run in both directions from the current-price bin:
  - Thin support:    walk DOWN, first LVN with no HVN between it and price.
  - Thin resistance: walk UP,   first LVN with no HVN between it and price.

"Clear path" (clearance) means no HVN sits between current price and the LVN -
price can travel through the air pocket without hitting a sticky cluster first.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .profile import Node, VolumeProfile


@dataclass
class ThinZone:
    direction: str          # "support" (below) or "resistance" (above)
    lvn_price: float
    lvn_bin: int
    distance_pct: float     # absolute % from current price to the LVN
    distance_atr: float     # ...in ATRs
    depth: float            # gap depth: how low the LVN volume is, 0..1 (1 = thinnest)
    width_pct: float        # price span of the contiguous thin zone, as % of price
    clearance: bool         # True if no HVN sits between price and the LVN
    score: float = 0.0      # filled in by screen.py
    # Where the move likely stalls: the first HVN *beyond* the LVN in this
    # direction — the far wall of the air pocket. None if the profile just
    # runs out (no HVN past the gap), which itself means extra room.
    next_hvn_price: float | None = None
    next_hvn_distance_pct: float | None = None  # % from current price to that HVN


def _gap_depth(profile: VolumeProfile, bin_index: int) -> float:
    """Normalize an LVN's volume to 0..1 where 1 = thinnest (near-zero volume),
    relative to the profile's peak (smoothed) volume."""
    peak_vol = float(profile.volume.max())
    if peak_vol <= 0:
        return 0.0
    return float(np.clip(1.0 - profile.volume[bin_index] / peak_vol, 0.0, 1.0))


def _gap_width_pct(profile: VolumeProfile, bin_index: int, price: float) -> float:
    """Width of the contiguous low-volume trough around the LVN, as % of price.

    Expand left/right while neighboring bins stay at/under the LVN gate (the same
    absolute threshold used to detect LVNs). Using the LVN gate — not a multiple
    of the LVN's own volume — keeps deep gaps from measuring artificially narrow."""
    vol = profile.volume
    n = len(vol)
    gate = profile.lvn_threshold or (vol[bin_index] * 1.5)  # fallback if unset
    gate = max(gate, float(vol[bin_index]))                 # the LVN bin always qualifies
    lo = hi = bin_index
    while lo > 0 and vol[lo - 1] <= gate:
        lo -= 1
    while hi < n - 1 and vol[hi + 1] <= gate:
        hi += 1
    span = float(profile.bin_edges[hi + 1] - profile.bin_edges[lo])
    return 100.0 * span / price if price > 0 else 0.0


def _has_hvn_between(profile: VolumeProfile, a: int, b: int) -> bool:
    """Is there an HVN with bin index strictly between a and b (exclusive)?"""
    lo, hi = sorted((a, b))
    for node in profile.hvns:
        if lo < node.bin_index < hi:
            return True
    return False


def _first_lvn(profile: VolumeProfile, direction: str) -> Node | None:
    """First LVN walking away from the current-price bin in `direction`."""
    price_bin = profile.price_to_bin(profile.current_price)
    if direction == "support":  # below price -> smaller bin indices, nearest first
        candidates = [n for n in profile.lvns if n.bin_index < price_bin]
        candidates.sort(key=lambda n: n.bin_index, reverse=True)
    else:                        # resistance: above price -> larger bin indices
        candidates = [n for n in profile.lvns if n.bin_index > price_bin]
        candidates.sort(key=lambda n: n.bin_index)
    return candidates[0] if candidates else None


def _next_hvn_beyond(profile: VolumeProfile, lvn_bin: int, direction: str) -> Node | None:
    """The first HVN past the LVN, continuing away from price in `direction` —
    the high-volume shelf where a move through the air pocket would likely stall."""
    if direction == "support":   # walking down: nearest HVN below the LVN
        candidates = [n for n in profile.hvns if n.bin_index < lvn_bin]
        return max(candidates, key=lambda n: n.bin_index) if candidates else None
    candidates = [n for n in profile.hvns if n.bin_index > lvn_bin]
    return min(candidates, key=lambda n: n.bin_index) if candidates else None


def _detect_one(profile: VolumeProfile, direction: str, cfg: Config) -> ThinZone | None:
    lvn = _first_lvn(profile, direction)
    if lvn is None:
        return None

    price = profile.current_price
    price_bin = profile.price_to_bin(price)
    dist_pct = 100.0 * abs(lvn.price - price) / price if price > 0 else float("inf")
    dist_atr = abs(lvn.price - price) / profile.atr if profile.atr > 0 else float("inf")

    max_pct = float(cfg.get("detect.max_distance_pct", 8.0))
    max_atr = float(cfg.get("detect.max_distance_atr", 4.0))
    if dist_pct > max_pct and dist_atr > max_atr:
        return None

    clearance = not _has_hvn_between(profile, price_bin, lvn.bin_index)

    # The whole premise is an air pocket with a clear path to it. If an HVN sits
    # between price and the nearest LVN, price hits that wall first — and every
    # farther LVN in this direction is blocked by the same HVN — so there is no
    # actionable thin zone this way. Gate it off (set detect.require_clearance:
    # false to fall back to the old "report it but flag clearance=False" behavior).
    if not clearance and bool(cfg.get("detect.require_clearance", True)):
        return None

    nxt = _next_hvn_beyond(profile, lvn.bin_index, direction)

    return ThinZone(
        direction=direction,
        lvn_price=lvn.price,
        lvn_bin=lvn.bin_index,
        distance_pct=dist_pct,
        distance_atr=dist_atr,
        depth=_gap_depth(profile, lvn.bin_index),
        width_pct=_gap_width_pct(profile, lvn.bin_index, price),
        clearance=clearance,
        next_hvn_price=None if nxt is None else nxt.price,
        next_hvn_distance_pct=(
            None if nxt is None or price <= 0
            else 100.0 * abs(nxt.price - price) / price
        ),
    )


def detect_thin_zones(profile: VolumeProfile, cfg: Config) -> dict[str, ThinZone | None]:
    """Return {"support": ThinZone|None, "resistance": ThinZone|None}."""
    return {
        "support": _detect_one(profile, "support", cfg),
        "resistance": _detect_one(profile, "resistance", cfg),
    }
