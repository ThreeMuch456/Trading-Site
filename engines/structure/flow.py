"""Options flow features for the learning log — logged, never scored.

Two sources, both best-effort (any failure -> None -> NULL features):

  1. **Snapshot flow** (`compute_flow`) — derived from the Massive chain snapshot
     the GEX phase already fetched and cached (src/gex.py), so it costs no extra
     API calls. Premium = day volume × day VWAP × 100, by side, strike and DTE.
  2. **Scanner flow** (`fetch_scanner_flow`) — the Options Flow Scanner's 24h
     per-ticker rollup (`GET /api/ticker/<symbol>`). It polls the same chains
     every 5 min, so it sees what one EOD snapshot can't: which contracts were
     *flagged* as unusual during the session (premium / vol-OI / burst gates).

These are X for src/tune.py to test against outcomes. They do NOT enter
`score.combined` — flag, GEX and RVOL all looked plausible too and sorted
backwards once labeled, so flow has to earn a weight on /learning first.

Caveats baked into the numbers:
  - No bid/ask entitlement on either side: a bought call and a sold call look
    identical, so "aligned share" measures *activity* in the setup's direction,
    not bullishness/bearishness.
  - Massive re-serves the previous session's day bar for contracts that haven't
    traded today. Those are dropped from every volume/premium sum (and counted,
    see `stale_share`); contracts with no bar date are kept (permissive — they
    have usually never traded, so they carry no volume anyway).
  - Deep-ITM contracts are excluded from premium: intrinsic-value prints (spread
    legs, buy-writes) inflate notional without expressing a directional view.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from .config import Config


@dataclass
class FlowInfo:
    session_date: str                 # the session the volume belongs to
    call_premium: float               # $ traded, fresh bars, ex deep-ITM
    put_premium: float
    call_premium_short: float         # the same, DTE <= short_dte_days only
    put_premium_short: float
    call_volume: float                # fresh-bar contracts traded
    put_volume: float
    call_oi: float                    # open interest over the same window (all contracts)
    put_oi: float
    n_fresh: int                      # volume-bearing contracts with a current bar
    n_stale: int                      # volume-bearing contracts dropped as stale
    iv_skew: float | None = None      # ~25Δ put IV − ~25Δ call IV (vol points, 0.05 = 5)
    strikes: list = field(default_factory=list)  # [(strike, call_$, put_$)] ascending
    snapshot_at: str | None = None    # ISO UTC fetch time of the snapshot used

    @property
    def premium(self) -> float:
        return self.call_premium + self.put_premium

    def _side(self, direction: str) -> str:
        return "call" if direction == "up" else "put"

    def aligned_share(self, direction: str) -> float | None:
        """Share of premium on the setup's side: calls for 'up', puts for 'down'."""
        if self.premium <= 0:
            return None
        side = self.call_premium if self._side(direction) == "call" else self.put_premium
        return round(side / self.premium, 4)

    def aligned_short_share(self, direction: str) -> float | None:
        """Of the aligned side's premium, the share in short-dated expiries."""
        if self._side(direction) == "call":
            tot, short = self.call_premium, self.call_premium_short
        else:
            tot, short = self.put_premium, self.put_premium_short
        return round(short / tot, 4) if tot > 0 else None

    def aligned_vol_oi(self, direction: str) -> float | None:
        """Aligned side's fresh volume over its open interest (new positioning)."""
        if self._side(direction) == "call":
            vol, oi = self.call_volume, self.call_oi
        else:
            vol, oi = self.put_volume, self.put_oi
        return round(vol / oi, 4) if oi > 0 else None

    def pocket_share(self, direction: str, spot: float, edge: float | None) -> float | None:
        """Of the aligned side's premium, the share struck inside the air pocket —
        between spot and `edge` (the far HVN, else the LVN). Positioning aimed
        through the thin zone is the most on-thesis flow there is."""
        return pocket_share(self.strikes, direction, spot, edge)

    @property
    def stale_share(self) -> float | None:
        n = self.n_fresh + self.n_stale
        return round(self.n_stale / n, 4) if n else None


def pocket_share(strikes, direction: str, spot: float, edge: float | None) -> float | None:
    """Shared by both sources: `strikes` is [(strike, call_$, put_$)]."""
    if not strikes or not edge or spot <= 0:
        return None
    col = 1 if direction == "up" else 2
    lo, hi = sorted((float(spot), float(edge)))
    tot = sum(float(s[col]) for s in strikes)
    if tot <= 0:
        return None
    inside = sum(float(s[col]) for s in strikes if lo <= float(s[0]) <= hi)
    return round(inside / tot, 4)


def _deep_itm(c: dict, spot: float, deep_delta: float) -> bool:
    """Delta when present; moneyness (15% ITM) when greeks are absent — Massive
    often omits greeks on exactly these contracts."""
    d = c.get("delta")
    if d is not None:
        try:
            return abs(float(d)) >= deep_delta
        except (TypeError, ValueError):
            pass
    k = float(c.get("strike") or 0)
    if k <= 0:
        return False
    return k <= spot * 0.85 if c["cp"] == "call" else k >= spot * 1.15


def _iv_skew(rows: list[dict], spot: float, session: date) -> float | None:
    """~25-delta put IV minus ~25-delta call IV on the nearest expiry at least a
    week out (nearest overall if none). Delta band 0.15-0.35; moneyness band
    3-10% OTM when greeks are absent. Positive = puts bid over calls."""
    exps = sorted({r["exp"] for r in rows})
    if not exps:
        return None
    week = [e for e in exps if (date.fromisoformat(e) - session).days >= 7]
    exp = week[0] if week else exps[0]

    def wing(cp: str) -> list[float]:
        out = []
        for r in rows:
            if r["exp"] != exp or r["cp"] != cp:
                continue
            iv = float(r.get("iv") or 0)
            if not 0.01 < iv < 5.0:
                continue
            d, k = r.get("delta"), float(r.get("strike") or 0)
            if d is not None:
                ok = 0.15 <= abs(float(d)) <= 0.35
            elif k > 0:
                otm = (k - spot) / spot if cp == "call" else (spot - k) / spot
                ok = 0.03 <= otm <= 0.10
            else:
                ok = False
            if ok:
                out.append(iv)
        return out

    puts, calls = wing("put"), wing("call")
    if not puts or not calls:
        return None
    return round(float(np.mean(puts)) - float(np.mean(calls)), 4)


def compute_flow(contracts: list[dict], spot: float, session_date: date,
                 max_days: int = 45, short_dte_days: int = 7,
                 deep_itm_delta: float = 0.95) -> FlowInfo | None:
    """Flow aggregates from trimmed Massive contracts (pure — see gex._trim_contract).

    `session_date` is the market-tz date the screen is for; a contract's volume
    counts only if its day bar ("bar") is that session. Expiries from the session
    itself (0DTE — its volume is today's flow) out to `max_days`. None when no
    contract carries usable fresh premium (pre-upgrade cache without "px", a
    non-trading day, or no listed options)."""
    if spot <= 0 or not contracts:
        return None
    session_iso = session_date.isoformat()

    rows: list[dict] = []
    for c in contracts:
        if c.get("cp") not in ("call", "put"):
            continue
        try:
            dte = (date.fromisoformat(c.get("exp") or "") - session_date).days
        except ValueError:
            continue
        if 0 <= dte <= max_days:
            rows.append({**c, "_dte": dte})
    if not rows:
        return None

    prem = {"call": 0.0, "put": 0.0}
    prem_short = {"call": 0.0, "put": 0.0}
    vol = {"call": 0.0, "put": 0.0}
    oi = {"call": 0.0, "put": 0.0}
    by_strike: dict[float, list[float]] = {}
    n_fresh = n_stale = 0
    for r in rows:
        cp = r["cp"]
        oi[cp] += float(r.get("oi") or 0)
        v = float(r.get("vol") or 0)
        if v <= 0:
            continue
        bar = r.get("bar")
        if bar is not None and bar != session_iso:
            n_stale += 1
            continue
        n_fresh += 1
        vol[cp] += v
        px = float(r.get("px") or 0)
        if px <= 0 or _deep_itm(r, spot, deep_itm_delta):
            continue
        dollars = v * px * 100.0
        prem[cp] += dollars
        if r["_dte"] <= short_dte_days:
            prem_short[cp] += dollars
        k = r.get("strike")
        if k:
            slot = by_strike.setdefault(float(k), [0.0, 0.0])
            slot[0 if cp == "call" else 1] += dollars

    if prem["call"] + prem["put"] <= 0:
        return None
    return FlowInfo(
        session_date=session_iso,
        call_premium=round(prem["call"], 2), put_premium=round(prem["put"], 2),
        call_premium_short=round(prem_short["call"], 2),
        put_premium_short=round(prem_short["put"], 2),
        call_volume=vol["call"], put_volume=vol["put"],
        call_oi=oi["call"], put_oi=oi["put"],
        n_fresh=n_fresh, n_stale=n_stale,
        iv_skew=_iv_skew(rows, spot, session_date),
        strikes=[(k, round(c, 2), round(p, 2)) for k, (c, p) in sorted(by_strike.items())],
    )


def _post_close(cfg: Config, session_date: date) -> datetime:
    """When a snapshot first holds the whole session: 16:00 market tz plus the
    feed delay (the 15:45-16:00 prints land in snapshots ~15 min late)."""
    from .schedule import market_tz
    delay = int(cfg.get("live.data_delay_min", 15))
    return datetime.combine(session_date, time(16, 0), tzinfo=market_tz(cfg)) + timedelta(minutes=delay)


def snapshot_flow(ticker: str, spot: float, cfg: Config, session_date: date,
                  now: datetime | None = None) -> FlowInfo | None:
    """compute_flow over the Massive snapshot the GEX phase cached.

    The cache lives gex.cache_hours (12h) and the intraday live monitor also
    fills it, so a 17:00 screen can find a 10:00 snapshot — mid-session volume
    that would be logged as the day's flow. When we're past the close and the
    cached copy predates it, refetch once (unlimited plan; GEX doesn't mind a
    fresher chain either). Before the close (a manual intraday run) whatever is
    cached is used as-is; `snapshot_at` is recorded either way so the log can
    be audited. None when unavailable or unusable."""
    from .gex import _massive_snapshot, read_snapshot_cache, snapshot_cache_ts
    try:
        cutoff = _post_close(cfg, session_date)
        now = now or datetime.now(timezone.utc)
        ts = snapshot_cache_ts(cfg, ticker)
        if now >= cutoff and (ts is None or ts < cutoff.timestamp()):
            contracts = _massive_snapshot(ticker, cfg, force=True)
            ts = snapshot_cache_ts(cfg, ticker)
        else:
            contracts = read_snapshot_cache(cfg, ticker)
        if not contracts:
            return None
        info = compute_flow(
            contracts, spot, session_date,
            max_days=int(cfg.get("gex.max_days", 45)),
            short_dte_days=int(cfg.get("flow.short_dte_days", 7)),
            deep_itm_delta=float(cfg.get("flow.deep_itm_delta", 0.95)),
        )
        if info is not None and ts:
            info.snapshot_at = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return info
    except Exception:  # noqa: BLE001 - a bonus feature; never fail the screen
        return None


# ── Options Flow Scanner (step 2) ───────────────────────────────────────────────

def scanner_url(cfg: Config) -> str | None:
    """Base URL of the Options Flow Scanner, env FLOW_SCANNER_URL first. None =
    disabled (the default: an unconfigured URL must not cost the job anything)."""
    url = os.environ.get("FLOW_SCANNER_URL") or cfg.get("flow.scanner_url")
    return str(url).rstrip("/") if url else None


def parse_scanner_payload(all_scope: dict, notable_scope: dict | None) -> dict | None:
    """Flatten the scanner's /api/ticker JSON into flat features (pure).

    None when the window held no alerts (`as_of` null). That is deliberate: the
    scanner answers zeros both for a ticker outside its universe and for a
    covered ticker with a quiet day, and those must not be logged as the same
    "no flow" — a covered-but-quiet signal is the smaller loss."""
    if not all_scope or not all_scope.get("as_of"):
        return None
    t = all_scope.get("totals") or {}
    out = {
        "as_of": all_scope.get("as_of"),
        "window_hours": all_scope.get("window_hours"),
        "call_premium": float(t.get("call_premium") or 0.0),
        "put_premium": float(t.get("put_premium") or 0.0),
        "contracts": int(t.get("contracts") or 0),
        "strikes": [(float(s["strike"]), float(s.get("call_premium") or 0.0),
                     float(s.get("put_premium") or 0.0))
                    for s in (all_scope.get("by_strike") or []) if s.get("strike")],
        "notable_call_premium": None,
        "notable_put_premium": None,
        "notable_contracts": None,
    }
    if notable_scope is not None:
        n = notable_scope.get("totals") or {}
        out["notable_call_premium"] = float(n.get("call_premium") or 0.0)
        out["notable_put_premium"] = float(n.get("put_premium") or 0.0)
        out["notable_contracts"] = int(n.get("contracts") or 0)
    return out


def fetch_scanner_flow(ticker: str, cfg: Config) -> dict | None:
    """The scanner's 24h rollup for one ticker (all alerts + the notable subset).
    None when disabled, unreachable, or no alerts in the window."""
    import requests

    base = scanner_url(cfg)
    if not base:
        return None
    timeout = float(cfg.get("flow.scanner_timeout", 15))
    sym = ticker.upper()
    try:
        resp = requests.get(f"{base}/api/ticker/{sym}", timeout=timeout)
        resp.raise_for_status()
        all_scope = resp.json()
        if not all_scope.get("as_of"):
            return None                       # no alerts -> skip the second call
        notable = None
        try:
            r2 = requests.get(f"{base}/api/ticker/{sym}", params={"scope": "notable"},
                              timeout=timeout)
            r2.raise_for_status()
            notable = r2.json()
        except Exception:  # noqa: BLE001 - notable subset is optional
            notable = None
        return parse_scanner_payload(all_scope, notable)
    except Exception:  # noqa: BLE001 - external bonus signal; never fail the screen
        return None


def prefetch_scanner_flow(tickers: list[str], cfg: Config) -> dict[str, dict | None]:
    """fetch_scanner_flow over the universe in parallel. {} when disabled."""
    if not tickers or not scanner_url(cfg):
        return {}
    workers = max(1, int(cfg.get("flow.scanner_workers", 4)))
    out: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_scanner_flow, t, cfg): t for t in tickers}
        for fut in as_completed(futures):
            try:
                out[futures[fut]] = fut.result()
            except Exception:  # noqa: BLE001
                out[futures[fut]] = None
    n = sum(1 for v in out.values() if v)
    print(f"[flow] scanner rollup for {n}/{len(tickers)} tickers")
    return out
