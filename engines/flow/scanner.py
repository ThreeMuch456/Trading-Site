"""
Background scanner — polls Massive.com option chain snapshots on a schedule,
detects unusual flow, saves alerts to the database, and updates price
snapshots for watchlist entries.

Run standalone:  python scanner.py
Or imported by main.py for local dev (runs in a background thread).
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from . import massive_client
from .massive_client import MassiveError
from .db import (Session, SweepAlert, WatchlistEntry, PriceSnapshot,
                ContractVolumeState, init_db)
from .signals import is_auto_watch
from .tickers import TICKER_UNIVERSE
from .terminal import print_alert, print_status, print_error

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MIN_PREMIUM            = float(os.getenv("MIN_PREMIUM", 25_000))
MIN_VOLUME             = int(os.getenv("MIN_VOLUME", 100))
VOL_OI_THRESHOLD       = float(os.getenv("VOL_OI_THRESHOLD", 0.5))
SCAN_INTERVAL_MINUTES  = int(os.getenv("SCAN_INTERVAL_MINUTES", 5))
MAX_EXPIRY_DAYS        = int(os.getenv("MAX_EXPIRY_DAYS", 45))  # only scan expiries within N days
MIN_VOLUME_DELTA       = int(os.getenv("MIN_VOLUME_DELTA", 100))  # fresh contracts since last scan required to flag
AUTO_WATCH_WHALES      = os.getenv("AUTO_WATCH_WHALES", "true").lower() in ("1", "true", "yes")
BATCH_DELAY            = 0.15   # seconds between ticker fetches (courtesy only — plan has unlimited calls)
DEDUP_WINDOW_MINUTES   = 60     # don't re-flag the same contract within this window
# Retention — keep Postgres from growing unbounded. The UI only looks back 24h;
# older rows are pure history. Alerts referenced by a watchlist entry are kept.
ALERT_RETENTION_DAYS    = int(os.getenv("ALERT_RETENTION_DAYS", 15))
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", 15))
# Index ETFs carry enormous chains — SPY's 45-day chain blows past the client's
# pagination cap every scan — and the UI only uses their 0DTE/1DTE flow, so
# fetch a much shorter expiry window for them.
ETF_FOCUS_TICKERS   = {t.strip().upper() for t in
                       os.getenv("ETF_FOCUS_TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()}
ETF_MAX_EXPIRY_DAYS = int(os.getenv("ETF_MAX_EXPIRY_DAYS", 7))

ET = pytz.timezone("US/Eastern")

DATA_DELAY_MINUTES = 15  # Massive Starter data lag — snapshots trail real time by this much


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    # Keep scanning past the bell: the data is 15-min delayed, so the heavy
    # 15:45–16:00 close-of-day flow only appears in snapshots after 16:00.
    close_ = (now.replace(hour=16, minute=0, second=0, microsecond=0)
              + timedelta(minutes=DATA_DELAY_MINUTES + SCAN_INTERVAL_MINUTES))
    return open_ <= now <= close_


def recent_alert_keys(session, ticker: str) -> set:
    """
    (strike, expiry, option_type) keys flagged for `ticker` inside the dedup
    window — loaded once per ticker so the scan loop never queries per contract.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    rows = session.query(
        SweepAlert.strike, SweepAlert.expiry, SweepAlert.option_type
    ).filter(
        SweepAlert.ticker      == ticker,
        SweepAlert.detected_at >= cutoff,
    ).all()
    return {(r.strike, r.expiry, r.option_type) for r in rows}


def safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def safe_int(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _trading_day() -> str:
    """Current trading date in ET."""
    return datetime.now(ET).strftime("%Y-%m-%d")


def _volume_deltas(session, candidates: list, today: str) -> dict:
    """
    Contracts traded since the previous scan, keyed by option ticker, with the
    per-contract volume state updated in place. Massive's day volume is
    cumulative, so the first sighting of a day treats the whole cumulative
    volume as the delta. `candidates` is a list of (option_ticker, volume);
    states are loaded in one batched query instead of one SELECT per contract.
    """
    deltas: dict[str, int] = {}
    occs = [occ for occ, _ in candidates]

    states = {}
    CHUNK = 500  # stay under SQLite's bound-parameter limit
    for i in range(0, len(occs), CHUNK):
        for s in session.query(ContractVolumeState).filter(
            ContractVolumeState.option_ticker.in_(occs[i:i + CHUNK])
        ).all():
            states[s.option_ticker] = s

    for occ, vol in candidates:
        if occ in deltas:               # duplicate row in the chain snapshot
            continue

        state = states.get(occ)
        if state is None:
            session.add(ContractVolumeState(
                option_ticker=occ, day=today, volume=vol
            ))
            deltas[occ] = vol
        elif state.day != today:        # stale row from a previous session
            state.day, state.volume = today, vol
            deltas[occ] = vol
        else:
            delta = vol - (state.volume or 0)
            if vol > (state.volume or 0):
                state.volume = vol
            deltas[occ] = max(delta, 0)

    return deltas


# ── Core scan logic ───────────────────────────────────────────────────────────

def scan_ticker(ticker: str, session, max_expiry_days: int = None) -> int:
    """
    Fetch the option chain snapshot for one ticker and save any flagged
    contracts. Returns the number of new alerts created.
    """
    try:
        contracts = massive_client.get_option_chain(
            ticker, max_expiry_days or MAX_EXPIRY_DAYS
        )
    except MassiveError as e:
        logger.debug("Massive error scanning %s: %s", ticker, e)
        return 0
    except Exception as e:
        logger.debug("Error scanning %s: %s", ticker, e)
        return 0

    alerts_found = 0
    today = _trading_day()

    # Parse and basic-filter first so the DB work can be batched — one query
    # for volume state and one for the dedup window, not one per contract.
    candidates = []
    stale_bars = 0
    for c in contracts:
        vol      = safe_int(c.get("volume"))
        strike   = safe_float(c.get("strike"))
        # `price` is the contract's last trade (quotes aren't entitled on this
        # plan), falling back to the day's close/VWAP.
        price    = safe_float(c.get("price"))
        opt_type = c.get("contract_type")
        expiry   = c.get("expiry")

        # Drop contracts still carrying a previous session's daily bar. Massive
        # returns the stale bar until a contract trades again, so its volume,
        # close and last trade are all from an earlier day. Without this, the
        # first scan of a session — where _volume_deltas treats the whole
        # cumulative volume as the delta — flags that old volume as fresh flow
        # and prices it off a trade that may be weeks old.
        # A missing date means we can't tell; stay permissive rather than
        # discarding the chain if the upstream field ever goes away.
        bar_date = c.get("day_bar_date")
        if bar_date and bar_date != today:
            stale_bars += 1
            continue

        if vol < MIN_VOLUME or price <= 0 or strike <= 0:
            continue
        if opt_type not in ("call", "put") or not expiry:
            continue
        candidates.append(c)

    if stale_bars:
        logger.debug("%s: skipped %d contracts on a prior-session bar",
                     ticker, stale_bars)

    if not candidates:
        return 0

    # Fresh activity since the previous scan (day volume is cumulative).
    # State is tracked for every candidate so deltas stay per-scan even
    # when a contract doesn't get flagged.
    deltas = _volume_deltas(
        session,
        [(c["option_ticker"], safe_int(c.get("volume")))
         for c in candidates if c.get("option_ticker")],
        today,
    )
    flagged = recent_alert_keys(session, ticker)

    for c in candidates:
        vol      = safe_int(c.get("volume"))
        oi       = safe_int(c.get("open_interest"))
        price    = safe_float(c.get("price"))
        bid      = safe_float(c.get("bid"))
        strike   = safe_float(c.get("strike"))
        opt_type = c.get("contract_type")
        expiry   = c.get("expiry")
        occ      = c.get("option_ticker")
        underlying_price = safe_float(c.get("underlying_price"))

        vol_delta = deltas.get(occ, vol) if occ else vol

        notional     = vol * price * 100
        vol_oi_ratio = (vol / oi) if oi > 1 else float(vol)

        if notional < MIN_PREMIUM:
            continue

        # Must clear the vol/OI threshold OR have volume exceed OI outright
        if vol_oi_ratio < VOL_OI_THRESHOLD and vol <= oi:
            continue

        # No fresh burst since the last scan → the day total was already seen;
        # don't re-flag stale cumulative volume.
        if vol_delta < MIN_VOLUME_DELTA:
            continue

        if (strike, expiry, opt_type) in flagged:
            continue
        flagged.add((strike, expiry, opt_type))

        alert = SweepAlert(
            ticker             = ticker,
            option_ticker      = occ,
            expiry             = expiry,
            strike             = strike,
            option_type        = opt_type,
            volume             = vol,
            volume_delta       = vol_delta,
            side               = c.get("side"),
            open_interest      = oi,
            vol_oi_ratio       = round(vol_oi_ratio, 2),
            estimated_premium  = round(notional),
            ask                = price,   # effective contract price (ask, else last trade)
            bid                = bid,
            underlying_price   = underlying_price,
            implied_volatility = safe_float(c.get("iv")) or None,
            delta              = c.get("delta"),
            gamma              = c.get("gamma"),
            theta              = c.get("theta"),
            vega               = c.get("vega"),
        )
        session.add(alert)
        session.flush()          # assign alert.id without a full commit cycle
        print_alert(alert)
        alerts_found += 1

        # Auto-watch large, high-conviction sweeps (whale premium + high score,
        # deep-ITM excluded) so P&L tracking starts without catching them live.
        if AUTO_WATCH_WHALES and is_auto_watch(alert):
            existing = session.query(WatchlistEntry).filter_by(
                ticker=ticker, strike=strike, expiry=expiry,
                option_type=opt_type, is_active=True,
            ).first()
            if existing is None:
                session.add(WatchlistEntry(
                    alert_id         = alert.id,
                    ticker           = ticker,
                    option_ticker    = occ,
                    expiry           = expiry,
                    strike           = strike,
                    option_type      = opt_type,
                    entry_price      = price,
                    entry_underlying = underlying_price,
                ))
                alert.on_watchlist = True

    # One commit per ticker: alerts, auto-watch entries, and volume-state
    # updates for candidates that weren't flagged.
    session.commit()
    return alerts_found


# ── Watchlist snapshot updates ────────────────────────────────────────────────

def update_watchlist_snapshots(session) -> None:
    """
    For each active watchlist entry, fetch the current option price
    and record a P&L snapshot.
    """
    entries = session.query(WatchlistEntry).filter_by(is_active=True).all()
    if not entries:
        return

    today = _trading_day()
    for entry in entries:
        try:
            # Retire expired contracts — they're dead, and their price never
            # updates again. ISO date strings compare correctly lexically.
            if entry.expiry and entry.expiry < today:
                entry.is_active = False
                continue

            if not entry.option_ticker:
                # Legacy row from before OCC tickers were stored — skip.
                logger.debug("Watchlist entry %s has no option_ticker", entry.id)
                continue

            snapshot = massive_client.get_contract_snapshot(
                entry.ticker, entry.option_ticker
            )
            if not snapshot:
                continue

            underlying_price = safe_float(snapshot.get("underlying_price"))

            # Effective price: ask if quotes are entitled, else last trade / close.
            current_price = safe_float(snapshot.get("price"))
            if current_price <= 0 or (entry.entry_price or 0) <= 0:
                continue

            # 15-min delayed data at a 5-min cadence means most polls return
            # the exact same prices — skip the duplicate row (roughly 2-3×
            # fewer snapshot rows). Still write hourly so charts keep moving.
            prev = (
                session.query(PriceSnapshot)
                .filter_by(entry_id=entry.id)
                .order_by(PriceSnapshot.captured_at.desc())
                .first()
            )
            if (prev is not None
                    and prev.option_price == current_price
                    and prev.underlying_price == underlying_price
                    and prev.captured_at is not None):
                cap = prev.captured_at
                if cap.tzinfo is None:
                    cap = cap.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - cap) < timedelta(hours=1):
                    continue

            pnl_pct = ((current_price - entry.entry_price) / entry.entry_price) * 100

            snap = PriceSnapshot(
                entry_id         = entry.id,
                option_price     = current_price,
                underlying_price = underlying_price,
                pnl_pct          = round(pnl_pct, 2),
            )
            session.add(snap)
            time.sleep(BATCH_DELAY)

        except Exception as e:
            logger.debug("Error updating watchlist entry %s: %s", entry.id, e)

    session.commit()


# ── Main scan loop ────────────────────────────────────────────────────────────

def run_scan(force: bool = False) -> None:
    """
    Run one full scan pass.
    Set force=True to bypass the market-hours check (e.g. end-of-day backfill).
    """
    if not force and not is_market_open():
        print_status("Market closed — skipping scan")
        return

    print_status(f"Scanning {len(TICKER_UNIVERSE)} tickers...")
    session = Session()
    total = 0
    started = time.monotonic()

    try:
        # Drop volume-state rows from previous trading days.
        session.query(ContractVolumeState).filter(
            ContractVolumeState.day != _trading_day()
        ).delete(synchronize_session=False)

        # Retention: purge old alerts (unless a watchlist entry references
        # them) and old P&L snapshots so the database stops growing.
        alert_cutoff = datetime.now(timezone.utc) - timedelta(days=ALERT_RETENTION_DAYS)
        watched_ids = session.query(WatchlistEntry.alert_id).filter(
            WatchlistEntry.alert_id.isnot(None)
        )
        session.query(SweepAlert).filter(
            SweepAlert.detected_at < alert_cutoff,
            ~SweepAlert.id.in_(watched_ids),
        ).delete(synchronize_session=False)

        snap_cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
        session.query(PriceSnapshot).filter(
            PriceSnapshot.captured_at < snap_cutoff
        ).delete(synchronize_session=False)
        session.commit()

        for ticker in TICKER_UNIVERSE:
            days = (ETF_MAX_EXPIRY_DAYS if ticker in ETF_FOCUS_TICKERS
                    else MAX_EXPIRY_DAYS)
            total += scan_ticker(ticker, session, days)
            time.sleep(BATCH_DELAY)

        update_watchlist_snapshots(session)
        elapsed = time.monotonic() - started
        print_status(f"Scan complete - {total} new alert{'s' if total != 1 else ''} "
                     f"in {elapsed/60:.1f}m")
        if elapsed > SCAN_INTERVAL_MINUTES * 60:
            print_status(f"Note: cycle ({elapsed/60:.1f}m) exceeds the "
                         f"{SCAN_INTERVAL_MINUTES}m interval — scans will run back-to-back")

    except Exception as e:
        print_error(f"Scan failed: {e}")
    finally:
        session.close()


def run_backfill() -> None:
    """
    One-shot scan that ignores market hours — useful for catching today's
    flow after the close, when the day's volume is still reflected in the snapshot.
    """
    print_status("Running end-of-day backfill (market-hours check bypassed)...")
    print_status(f"Universe: {len(TICKER_UNIVERSE)} tickers | "
                 f"min premium: ${MIN_PREMIUM:,.0f}")
    run_scan(force=True)
    print_status("Backfill complete. Check http://localhost:5000 for results.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if not massive_client.API_KEY:
        print_error(
            "MASSIVE_API_KEY is not set. Add it to your .env before running "
            "the scanner (get a key at https://massive.com/dashboard)."
        )
        sys.exit(1)

    init_db()

    if "--backfill" in sys.argv:
        # Single pass, then exit — no scheduler
        run_backfill()
    else:
        print_status("Options Flow Scanner starting...")
        print_status(f"Universe: {len(TICKER_UNIVERSE)} tickers | "
                     f"interval: {SCAN_INTERVAL_MINUTES}m | "
                     f"min premium: ${MIN_PREMIUM:,.0f}")

        # Run once immediately, then on schedule
        run_scan()

        scheduler = BlockingScheduler(timezone=pytz.utc)
        scheduler.add_job(run_scan, "interval", minutes=SCAN_INTERVAL_MINUTES)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print_status("Scanner stopped.")
