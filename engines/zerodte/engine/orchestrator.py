"""Per-ticker engine: owns state, applies the two-clock rule, recomputes the
map every poll, and emits events. All mutation happens on the event loop."""
import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timedelta

from .. import config
from ..sources.massive import MassiveClient, MassiveError
from ..sources.underlying import NoSessionBars, UnderlyingFeed
from . import classify, flip as flip_mod, gex, levels as levels_mod
from . import metrics as metrics_mod, regime as regime_mod
from . import touches as touches_mod, zones as zones_mod

log = logging.getLogger("engine")

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def _now():
    return datetime.now(config.ET)


class TickerEngine:
    def __init__(self, ticker: str, db, massive: MassiveClient):
        self.ticker = ticker
        self.db = db
        self.massive = massive
        self.feed = UnderlyingFeed(ticker)
        self.gex = gex.GexEngine()
        self.regime = regime_mod.RegimeMachine()
        self.session_date = _now().date().isoformat()

        # underlying (fast clock)
        self.bars: list[dict] = []
        self.bar_by_minute: dict[str, dict] = {}   # "HH:MM" -> bar
        self.spot: float | None = None
        self.spot_ts: str | None = None
        self.session_high = self.session_low = None
        self.underlying_status = {"ok": False, "error": "not yet polled",
                                  "last_good": None}
        # True while spot is too old to derive a positional map from.
        self.map_frozen = False

        # options (slow clock, T-15)
        self.expiry = None
        self.no_0dte = False
        self.chain: list[dict] = []
        self.chain_fetched_at: str | None = None
        self.option_asof: str | None = None        # fetch time minus feed delay
        self.option_status = {"ok": False, "error": "not yet polled",
                              "last_good": None}
        self.next_expiry = None
        self.next_atm_iv: float | None = None
        self._next_expiry_checked_at: float = 0.0
        self.prev_volume: dict[str, float] = {}    # contract -> day volume last poll
        self.prev_mark: dict[str, float] = {}      # contract -> mark last poll
        self.trades_since_ns: dict[str, int] = {}
        self.seen_trades: dict[str, set] = {}
        self.quote_history: dict[str, deque] = {}  # contract -> (epoch, bid, ask)
        self.last_print_price: dict[str, float] = {}
        self.print_log: dict[float, list[dict]] = {}  # strike -> prints
        self.flow_coverage = {"classified_volume": 0.0, "chain_volume": 0.0}
        self.flow_source: str = "unknown"  # 'prints' | 'snapshot_deltas'
        self.prints_over_budget = False    # entitled to prints, can't afford them
        self._first_flow_poll = True

        # levels
        self.strike_map: dict[float, dict] = {}
        self.walls = {"above": [], "below": []}
        self.max_pain: float | None = None
        self.straddle_snap: dict | None = None
        self.em: dict | None = None
        self.baseline_anchor_source: str | None = None
        self.flip_result: dict = {"flips": [], "nearest": None, "local_gamma": 0.0,
                                  "widened": False, "error": "not computed"}
        self.regime_state: dict = {}
        self.level_history: dict[float, deque] = {}  # strike -> (asof_epoch, |gamma|)
        self.levels_version = 0
        self._five_hash = None

        # touches / zones / events
        self.trackers: dict[float, touches_mod.TouchTracker] = {}
        self.resolved_touches: list[touches_mod.TouchTracker] = []
        self.breached_strikes: set[float] = set()
        self.decayed_strikes: set[float] = set()
        self.zones: list[dict] = []
        self.expired_zones: list[dict] = []
        self.events: list[dict] = []
        self._last_processed_bar: str | None = None
        self._prev_wall_strikes: set[float] = set()
        self._had_first_walls = False

        # source health
        self.underlying_fail_streak = 0
        # Massive 1-minute-agg fallback for the two-clock bar series when
        # yfinance is rate-limited. Entitlement lives on the shared client
        # (`massive.aggs_entitled`) because it is account-level: probing it
        # once covers every ticker, instead of all three engines separately
        # rediscovering a 403 during an outage, when budget is scarcest.
        self._last_bar_fallback = 0.0
        self._fallback_active = False
        self._last_hist_retry = 0.0
        self.chain_underlying_price: float | None = None

    # --------------------------------------------------------- session rollover
    async def maybe_roll_session(self):
        """Start a clean session when the ET date changes.

        The process is long-lived on a host like Railway, so nothing about a
        session may carry over: the 09:30 baseline is a per-day assumption
        layer, the expiry changes, and yesterday's touches/zones/flow would
        otherwise be treated as today's.
        """
        import asyncio
        today = _now().date().isoformat()
        if today == self.session_date:
            return
        prior = self.session_date
        self.session_date = today
        log.info("%s: session rollover %s -> %s", self.ticker, prior, today)

        # underlying
        self.bars = []
        self.bar_by_minute = {}
        self.spot = self.spot_ts = None
        self.session_high = self.session_low = None
        self.underlying_status = {"ok": False, "error": "not yet polled",
                                  "last_good": None}
        self.underlying_fail_streak = 0
        self._fallback_active = False
        self.map_frozen = False
        self._last_bar_fallback = 0.0
        self.chain_underlying_price = None
        # Aggs entitlement lives on the shared client and survives rollover —
        # the plan doesn't change overnight.

        # options
        self.expiry = None
        self.no_0dte = False
        self.chain = []
        self.chain_fetched_at = self.option_asof = None
        self.option_status = {"ok": False, "error": "not yet polled",
                              "last_good": None}
        self.next_expiry = None
        self.next_atm_iv = None
        self._next_expiry_checked_at = 0.0
        self.prev_volume = {}
        self.prev_mark = {}
        self.trades_since_ns = {}
        self.seen_trades = {}
        self.quote_history = {}
        self.last_print_price = {}
        self.print_log = {}
        self.flow_coverage = {"classified_volume": 0.0, "chain_volume": 0.0}
        self.prints_over_budget = False
        self._first_flow_poll = True
        # Plan entitlement doesn't change overnight — keep the probe result.
        self.flow_source = ("prints" if self.massive.trades_entitled else
                            "snapshot_deltas") if self.massive.trades_entitled \
            is not None else "unknown"

        # engines carrying frozen per-session assumptions
        self.gex = gex.GexEngine()
        self.regime = regime_mod.RegimeMachine()

        # levels
        self.strike_map = {}
        self.walls = {"above": [], "below": []}
        self.max_pain = None
        self.straddle_snap = None
        self.em = None
        self.baseline_anchor_source = None
        self.flip_result = {"flips": [], "nearest": None, "local_gamma": 0.0,
                            "widened": False, "error": "not computed"}
        self.regime_state = {}
        self.level_history = {}
        self.levels_version = 0
        self._five_hash = None

        # touches / zones / events
        self.trackers = {}
        self.resolved_touches = []
        self.breached_strikes = set()
        self.decayed_strikes = set()
        self.zones = []
        self.expired_zones = []
        self.events = []
        self._last_processed_bar = None
        self._prev_wall_strikes = set()
        self._had_first_walls = False

        self.emit("SESSION_ROLLOVER", {"from": prior, "to": today})
        # Prior close, 20d volume and the time-of-day baseline are all
        # day-specific — reload them.
        try:
            await asyncio.to_thread(self.feed.load_history)
        except Exception as e:
            log.warning("%s: history reload failed: %s", self.ticker, e)
        await self._discover_expiry()

    # ------------------------------------------------------------------ events
    def emit(self, type_: str, payload: dict):
        ev = {"ts": _now().isoformat(), "type": type_, "ticker": self.ticker,
              "payload": payload}
        self.events.append(ev)
        self.events = self.events[-300:]
        try:
            self.db.save_event(self.session_date, self.ticker, ev["ts"], type_, payload)
        except Exception:
            log.exception("event persist failed")

    # ---------------------------------------------------------------- startup
    async def start(self):
        import asyncio
        try:
            await asyncio.to_thread(self.feed.load_history)
        except Exception as e:
            log.warning("%s: history load failed: %s", self.ticker, e)
        await self._discover_expiry()

    async def _discover_expiry(self):
        today = _now().date()
        if today.weekday() >= 5:
            self.no_0dte = True
            self.emit("NO_0DTE", {"reason": "weekend"})
            return
        try:
            has_today = await self.massive.has_contracts_for_expiry(self.ticker, today)
        except Exception as e:
            self.option_status = {"ok": False, "error": f"expiry probe: {e}",
                                  "last_good": self.option_status.get("last_good")}
            return
        if not has_today:
            self.no_0dte = True
            self.emit("NO_0DTE", {"reason": f"no contracts expiring {today}"})
        else:
            self.expiry = today

    # ------------------------------------------------------- underlying (15s)
    async def poll_underlying(self):
        import asyncio
        try:
            data = await asyncio.to_thread(self.feed.poll)
        except NoSessionBars as e:
            # No bars for today yet. Before the open that is simply the truth,
            # not a fault: counting it would leave the backoff minutes deep by
            # 09:30 and delay the baseline freeze. After the open it is a real
            # outage and falls through to the normal failure path.
            if _now().timetz().replace(tzinfo=None) < config.SESSION_OPEN:
                self.underlying_status = {
                    "ok": False, "error": "waiting for the opening bar",
                    "last_good": self.underlying_status.get("last_good")}
                return
            return await self._on_underlying_failure(e)
        except Exception as e:
            return await self._on_underlying_failure(e)
        await self._on_underlying_success(data)

    async def _on_underlying_failure(self, e: Exception):
        # Yahoo rate-limits datacenter IPs hard; back off rather than
        # hammering a source that is already refusing us.
        self.underlying_fail_streak += 1
        self.underlying_status = {"ok": False, "error": str(e),
                                  "last_good": self.underlying_status.get("last_good")}
        if self.underlying_fail_streak in (1, 5, 20) or \
                self.underlying_fail_streak % 100 == 0:
            self.emit("SOURCE_ERROR", {"source": "underlying", "error": str(e),
                                       "consecutive_failures": self.underlying_fail_streak})
        await self._try_massive_bar_fallback()

    async def _on_underlying_success(self, data: dict):
        import asyncio
        if self._fallback_active:
            self._fallback_active = False
        if self.underlying_fail_streak:
            self.emit("SOURCE_RECOVERED", {"source": "underlying",
                                           "after_failures": self.underlying_fail_streak})
            self.underlying_fail_streak = 0
        self.underlying_status = {"ok": True, "error": None,
                                  "last_good": data["fetched_at"]}
        self.spot, self.spot_ts = data["spot"], data["spot_ts"]
        self.session_high, self.session_low = data["session_high"], data["session_low"]
        new = [b for b in data["bars"] if b["ts"] not in self.bar_by_minute
               or self.bar_by_minute[b["ts"]]["c"] != b["c"]]
        self.bars = data["bars"]
        for b in data["bars"]:
            self.bar_by_minute[b["ts"]] = b
        for b in new:
            try:
                self.db.save_bar(self.session_date, self.ticker, b["ts"],
                                 b["o"], b["h"], b["l"], b["c"], b["v"])
            except Exception:
                pass
        self._process_touch_bars()
        # Cheap regime refresh against near-live spot (no flip sweep). Gated on
        # the frozen baseline alone: flow on its own is not a dealer position
        # map, so pre-09:30 there is no regime. Must match `have_positions`
        # in recompute() or the two paths disagree about whether a regime exists.
        # Also skipped while the map is frozen — a regime derived from a stale
        # spot is exactly what the freeze exists to prevent.
        if self.spot and self.gex.baseline_frozen and not self.map_frozen and \
                self.flip_result.get("error") != "not computed":
            state, event = self.regime.update(_now(), self.spot, self.flip_result,
                                              self._atr())
            self.regime_state = state
            if event:
                self._on_regime_change(event)
        # Day-specific history (prior close, time-of-day volume baseline) can
        # fail at startup if Yahoo throttles the first burst — retry slowly
        # instead of shipping "—" all day.
        import time as _t
        if (self.feed.prior_close is None or not self.feed.tod_volume_baseline) \
                and _t.monotonic() - self._last_hist_retry >= 600:
            self._last_hist_retry = _t.monotonic()
            try:
                await asyncio.to_thread(self.feed.load_history)
                if self.feed.prior_close is not None:
                    log.info("%s: history baseline recovered on retry", self.ticker)
            except Exception as e:
                log.warning("%s: history retry failed: %s", self.ticker, e)

    async def _try_massive_bar_fallback(self):
        """yfinance is refusing us — keep the two-clock bar series alive from
        Massive's 1-minute aggregates (T-15). Everything that consumes BARS
        (flow-vs-bar evaluation, touch processing, ATR/VWAP/rvol) keeps
        working; SPOT stays stale and the panel stays degraded, because a
        15-min-delayed price must never impersonate the near-live clock."""
        import time as _t
        if self.underlying_fail_streak < 2 or self.massive.aggs_entitled is False:
            return
        if _t.monotonic() - self._last_bar_fallback < 90:
            return  # respect the shared request budget during outages
        self._last_bar_fallback = _t.monotonic()
        try:
            bars = await self.massive.fetch_minute_bars(self.ticker, _now().date())
        except MassiveError as e:
            # Same rule as the startup probe: only a 401, or a sustained run of
            # 403s, is a verdict. A single 403 during an outage must not
            # disable the fallback for the rest of the session.
            if e.status == 401:
                self.massive.aggs_entitled = False
                log.warning("%s: stock 1m aggs not entitled (HTTP 401) — no "
                            "bar fallback available", self.ticker)
            elif e.status == 403:
                self.massive.aggs_403_streak += 1
                if self.massive.aggs_403_streak >= config.AGGS_403_VERDICT_STREAK:
                    self.massive.aggs_entitled = False
                    log.warning("%s: stock 1m aggs refused with 403 %d times "
                                "running — treating as not entitled",
                                self.ticker, self.massive.aggs_403_streak)
                else:
                    log.warning("%s: stock 1m aggs 403 (%d/%d) — will retry",
                                self.ticker, self.massive.aggs_403_streak,
                                config.AGGS_403_VERDICT_STREAK)
            return
        except Exception as e:  # transient — try again next window
            log.warning("%s: bar fallback fetch failed: %s", self.ticker, e)
            return
        session = [b for b in bars
                   if config.SESSION_OPEN
                   <= datetime.fromisoformat(b["ts"]).timetz().replace(tzinfo=None)
                   <= config.SESSION_CLOSE]
        if not session:
            return
        if not self._fallback_active:
            self._fallback_active = True
            self.massive.aggs_entitled = True
            self.massive.aggs_403_streak = 0
            self.emit("UNDERLYING_FALLBACK", {
                "source": "massive_1m_aggs",
                "note": "yfinance failing; bar series continues from Massive "
                        "(T-15). Spot remains stale."})
        added = 0
        for b in session:
            if b["ts"] not in self.bar_by_minute:  # yfinance stays authoritative
                self.bar_by_minute[b["ts"]] = b
                added += 1
                try:
                    self.db.save_bar(self.session_date, self.ticker, b["ts"],
                                     b["o"], b["h"], b["l"], b["c"], b["v"])
                except Exception:
                    pass
        if added:
            merged = {b["ts"]: b for b in self.bars}
            for b in session:
                merged.setdefault(b["ts"], b)
            self.bars = sorted(merged.values(), key=lambda b: b["ts"])
            self._process_touch_bars()

    def _atr(self):
        sess = metrics_mod.session_bars(self.bars)
        return metrics_mod.atr14_1min(sess, self.feed.prior_day_bars)

    def bar_at(self, dt: datetime) -> dict | None:
        """1-minute bar covering dt (two-clock lookup); nearest within 5 min."""
        key = dt.replace(second=0, microsecond=0)
        for back in range(0, 6):
            b = self.bar_by_minute.get((key - timedelta(minutes=back)).isoformat())
            if b:
                return b
        return None

    def _anchor_price(self, asof: datetime) -> tuple[float | None, str]:
        """Price to anchor a T-15-derived artifact (09:30 baseline, opening
        straddle) to, plus which clock it came from.

        The chain is T-15, so the matching bar is the consistent anchor. But
        `asof` at the 09:30 freeze is ~09:15, and the underlying feed carries
        no pre-market bars — so that lookup misses and we fall back to
        near-live spot. Both are defensible; which one was used is not
        inferable after the fact, so it is recorded rather than left silent."""
        bar = self.bar_at(asof)
        if bar:
            return bar["c"], "bar_t15"
        if self.spot is not None:
            return self.spot, "spot_live"
        return None, "none"

    # ---------------------------------------------------------- options (60s)
    async def poll_options(self):
        if self.no_0dte:
            return
        if self.expiry is None:
            await self._discover_expiry()
            if self.expiry is None:
                return
        now = _now()
        try:
            # Strike-windowed fetch keeps each snapshot to ~1 page — the
            # account answers ~MASSIVE_RPM requests/min before 429ing.
            gte = lte = None
            if self.spot:
                gte = self.spot * (1 - config.CHAIN_STRIKE_WINDOW_PCT)
                lte = self.spot * (1 + config.CHAIN_STRIKE_WINDOW_PCT)
            chain = await self.massive.fetch_chain(self.ticker, self.expiry,
                                                   strike_gte=gte, strike_lte=lte)
            if not chain:
                raise MassiveError("empty chain for 0DTE expiry")
        except Exception as e:
            self.option_status = {"ok": False, "error": str(e),
                                  "last_good": self.option_status.get("last_good")}
            self.emit("SOURCE_ERROR", {"source": "massive", "error": str(e)})
            return

        self.chain = chain
        self.chain_fetched_at = now.isoformat()
        asof = now - timedelta(seconds=config.OPTION_FEED_DELAY_S)
        self.option_asof = asof.isoformat()
        self.chain_underlying_price = next(
            (c["underlying_price"] for c in chain
             if c.get("underlying_price")), None)
        self.option_status = {"ok": True, "error": None, "last_good": now.isoformat()}
        self.gex.update_meta(chain)
        self._record_quotes(asof)

        # Freeze the assumption layer once, at/after 09:30 ET.
        if not self.gex.baseline_frozen and now.timetz().replace(tzinfo=None) >= config.SESSION_OPEN:
            spot930, anchor_src = self._anchor_price(asof)
            if spot930:
                late = now.timetz().replace(tzinfo=None) > (
                    datetime.combine(now.date(), config.SESSION_OPEN)
                    + timedelta(minutes=20)).timetz()
                self.gex.freeze_baseline(chain, spot930, now.isoformat(), late)
                self.baseline_anchor_source = anchor_src
                self.emit("BASELINE_FROZEN", {
                    "spot": spot930, "late_start": late,
                    "anchor_source": anchor_src,
                    "contracts": len(self.gex.baseline)})

        # Freeze the opening straddle once.
        if self.straddle_snap is None and self.gex.baseline_frozen:
            anchor, anchor_src = self._anchor_price(asof)
            snap = levels_mod.straddle_snapshot(chain, anchor or 0)
            if snap:
                self.straddle_snap = snap
                t_years = self._t_years(now)
                self.em = levels_mod.expected_move(snap, anchor, t_years)
                self.em["anchor_source"] = anchor_src
                self.emit("EM_SNAPSHOT", {"straddle": snap["straddle_mid"],
                                          "atm_strike": snap["atm_strike"],
                                          "anchor": anchor,
                                          "anchor_source": anchor_src})

        await self._ingest_flow(now)
        self.recompute(now)
        self._persist_snapshot(now)

        # running total of today's 0DTE volume for the 20-day baseline
        # (only volume from bars actually stamped today — Massive serves
        # stale prior-session bars for contracts that haven't traded yet)
        try:
            self.db.save_daily_option_volume(
                self.ticker, self.session_date,
                sum((c.get("volume") or 0) for c in chain
                    if c.get("day_bar_date") in (None, self.session_date)))
        except Exception:
            pass

        # front-vs-next expiry IV spread — refreshed on a slow cadence to
        # stay inside the request budget
        import time as _t
        if _t.monotonic() - self._next_expiry_checked_at >= config.NEXT_EXPIRY_REFRESH_S:
            self._next_expiry_checked_at = _t.monotonic()
            try:
                if self.next_expiry is None:
                    self.next_expiry = await self.massive.fetch_next_expiry(
                        self.ticker, self.expiry)
                if self.next_expiry and self.spot:
                    nxt = await self.massive.fetch_chain(
                        self.ticker, self.next_expiry,
                        strike_gte=self.spot * 0.97, strike_lte=self.spot * 1.03)
                    self.next_atm_iv = metrics_mod.atm_iv(nxt, self.spot)
            except Exception as e:
                log.warning("%s: next-expiry IV fetch failed: %s", self.ticker, e)

    def _record_quotes(self, asof: datetime):
        epoch = asof.timestamp()
        for c in self.chain:
            if c.get("bid") is None or c.get("ask") is None:
                continue
            q = self.quote_history.setdefault(c["key"], deque(maxlen=40))
            q.append((epoch, c["bid"], c["ask"]))

    def _quote_at(self, key: str, ts_epoch: float):
        """Most recent stored NBBO at or before the print time (+60s slack)."""
        best = None
        for epoch, bid, ask in self.quote_history.get(key, ()):  # oldest->newest
            if epoch <= ts_epoch + 60:
                best = (bid, ask)
            else:
                break
        return best if best else (None, None)

    async def _ingest_flow(self, now: datetime):
        """Classify today's flow. Two paths, chosen by plan entitlement:

        prints          — per-trade classification against the NBBO/tick at
                          the print's own timestamp (quotes+trades tiers).
        snapshot_deltas — per-contract day-volume deltas between polls,
                          classified by a tick test on the contract's mark.
                          Everything from this path is low-confidence by
                          construction and flagged as such.
        """
        by_key = {c["key"]: c for c in self.chain}

        # Volume deltas — only trust bars actually stamped today. Massive
        # keeps serving the prior session's day bar for contracts that
        # haven't traded yet (same shape, stale volume).
        deltas = {}
        for key, c in by_key.items():
            if c.get("day_bar_date") not in (None, self.session_date):
                continue
            vol = c.get("volume") or 0
            prev = self.prev_volume.get(key)
            if prev is not None and vol > prev:
                deltas[key] = vol - prev
            self.prev_volume[key] = vol
        first_poll = self._first_flow_poll
        self._first_flow_poll = False

        total_delta = sum(deltas.values())
        if total_delta > 0:
            self.flow_coverage["chain_volume"] += total_delta

        # Resolve entitlement FIRST — it decides which path this poll takes,
        # so reporting the path before probing labels the poll wrongly for a
        # full minute. One call per session; the probe runs on the critical
        # tier so a saturated budget can't strand it at "unknown".
        if self.massive.trades_entitled is None and deltas:
            probe_key = max(deltas, key=deltas.get)
            contract = by_key[probe_key].get("contract")
            if contract:
                session_start = datetime.combine(now.date(), config.SESSION_OPEN,
                                                 config.ET)
                try:
                    await self.massive.probe_trades_entitlement(
                        contract, int(session_start.timestamp() * 1e9))
                except Exception as e:
                    log.warning("%s: trades entitlement probe inconclusive: %s",
                                self.ticker, e)

        # Entitlement alone doesn't make prints usable — they also have to fit
        # the request budget. Being entitled but unable to afford them is a
        # distinct state from not being entitled, and the UI should say so
        # rather than reporting the fallback as if it were a plan limit.
        affordable = config.MAX_TRADE_CONTRACTS_EFFECTIVE > 0
        self.prints_over_budget = bool(self.massive.trades_entitled) and not affordable

        # Report the path flow is ACTUALLY being classified by, not the
        # entitlement state. Entitlement is account-level and probed by
        # whichever ticker sees flow first, so keying the label off the probe
        # left the other engines reporting "unknown" all session while
        # classifying happily via snapshot deltas — and left every ticker
        # stuck on "unknown" whenever the probe itself couldn't complete.
        use_prints = bool(self.massive.trades_entitled) and affordable
        path = "prints" if use_prints else "snapshot_deltas"
        if path != self.flow_source:
            self.flow_source = path
            self.emit("FLOW_SOURCE", {
                "source": path,
                "trades_entitled": self.massive.trades_entitled,
                "prints_over_budget": self.prints_over_budget})
            if self.prints_over_budget:
                log.warning(
                    "%s: trade prints ARE entitled but unaffordable at "
                    "MASSIVE_RPM=%d (critical load %.1f/min leaves %.1f/min "
                    "for prints); using snapshot deltas. Raise MASSIVE_RPM to "
                    "~%d to enable print-level classification.",
                    self.ticker, config.MASSIVE_RPM, config.CRITICAL_RPM,
                    config.PRINT_BUDGET_RPM,
                    int(config.CRITICAL_RPM + len(config.TICKERS)
                        * config.MAX_TRADE_CONTRACTS_PER_POLL
                        * (60.0 / max(config.OPTION_POLL_SECONDS, 1))))

        if use_prints:
            await self._ingest_prints(now, by_key, deltas)
        else:
            self._ingest_snapshot_deltas(now, by_key, deltas, first_poll)

    def _ingest_snapshot_deltas(self, now: datetime, by_key: dict,
                                deltas: dict, first_poll: bool):
        """Aggregate flow from poll-to-poll volume deltas. The trade time is
        unknown within the window, so the contract's last_trade timestamp is
        used when present, else the feed's as-of time — never wall-clock now."""
        asof_dt = datetime.fromisoformat(self.option_asof)
        rows_for_db = []
        for key, size in deltas.items():
            c = by_key[key]
            mark = c.get("mark")
            if first_poll or mark is None:
                # No prior mark to tick against (or no price at all): the
                # volume is real but unclassifiable — hold it, don't guess.
                if mark is not None:
                    self.prev_mark[key] = mark
                self.gex.apply_print(key, "unclassified", size, None, 0.0, True)
                continue
            prev = self.prev_mark.get(key)
            self.prev_mark[key] = mark
            side, low_conf, at_ask = classify.classify_print(
                mark, c.get("bid"), c.get("ask"), prev)
            low_conf = True  # aggregate deltas are never print-grade
            ts_ns = c.get("last_trade_ts_ns")
            pdt = datetime.fromtimestamp(int(ts_ns) / 1e9, config.ET) \
                if ts_ns else asof_dt
            bar = self.bar_at(pdt)  # two-clock: bar at the flow's own time
            spot_at_flow = bar["c"] if bar else None
            meta = self.gex.contract_meta.get(key, {})
            self.gex.apply_print(key, side, size, meta.get("gamma"),
                                 spot_at_flow or 0.0, low_conf)
            if side in ("buy", "sell"):
                self.flow_coverage["classified_volume"] += size
            strike = meta.get("strike") or float(key.split(":")[1])
            rec = {"ts": pdt.isoformat(), "contract": key, "side": side,
                   "size": int(size), "price": mark, "at_ask": at_ask,
                   "low_conf": True, "strike": strike}
            self.print_log.setdefault(strike, []).append(rec)
            rows_for_db.append(rec)
        if rows_for_db:
            try:
                self.db.save_prints(self.session_date, self.ticker, rows_for_db)
            except Exception:
                log.exception("flow persist failed")

    async def _ingest_prints(self, now: datetime, by_key: dict, deltas: dict):
        """Print-level classification (plans with trade entitlement)."""
        active = [k for k, d in sorted(deltas.items(), key=lambda kv: -kv[1])]
        # Sized to the budget left over after critical calls, not to the raw
        # configured maximum — otherwise print fetches queue ahead of the next
        # chain snapshot and the whole map arrives late.
        chosen = active[:config.MAX_TRADE_CONTRACTS_EFFECTIVE]
        if deltas and len(chosen) < len(active):
            covered = sum(deltas[k] for k in chosen)
            log.info("%s: fetching prints for %d/%d active contracts "
                     "(%.0f%% of volume delta)", self.ticker, len(chosen),
                     len(active), 100 * covered / max(sum(deltas.values()), 1))

        session_start = datetime.combine(now.date(), config.SESSION_OPEN, config.ET)
        default_since = int(session_start.timestamp() * 1e9)
        reqs = []
        for key in chosen:
            c = by_key[key]
            if not c.get("contract"):
                continue
            reqs.append((key, c["contract"],
                         self.trades_since_ns.get(key, default_since)))
        results = await self.massive.fetch_trades_bulk(
            [(contract, since) for _, contract, since in reqs])

        rows_for_db = []
        for key, contract, _ in reqs:
            prints = results.get(contract)
            if prints is None:
                continue  # fetch failed; volume stays unclassified
            seen = self.seen_trades.setdefault(key, set())
            meta = self.gex.contract_meta.get(key, {})
            strike = meta.get("strike") or float(key.split(":")[1])
            for p in prints:
                pid = p.get("id") or f"{p['ts_ns']}:{p['price']}:{p['size']}"
                if pid in seen:
                    continue
                seen.add(pid)
                self.trades_since_ns[key] = max(
                    self.trades_since_ns.get(key, 0), int(p["ts_ns"]) + 1)
                pdt = datetime.fromtimestamp(int(p["ts_ns"]) / 1e9, config.ET)
                bid, ask = self._quote_at(key, pdt.timestamp())
                side, low_conf, at_ask = classify.classify_print(
                    p["price"], bid, ask, self.last_print_price.get(key))
                self.last_print_price[key] = p["price"]
                bar = self.bar_at(pdt)  # two-clock: spot at the print's minute
                spot_at_print = bar["c"] if bar else None
                self.gex.apply_print(key, side, p["size"], meta.get("gamma"),
                                     spot_at_print or 0.0, low_conf)
                if side in ("buy", "sell"):
                    self.flow_coverage["classified_volume"] += p["size"]
                rec = {"ts": pdt.isoformat(), "contract": key, "side": side,
                       "size": int(p["size"]), "price": p["price"],
                       "at_ask": at_ask, "low_conf": low_conf, "strike": strike}
                self.print_log.setdefault(strike, []).append(rec)
                rows_for_db.append(rec)
        if rows_for_db:
            try:
                self.db.save_prints(self.session_date, self.ticker, rows_for_db)
            except Exception:
                log.exception("print persist failed")

    # -------------------------------------------------------------- recompute
    def _t_years(self, now: datetime) -> float:
        if not self.expiry:
            return 0.0
        close = datetime.combine(self.expiry, config.SESSION_CLOSE, config.ET)
        return max(0.0, (close - now).total_seconds()) / SECONDS_PER_YEAR

    def _spot_age_s(self, now: datetime) -> float | None:
        return self._age_s(self.spot_ts, now)

    def recompute(self, now: datetime):
        if not self.spot:
            return

        # Spot is the fast clock, and everything positional hangs off it:
        # walls rank by distance from spot, the flip sweep is centred on it,
        # and zones are bands around it. Recomputing those against a spot that
        # is hours old doesn't produce a degraded map, it produces a confident
        # wrong one — so freeze instead, the same way the engine refuses to
        # produce a regime before the 09:30 baseline exists.
        age = self._spot_age_s(now)
        was_frozen = self.map_frozen
        self.map_frozen = age is not None and age > config.DEGRADED_AFTER_S
        if self.map_frozen:
            if not was_frozen:
                self.emit("MAP_FROZEN", {
                    "reason": "spot stale", "spot_age_s": round(age),
                    "frozen_at": self.spot_ts,
                    "note": "levels held at their last live values; regime and "
                            "zones are not being recomputed"})
                log.warning("%s: spot is %.0f min old — freezing the map "
                            "(levels held, regime/zones not recomputed)",
                            self.ticker, age / 60)
            # Flow-derived state is still honest: it comes off the option feed
            # and its own as-of clock, not spot. Keep it current so decay and
            # level history stay live for when price returns.
            self.strike_map = self.gex.strike_map()
            self._record_level_history(now)
            self.flip_result = {
                "flips": [], "nearest": None, "local_gamma": 0.0,
                "widened": False,
                "error": f"spot stale ({round(age / 60)} min) — map frozen"}
            return
        if was_frozen:
            self.emit("MAP_RESUMED", {"spot_age_s": round(age) if age else 0})

        self.strike_map = self.gex.strike_map()
        self.walls = levels_mod.walls(self.strike_map, self.spot)
        self.max_pain = levels_mod.max_pain(self.chain)

        self._record_level_history(now)

        # Before the 09:30 baseline exists there are no dealer positions —
        # a flip sweep over nothing is not an error, it's "not yet".
        #
        # Gated on the frozen baseline ONLY. Pollers start at 09:00, and
        # _ingest_flow runs on those pre-open polls, so keying off
        # `self.gex.flow` let a single pre-open volume delta run the sweep
        # against a flow-only position map and set a regime before 09:30 —
        # contradicting the documented "regime/zones stay unset pre-baseline".
        event = None
        have_positions = self.gex.baseline_frozen
        if not have_positions:
            self.flip_result = {"flips": [], "nearest": None, "local_gamma": 0.0,
                                "widened": False,
                                "error": "awaiting 09:30 baseline"}
        else:
            self.flip_result = flip_mod.find_flips(
                self.gex.dealer_positions(), self.gex.contract_meta,
                self.spot, self._t_years(now))
            if self.flip_result.get("error"):
                self.emit("FLIP_ERROR", {"error": self.flip_result["error"]})

            atr = self._atr()
            self.regime_state, event = self.regime.update(now, self.spot,
                                                          self.flip_result, atr)
            if event:
                self._on_regime_change(event)

        self._detect_new_walls()
        self._sync_trackers(now)
        self._classify_pending_touches()
        self._check_decay(now)
        self._refresh_zones(now, regime_changed=bool(event))
        self._version_five_numbers()

    def _record_level_history(self, now: datetime):
        """Level-magnitude history, keyed to the option as-of time rather than
        wall clock. Driven by the option feed, so it stays valid even while
        spot is stale and the positional map is frozen."""
        asof_epoch = datetime.fromisoformat(self.option_asof).timestamp() \
            if self.option_asof else now.timestamp()
        for s in self.strike_map.values():
            h = self.level_history.setdefault(s["strike"], deque(maxlen=720))
            h.append((asof_epoch, abs(s["dealer_gamma"])))

    def _on_regime_change(self, event):
        before = {"walls": self.walls, "flip": self.flip_result.get("nearest"),
                  "max_pain": self.max_pain}
        self.emit("REGIME_CHANGE", {**event, "levels_in_force": before})
        now_iso = _now().isoformat()
        zones_mod.expire_zones(self.zones, now_iso)
        if self.zones:
            self.emit("ZONES_EXPIRED", {"count": len(self.zones)})
        self.expired_zones = (self.zones + self.expired_zones)[:20]
        self.zones = []

    def _detect_new_walls(self):
        current = {w["strike"] for w in self.walls["above"]} | \
                  {w["strike"] for w in self.walls["below"]}
        if self._had_first_walls:
            for s in current - self._prev_wall_strikes:
                self.emit("NEW_WALL", {"strike": s,
                                       "dealer_gamma": self.strike_map[s]["dealer_gamma"],
                                       "observed_share": self.strike_map[s]["observed_share"]})
        self._prev_wall_strikes = current
        self._had_first_walls = True

    # ---------------------------------------------------------------- touches
    def _sync_trackers(self, now: datetime):
        """Every current wall needs a live tracker. A resolved tracker is
        already archived in resolved_touches, so it is replaced with a fresh
        one — the same strike can be tested again later in the session."""
        for side in ("above", "below"):
            for w in self.walls[side]:
                st = w["strike"]
                t = self.trackers.get(st)
                if t is None or t.resolved():
                    self.trackers[st] = touches_mod.TouchTracker(
                        st, f"wall_{side}", now)

    def _process_touch_bars(self):
        sess = metrics_mod.session_bars(self.bars)
        if len(sess) < 2:
            return
        atr = self._atr()
        completed = sess[:-1]  # last bar may still be forming
        start_idx = 0
        if self._last_processed_bar:
            for i, b in enumerate(completed):
                if b["ts"] > self._last_processed_bar:
                    start_idx = i
                    break
            else:
                return
        for b in completed[start_idx:]:
            if self._last_processed_bar and b["ts"] <= self._last_processed_bar:
                continue
            bar_dt = datetime.fromisoformat(b["ts"])
            for st, tr in list(self.trackers.items()):
                if tr.resolved():
                    continue
                bw = touches_mod.band_width(st, atr)
                outcome = tr.process_bar(b, bar_dt, bw)
                if outcome:
                    tr.rel_volume = metrics_mod.window_relative_volume(
                        self.bars, tr.entry_ts, tr.exit_ts or b["ts"],
                        self.feed.tod_volume_baseline)
                    if outcome == "BREACHED":
                        self.breached_strikes.add(st)
                    self.resolved_touches.append(tr)
                    self.emit("TOUCH_RESOLVED", {"strike": st, "outcome": outcome,
                                                 "touch": tr.to_dict()})
                    self._persist_touch(tr)
            self._last_processed_bar = b["ts"]

    def _classify_pending_touches(self):
        """Attach flow labels once T-15 data covering the window has arrived."""
        if not self.option_asof:
            return
        asof = self.option_asof
        for tr in self.resolved_touches:
            if tr.flow is not None or not tr.exit_ts:
                continue
            if asof < tr.exit_ts:
                continue  # delayed flow for the window hasn't landed yet
            prints = [p for p in self.print_log.get(tr.strike, [])
                      if tr.entry_ts <= p["ts"] <= tr.exit_ts]
            hist = self.level_history.get(tr.strike, ())
            entry_e = datetime.fromisoformat(tr.entry_ts).timestamp()
            exit_e = datetime.fromisoformat(tr.exit_ts).timestamp()
            before = after = None
            for epoch, mag in hist:
                if epoch <= entry_e:
                    before = mag
                if after is None and epoch >= exit_e:
                    after = mag
            if after is None and hist:
                after = hist[-1][1]
            tr.flow = touches_mod.classify_flow(tr, prints, before, after)
            tr.label = tr.flow["label"]
            self.emit("TOUCH_CLASSIFIED", {"strike": tr.strike, "label": tr.label,
                                           "touch": tr.to_dict()})
            self._persist_touch(tr)

    def _persist_touch(self, tr):
        try:
            self.db.upsert_touch(self.session_date, self.ticker, tr.id, tr.to_dict())
        except Exception:
            log.exception("touch persist failed")

    def touch_summaries(self) -> dict[float, list]:
        out: dict[float, list] = {}
        for tr in self.resolved_touches:
            out.setdefault(tr.strike, []).append(
                {"outcome": tr.price_outcome, "label": tr.label, "id": tr.id})
        return out

    # ------------------------------------------------------------------ decay
    def _check_decay(self, now: datetime):
        tracked = {w["strike"] for w in self.walls["above"]} | \
                  {w["strike"] for w in self.walls["below"]}
        for st in tracked:
            if st in self.decayed_strikes:
                continue
            hist = self.level_history.get(st)
            if not hist or len(hist) < 2:
                continue
            latest_e, latest_m = hist[-1]
            ref = None
            for epoch, mag in hist:
                if latest_e - epoch <= config.DECAY_WINDOW_S:
                    ref = mag
                    break
            if ref and ref > 0 and latest_m < ref * (1 - config.DECAY_DROP_RATIO):
                tr = self.trackers.get(st)
                touched = any(t.strike == st for t in self.resolved_touches) or \
                    (tr is not None and tr.entries > 0)
                if not touched:
                    self.decayed_strikes.add(st)
                    self.emit("LEVEL_DECAYED", {
                        "strike": st, "from": ref, "to": latest_m,
                        "drop_pct": round(100 * (1 - latest_m / ref), 1)})

    def _falling(self, strike: float) -> bool:
        hist = self.level_history.get(strike)
        if not hist or len(hist) < 2:
            return False
        latest_e, latest_m = hist[-1]
        for epoch, mag in reversed(hist):
            if latest_e - epoch >= 600:
                return mag > 0 and latest_m < mag * 0.9
        return False

    # ------------------------------------------------------------------ zones
    def _refresh_zones(self, now: datetime, regime_changed: bool):
        state = self.regime_state.get("state")
        if state in ("PIN", "TREND"):
            sess = metrics_mod.session_bars(self.bars)
            vw = metrics_mod.vwap_bands(sess)
            rvol = metrics_mod.relative_volume(sess, self.feed.tod_volume_baseline,
                                               self.feed.tod_baseline_days)
            em_band = self.em["band_085"] if self.em else None
            self.zones = zones_mod.derive_zones(
                state, self.walls, self.spot, self._atr(), self.max_pain,
                vw["vwap"] if vw else None, em_band,
                rvol["ratio"] if rvol else None,
                self.touch_summaries(), self.breached_strikes)

    # ------------------------------------------------------------- versioning
    def _version_five_numbers(self):
        five = {
            "above": [round(w["strike"], 2) for w in self.walls["above"]],
            "below": [round(w["strike"], 2) for w in self.walls["below"]],
            "flip": round(self.flip_result["nearest"], 2)
            if self.flip_result.get("nearest") else None,
            "max_pain": self.max_pain,
            "em": round(self.em["straddle_raw"], 3) if self.em else None,
        }
        h = hashlib.md5(json.dumps(five, sort_keys=True).encode()).hexdigest()
        if h != self._five_hash:
            self.levels_version += 1
            self._five_hash = h

    # ----------------------------------------------------------------- output
    def _age_s(self, iso: str | None, now: datetime) -> float | None:
        if not iso:
            return None
        return (now - datetime.fromisoformat(iso)).total_seconds()

    def get_state(self, include_events=True) -> dict:
        now = _now()
        sess = metrics_mod.session_bars(self.bars)
        vw = metrics_mod.vwap_bands(sess)
        atr = self._atr()
        rvol = metrics_mod.relative_volume(sess, self.feed.tod_volume_baseline,
                                           self.feed.tod_baseline_days)
        front_iv = metrics_mod.atm_iv(self.chain, self.spot or 0)
        avg_vol, avg_days = self.db.avg_daily_option_volume(
            self.ticker, self.session_date)
        total_vol = metrics_mod.total_volume(self.chain)

        u_age = self._age_s(self.underlying_status.get("last_good"), now)
        o_age = self._age_s(self.option_status.get("last_good"), now)
        u_degraded = (not self.underlying_status["ok"]) or u_age is None or \
            u_age > config.DEGRADED_AFTER_S
        o_degraded = (not self.option_status["ok"]) or o_age is None or \
            o_age > config.DEGRADED_AFTER_S

        ladder = self._build_ladder()
        summaries = self.touch_summaries()

        cov = self.flow_coverage
        coverage_ratio = (cov["classified_volume"] / cov["chain_volume"]) \
            if cov["chain_volume"] > 0 else None

        return {
            "ticker": self.ticker,
            "session_date": self.session_date,
            "ts": now.isoformat(),
            "no_0dte": self.no_0dte,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "sources": {
                "underlying": {**self.underlying_status, "age_s": u_age,
                               "degraded": u_degraded},
                "massive": {**self.option_status, "age_s": o_age,
                            "degraded": o_degraded,
                            "asof": self.option_asof,
                            "delay_s": config.OPTION_FEED_DELAY_S,
                            "probe": self.massive.probe_report},
            },
            "spot": self.spot, "spot_ts": self.spot_ts,
            "spot_age_s": self._age_s(self.spot_ts, now),
            "chain_underlying_price": self.chain_underlying_price,
            "bar_fallback_active": self._fallback_active,
            # Levels are held at their last live values; regime and zones are
            # deliberately not being recomputed against a stale spot.
            "map_frozen": self.map_frozen,
            "session_high": self.session_high, "session_low": self.session_low,
            "prior_close": self.feed.prior_close,
            "regime": self.regime_state,
            "five": {
                "walls_above": self.walls["above"],
                "walls_below": self.walls["below"],
                "flip": self.flip_result.get("nearest"),
                "flips_all": self.flip_result.get("flips"),
                "flip_error": self.flip_result.get("error"),
                "flip_widened": self.flip_result.get("widened"),
                "max_pain": self.max_pain,
                "expected_move": self.em,
            },
            "levels_version": self.levels_version,
            "ladder": ladder,
            "zones": {"active": self.zones, "expired": self.expired_zones},
            "touches": [t.to_dict() for t in self.resolved_touches]
            + [t.to_dict() for t in self.trackers.values() if t.entries > 0],
            "touch_summaries": {f"{k:g}": v for k, v in summaries.items()},
            "events": self.events[-60:] if include_events else [],
            "baseline": {"frozen": self.gex.baseline_frozen,
                         "ts": self.gex.baseline_ts,
                         "late_start": self.gex.baseline_late,
                         "anchor_source": self.baseline_anchor_source},
            "flow_coverage": {**cov, "ratio": coverage_ratio,
                              "source": self.flow_source,
                              "prints_over_budget": self.prints_over_budget},
            "metrics": {
                "vwap": vw,
                "atr14_1m": atr,
                "rel_volume": rvol,
                "put_call_ratio": metrics_mod.put_call_ratio(self.chain),
                "front_atm_iv": front_iv,
                "next_atm_iv": self.next_atm_iv,
                "iv_spread": (front_iv - self.next_atm_iv)
                if front_iv and self.next_atm_iv else None,
                "total_0dte_volume": total_vol,
                "avg_0dte_volume": avg_vol,
                "avg_0dte_volume_days": avg_days,
                "volume_vs_avg": (total_vol / avg_vol) if avg_vol else None,
                "cum_customer_delta": metrics_mod.cumulative_customer_delta(
                    self.gex.flow, self.gex.contract_meta),
            },
        }

    def _build_ladder(self) -> list[dict]:
        if not self.spot or not self.strike_map:
            return []
        wall_strikes = {w["strike"] for w in self.walls["above"]} | \
                       {w["strike"] for w in self.walls["below"]}
        rows = []
        lo, hi = self.spot * 0.985, self.spot * 1.015
        if self.em and self.em.get("band_raw"):
            lo, hi = min(lo, self.em["band_raw"][0]), max(hi, self.em["band_raw"][1])
        cands = [s for s in self.strike_map.values()
                 if (lo <= s["strike"] <= hi or s["strike"] in wall_strikes)
                 and s["dealer_gamma"] != 0]
        cands.sort(key=lambda s: -abs(s["dealer_gamma"]))
        summaries = self.touch_summaries()
        for s in cands[:25]:
            rows.append({
                "strike": s["strike"],
                "dealer_gamma": s["dealer_gamma"],
                "observed_share": round(s["observed_share"], 3),
                "low_conf_share": round(s["low_conf_share"], 3),
                "is_wall": s["strike"] in wall_strikes,
                "touches": summaries.get(s["strike"], []),
                "decaying": self._falling(s["strike"]),
                "decayed": s["strike"] in self.decayed_strikes,
            })
        rows.sort(key=lambda r: -r["strike"])
        return rows

    def _persist_snapshot(self, now: datetime):
        """Store a slimmed snapshot: everything the replay renderer needs,
        nothing that lives in its own table. Touches have a table, events
        have a table, and the schema-probe report is a startup log — writing
        them into every 60s snapshot triples the row size for no replay
        value. At ~5 KB/row this is ~6 MB/day for three tickers."""
        try:
            st = self.get_state(include_events=False)
            st.pop("touches", None)
            st.pop("touch_summaries", None)
            st["sources"]["massive"].pop("probe", None)
            self.db.save_snapshot(self.session_date, self.ticker,
                                  now.isoformat(), st)
        except Exception:
            log.exception("snapshot persist failed")
