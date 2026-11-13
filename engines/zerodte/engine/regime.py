"""Regime state machine: PIN / TREND / TRANSITION.

PIN        — local dealer gamma positive (spot above the nearest flip).
TREND      — local dealer gamma negative (spot below the flip).
TRANSITION — spot within 0.15% of a flip, or the flip moved more than
             0.3% in the last 15 minutes.

Every transition emits a REGIME_CHANGE event carrying the trigger and the
level set in force before and after. That event is a headline in the UI.
"""
from datetime import datetime

from .. import config


class RegimeMachine:
    def __init__(self):
        self.state: str | None = None
        self.since: datetime | None = None
        self.flip_history: list[tuple[datetime, float]] = []  # (ts, nearest flip)

    def _flip_migration(self, now: datetime, flip_level: float | None,
                        spot: float) -> dict:
        """Is the flip moving toward or away from spot, and how much in 15m?"""
        out = {"moved_pct_15m": 0.0, "direction": None}
        if flip_level is None:
            return out
        self.flip_history.append((now, flip_level))
        cutoff = now.timestamp() - config.FLIP_MOVE_WINDOW_S
        self.flip_history = [(t, f) for t, f in self.flip_history
                             if t.timestamp() >= cutoff]
        if len(self.flip_history) >= 2:
            old = self.flip_history[0][1]
            if old:
                out["moved_pct_15m"] = abs(flip_level - old) / old
            old_dist, new_dist = abs(spot - old), abs(spot - flip_level)
            if abs(new_dist - old_dist) > 1e-9:
                out["direction"] = "toward_spot" if new_dist < old_dist else "away_from_spot"
        return out

    def update(self, now: datetime, spot: float, flip_result: dict,
               atr: float | None) -> tuple[dict, dict | None]:
        """Returns (regime_state, transition_event_or_None)."""
        nearest = flip_result.get("nearest")
        local_gamma = flip_result.get("local_gamma", 0.0)
        migration = self._flip_migration(now, nearest, spot)

        trigger = None
        if nearest is not None and spot > 0:
            dist_pct = abs(spot - nearest) / spot
            if dist_pct <= config.TRANSITION_FLIP_DIST_PCT:
                new_state, trigger = "TRANSITION", f"spot within 0.15% of flip {nearest:.2f}"
            elif migration["moved_pct_15m"] > config.TRANSITION_FLIP_MOVE_PCT:
                new_state, trigger = "TRANSITION", (
                    f"flip moved {migration['moved_pct_15m']*100:.2f}% in 15m")
            elif local_gamma > 0:
                new_state, trigger = "PIN", "local dealer gamma positive"
            else:
                new_state, trigger = "TREND", "local dealer gamma negative"
        else:
            # No flip found — classify on local gamma sign alone.
            new_state = "PIN" if local_gamma > 0 else "TREND"
            trigger = f"no flip in sweep; local gamma {'positive' if local_gamma > 0 else 'negative'}"

        event = None
        if new_state != self.state:
            event = {"from": self.state, "to": new_state, "trigger": trigger,
                     "ts": now.isoformat()}
            self.state = new_state
            self.since = now

        dist_dollars = (spot - nearest) if nearest is not None else None
        state = {
            "state": self.state,
            "since": self.since.isoformat() if self.since else None,
            "seconds_in_state": (now - self.since).total_seconds() if self.since else 0,
            "flip": nearest,
            "flips_all": flip_result.get("flips", []),
            "flip_widened": flip_result.get("widened", False),
            "flip_error": flip_result.get("error"),
            "local_gamma": local_gamma,
            "dist_to_flip_dollars": dist_dollars,
            "dist_to_flip_atr": (abs(dist_dollars) / atr) if (dist_dollars is not None and atr) else None,
            "flip_migration": migration,
        }
        return state, event
