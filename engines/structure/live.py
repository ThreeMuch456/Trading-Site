"""Intraday activation monitor — streams delayed options AM aggregates for the
day's top setups and flags the ones whose option flow is firing in-direction.

Scope (features #1 + #2): read-only over the EOD core. It reads the persisted
screen results (top-N tickers + their gamma flip/walls + setup direction), streams
Massive's *delayed* options AM (minute-aggregate) feed for those chains, and rolls
up per-ticker option volume so the /live page can show:

  #1 activation   — option volume surging vs the ticker's own baseline, in the
                    setup's direction (calls for a bull/resistance setup, puts for
                    a bear/support setup).
  #2 flip / walls — how much of that volume is clustering at the gamma flip strike
                    and the gamma walls (dealers being pushed toward the flip = the
                    negative-gamma amplification regime engaging, live).

It also persists each day's peak-of-day signals (see "Daily-peak persistence"
below) so oplog.py can fold "was this setup live-confirmed today?" into the
opportunity log as a new feature — the data path for src/tune.py to later test
whether live confirmation actually predicts better outcomes. Advisory only, same
as the rest of the calibration pipeline; never changes today's ranking.

Everything below the "Consumer" divider is the websocket plumbing; everything above
it is pure and unit-tested. The feed is 15-min delayed — confirmation, not a trigger.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .schedule import market_tz

# ── Option symbol helpers ────────────────────────────────────────────────────────

def parse_option_symbol(sym: str) -> dict | None:
    """Parse a Massive/OCC option symbol 'O:AAPL260717C00100000' ->
    {underlying, exp (ISO), cp ('call'/'put'), strike}. None if malformed."""
    if not sym:
        return None
    s = sym[2:] if sym.startswith("O:") else sym
    if len(s) < 15:
        return None
    tail = s[-15:]           # YYMMDD + C/P + 8-digit strike(×1000)
    root = s[:-15]
    yymmdd, cp_ch, strike_str = tail[:6], tail[6], tail[7:]
    if cp_ch not in ("C", "P") or not root:
        return None
    try:
        exp = datetime.strptime(yymmdd, "%y%m%d").date().isoformat()
        strike = int(strike_str) / 1000.0
    except ValueError:
        return None
    return {"underlying": root, "exp": exp, "cp": "call" if cp_ch == "C" else "put",
            "strike": strike}


def occ_symbol(underlying: str, exp_iso: str, cp: str, strike: float) -> str | None:
    """Build 'O:ROOT YYMMDD C/P 00100000' from parts (fallback when a cached
    snapshot lacks the raw symbol). Root drops dots/dashes (BRK-B -> BRKB)."""
    try:
        yymmdd = datetime.fromisoformat(exp_iso).strftime("%y%m%d")
    except ValueError:
        return None
    root = underlying.replace("-", "").replace(".", "").upper()
    cp_ch = "C" if cp == "call" else "P"
    return f"O:{root}{yymmdd}{cp_ch}{int(round(strike * 1000)):08d}"


def _nearest(strikes: list[float], target: float) -> float | None:
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


# ── Setup direction ──────────────────────────────────────────────────────────────

def setup_direction(result) -> str | None:
    """'up' | 'down' | None — the direction the setup expects price to travel."""
    flag = getattr(result, "flag", None)
    if flag is not None:
        return "up" if flag.kind == "bull" else "down"
    zone = result.aligned_zone
    if zone is None:
        return None
    return "up" if zone.direction == "resistance" else "down"


# ── Subscription builder (pure) ──────────────────────────────────────────────────

def build_subscription(results, cfg: Config, snapshot_loader) -> tuple[list[dict], dict]:
    """From the ranked results, build the AM subscription list and per-ticker meta.

    snapshot_loader(ticker) -> trimmed contract dicts (cp/exp/strike/oi/vol[/sym]) or
    None. Returns (entries, meta):
      entries: [{sym, ticker, cp, strike, role in {near,flip,wall}}]
      meta[ticker]: {direction, score, spot, flip_strike, wall_strikes,
                     baseline_call, baseline_put}
    Contracts are the nearest expiries' strikes within ±strike_window_pct of spot with
    OI ≥ min_contract_oi, plus the flip and wall strikes. Capped at max_sub_symbols."""
    top_n     = int(cfg.get("live.top_n", 20))
    window    = float(cfg.get("live.strike_window_pct", 10)) / 100.0
    min_oi    = float(cfg.get("live.min_contract_oi", 100))
    max_exp   = int(cfg.get("gex.max_expiries", 2))
    cap       = int(cfg.get("live.max_sub_symbols", 1200))

    entries: list[dict] = []
    meta: dict = {}
    seen: set[str] = set()

    for r in results[:top_n]:
        spot = float(r.profile.current_price)
        if spot <= 0:
            continue
        contracts = snapshot_loader(r.ticker)
        if not contracts:
            continue

        # Restrict to the nearest expiries (matches the GEX/snapshot methodology).
        exps = sorted({c["exp"] for c in contracts if c.get("exp")})[:max(1, max_exp)]
        live_contracts = [c for c in contracts if c.get("exp") in exps]
        listed_strikes = sorted({float(c["strike"]) for c in live_contracts if c.get("strike")})

        gex = getattr(r, "gex", None)
        flip_strike = _nearest(listed_strikes, gex.flip_price) if (gex and gex.flip_price) else None
        wall_strikes = [_nearest(listed_strikes, w) for w in (gex.top_gamma_strikes or [])] \
            if gex else []
        wall_strikes = [w for w in wall_strikes if w is not None]

        lo, hi = spot * (1 - window), spot * (1 + window)
        ticker_syms: list[tuple[str, dict]] = []

        for c in live_contracts:
            strike = float(c["strike"]) if c.get("strike") else None
            if strike is None:
                continue
            near = lo <= strike <= hi and float(c.get("oi") or 0) >= min_oi
            is_flip = flip_strike is not None and strike == flip_strike
            is_wall = strike in wall_strikes
            if not (near or is_flip or is_wall):
                continue
            sym = c.get("sym") or occ_symbol(r.ticker, c["exp"], c["cp"], strike)
            if not sym or sym in seen:
                continue
            role = "flip" if is_flip else ("wall" if is_wall else "near")
            entry = {"sym": sym, "ticker": r.ticker, "cp": c["cp"], "strike": strike, "role": role}
            ticker_syms.append((sym, entry))

        if not ticker_syms:
            continue
        if len(seen) + len(ticker_syms) > cap:      # keep this run under the symbol cap
            break
        for sym, entry in ticker_syms:
            seen.add(sym)
            entries.append(entry)

        # RVOL baseline: the ticker's full COMPLETED-day option volume from the
        # persisted daily screen (gex.call_volume/put_volume), never a same-day
        # live fetch — a live snapshot's "vol" is today's partial total, and
        # comparing today's streamed volume against itself is meaningless. None
        # here (no baseline yet) makes evaluate() correctly show "—" instead of
        # a bogus ratio.
        meta[r.ticker] = {
            "direction": setup_direction(r),
            "score": round(float(getattr(r, "combined_score", 0.0)), 4),
            "spot": spot,
            "flip_strike": flip_strike,
            "wall_strikes": wall_strikes,
            "baseline_call": getattr(gex, "call_volume", None) if gex else None,
            "baseline_put": getattr(gex, "put_volume", None) if gex else None,
        }

    return entries, meta


def subscription_params(entries: list[dict]) -> list[str]:
    """AM channel strings for a subscribe/unsubscribe message, deduped."""
    return sorted({f"AM.{e['sym']}" for e in entries})


def index_entries(entries: list[dict]) -> dict[str, dict]:
    return {e["sym"]: e for e in entries}


# ── State reducer (pure) ─────────────────────────────────────────────────────────

def new_state() -> dict:
    """Per-ticker accumulator: latest accumulated-day volume (av) per contract."""
    return {"av": {}, "as_of": None}


def apply_am(states: dict, entry_index: dict, msg: dict) -> None:
    """Fold one AM message into per-ticker state. Ignores unknown/out-of-scope syms."""
    if msg.get("ev") != "AM":
        return
    sym = msg.get("sym")
    entry = entry_index.get(sym)
    if entry is None:
        return
    st = states.setdefault(entry["ticker"], new_state())
    # av = accumulated day volume for this contract (Massive gives it per bar).
    st["av"][sym] = float(msg.get("av") or msg.get("v") or 0.0)
    end = msg.get("e")
    if end:
        st["as_of"] = int(end)


# ── Signal math (pure) ───────────────────────────────────────────────────────────

def session_fraction(now_utc: datetime, cfg: Config) -> float:
    """Fraction of the RTH session elapsed as of (now − feed delay), 0..1.
    A 15-min-delayed feed only reflects volume up to 15 min ago, so the baseline
    is scaled to that same point to avoid overstating RVOL."""
    tz = ZoneInfo(cfg.get("live.session_tz", "America/New_York"))
    delay = float(cfg.get("live.data_delay_min", 15))
    eff = now_utc.astimezone(tz).timestamp() - delay * 60.0
    local = datetime.fromtimestamp(eff, tz)
    open_min, close_min = 9 * 60 + 30, 16 * 60
    minutes = local.hour * 60 + local.minute
    return max(0.0, min(1.0, (minutes - open_min) / (close_min - open_min)))


def evaluate(ticker: str, state: dict, entry_index: dict, meta: dict,
             cfg: Config, session_frac: float) -> dict:
    """Derive the display/signal fields for one ticker from its av state."""
    m = meta.get(ticker, {})
    direction = m.get("direction")
    av = state.get("av", {})

    call_vol = put_vol = flip_vol = 0.0
    wall_vol: dict[float, float] = {}
    for sym, v in av.items():
        e = entry_index.get(sym)
        if e is None or e["ticker"] != ticker:
            continue
        if e["cp"] == "call":
            call_vol += v
        else:
            put_vol += v
        if e["role"] == "flip":
            flip_vol += v
        elif e["role"] == "wall":
            wall_vol[e["strike"]] = wall_vol.get(e["strike"], 0.0) + v

    opt_vol = call_vol + put_vol
    base_call, base_put = m.get("baseline_call"), m.get("baseline_put")
    base_opt = None if base_call is None and base_put is None else (base_call or 0.0) + (base_put or 0.0)

    def rvol(today: float, base: float | None) -> float | None:
        if base is None:
            return None
        denom = base * session_frac
        return round(today / denom, 2) if denom > 0 else None

    opt_rvol = rvol(opt_vol, base_opt)
    aligned_today = call_vol if direction == "up" else put_vol if direction == "down" else 0.0
    aligned_base = base_call if direction == "up" else base_put if direction == "down" else 0.0
    aligned_rvol = rvol(aligned_today, aligned_base)

    act_rvol = float(cfg.get("live.activation_rvol", 2.0))
    min_frac = float(cfg.get("live.min_session_fraction", 0.1))
    aligned_is_majority = (
        (direction == "up" and call_vol > put_vol) or
        (direction == "down" and put_vol > call_vol)
    )
    # Fire on the DIRECTIONAL surge: aligned-side RVOL over threshold with the
    # aligned side leading. Total option RVOL is shown but not gated on — a quiet
    # opposite side shouldn't mask a clean call/put surge.
    activating = bool(
        session_frac >= min_frac and direction in ("up", "down")
        and aligned_rvol is not None and aligned_rvol >= act_rvol
        and aligned_is_majority
    )

    flip_share = round(flip_vol / opt_vol, 3) if opt_vol > 0 else None
    flip_pressure = bool(flip_share is not None and flip_share >= float(cfg.get("live.flip_share_hot", 0.25)))

    return {
        "ticker": ticker,
        "direction": direction,
        "score": m.get("score"),
        "spot": m.get("spot"),
        "as_of": state.get("as_of"),
        "call_vol": int(call_vol),
        "put_vol": int(put_vol),
        "opt_vol": int(opt_vol),
        "opt_rvol": opt_rvol,
        "aligned_rvol": aligned_rvol,
        "activating": activating,
        "flip_strike": m.get("flip_strike"),
        "flip_vol": int(flip_vol),
        "flip_share": flip_share,
        "flip_pressure": flip_pressure,
        "walls": [{"strike": k, "vol": int(v)} for k, v in sorted(wall_vol.items())],
    }


# ── Daily-peak persistence (for the learning pipeline) ──────────────────────────
#
# The daily screen job runs right around market close, so this trading day's
# live-observed option flow is exactly what should get folded into that day's
# opportunity log as a NEW feature: did live flow ever confirm the setup, and
# how strongly? This is the "train the screener" half — oplog.py reads what's
# written here and logs it alongside GEX/RVOL; src/tune.py later asks whether
# live-confirmed setups actually won more. Purely advisory, like everything else
# in the calibration pipeline — this file never influences today's ranking.
#
# Persisted as a small JSON file holding only the CURRENT trading day's peak-of-
# day summary per ticker (overwritten fresh when the date rolls over) — by the
# time the job reads and logs it, the value lives permanently in the opportunity
# log's features JSON, so this file itself doesn't need to retain history.

def _daily_summary_path(cfg: Config) -> Path:
    return Path(cfg.get("data.cache_dir", "data")) / "live_daily.json"


def _load_daily_summary_file(cfg: Config) -> dict:
    path = _daily_summary_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - corrupt file -> start fresh
        return {}


def merge_daily_peak(existing: dict, ticker: str, row: dict, run_date: str) -> dict:
    """Pure merge: fold one evaluate()-shaped row's signals into the running
    peak-of-day summary for `ticker`. Resets everything when `run_date` differs
    from what's stored (a new trading day starting fresh)."""
    if existing.get("run_date") != run_date:
        existing = {"run_date": run_date, "tickers": {}}
    tickers = existing.setdefault("tickers", {})
    cur = tickers.get(ticker) or {
        "ever_activated": False, "peak_aligned_rvol": None,
        "ever_flip_pressure": False, "peak_flip_share": None,
    }
    cur["ever_activated"] = bool(cur["ever_activated"] or row.get("activating"))
    if row.get("aligned_rvol") is not None:
        cur["peak_aligned_rvol"] = max(cur["peak_aligned_rvol"] or 0.0, float(row["aligned_rvol"]))
    cur["ever_flip_pressure"] = bool(cur["ever_flip_pressure"] or row.get("flip_pressure"))
    if row.get("flip_share") is not None:
        cur["peak_flip_share"] = max(cur["peak_flip_share"] or 0.0, float(row["flip_share"]))
    tickers[ticker] = cur
    return existing


def write_daily_summary(cfg: Config, run_date: str, rows: list[dict]) -> None:
    """Best-effort persist of today's peak-of-day live signals, one evaluate()
    row per ticker. Called after each evaluation cycle; never raises."""
    try:
        existing = _load_daily_summary_file(cfg)
        for row in rows:
            existing = merge_daily_peak(existing, row["ticker"], row, run_date)
        _daily_summary_path(cfg).write_text(json.dumps(existing))
    except Exception:  # noqa: BLE001 - best-effort, never break the monitor
        pass


def read_daily_summary(cfg: Config, run_date: str) -> dict[str, dict]:
    """{ticker: {ever_activated, peak_aligned_rvol, ever_flip_pressure,
    peak_flip_share}} for `run_date`. {} if there's nothing for that date (the
    monitor was off, the market hasn't opened yet, or it's a different day)."""
    existing = _load_daily_summary_file(cfg)
    if existing.get("run_date") != run_date:
        return {}
    return existing.get("tickers", {})


def daily_summary_status(cfg: Config) -> dict:
    """Diagnostic snapshot of the live peak-of-day file that feeds the learning
    log. The /learning page's live section stayed empty even after the monitor
    was confirmed streaming, so this makes the actual hand-off visible: does the
    file exist, is it stamped with TODAY's market-tz run_date (the key oplog
    matches on), and how many tickers / activations did it capture? Surfaced at
    /healthz so 'no live data' can be diagnosed without shell access."""
    path = _daily_summary_path(cfg)
    today = today_run_date(cfg)
    existing = _load_daily_summary_file(cfg)
    tickers = existing.get("tickers", {}) if existing.get("run_date") == today else {}
    return {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "file_run_date": existing.get("run_date"),
        "today_run_date": today,
        "matches_today": existing.get("run_date") == today,
        "n_tickers": len(tickers),
        "n_activated": sum(1 for t in tickers.values() if t.get("ever_activated")),
    }


def today_run_date(cfg: Config) -> str:
    """Today's date string in market tz, matching oplog's run_date derivation."""
    return datetime.now(timezone.utc).astimezone(market_tz(cfg)).date().isoformat()


# ─────────────────────────────────────────────────────────────────────────────────
# Consumer (websocket plumbing — not unit-tested; guarded so import stays cheap)
# ─────────────────────────────────────────────────────────────────────────────────

def _market_open(now_utc: datetime, cfg: Config) -> bool:
    """RTH (weekday 9:30–16:00 ET) plus the delay tail, so the delayed feed's last
    bars still arrive shortly after the close."""
    tz = ZoneInfo(cfg.get("live.session_tz", "America/New_York"))
    local = now_utc.astimezone(tz)
    if local.weekday() >= 5:
        return False
    delay = float(cfg.get("live.data_delay_min", 15))
    minutes = local.hour * 60 + local.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60 + delay + 5)


class LiveMonitor:
    """Owns the delayed-AM websocket, rebuilds its subscription after each daily
    screen, and exposes a snapshot of derived per-ticker signals for /live."""

    def __init__(self, cfg: Config, api_key: str, results_loader, snapshot_loader,
                 store_mtime):
        self.cfg = cfg
        self.api_key = api_key
        self._results_loader = results_loader     # () -> (results, mtime)
        self._snapshot_loader = snapshot_loader    # (ticker) -> contracts|None
        self._store_mtime = store_mtime            # () -> float
        self._lock = threading.Lock()
        self._states: dict = {}
        self._entry_index: dict = {}
        self._meta: dict = {}
        self._sub_params: list[str] = []
        self._loaded_mtime = 0.0
        self._derived: list[dict] = []
        self._status = "idle"
        self._last_error: str | None = None
        self._authed = False
        self._connected = False
        self._ws = None
        self._stop = threading.Event()

    # -- lifecycle --
    def start(self) -> None:
        threading.Thread(target=self._control_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "last_error": self._last_error,
                "as_of_run": self._loaded_mtime,
                "rows": list(self._derived),
                "n_symbols": len(self._sub_params),
            }

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            if error:
                self._last_error = error

    # -- subscription --
    def _rebuild(self) -> None:
        results, mtime = self._results_loader()
        if not results:
            return
        entries, meta = build_subscription(results, self.cfg, self._snapshot_loader)
        params = subscription_params(entries)
        with self._lock:
            self._entry_index = index_entries(entries)
            self._meta = meta
            self._sub_params = params
            self._loaded_mtime = mtime
            self._states = {}           # reset accumulators for the new trading day/set
        if params:
            if self._connected and self._authed:
                self._send({"action": "subscribe", "params": ",".join(params)})
                self._set_status("streaming")
        elif self._authed:
            self._set_status(f"authenticated — built 0 contracts from {len(results)} "
                             f"setups (option snapshots unavailable?)")

    def _evaluate_all(self) -> None:
        frac = session_fraction(datetime.now(timezone.utc), self.cfg)
        with self._lock:
            rows = [evaluate(t, self._states.get(t, new_state()), self._entry_index,
                             self._meta, self.cfg, frac)
                    for t in self._meta]
        rows.sort(key=lambda r: (r["activating"], r["aligned_rvol"] or 0.0), reverse=True)
        with self._lock:
            self._derived = rows
        if rows:
            # Best-effort: today's peak-of-day signals, for oplog to fold into
            # the opportunity log at the next screen (the "train the screener"
            # data path). Never raises; see write_daily_summary's own guard.
            write_daily_summary(self.cfg, today_run_date(self.cfg), rows)

    # -- websocket --
    def _send(self, obj: dict) -> None:
        try:
            if self._ws is not None:
                self._ws.send(json.dumps(obj))
        except Exception:  # noqa: BLE001
            pass

    def _on_open(self, ws) -> None:
        with self._lock:
            self._connected = True
            self._authed = False
        self._set_status("authenticating")
        self._send({"action": "auth", "params": self.api_key})

    def _handle_status(self, ev: dict) -> None:
        st = str(ev.get("status") or "").lower()
        msg = str(ev.get("message") or "")
        if st == "connected":                       # some clusters want auth after this
            self._set_status("authenticating")
            self._send({"action": "auth", "params": self.api_key})
        elif st in ("auth_success", "authenticated"):
            with self._lock:
                self._authed = True
                params = ",".join(self._sub_params)
            if params:
                self._send({"action": "subscribe", "params": params})
                self._set_status("streaming")
            else:
                self._set_status("authenticated — no contracts to subscribe "
                                 "(no screen/snapshots yet?)")
        elif st in ("auth_failed", "auth_timeout", "error", "forbidden", "max_connections"):
            self._set_status(f"auth/entitlement error: {st}", error=f"{st}: {msg}".strip(": "))
        # "success" = subscribe confirmation; nothing to do (already streaming).

    def _on_message(self, ws, message: str) -> None:
        try:
            events = json.loads(message)
        except Exception:  # noqa: BLE001
            return
        for ev in events if isinstance(events, list) else [events]:
            kind = ev.get("ev")
            if kind == "status":
                self._handle_status(ev)
            elif kind == "AM":
                with self._lock:
                    apply_am(self._states, self._entry_index, ev)

    def _on_error(self, ws, err) -> None:
        self._set_status("connection error", error=str(err))

    def _on_close(self, ws, *a) -> None:
        with self._lock:
            self._connected = False
            self._authed = False
            if self._status == "streaming":
                self._status = "reconnecting"

    def _connect(self) -> None:
        import websocket  # websocket-client; imported lazily so the dep is optional

        self._set_status("connecting")
        self._ws = websocket.WebSocketApp(
            self.cfg.get("live.ws_url", "wss://delayed.massive.com/options"),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # Blocks until the socket drops; _on_open/_on_close own the connected flag.
        self._ws.run_forever(ping_interval=30, ping_timeout=10)
        with self._lock:
            self._connected = False

    def _control_loop(self) -> None:
        refresh = float(self.cfg.get("live.refresh_seconds", 45))
        backoff = 5.0
        ws_thread: threading.Thread | None = None
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            open_now = _market_open(now, self.cfg)

            if open_now:
                if self._store_mtime() != self._loaded_mtime:
                    self._rebuild()
                if not self._connected and (ws_thread is None or not ws_thread.is_alive()):
                    ws_thread = threading.Thread(target=self._connect, daemon=True)
                    ws_thread.start()
                    time.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 2, 120.0) if not self._connected else 5.0
                self._evaluate_all()
            else:
                if self._connected and self._ws is not None:
                    self._ws.close()
                with self._lock:
                    self._status = "closed (outside market hours)"
            self._stop.wait(refresh)
