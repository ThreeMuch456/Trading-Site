"""Touch events — defended vs attacked. Retrospective by design.

Price outcome is classified from the near-live underlying alone
(REJECTED / BREACHED / CHURN). Flow classification waits until the T-15
option data covering the touch window has actually arrived, then combines
into the final status label. Never evaluate flow against current spot.
"""
from datetime import datetime

from .. import config


def band_width(level: float, atr: float | None) -> float:
    return max(config.TOUCH_BAND_MIN_PCT * level,
               config.TOUCH_BAND_ATR_MULT * (atr or 0.0))


class TouchTracker:
    """State machine for one strike-level. Bars are 1-minute, in order."""

    def __init__(self, strike: float, kind: str, created: datetime):
        self.strike = strike
        self.kind = kind                     # 'wall_above' | 'wall_below'
        self.id = f"{kind}:{strike:g}:{created.strftime('%H%M%S')}"
        self.state = "IDLE"
        self.origin_side: str | None = None  # side spot came from: 'above'|'below'
        self.entry_ts: str | None = None
        self.exit_ts: str | None = None
        self.entries = 0
        self.time_inside_s = 0
        self.max_excursion = 0.0
        self.volume_inside = 0.0
        self.beyond_run = 0                  # consecutive closes beyond by > band
        self.exit_side: str | None = None
        self.price_outcome: str | None = None
        self.flow: dict | None = None
        self.label: str | None = None
        self.rel_volume: float | None = None
        self._prev_close: float | None = None
        self._last_exit_dt: datetime | None = None

    def resolved(self) -> bool:
        return self.price_outcome is not None

    def _inside(self, bar, band):
        return bar["l"] <= self.strike + band and bar["h"] >= self.strike - band

    def process_bar(self, bar: dict, bar_dt: datetime, band: float) -> str | None:
        """Feed one completed 1-minute bar. Returns an event string when the
        price outcome resolves, else None."""
        if self.resolved():
            return None
        inside = self._inside(bar, band)
        out = None

        if self.state == "IDLE":
            if inside and self._prev_close is not None:
                self.state = "INSIDE"
                self.entries += 1
                self.entry_ts = self.entry_ts or bar["ts"]
                self.origin_side = self.origin_side or (
                    "above" if self._prev_close > self.strike else "below")

        elif self.state == "INSIDE":
            if inside:
                self.time_inside_s += 60
                self.volume_inside += bar["v"]
                exc = (self.strike - bar["l"]) if self.origin_side == "above" \
                    else (bar["h"] - self.strike)
                self.max_excursion = max(self.max_excursion, exc)
            else:
                self.state = "WATCH"
                self.exit_ts = bar["ts"]
                self._last_exit_dt = bar_dt
                if self.origin_side == "above":
                    self.exit_side = "origin" if bar["c"] > self.strike else "beyond"
                else:
                    self.exit_side = "origin" if bar["c"] < self.strike else "beyond"

        if self.state == "WATCH":
            if inside:
                self.state = "INSIDE"
                self.entries += 1
                self.beyond_run = 0
            else:
                beyond = (self.strike - bar["c"]) if self.origin_side == "above" \
                    else (bar["c"] - self.strike)
                self.beyond_run = self.beyond_run + 1 if beyond > band else 0
                if self.beyond_run >= config.TOUCH_BREACH_BARS:
                    self.price_outcome = "BREACHED"
                    out = "BREACHED"
                elif self._last_exit_dt and (bar_dt - self._last_exit_dt).total_seconds() \
                        >= config.TOUCH_REENTRY_WINDOW_S:
                    if self.entries >= 2:
                        self.price_outcome = "CHURN"
                        out = "CHURN"
                    elif self.exit_side == "origin":
                        self.price_outcome = "REJECTED"
                        out = "REJECTED"
                    else:
                        self.price_outcome = "CHURN"
                        out = "CHURN"

        self._prev_close = bar["c"]
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id, "strike": self.strike, "kind": self.kind,
            "entry_ts": self.entry_ts, "exit_ts": self.exit_ts,
            "entries": self.entries, "time_inside_s": self.time_inside_s,
            "max_excursion": round(self.max_excursion, 4),
            "volume_inside": self.volume_inside, "rel_volume": self.rel_volume,
            "price_outcome": self.price_outcome, "flow": self.flow,
            "label": self.label,
        }


def classify_flow(touch: TouchTracker, prints: list[dict],
                  mag_before: float | None, mag_after: float | None) -> dict:
    """Combine price outcome with flow during the touch window.
    prints: classified prints at this strike inside [entry_ts, exit_ts]."""
    net = sum(p["size"] if p["side"] == "buy" else -p["size"]
              for p in prints if p["side"] in ("buy", "sell"))
    ask_vol = sum(p["size"] for p in prints if p.get("at_ask") is True)
    bid_vol = sum(p["size"] for p in prints if p.get("at_ask") is False)
    total = ask_vol + bid_vol
    aggression = (ask_vol / total) if total > 0 else None
    low_conf = any(p["low_conf"] for p in prints)

    grew = shrank = collapsed = intact = False
    mag_change = None
    if mag_before and mag_before > 0 and mag_after is not None:
        r = mag_after / mag_before
        mag_change = r - 1.0
        grew = r > config.WALL_GREW_RATIO
        shrank = r < config.WALL_SHRANK_RATIO
        collapsed = r < config.WALL_COLLAPSED_RATIO
        intact = r >= config.WALL_INTACT_RATIO

    label = None
    po = touch.price_outcome
    if po == "REJECTED":
        if grew and net < 0:
            label = "DEFENDED"
        elif shrank:
            label = "DEFENDED — WEAKENING"
        else:
            label = "DEFENDED"  # rejected with magnitude flat: still defended
    elif po == "BREACHED":
        heavy_buy = (aggression is not None and
                     aggression > config.HEAVY_ASK_AGGRESSION and net > 0)
        if collapsed and heavy_buy:
            label = "ATTACKED — BROKEN"
        elif intact:
            label = "SLIPPED"
        else:
            label = "ATTACKED — BROKEN" if collapsed else "SLIPPED"
    elif po == "CHURN":
        label = "CONTESTED"

    return {
        "net_customer_contracts": net,
        "aggression_ratio": aggression,
        "mag_before": mag_before, "mag_after": mag_after,
        "mag_change": mag_change,
        "low_conf": low_conf,
        "label": label,
    }
