"""Signed dealer gamma: frozen OI baseline + cumulative classified flow.

Baseline (start-of-day OI) uses the standard convention — dealers long calls,
short puts — because no aggression data exists for prior sessions. It is
snapshotted once at 09:30 ET and frozen. It is the ASSUMPTION layer.

Today's flow is the OBSERVATION layer. Gamma is positive for both calls and
puts; the sign comes from which side the dealer ended up on:
    customer buy  -> dealer short -> NEGATIVE gamma contribution
    customer sell -> dealer long  -> POSITIVE gamma contribution

observed_share per strike = |flow magnitude| / (|flow| + |baseline|):
the fraction of a level that is measured rather than assumed.
"""
from dataclasses import dataclass, field

from . import bs


@dataclass
class ContractFlow:
    net_customer: float = 0.0        # net contracts bought by customers
    buy_contracts: float = 0.0
    sell_contracts: float = 0.0
    unclassified_contracts: float = 0.0
    dealer_gamma: float = 0.0        # accumulated signed $ gamma from flow
    low_conf_contracts: float = 0.0  # classified only via tick test


class GexEngine:
    def __init__(self):
        self.baseline: dict[str, float] = {}       # contract key -> signed $ gamma
        self.baseline_oi: dict[str, float] = {}    # contract key -> signed contracts
        self.baseline_ts: str | None = None
        self.baseline_frozen = False
        self.baseline_late = False                 # frozen after 09:30 (late start)
        self.flow: dict[str, ContractFlow] = {}    # contract key -> flow aggregate
        self.contract_meta: dict[str, dict] = {}   # key -> {strike,type,iv,gamma,delta}

    # ---- baseline ----
    def freeze_baseline(self, chain: list[dict], spot: float, ts_iso: str, late: bool):
        """Snapshot start-of-day OI GEX once and never touch it again."""
        if self.baseline_frozen:
            return
        for c in chain:
            gamma, oi = c.get("gamma"), c.get("oi")
            if gamma is None or oi in (None, 0):
                continue
            sign = 1.0 if c["type"] == "C" else -1.0
            self.baseline[c["key"]] = sign * gamma * oi * 100.0 * spot * spot * 0.01
            self.baseline_oi[c["key"]] = sign * oi
        self.baseline_ts = ts_iso
        self.baseline_frozen = True
        self.baseline_late = late

    # ---- flow ----
    def update_meta(self, chain: list[dict]):
        for c in chain:
            self.contract_meta[c["key"]] = {
                "strike": c["strike"], "type": c["type"],
                "iv": c.get("iv"), "gamma": c.get("gamma"),
                "delta": c.get("delta"),
            }

    def apply_print(self, key: str, side: str, size: float, gamma: float | None,
                    spot_at_print: float, low_conf: bool):
        """spot_at_print comes from the 1-minute bar matching the PRINT's
        timestamp (two-clock rule), never current spot."""
        f = self.flow.setdefault(key, ContractFlow())
        if side == "unclassified":
            f.unclassified_contracts += size
            return
        signed = size if side == "buy" else -size
        f.net_customer += signed
        if side == "buy":
            f.buy_contracts += size
        else:
            f.sell_contracts += size
        if low_conf:
            f.low_conf_contracts += size
        if gamma is not None and spot_at_print:
            # dealer takes the other side: -(net customer bought)
            f.dealer_gamma += bs.dollar_gamma(-signed, gamma, spot_at_print)

    # ---- aggregation ----
    def strike_map(self) -> dict[float, dict]:
        """Live map = frozen baseline + cumulative signed flow, per strike."""
        strikes: dict[float, dict] = {}

        def slot(strike):
            return strikes.setdefault(strike, {
                "strike": strike, "baseline": 0.0, "flow": 0.0,
                "net_customer": 0.0, "unclassified": 0.0, "low_conf": 0.0,
                # gross buy+sell contracts, the denominator for low_conf_share
                "classified": 0.0})

        for key, g in self.baseline.items():
            meta = self.contract_meta.get(key)
            strike = meta["strike"] if meta else float(key.split(":")[1])
            slot(strike)["baseline"] += g
        for key, f in self.flow.items():
            meta = self.contract_meta.get(key)
            strike = meta["strike"] if meta else float(key.split(":")[1])
            s = slot(strike)
            s["flow"] += f.dealer_gamma
            s["net_customer"] += f.net_customer
            s["unclassified"] += f.unclassified_contracts
            s["low_conf"] += f.low_conf_contracts
            s["classified"] += f.buy_contracts + f.sell_contracts

        for s in strikes.values():
            s["dealer_gamma"] = s["baseline"] + s["flow"]
            denom = abs(s["baseline"]) + abs(s["flow"])
            s["observed_share"] = (abs(s["flow"]) / denom) if denom > 0 else 0.0
            # Both sides of this ratio must be GROSS contract counts.
            # low_conf_contracts accumulates every low-confidence classified
            # trade, buys and sells alike, so dividing it by NET customer flow
            # (buys minus sells) compared a gross numerator against a
            # near-cancelling denominator: two-sided tape — the normal case in
            # 0DTE — drove it far above 1.0, and perfectly balanced flow drove
            # the denominator to 0 and collapsed it to 0.0.
            s["low_conf_share"] = (s["low_conf"] / s["classified"]) \
                if s["classified"] > 0 else 0.0
        return strikes

    def dealer_positions(self) -> dict[str, float]:
        """Signed dealer contract position per contract, for the flip sweep:
        baseline OI position + accumulated flow position."""
        pos = dict(self.baseline_oi)
        for key, f in self.flow.items():
            pos[key] = pos.get(key, 0.0) - f.net_customer
        return pos
