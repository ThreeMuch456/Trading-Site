"""Massive API adapter — option chains, greeks, OI, per-strike volume, and
(when entitled) trade prints. Massive (https://massive.com) is a
Polygon.io-compatible REST API; auth is a Bearer token. The feed is
15-minute delayed on the Starter/Developer tiers.

Plan-tier realities this adapter is built around (observed in the Options
Flow Scanner / Trading-Agent projects using the same account):
  - The plan answers ~5 requests/minute and then 429s. A shared sliding-window
    rate limiter throttles every call in the process; MASSIVE_RPM raises it.
  - `last_quote` (NBBO) is empty on every contract unless the plan includes
    quotes. `last_trade` likewise requires trade entitlement.
  - `/v3/trades/{contract}` is probed once; a 4xx marks prints not-entitled
    for the session and flow classification falls back to snapshot deltas.
  - Massive returns the PREVIOUS session's `day` bar for contracts that have
    not traded yet today (same shape, stale volume/close). `day.last_updated`
    is stamped inside the bar's own session, so `day_bar_date` lets callers
    drop stale volume instead of ingesting it.

SCHEMA MAPPING
--------------
Every field this app consumes is declared below as a list of candidate paths
into the raw JSON, in preference order. On the first successful chain fetch we
probe which candidate actually resolved for each field and log any field that
needed a fallback (or is missing entirely).
"""
import asyncio
import logging
import math
import time as _time
from datetime import datetime, date, timezone

import httpx

from .. import config

log = logging.getLogger("massive")

# our field -> candidate dotted paths into a chain-snapshot result item
CHAIN_FIELD_MAP = {
    "contract":   ["details.ticker", "ticker", "symbol"],
    "type":       ["details.contract_type", "contract_type", "type"],
    "strike":     ["details.strike_price", "strike_price", "strike"],
    "expiry":     ["details.expiration_date", "expiration_date", "expiry"],
    "bid":        ["last_quote.bid", "quote.bid", "bid"],
    "ask":        ["last_quote.ask", "quote.ask", "ask"],
    "midpoint":   ["last_quote.midpoint", "quote.midpoint"],
    "last_trade_price": ["last_trade.price"],
    "last_trade_ts_ns": ["last_trade.sip_timestamp", "last_trade.timestamp"],
    "day_close":  ["day.close"],
    "day_last_updated": ["day.last_updated"],
    "iv":         ["implied_volatility", "greeks.iv", "iv"],
    "delta":      ["greeks.delta", "delta"],
    "gamma":      ["greeks.gamma", "gamma"],
    "oi":         ["open_interest", "oi"],
    "volume":     ["day.volume", "volume", "day_volume"],
    # The chain snapshot carries the underlying's price as seen by the
    # options feed (T-15) — a free cross-check against the yfinance clock.
    "underlying_price": ["underlying_asset.price", "underlying_asset.value"],
}
# Fields that are entitlement-gated: absence is a plan limitation, not a
# schema problem — probe logs them at INFO, not WARNING.
ENTITLEMENT_FIELDS = {"bid", "ask", "midpoint", "last_trade_price",
                      "last_trade_ts_ns"}

# our field -> candidate paths into a trade-print result item
TRADE_FIELD_MAP = {
    "price":      ["price", "p"],
    "size":       ["size", "s"],
    "ts_ns":      ["sip_timestamp", "participant_timestamp", "timestamp", "t"],
    "id":         ["id", "sequence_number", "trade_id"],
    "conditions": ["conditions", "c"],
}


# Fields that must be real numbers before the engine touches them.
NUMERIC_CHAIN_FIELDS = {"strike", "bid", "ask", "midpoint", "last_trade_price",
                        "day_close", "iv", "delta", "gamma", "oi", "volume",
                        "underlying_price"}
NUMERIC_TRADE_FIELDS = {"price", "size"}


def _finite(v):
    """Coerce to a finite float, else None.

    This adapter speaks Polygon-compatible JSON, and Python's parser ACCEPTS
    the non-standard `NaN`/`Infinity` literals — so a failed IV solve on an
    illiquid 0DTE strike arrives as float('nan') rather than null. Left alone
    it propagates silently: NaN gamma makes dealer gamma NaN, which poisons
    the strike map, the wall ranking and the flip sweep, and finally 500s the
    API because JSON cannot represent it.

    A greek that is not a real number is a MISSING greek, which the engine
    already knows how to skip. This is the "never guess" rule applied at the
    source boundary rather than papered over downstream."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dig(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _resolve(obj, candidates):
    """Return (value, index_of_candidate_used or None)."""
    for i, path in enumerate(candidates):
        v = _dig(obj, path)
        if v is not None:
            return v, i
    return None, None


class MassiveError(Exception):
    def __init__(self, msg, status: int | None = None):
        super().__init__(msg)
        self.status = status


class MassiveBudgetError(MassiveError):
    """A best-effort call was dropped to protect the critical path. Not a
    source fault and not an entitlement verdict — the data simply wasn't
    affordable this poll, and the caller should degrade rather than retry."""


class _AsyncRateLimiter:
    """Sliding-window limiter shared by every Massive call in the process —
    the quota is per account, not per endpoint.

    Throttling here is invisible from the outside: a starved poll doesn't
    error, it just arrives late, so a too-small MASSIVE_RPM looks like a slow
    feed rather than a budget problem. The counters below make that
    distinction observable (surfaced on /healthz)."""

    def __init__(self, per_minute: int):
        self.per_minute = max(per_minute, 1)
        self._hits: list[float] = []
        self._lock = asyncio.Lock()
        self.requests = 0          # calls that passed through the gate
        self.throttled = 0         # calls that had to wait at all
        self.total_wait_s = 0.0    # cumulative time spent waiting on quota
        self.max_wait_s = 0.0      # worst single wait
        self.waiting = 0           # callers queued right now
        self.shed = 0              # best-effort calls dropped to protect the
                                   # critical path

    async def acquire(self, critical: bool = True) -> bool:
        """Take a slot. Returns True if granted.

        Critical calls (chain snapshots, expiry discovery) wait as long as
        needed. Best-effort calls (trade prints) may only use slots above the
        reserve, and are DROPPED rather than queued once they would wait
        longer than the configured ceiling — a print fetch that lands two
        minutes late is worth less than the chain snapshot it delayed."""
        ceiling = self.per_minute if critical else max(
            self.per_minute - config.CRITICAL_RESERVE, 0)
        waited = 0.0
        self.waiting += 1
        try:
            while True:
                async with self._lock:
                    now = _time.monotonic()
                    self._hits = [h for h in self._hits if now - h < 60]
                    if len(self._hits) < ceiling:
                        self._hits.append(now)
                        self.requests += 1
                        if waited > 0:
                            self.throttled += 1
                            self.total_wait_s += waited
                            self.max_wait_s = max(self.max_wait_s, waited)
                        return True
                    if not critical:
                        # No reserved headroom at all, or we'd wait too long.
                        if ceiling <= 0:
                            self.shed += 1
                            return False
                        wait = 60 - (now - self._hits[0]) + 0.25
                        if waited + wait > config.NONCRITICAL_MAX_WAIT_S:
                            self.shed += 1
                            return False
                    else:
                        wait = 60 - (now - self._hits[0]) + 0.25
                waited += wait
                await asyncio.sleep(wait)
        finally:
            self.waiting -= 1

    def stats(self) -> dict:
        return {
            "rpm_limit": self.per_minute,
            "critical_reserve": config.CRITICAL_RESERVE,
            "requests": self.requests,
            "throttled": self.throttled,
            "shed_best_effort": self.shed,
            "throttled_pct": round(100 * self.throttled / self.requests, 1)
            if self.requests else 0.0,
            "avg_wait_s": round(self.total_wait_s / self.throttled, 1)
            if self.throttled else 0.0,
            "max_wait_s": round(self.max_wait_s, 1),
            "queued_now": self.waiting,
        }


class MassiveClient:
    def __init__(self):
        self.base = config.MASSIVE_BASE_URL.rstrip("/")
        self.key = config.MASSIVE_API_KEY
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={"Authorization": f"Bearer {self.key}",
                     "Accept": "application/json"} if self.key else {})
        self._limiter = _AsyncRateLimiter(config.MASSIVE_RPM)
        self._probed = False
        self.probe_report: dict[str, str] = {}
        # None = unknown (not probed yet); True/False once determined.
        # Both are ACCOUNT-level, not per-ticker: probing once covers every
        # engine in the process.
        self.trades_entitled: bool | None = None
        self.aggs_entitled: bool | None = None
        # 403 is ambiguous on this API — it covers both "your plan lacks this"
        # and transient quota refusals. Latching it on the first occurrence
        # disabled the bar fallback for a whole session (observed 2026-08-06:
        # stock_aggs flipped true -> false between sessions with no plan
        # change, and the fallback then sat out a genuine Yahoo outage). Only
        # a sustained run of them is treated as a verdict.
        self.aggs_403_streak = 0

    async def close(self):
        await self._client.aclose()

    def rate_stats(self) -> dict:
        """Request-budget health. Sustained load is ~1 chain fetch per ticker
        per OPTION_POLL_SECONDS plus the next-expiry refresh; if throttled_pct
        climbs, MASSIVE_RPM is the binding constraint, not the feed."""
        return self._limiter.stats()

    async def _get(self, path, params=None, critical: bool = True):
        if not self.key:
            raise MassiveError("MASSIVE_API_KEY is not set", status=None)
        url = path if path.startswith("http") else f"{self.base}{path}"
        backoff = 2.0
        for attempt in range(4):
            if not await self._limiter.acquire(critical=critical):
                raise MassiveBudgetError(
                    f"best-effort call to {path} shed to protect the chain "
                    f"fetch (request budget saturated)")
            r = await self._client.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403, 404):
                raise MassiveError(f"HTTP {r.status_code} from {path}",
                                   status=r.status_code)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise MassiveError(
                        f"HTTP {r.status_code} from {path} after retries",
                        status=r.status_code)
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else backoff
                except ValueError:
                    delay = backoff
                await asyncio.sleep(min(delay, 30))
                backoff *= 2
                continue
            raise MassiveError(f"HTTP {r.status_code} from {path}: "
                               f"{r.text[:200]}", status=r.status_code)
        raise MassiveError(f"exhausted retries for {path}")

    def _probe(self, item):
        """One-time schema probe: log every field that fell back or is absent."""
        for field, candidates in CHAIN_FIELD_MAP.items():
            _, idx = _resolve(item, candidates)
            if idx is None:
                self.probe_report[field] = "MISSING"
                if field in ENTITLEMENT_FIELDS:
                    log.info("schema probe: %r absent — plan likely lacks the "
                             "entitlement (quotes/trades tier)", field)
                else:
                    log.warning("schema probe: field %r missing from chain "
                                "response (tried %s)", field, candidates)
            elif idx > 0:
                self.probe_report[field] = f"fallback:{candidates[idx]}"
                log.warning("schema probe: field %r resolved via fallback "
                            "path %r", field, candidates[idx])
            else:
                self.probe_report[field] = candidates[0]
        self._probed = True
        log.info("schema probe complete: %s", self.probe_report)

    @staticmethod
    def _day_bar_date(ns) -> str | None:
        """ET trading date the `day` aggregate belongs to. Massive keeps
        serving the prior session's bar for contracts that haven't traded
        today — this is how the two cases are separated."""
        if not ns:
            return None
        try:
            dt = datetime.fromtimestamp(float(ns) / 1e9, timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
        return dt.astimezone(config.ET).strftime("%Y-%m-%d")

    async def fetch_chain(self, underlying: str, expiry: date,
                          strike_gte: float | None = None,
                          strike_lte: float | None = None) -> list[dict]:
        """Option-chain snapshot for one expiry, normalized. Pass a strike
        window to keep pagination (and the request budget) small."""
        params = {"expiration_date": expiry.isoformat(), "limit": 250}
        if strike_gte is not None:
            params["strike_price.gte"] = strike_gte
        if strike_lte is not None:
            params["strike_price.lte"] = strike_lte
        out, url = [], f"/v3/snapshot/options/{underlying}"
        pages = 0
        while url and pages < 8:
            data = await self._get(url, params)
            params = None  # next_url already carries the query string
            results = data.get("results") or []
            if results and not self._probed:
                self._probe(results[0])
            for item in results:
                c = {}
                for field, candidates in CHAIN_FIELD_MAP.items():
                    v, _ = _resolve(item, candidates)
                    c[field] = _finite(v) if field in NUMERIC_CHAIN_FIELDS else v
                if c["strike"] is None or c["type"] is None:
                    continue
                c["type"] = "C" if str(c["type"]).lower().startswith("c") else "P"
                c["strike"] = float(c["strike"])
                c["key"] = f"{c['type']}:{c['strike']:g}"
                c["day_bar_date"] = self._day_bar_date(c.pop("day_last_updated"))
                # best available contract price, in preference order
                c["mark"] = c.get("midpoint") or c.get("last_trade_price") \
                    or c.get("day_close")
                out.append(c)
            url = data.get("next_url")
            pages += 1
        if url:
            log.warning("%s: chain truncated after %d pages", underlying, pages)
        return out

    async def probe_trades_entitlement(self, option_ticker: str,
                                       since_ns: int) -> bool:
        """One-shot check whether the plan includes trade prints."""
        if self.trades_entitled is not None:
            return self.trades_entitled
        try:
            # Critical: one call per session, and the answer selects the flow
            # path. Shedding it strands entitlement at "unknown" forever.
            await self.fetch_trades(option_ticker, since_ns, critical=True)
            self.trades_entitled = True
            log.info("trade prints ARE entitled on this plan — using "
                     "print-level flow classification")
        except MassiveError as e:
            if e.status in (401, 403, 404):
                self.trades_entitled = False
                log.warning("trade prints NOT entitled (HTTP %s) — falling "
                            "back to snapshot-delta flow classification, "
                            "all flow flagged low-confidence", e.status)
            else:
                raise  # transient — leave undetermined, retry next poll
        return self.trades_entitled

    async def fetch_trades(self, option_ticker: str, since_ns: int,
                           critical: bool = False) -> list[dict]:
        """Trade prints for one contract since a nanosecond timestamp.

        Best-effort by default: routine print fetches must never delay a chain
        snapshot. The entitlement probe passes critical=True — it runs once per
        session and decides which flow path the whole session uses, so letting
        it be shed means never discovering the plan has prints at all."""
        params = {"timestamp.gte": since_ns, "limit": 1000,
                  "sort": "timestamp", "order": "asc"}
        out, url = [], f"/v3/trades/{option_ticker}"
        pages = 0
        while url and pages < 3:
            data = await self._get(url, params, critical=critical)
            params = None
            for item in data.get("results") or []:
                t = {}
                for field, candidates in TRADE_FIELD_MAP.items():
                    v, _ = _resolve(item, candidates)
                    t[field] = _finite(v) if field in NUMERIC_TRADE_FIELDS else v
                if t["price"] is None or t["size"] is None or t["ts_ns"] is None:
                    continue
                out.append(t)
            url = data.get("next_url")
            pages += 1
        return out

    async def probe_aggs_entitlement(self, ticker: str, day: date) -> bool | None:
        """One-shot check whether the plan includes STOCK aggregates.

        Stock aggs are the fallback that keeps the 1-minute bar series alive
        when Yahoo rate-limits the underlying feed (a live risk on datacenter
        IPs). Left unprobed, entitlement stays unknown until the first Yahoo
        outage — meaning the safety net is only tested at the moment it is
        needed. Probing at startup costs one request and turns that into a
        boot-time fact.

        Returns None when the answer is inconclusive (transient error), so the
        caller can retry rather than record a false negative.
        """
        if self.aggs_entitled is not None:
            return self.aggs_entitled
        try:
            await self.fetch_minute_bars(ticker, day)
        except MassiveError as e:
            # Only 401 is unambiguous. 404 can mean an empty date range, and
            # 403 covers transient quota refusals as well as missing
            # entitlement — neither is a verdict on its own.
            if e.status == 401:
                self.aggs_entitled = False
                log.warning("stock 1m aggregates NOT entitled (HTTP 401) — "
                            "there is NO underlying bar fallback; if yfinance "
                            "fails, the bar series stalls")
                return False
            if e.status == 403:
                self.aggs_403_streak += 1
                if self.aggs_403_streak >= config.AGGS_403_VERDICT_STREAK:
                    self.aggs_entitled = False
                    log.warning("stock 1m aggregates refused with HTTP 403 %d "
                                "times running — treating as not entitled. "
                                "There is NO underlying bar fallback.",
                                self.aggs_403_streak)
                    return False
                log.warning("stock aggs probe got HTTP 403 (%d/%d before it is "
                            "treated as a verdict) — leaving entitlement "
                            "unknown and retryable",
                            self.aggs_403_streak, config.AGGS_403_VERDICT_STREAK)
                return None
            log.warning("stock aggs entitlement probe inconclusive: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 — inconclusive, not a verdict
            log.warning("stock aggs entitlement probe inconclusive: %s", e)
            return None
        self.aggs_403_streak = 0
        self.aggs_entitled = True
        log.info("stock 1m aggregates ARE entitled — underlying bar fallback "
                 "is available if yfinance fails")
        return True

    async def fetch_minute_bars(self, ticker: str, day: date) -> list[dict]:
        """1-minute stock aggregates (15-min delayed) — the two-clock
        fallback when yfinance is rate-limited. Delayed bars are fine for
        everything except live spot: flow is evaluated against bars that are
        >=15 min old by definition. Stock aggs may not be included in an
        options-only plan; callers must treat MassiveError 401/403/404 as
        'not entitled' and stop asking."""
        url = (f"/v2/aggs/ticker/{ticker}/range/1/minute/"
               f"{day.isoformat()}/{day.isoformat()}")
        data = await self._get(url, {"adjusted": "true", "sort": "asc",
                                     "limit": 50000})
        out = []
        for r in data.get("results") or []:
            t = r.get("t")
            if t is None or r.get("c") is None:
                continue
            dt = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(config.ET)
            bar = {"ts": dt.isoformat(), "o": _finite(r.get("o")),
                   "h": _finite(r.get("h")), "l": _finite(r.get("l")),
                   "c": _finite(r.get("c")), "v": _finite(r.get("v")) or 0.0}
            if bar["c"] is None:   # a bar without a close is a gap, not data
                continue
            out.append(bar)
        return out

    async def has_contracts_for_expiry(self, underlying: str, day: date) -> bool:
        """Cheap same-day-expiry check — one request, no chain pagination."""
        data = await self._get("/v3/reference/options/contracts", {
            "underlying_ticker": underlying,
            "expiration_date": day.isoformat(),
            "limit": 1,
        })
        return bool(data.get("results"))

    async def fetch_next_expiry(self, underlying: str, after: date) -> date | None:
        """Earliest listed expiration strictly after `after`."""
        data = await self._get("/v3/reference/options/contracts", {
            "underlying_ticker": underlying,
            "expiration_date.gt": after.isoformat(),
            "sort": "expiration_date", "order": "asc", "limit": 1,
        })
        results = data.get("results") or []
        if not results:
            return None
        exp, _ = _resolve(results[0], ["expiration_date", "details.expiration_date"])
        return date.fromisoformat(exp) if exp else None

    async def fetch_trades_bulk(self, tickers_since: list[tuple[str, int]],
                                concurrency: int = 2) -> dict[str, list | None]:
        """Fetch prints for several contracts. Concurrency stays low — the
        account-wide rate limiter is the real throttle. A contract whose
        fetch errors maps to None so callers can tell 'no trades' from
        'fetch failed'."""
        sem = asyncio.Semaphore(concurrency)
        out: dict[str, list | None] = {}

        async def one(tk, since):
            async with sem:
                try:
                    out[tk] = await self.fetch_trades(tk, since)
                except Exception as e:  # noqa: BLE001 — recorded, surfaced upstream
                    log.warning("trade fetch failed for %s: %s", tk, e)
                    out[tk] = None

        await asyncio.gather(*(one(tk, s) for tk, s in tickers_since))
        return out
