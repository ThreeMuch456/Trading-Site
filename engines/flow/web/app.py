"""
Flask web app — serves the alerts feed and watchlist UI.

Local:   python web/app.py   (or via main.py)
Railway: gunicorn "web.app:create_app()" --bind 0.0.0.0:$PORT
"""

import os
import re
import sys

# Allow importing db, etc. from project root when run directly

from datetime import date, datetime, time, timedelta, timezone
from flask import Flask, render_template, jsonify, redirect, url_for, request, abort
from sqlalchemy import or_
from dotenv import load_dotenv

from ..db import Session, SweepAlert, WatchlistEntry, PriceSnapshot, init_db
from ..signals import (
    ET, HIGHLIGHT_MIN_PREMIUM, HIGHLIGHT_MIN_VOL_OI, WHALE_PREMIUM,
    ETF_MIN_DELTA, ETF_FAR_OTM_PCT_0DTE, ETF_FAR_OTM_PCT_1DTE,
    days_to_expiry, today_et, trading_days_to_expiry,
    is_deep_itm, is_expired, is_far_otm, is_notable, score_alert,
)
from ..sectors import (
    SECTOR_META, CONTEXT_GROUPS, CYCLICAL, DEFENSIVE, is_proxy, sector_of,
)

load_dotenv()

# ── Notable-flow ("sweeps") configuration ──────────────────────────────────────
# A curated view that ranks alerts by significance so the largest / most unusual
# entries stay pinned at the top instead of scrolling past on the 30s refresh.
# Scoring / classification thresholds live in signals.py (shared with scanner).

HIGHLIGHT_WINDOW_HOURS = int(os.getenv("HIGHLIGHT_WINDOW_HOURS", 24))
HIGHLIGHT_NEW_MINUTES  = int(os.getenv("HIGHLIGHT_NEW_MINUTES", 15))
HIGHLIGHT_BURST_VOLUME = int(os.getenv("HIGHLIGHT_BURST_VOLUME", 500))
SWEEPS_MIN_SCORE       = int(os.getenv("SWEEPS_MIN_SCORE", 85))  # hide lower-conviction entries

# Dedicated short-dated index-ETF page (SPY/QQQ/IWM only, 0DTE + 1DTE, no floor).
ETF_FOCUS_TICKERS = [t.strip().upper() for t in
                     os.getenv("ETF_FOCUS_TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()]
ETF_MAX_DTE       = int(os.getenv("ETF_MAX_DTE", 1))  # 0DTE and 1DTE

# The index-ETF page is scoped to one trading session rather than a rolling
# window: 0DTE/1DTE are relative to *today*, so yesterday's 1DTE flow would
# otherwise reappear each morning re-labelled as today's 0DTE. The page starts
# over at this ET time each day and shows nothing before it — no rollback to
# the last session (the EOD Positioning section is the deliberate exception).
ETF_SESSION_START_ET = os.getenv("ETF_SESSION_START_ET", "09:30")  # market open

# Fresh-flow "wave" detection on the index-ETF page. A wave is a burst of
# contracts hitting one ticker on one side within the last HIGHLIGHT_NEW_MINUTES
# with little on the other side to match it — the read this page exists for.
ETF_SURGE_MIN_VOLUME = int(os.getenv("ETF_SURGE_MIN_VOLUME", 2000))    # fresh contracts
ETF_SURGE_MIN_SKEW   = float(os.getenv("ETF_SURGE_MIN_SKEW", 0.70))    # one side's share

# End-of-day positioning. Late-session flow into contracts that outlive today's
# close is a bet on *tomorrow*, so it gets its own window and its own (looser,
# because it accumulates over hours rather than one scan) thresholds.
ETF_EOD_START_ET     = os.getenv("ETF_EOD_START_ET", "15:00")          # ET — 12:00 Pacific
ETF_EOD_MIN_VOLUME   = int(os.getenv("ETF_EOD_MIN_VOLUME", 5000))      # contracts in the window
ETF_EOD_MIN_SKEW     = float(os.getenv("ETF_EOD_MIN_SKEW", 0.65))      # one side's share

# Per-ticker rollup page (/ticker/<symbol>) — a drill-down, not a default view:
# every ticker on the Alerts / Sweeps / Index ETF pages links into it. It answers
# one question the feeds can't, because they're sorted by print rather than by
# name: which way is this security's flow actually leaning?
TICKER_LEAN_MIN_SKEW = float(os.getenv("TICKER_LEAN_MIN_SKEW", 0.60))  # one side's premium share
TICKER_TOP_STRIKES   = int(os.getenv("TICKER_TOP_STRIKES", 12))        # rows in the strike table
TICKER_MAX_CARDS     = int(os.getenv("TICKER_MAX_CARDS", 60))          # entries rendered below

# Rotation page (/rotation) — the per-ticker rollup one level up. Alerts are
# bucketed by sector (sectors.py) so the question becomes "which corner of the
# market is money positioning in", which no per-name feed can answer.
#
# Two axes, mirroring a Relative Rotation Graph: where a sector's call/put tilt
# sits *relative to the rest of the tape* (level), and whether that tilt is
# building or fading (momentum). See _rotation_sectors for how each is measured
# — they deliberately use different sources, and the reason matters.
ROTATION_WINDOW_HOURS = int(os.getenv("ROTATION_WINDOW_HOURS", HIGHLIGHT_WINDOW_HOURS))
ROTATION_RECENT_HOURS = float(os.getenv("ROTATION_RECENT_HOURS", 2))   # the "now" slice
# Gates. A sector with one $40k print is 100% call-skewed and means nothing, so
# thin sectors are listed separately rather than ranked.
ROTATION_MIN_PREMIUM  = float(os.getenv("ROTATION_MIN_PREMIUM", 2_000_000))
ROTATION_MIN_TICKERS  = int(os.getenv("ROTATION_MIN_TICKERS", 2))
# Dead bands. Both axes must clear theirs before a sector earns a quadrant —
# otherwise a 1pp drift would read as "rotating in".
ROTATION_MIN_EDGE     = float(os.getenv("ROTATION_MIN_EDGE", 0.03))    # vs the tape
ROTATION_MIN_SHIFT    = float(os.getenv("ROTATION_MIN_SHIFT", 0.05))   # recent vs prior
# One name holding this much of a sector's premium makes the sector read a
# single position, not a rotation. Surfaced rather than filtered.
ROTATION_CONC_WARN    = float(os.getenv("ROTATION_CONC_WARN", 0.60))
# Gap between a sector ETF's own tilt and its constituents' before it's called out.
ROTATION_DIVERGE_MIN  = float(os.getenv("ROTATION_DIVERGE_MIN", 0.25))
# Cyclical-minus-defensive call share before the tape is called risk-on/risk-off.
ROTATION_RISK_MIN     = float(os.getenv("ROTATION_RISK_MIN", 0.10))
# Most notes to render; beyond a handful the section stops being a summary.
ROTATION_MAX_NOTES    = int(os.getenv("ROTATION_MAX_NOTES", 4))
# Sectors named inside a single note before it says "and N others".
ROTATION_NOTE_MAX_SECTORS = int(os.getenv("ROTATION_NOTE_MAX_SECTORS", 3))
# Heat strip: how many time buckets the window is cut into, and the Edge
# magnitude that saturates a cell's colour.
ROTATION_TREND_BUCKETS    = int(os.getenv("ROTATION_TREND_BUCKETS", 12))
ROTATION_TREND_FULL_SCALE = float(os.getenv("ROTATION_TREND_FULL_SCALE", 0.25))
# Shape of the heat-strip colour ramp (see `intensity` in _rotation_trend).
# GAMMA below 1 lifts the middle of the range; FLOOR keeps a small-but-real
# edge visibly coloured rather than fading into the cell base.
ROTATION_TREND_GAMMA      = float(os.getenv("ROTATION_TREND_GAMMA", 0.60))
ROTATION_TREND_FLOOR      = float(os.getenv("ROTATION_TREND_FLOOR", 0.10))


def _parse_hhmm(value, default=(15, 0)):
    """"HH:MM" → (hour, minute), falling back to `default` on anything odd."""
    try:
        h, m = str(value).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default


ETF_EOD_START_HM     = _parse_hhmm(ETF_EOD_START_ET)
ETF_SESSION_START_HM = _parse_hhmm(ETF_SESSION_START_ET, default=(9, 30))


def _to_utc(dt):
    """Aware ET datetime → naive UTC, matching how the DB stores timestamps."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _to_et(dt):
    """Naive-UTC datetime (as stored) → aware ET. The inverse of _to_utc."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def _recent_session_date(cutoff_hm):
    """
    The most recent *trading* date whose `cutoff_hm` (ET) has already passed.

    Before the cutoff this rolls back a day, and it keeps rolling back over
    weekends, so a pre-market or weekend look lands on the last real session
    instead of an empty window. Weekends only — market holidays aren't
    accounted for (same simplification as signals.trading_days_to_expiry).

    Used by the EOD window only: yesterday's late-day positioning is a read on
    the session about to start, so it survives the morning reset that clears
    the rest of the page (`_session_window`).
    """
    now_et = datetime.now(ET)
    d = now_et.date()
    if now_et.time() < time(*cutoff_hm):
        d -= timedelta(days=1)
    while d.weekday() >= 5:          # Sat / Sun
        d -= timedelta(days=1)
    return d


def _session_window():
    """
    The current trading session as (start_utc, end_utc, session_date).

    Always *today's* ET date — deliberately no rollback to the last session
    (unlike `_eod_window`, which needs the previous afternoon to say anything
    about the session now starting). The window opens at ETF_SESSION_START_ET
    and closes at midnight ET, so from midnight until the open it's a window in
    the future and the page is empty: the card feed, rollup and waves all start
    each day blank rather than replaying yesterday pre-market. After the open
    the start bound is moot — the scanner never writes before 9:30 — and the
    midnight end bound covers the ~20 min it runs past the bell for the delayed
    close-of-day flow.

    On a weekend or holiday the page stays blank for the same reason. That's
    the intended trade-off: no stale session masquerading as the current one.
    """
    session_date = datetime.now(ET).date()
    start_et = ET.localize(datetime.combine(session_date, time(*ETF_SESSION_START_HM)))
    end_et   = ET.localize(datetime.combine(session_date + timedelta(days=1), time(0, 0)))
    return _to_utc(start_et), _to_utc(end_et), session_date


# How often pages auto-refresh. Massive data is 15-min delayed and the scanner
# only writes every SCAN_INTERVAL_MINUTES, so refreshing faster than the scan
# cadence just re-downloads identical HTML (Railway egress for nothing).
PAGE_REFRESH_SECONDS = int(os.getenv("PAGE_REFRESH_SECONDS",
                                     int(os.getenv("SCAN_INTERVAL_MINUTES", 5)) * 60))

# The Alerts feed is a tail, not a full day: it renders the most recent N rows
# inside the 24h window. Kept here so the page can say which it is.
ALERTS_PAGE_LIMIT = int(os.getenv("ALERTS_PAGE_LIMIT", 200))
# The scanner's notional floor (scanner.py owns it) — read only to label the page.
ALERTS_MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", 25_000))

# Cap sparkline data embedded in the watchlist page — a long-lived entry
# accumulates thousands of snapshots; ~150 points renders identically.
MAX_SPARK_POINTS = int(os.getenv("MAX_SPARK_POINTS", 150))


# ── Glossary (single source of truth for hover tooltips + the glossary panel) ───
# `title=` attributes on column headers / metric labels / flag tags pull from
# here, and the collapsible glossary rendered on every page lists the same terms.
COLUMN_GLOSSARY = {
    "Time (ET)":  "When the scanner first flagged this contract (US/Eastern).",
    "Ticker":     "Underlying stock or ETF symbol.",
    "Contract":   "Option type (CALL/PUT), strike price, and expiration date.",
    "Strike":     "Strike price of the option contract.",
    "Expiry":     "Expiration date of the option contract.",
    "Premium":    "Estimated notional traded = volume × contract price × 100.",
    "Volume":     "Contracts traded today (cumulative day volume).",
    "Δ Vol":      "New contracts traded since the previous scan (burst detection).",
    "Open Int.":  "Open interest — total outstanding contracts entering today.",
    "Vol/OI":     "Day volume ÷ open interest. High = unusually active vs. existing positions. "
                  "Shows \"no OI\" when the contract had no open interest at all — the ratio is "
                  "undefined there, and that's already the strongest version of this signal.",
    "Price":      "The contract's last trade at the moment the alert fired, on 15-min delayed data. "
                  "This is a record, not a live quote — it is never updated afterwards, so on "
                  "short-dated contracts it can sit far from the current market. Check the age "
                  "next to it.",
    "IV":         "Implied volatility — the market's expected annualized move priced into the option.",
    "Delta":      "Price change per $1 move in the underlying; also a rough probability of finishing in-the-money.",
    "Underlying": "Price of the underlying stock/ETF when the alert fired (15-min delayed), "
                  "not its current price.",
    "Signal":     "Composite 0–100 significance score: premium + vol/OI + aggressiveness.",
    "Entry price": "Option price recorded when the contract was added to the watchlist.",
    "Entry underlying": "Price of the underlying when the contract was added to the watchlist.",
    "Latest price": "Most recent option price for this contract — refreshed by the scanner every "
                    "cycle, on 15-min delayed data. Unlike an alert's price, this one keeps updating.",
    "P&L":        "Percent change in the option's price since it was added to the watchlist.",
    # Index ETFs page concepts
    "Fresh flow": f"Contracts traded since the previous scan (not cumulative day volume), summed "
                  f"over entries flagged in the last {HIGHLIGHT_NEW_MINUTES} min — what just printed.",
    "Window premium": "Notional that printed inside a window (the fresh-flow banner, the EOD "
                      "chip): that window's contracts priced at the contract's own price. Distinct "
                      "from an entry's Premium, which prices the whole day's cumulative volume.",
    "Skew":       "One side's share of the call+put volume in a window. 50% is balanced; "
                  "the further from 50%, the more one-directional the flow.",
    "EOD window": f"Late-session positioning window — flow from {ETF_EOD_START_ET} ET onward into "
                  f"contracts that expire *after* that session's close. Excludes 0DTE, which is a "
                  f"bet on the current day rather than the next one.",
    # Per-ticker rollup page
    "Lean":       f"Which way one ticker's flow is tilted. A side holding ≥"
                  f"{TICKER_LEAN_MIN_SKEW * 100:.0f}% of the window's premium earns a CALL or PUT "
                  f"lean; anything closer reads BALANCED. Inferred from the call/put mix alone — "
                  f"this data tier carries no bid/ask, so a bought call and a sold call look "
                  f"identical. Read it as where the size is, not as proof of direction.",
    "Put/Call":   "Put ÷ call, by premium and by volume. Below 1 is call-heavy, above 1 "
                  "put-heavy, 1.0 balanced.",
    "Moneyness":  "How far the strike sits from the underlying at flag time, as a percent. "
                  "Positive = strike above spot, negative = below.",
    # Rotation page concepts
    "Sector":     "Which of the ~21 flow groups the ticker belongs to (sectors.py). "
                  "GICS-shaped, but with the buckets this universe actually trades split "
                  "out — Semis apart from Megacap Tech, plus Crypto, Biotech, China/EM "
                  "and Speculative. Index and volatility products aren't a sector and are "
                  "reported separately as context.",
    "Edge":       "A sector's call share of premium minus the whole tape's, in percentage "
                  "points. Strips out the day's direction: on a strong tape everything is "
                  "call-heavy, and what matters is which sectors are *more* call-heavy "
                  "than the rest. Positive = relatively bullish positioning.",
    "Shift":      f"Change in a sector's call share over the last "
                  f"{ROTATION_RECENT_HOURS:g}h versus earlier in the window, in percentage "
                  f"points. Priced off contracts that traded in each slice (Δ Vol), not "
                  f"cumulative day volume, so it measures what is printing now rather than "
                  f"what already printed. Positive = the tilt is turning more bullish.",
    "Heat strip": f"Edge for each sector, bucket by bucket across the window — "
                  f"green where a sector was more call-heavy than the tape *at that "
                  f"hour*, red where it was less. Taking the tape out bucket by bucket "
                  f"is what makes a diagonal in the grid mean rotation rather than the "
                  f"whole market moving together. Colour saturates at ±"
                  f"{ROTATION_TREND_FULL_SCALE * 100:.0f}pp. A blank cell means the "
                  f"sector had no flow in that bucket — not that it was balanced.",
    "Slopegraph": f"Each sector's call share before the last "
                  f"{ROTATION_RECENT_HOURS:g}h against its call share during it — the "
                  f"Shift axis drawn as the two numbers it is made of. Length of the "
                  f"line is the size of the turn; sectors are ordered by it, most "
                  f"bullish turn first.",
    "Quadrant":   f"Edge and Shift combined, as on a relative-rotation graph. "
                  f"LEADING = bullish and building · IMPROVING = bearish but turning "
                  f"(the early-rotation tell) · WEAKENING = bullish but fading · "
                  f"LAGGING = bearish and getting worse. Needs |Edge| ≥ "
                  f"{ROTATION_MIN_EDGE * 100:.0f}pp and |Shift| ≥ "
                  f"{ROTATION_MIN_SHIFT * 100:.0f}pp; anything inside those bands is "
                  f"NEUTRAL rather than a made-up label.",
    "Breadth":    f"How many of a sector's names lean each way on their own premium "
                  f"(a name needs ≥{TICKER_LEAN_MIN_SKEW * 100:.0f}% on one side to count). "
                  f"Separates \"the whole sector is bid\" from \"one name is\".",
    "Concentration": f"The largest single name's share of its sector's premium. Above "
                     f"{ROTATION_CONC_WARN * 100:.0f}% the sector row is really that one "
                     f"position, and is tagged to say so.",
    "ETF vs names": f"The sector ETF's own call share minus its constituents'. A gap of "
                    f"≥{ROTATION_DIVERGE_MIN * 100:.0f}pp is flagged: ETF puts against "
                    f"call-heavy single names usually means the basket is being hedged "
                    f"while the names are being bought, not that anyone is bearish.",
    "Risk appetite": f"Cyclical sectors' call share minus defensive sectors', in "
                     f"percentage points. Above +{ROTATION_RISK_MIN * 100:.0f}pp reads "
                     f"RISK-ON (upside bought in growth, hedges bought in safety), below "
                     f"−{ROTATION_RISK_MIN * 100:.0f}pp RISK-OFF. Posture per sector is "
                     f"set in sectors.py.",
}

# Keyed by the flag's CSS class (from alert_flags) → (example label, definition).
FLAG_GLOSSARY = {
    "whale":   ("WHALE",         f"Premium ≥ ${WHALE_PREMIUM:,.0f} — institution-sized single print."),
    "big":     ("BIG $",         f"Premium ≥ ${HIGHLIGHT_MIN_PREMIUM:,.0f} (notable) but below whale size."),
    "unusual": ("×VOL/OI",       "Day volume is a high multiple of open interest — unusually active."),
    "burst":   ("BURST",         "Large jump in volume since the previous scan — fresh flow this cycle."),
    "buy":     ("@ASK",          "Traded at/near the ask — aggressive buyer."),
    "sell":    ("@BID",          "Traded at/near the bid — aggressive seller."),
    "newoi":   ("NO OI / VOL≫OI", "Little or no prior open interest — likely a brand-new position."),
    "new":     ("NEW",           f"First detected within the last {HIGHLIGHT_NEW_MINUTES} minutes."),
    "wave":    ("⚡ WAVE",        f"Part of a one-sided burst on this ticker — ≥{ETF_SURGE_MIN_VOLUME:,} fresh "
                                 f"contracts in the last {HIGHLIGHT_NEW_MINUTES} min with ≥"
                                 f"{ETF_SURGE_MIN_SKEW * 100:.0f}% on this side (Index ETFs page)."),
    "eod":     ("◗ EOD",         f"Late-session positioning for the next day — bought after "
                                 f"{ETF_EOD_START_ET} ET into a contract that outlives today's close, "
                                 f"on the heavy side of a ≥{ETF_EOD_MIN_VOLUME:,}-contract, ≥"
                                 f"{ETF_EOD_MIN_SKEW * 100:.0f}% one-sided flow (Index ETFs page)."),
    "farotm":  ("FAR OTM",       f"|delta| below {ETF_MIN_DELTA:.2f} (or, without greeks, more than "
                                 f"{ETF_FAR_OTM_PCT_0DTE:g}% OTM on 0DTE / {ETF_FAR_OTM_PCT_1DTE:g}% on 1DTE) — "
                                 f"a cheap lottery ticket. Hidden on the Index ETFs page unless "
                                 f"'Far OTM' is on, and left out of its rollup, waves and EOD read."),
}


def alert_flags(a, now) -> list:
    """Human-readable reason tags explaining why an alert is notable."""
    flags = []
    premium = a.estimated_premium or 0
    ratio   = a.vol_oi_ratio or 0
    vol     = a.volume or 0
    oi      = a.open_interest or 0

    if premium >= WHALE_PREMIUM:
        flags.append(("whale", "WHALE"))
    elif premium >= HIGHLIGHT_MIN_PREMIUM:
        flags.append(("big", "BIG $"))
    # The scanner stores raw volume in vol_oi_ratio when a contract has no open
    # interest — the ratio is undefined, so rendering it as a multiple invents a
    # number ("6,690× VOL/OI" on a 6,690-lot print). Those carry the NO OI tag
    # below instead, which is what's actually being said.
    if oi > 0 and ratio >= HIGHLIGHT_MIN_VOL_OI:
        flags.append(("unusual", f"{ratio:.0f}× VOL/OI"))
    if (a.volume_delta or 0) >= HIGHLIGHT_BURST_VOLUME:
        flags.append(("burst", f"+{a.volume_delta:,} BURST"))
    if a.side == "buy":
        flags.append(("buy", "@ASK"))
    elif a.side == "sell":
        flags.append(("sell", "@BID"))
    # Fresh positioning: volume dwarfs a small/zero open interest
    if oi == 0 and vol > 0:
        flags.append(("newoi", "NO OI"))
    elif oi > 0 and vol > oi * 2:
        flags.append(("newoi", "VOL≫OI"))
    if a.detected_at and a.detected_at >= now - timedelta(minutes=HIGHLIGHT_NEW_MINUTES):
        flags.append(("new", "NEW"))
    return flags


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

    # Gzip responses (~85-90% smaller HTML) — the web service's egress is
    # almost entirely page reloads, so this is the cheapest bandwidth win.
    try:
        from flask_compress import Compress
        Compress(app)
    except ImportError:
        pass  # optional locally; installed in prod via requirements.txt

    @app.context_processor
    def inject_glossary():
        """Make the glossary definitions + refresh cadence available to every template."""
        return {
            "COLUMN_GLOSSARY": COLUMN_GLOSSARY,
            "FLAG_GLOSSARY":   FLAG_GLOSSARY,
            "REFRESH_SECONDS": PAGE_REFRESH_SECONDS,
        }

    @app.template_filter("et")
    def fmt_et(dt, fmt="%H:%M:%S"):
        """Render a stored-UTC datetime in US/Eastern."""
        if dt is None:
            return "—"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime(fmt)

    @app.template_filter("ago")
    def fmt_ago(dt):
        """Compact age of a stored-UTC timestamp: 8m / 3h / 2d."""
        if dt is None:
            return "—"
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        mins = int((datetime.utcnow() - dt).total_seconds() // 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m"
        if mins < 60 * 24:
            return f"{mins // 60}h"
        return f"{mins // (60 * 24)}d"

    @app.template_filter("voloi")
    def fmt_voloi(a, fmt="%.1f×"):
        """
        Vol/OI for display. `vol_oi_ratio` holds raw volume when open interest
        is zero (see alert_flags) — a sentinel, not a ratio, so it's shown as
        "no OI" rather than as a multiple that doesn't exist.
        """
        if not a.open_interest:
            return "no OI"
        if not a.vol_oi_ratio:
            return "—"
        return fmt % a.vol_oi_ratio

    @app.template_filter("usd")
    def fmt_usd(v):
        """Compact dollars for rollup chips: $1.3M / $850k / $420."""
        v = v or 0
        if abs(v) >= 1_000_000:
            return f"${v / 1_000_000:,.1f}M"
        if abs(v) >= 1_000:
            return f"${v / 1_000:,.0f}k"
        return f"${v:,.0f}"

    @app.template_filter("slug")
    def fmt_slug(v):
        """Sector name → anchor-safe id, so a note can link to its table row."""
        return re.sub(r"[^a-z0-9]+", "-", str(v).lower()).strip("-")

    @app.template_filter("strike")
    def fmt_strike(v):
        """Strike price without a forced decimal — 397.5 stays 397.5, 400 stays 400."""
        if v is None:
            return "—"
        return f"{float(v):.2f}".rstrip("0").rstrip(".")

    # ── Alerts feed ───────────────────────────────────────────────────────────

    @app.route("/")
    def alerts():
        session = Session()
        try:
            since = datetime.utcnow() - timedelta(days=1)
            in_window = session.query(SweepAlert).filter(SweepAlert.detected_at >= since)
            rows = (
                in_window
                .order_by(SweepAlert.detected_at.desc())
                .limit(ALERTS_PAGE_LIMIT)
                .all()
            )
            # A single scan over ~250 tickers can write thousands of rows, so the
            # cap usually bites long before 24h does. Report the real total — the
            # page used to just say "last 24 hours" over the most recent 200.
            total = in_window.count() if len(rows) == ALERTS_PAGE_LIMIT else len(rows)
            return render_template("alerts.html", alerts=rows, total=total,
                                   limit=ALERTS_PAGE_LIMIT,
                                   min_premium=ALERTS_MIN_PREMIUM)
        finally:
            session.close()

    @app.route("/api/alerts")
    def api_alerts():
        session = Session()
        try:
            since = datetime.utcnow() - timedelta(days=1)
            rows = (
                session.query(SweepAlert)
                .filter(SweepAlert.detected_at >= since)
                .order_by(SweepAlert.detected_at.desc())
                .limit(200)
                .all()
            )
            return jsonify([{
                "id":                 a.id,
                "ticker":             a.ticker,
                "option_ticker":      a.option_ticker,
                "expiry":             a.expiry,
                "strike":             a.strike,
                "option_type":        a.option_type,
                "volume":             a.volume,
                "volume_delta":       a.volume_delta,
                "side":               a.side,
                "open_interest":      a.open_interest,
                "vol_oi_ratio":       a.vol_oi_ratio,
                "estimated_premium":  a.estimated_premium,
                "ask":                a.ask,
                "bid":                a.bid,
                "underlying_price":   a.underlying_price,
                "implied_volatility": a.implied_volatility,
                "delta":              a.delta,
                "gamma":              a.gamma,
                "theta":              a.theta,
                "vega":               a.vega,
                "detected_at":        a.detected_at.isoformat(),
                "on_watchlist":       a.on_watchlist,
            } for a in rows])
        finally:
            session.close()

    # ── Notable flow / sweeps ─────────────────────────────────────────────────

    # Columns the flow reads actually touch. Loading these as a plain row tuple
    # instead of a full SweepAlert entity is ~2.3× faster to hydrate (measured
    # at 88k rows: 1231ms → 529ms), which is what keeps the whole-universe
    # rotation query viable. Everything downstream — signals.score_alert,
    # is_deep_itm, alert_flags, _side_totals — reads attributes and never
    # writes, and signals.py is explicit that it takes "anything with the
    # attributes", so a SQLAlchemy Row duck-types cleanly here.
    LITE_COLUMNS = (
        SweepAlert.id, SweepAlert.ticker, SweepAlert.option_ticker,
        SweepAlert.expiry, SweepAlert.strike, SweepAlert.option_type,
        SweepAlert.volume, SweepAlert.volume_delta, SweepAlert.side,
        SweepAlert.open_interest, SweepAlert.vol_oi_ratio,
        SweepAlert.estimated_premium, SweepAlert.ask,
        SweepAlert.underlying_price, SweepAlert.delta, SweepAlert.detected_at,
    )

    def _notable_rows(tickers=None, exclude_tickers=None, since=None, until=None,
                      notable_only=True, lite=False):
        """
        Alerts in the window, **un-collapsed** — one row per flag.

        Callers that measure volume over a multi-hour window need every flag of
        a contract, not just the latest one, since each row's `volume_delta` is
        the slice that printed during that scan.

        since        — start of the window (defaults to HIGHLIGHT_WINDOW_HOURS back)
        until        — end of the window (None = up to now)
        notable_only — apply signals.is_notable. The per-ticker rollup turns this
                       off: measuring which way a name is leaning wants every
                       print the scanner kept, not just the headline ones, since
                       a premium cut-off biases the call/put split toward
                       whichever side happened to trade big.
        lite         — return LITE_COLUMNS rows rather than full entities. Only
                       for callers that read attributes and don't need the
                       greeks, quotes, or the watchlist relationship.
        """
        session = Session()
        try:
            if since is None:
                since = datetime.utcnow() - timedelta(hours=HIGHLIGHT_WINDOW_HOURS)
            query = (session.query(*LITE_COLUMNS) if lite
                     else session.query(SweepAlert))
            query = query.filter(SweepAlert.detected_at >= since)
            # Expired contracts are dropped in Python below regardless; doing it
            # in SQL too just avoids hydrating them. Safe as a string compare
            # because ISO-8601 dates sort lexicographically.
            query = query.filter(SweepAlert.expiry >= today_et().isoformat())
            if until is not None:
                query = query.filter(SweepAlert.detected_at < until)
            if notable_only:
                # SQL-side mirror of signals.is_notable — filters in the database
                # instead of hydrating every alert in the window into Python.
                query = query.filter(or_(
                    SweepAlert.estimated_premium >= HIGHLIGHT_MIN_PREMIUM,
                    SweepAlert.vol_oi_ratio >= HIGHLIGHT_MIN_VOL_OI,
                ))
            if tickers:
                query = query.filter(SweepAlert.ticker.in_(tickers))
            if exclude_tickers:
                query = query.filter(~SweepAlert.ticker.in_(exclude_tickers))
            # Exclude deep-ITM prints (near-pure intrinsic — usually spread legs)
            # and contracts that have already expired (dead, not actionable).
            return [a for a in query.all()
                    if (is_notable(a) or not notable_only)
                    and not is_deep_itm(a) and not is_expired(a)]
        finally:
            session.close()

    def _notable_ranked(tickers=None, exclude_tickers=None,
                        min_score=SWEEPS_MIN_SCORE, dte_max=None,
                        since=None, until=None, notable_only=True, rows=None):
        """
        Fetch notable alerts in the window, scored and ranked by significance.

        tickers         — restrict to these symbols (None = all)
        exclude_tickers — drop these symbols (e.g. ETFs shown on their own page)
        min_score       — significance floor (0 disables it)
        dte_max         — keep only contracts expiring within this many days (None = any)
        since / until   — explicit window (defaults to HIGHLIGHT_WINDOW_HOURS back)
        notable_only    — apply the notable filter (see _notable_rows)
        rows            — pre-fetched _notable_rows output to collapse instead of
                          querying again. The rotation page needs both the
                          un-collapsed rows (for its windowed sums) and the
                          collapsed items (for its level read) over the same
                          whole-universe window; without this it would run that
                          query twice.
        """
        now     = datetime.utcnow()
        notable = rows if rows is not None else _notable_rows(
            tickers, exclude_tickers, since=since, until=until,
            notable_only=notable_only)

        # Collapse repeat flags of the same contract. Day volume is
        # cumulative, so the scanner re-flags a growing position each time
        # the 60-min dedup window lapses — keep the latest flag (fullest
        # picture of the day) and surface how often it re-triggered.
        grouped = {}
        for a in notable:
            key = (a.ticker, a.strike, a.expiry, a.option_type)
            g = grouped.get(key)
            if g is None:
                grouped[key] = {"alert": a, "hits": 1, "first_seen": a.detected_at}
            else:
                g["hits"] += 1
                if a.detected_at > g["alert"].detected_at:
                    g["alert"] = a
                if a.detected_at < g["first_seen"]:
                    g["first_seen"] = a.detected_at

        items = [{
            "alert": g["alert"],
            "score": score_alert(g["alert"]),
            "flags": alert_flags(g["alert"], now),
            "hits": g["hits"],
            "first_seen": g["first_seen"],
            "is_new": bool(g["alert"].detected_at and
                           g["alert"].detected_at >= now - timedelta(minutes=HIGHLIGHT_NEW_MINUTES)),
            "dte": days_to_expiry(g["alert"].expiry),
            "tdte": trading_days_to_expiry(g["alert"].expiry),
        } for g in grouped.values()]
        # 0DTE / 1DTE / Other buckets use *trading* days so that, e.g., a
        # Friday-viewed Monday expiry is "1DTE" (next session) not "3DTE".
        for i in items:
            t = i["tdte"]
            i["bucket"] = "0dte" if t == 0 else ("1dte" if t == 1 else "other")
        # Only surface high-conviction entries — anything below the score
        # floor is noise for this page (it's still on the Alerts feed).
        if min_score:
            items = [i for i in items if i["score"] >= min_score]
        # Optional expiry cap (e.g. the short-dated index-ETF page) — by
        # trading days, so a Friday's 1DTE window includes Monday's expiry.
        if dte_max is not None:
            items = [i for i in items
                     if i["tdte"] is not None and 0 <= i["tdte"] <= dte_max]
        # Highest significance first; break ties by most recent.
        items.sort(key=lambda i: (i["score"], i["alert"].detected_at), reverse=True)
        return items

    @app.route("/sweeps")
    def sweeps():
        # Index ETFs (SPY/QQQ/IWM) have their own page — their high-vol/OI 0DTE
        # flow would otherwise crowd out single-name sweeps here.
        items = _notable_ranked(exclude_tickers=ETF_FOCUS_TICKERS)
        new_count = sum(1 for i in items if i["is_new"])

        # Per-ticker rollup — spot campaigns (many contracts, one name) rather
        # than single prints. Top tickers by total notable premium.
        by_ticker = {}
        for i in items:
            a = i["alert"]
            r = by_ticker.setdefault(a.ticker, {
                "ticker": a.ticker, "call_premium": 0, "put_premium": 0, "contracts": 0,
            })
            r["contracts"] += 1
            if a.option_type == "call":
                r["call_premium"] += a.estimated_premium or 0
            else:
                r["put_premium"] += a.estimated_premium or 0
        rollup = sorted(
            by_ticker.values(),
            key=lambda r: r["call_premium"] + r["put_premium"],
            reverse=True,
        )[:8]

        return render_template(
            "sweeps.html",
            items=items,
            rollup=rollup,
            new_count=new_count,
            window_hours=HIGHLIGHT_WINDOW_HOURS,
            new_minutes=HIGHLIGHT_NEW_MINUTES,
            min_premium=HIGHLIGHT_MIN_PREMIUM,
            min_vol_oi=HIGHLIGHT_MIN_VOL_OI,
            min_score=SWEEPS_MIN_SCORE,
        )

    @app.route("/api/sweeps")
    def api_sweeps():
        items = _notable_ranked(exclude_tickers=ETF_FOCUS_TICKERS)
        return jsonify([{
            "id":                i["alert"].id,
            "ticker":            i["alert"].ticker,
            "option_ticker":     i["alert"].option_ticker,
            "expiry":            i["alert"].expiry,
            "strike":            i["alert"].strike,
            "option_type":       i["alert"].option_type,
            "volume":            i["alert"].volume,
            "volume_delta":      i["alert"].volume_delta,
            "side":              i["alert"].side,
            "open_interest":     i["alert"].open_interest,
            "vol_oi_ratio":      i["alert"].vol_oi_ratio,
            "estimated_premium": i["alert"].estimated_premium,
            "underlying_price":  i["alert"].underlying_price,
            "implied_volatility": i["alert"].implied_volatility,
            "delta":             i["alert"].delta,
            "detected_at":       i["alert"].detected_at.isoformat(),
            "score":             i["score"],
            "flags":             [label for _, label in i["flags"]],
            "hits":              i["hits"],
            "first_seen":        i["first_seen"].isoformat(),
            "is_new":            i["is_new"],
        } for i in items])

    # ── Per-ticker rollup (/ticker/<symbol>) ──────────────────────────────────
    # A drill-down, reached by clicking a ticker anywhere in the app. The feeds
    # are ordered by print, which makes it hard to read a name as a whole; this
    # collapses one symbol's window into a single call-vs-put picture, then
    # breaks it down by expiry and by strike so the positioning is visible
    # rather than inferred from scrolling.

    TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

    def _side_totals(items):
        """
        Call-vs-put split of a set of ranked items.

        Sums run off the *collapsed* items (one row per contract, latest flag):
        day volume is cumulative, so adding up every re-flag would count the
        same position once per scan it survived.
        """
        t = {"call_premium": 0.0, "put_premium": 0.0,
             "call_volume": 0, "put_volume": 0,
             "call_contracts": 0, "put_contracts": 0}
        for i in items:
            a = i["alert"]
            side = "call" if a.option_type == "call" else "put"
            t[side + "_premium"]   += a.estimated_premium or 0
            t[side + "_volume"]    += a.volume or 0
            t[side + "_contracts"] += 1
        t["premium"]   = t["call_premium"] + t["put_premium"]
        t["volume"]    = t["call_volume"] + t["put_volume"]
        t["contracts"] = t["call_contracts"] + t["put_contracts"]
        # Share is taken on *premium*, not contract count: one $2M print is the
        # positioning, fifty $30k prints are usually noise around it.
        t["call_share"]     = (t["call_premium"] / t["premium"]) if t["premium"] else 0.0
        t["call_vol_share"] = (t["call_volume"] / t["volume"]) if t["volume"] else 0.0
        t["pc_premium"] = (t["put_premium"] / t["call_premium"]) if t["call_premium"] else None
        t["pc_volume"]  = (t["put_volume"] / t["call_volume"]) if t["call_volume"] else None
        # Which way it leans, and by how much. None = balanced.
        if t["premium"]:
            call_share, put_share = t["call_share"], 1 - t["call_share"]
            if call_share >= TICKER_LEAN_MIN_SKEW:
                t["lean"], t["lean_share"] = "call", call_share
            elif put_share >= TICKER_LEAN_MIN_SKEW:
                t["lean"], t["lean_share"] = "put", put_share
            else:
                t["lean"], t["lean_share"] = None, max(call_share, put_share)
        else:
            t["lean"], t["lean_share"] = None, 0.0
        return t

    def _top_print(items):
        """The largest single print in a group — what's driving its numbers."""
        if not items:
            return None
        return max(items, key=lambda i: i["alert"].estimated_premium or 0)["alert"]

    def _by_expiry(items):
        """Per-expiry call/put split, nearest expiry first — the term structure."""
        groups = {}
        for i in items:
            groups.setdefault(i["alert"].expiry, []).append(i)
        rows = []
        for expiry, group in groups.items():
            row = _side_totals(group)
            row.update(expiry=expiry,
                       dte=group[0]["dte"], tdte=group[0]["tdte"],
                       bucket=group[0]["bucket"], top=_top_print(group))
            rows.append(row)
        rows.sort(key=lambda r: r["expiry"])
        return rows

    def _by_strike(items, limit=None):
        """
        Per-strike call/put split, biggest premium first — where the positioning
        is clustered. Strikes pool across expiries: a name being played to $250
        is the read, whichever week it's expressed in.
        """
        groups = {}
        for i in items:
            groups.setdefault(i["alert"].strike, []).append(i)
        rows = []
        for strike, group in groups.items():
            row = _side_totals(group)
            row["strike"]   = strike
            row["expiries"] = sorted({i["alert"].expiry for i in group})
            # Moneyness against the most recent underlying price in the group —
            # every alert carries the spot at its own flag time.
            latest = max(group, key=lambda i: i["alert"].detected_at)["alert"]
            under  = latest.underlying_price or 0
            row["moneyness"] = ((strike - under) / under * 100) if under else None
            rows.append(row)
        rows.sort(key=lambda r: r["premium"], reverse=True)
        return rows[:limit] if limit else rows

    def _ticker_flow(symbol, notable_only=False):
        """Everything the per-ticker page needs, from one pass over the window."""
        items = _notable_ranked(tickers=[symbol], min_score=0,
                                notable_only=notable_only)
        latest = max(items, key=lambda i: i["alert"].detected_at)["alert"] if items else None
        return {
            "items":      items,
            "totals":     _side_totals(items),
            "by_expiry":  _by_expiry(items),
            "by_strike":  _by_strike(items, TICKER_TOP_STRIKES),
            "underlying": latest.underlying_price if latest else None,
            "as_of":      latest.detected_at if latest else None,
        }

    def _ticker_symbol(symbol):
        """Normalise a path segment to a symbol, 404-ing on anything unusable."""
        sym = (symbol or "").strip().upper()
        if not TICKER_RE.match(sym):
            abort(404)
        return sym

    @app.route("/ticker/<symbol>")
    def ticker_detail(symbol):
        sym = _ticker_symbol(symbol)
        # ?scope=notable narrows to the Sweeps population, which answers the
        # obvious follow-up: does the lean hold once the small prints drop out?
        scope = "notable" if request.args.get("scope") == "notable" else "all"
        flow  = _ticker_flow(sym, notable_only=(scope == "notable"))

        return render_template(
            "ticker.html",
            ticker=sym,
            scope=scope,
            # Cards are the evidence behind the rollup, not the point of the
            # page — a busy name can print hundreds, so show the top slice.
            items=flow["items"][:TICKER_MAX_CARDS],
            total_items=len(flow["items"]),
            totals=flow["totals"],
            by_expiry=flow["by_expiry"],
            by_strike=flow["by_strike"],
            underlying=flow["underlying"],
            as_of=flow["as_of"],
            window_hours=HIGHLIGHT_WINDOW_HOURS,
            # Labels the "Notable only" toggle with the bar it applies.
            min_premium=HIGHLIGHT_MIN_PREMIUM,
            min_vol_oi=HIGHLIGHT_MIN_VOL_OI,
        )

    @app.route("/api/ticker/<symbol>")
    def api_ticker(symbol):
        sym   = _ticker_symbol(symbol)
        scope = "notable" if request.args.get("scope") == "notable" else "all"
        flow  = _ticker_flow(sym, notable_only=(scope == "notable"))

        def totals_json(row, **extra):
            out = {k: v for k, v in row.items() if k != "top"}
            out.update(extra)
            return out

        return jsonify({
            "ticker":       sym,
            "scope":        scope,
            "window_hours": HIGHLIGHT_WINDOW_HOURS,
            "underlying":   flow["underlying"],
            "as_of":        flow["as_of"].isoformat() if flow["as_of"] else None,
            "totals":       totals_json(flow["totals"]),
            "by_expiry":    [totals_json(
                                 r,
                                 top_strike=r["top"].strike if r["top"] else None,
                                 top_type=r["top"].option_type if r["top"] else None,
                             ) for r in flow["by_expiry"]],
            "by_strike":    [totals_json(r) for r in flow["by_strike"]],
        })

    # ── Index ETFs (SPY / QQQ / IWM, short-dated) ─────────────────────────────
    # A focused view of 0DTE/1DTE flow on the big index ETFs. These names carry
    # enormous open interest, so most of their calls can't clear the Sweeps
    # score floor — here we drop the floor and show all notable short-dated flow.

    def _window_premium(a):
        """
        Notional for the slice of a contract that printed inside a window.

        `estimated_premium` is priced off *cumulative day* volume, so summing it
        over a window attributes the whole session's notional to that window —
        and, where a window spans several flags of one contract, counts that
        cumulative figure once per flag. Both the fresh-flow banner and the EOD
        chip are per-window reads, so they price `volume_delta` (what actually
        traded since the previous scan) at the same contract price the premium
        itself uses.
        """
        return (a.volume_delta or 0) * (a.ask or 0) * 100

    def _fresh_flow(items):
        """
        Per-ticker breakdown of the flow that *just* came in, by side.

        Only "new" entries count (latest detection within HIGHLIGHT_NEW_MINUTES),
        and they're summed by `volume_delta` — contracts traded since the previous
        scan — rather than cumulative day volume, so a contract that's been active
        all session only contributes what actually printed this cycle.

        A ticker is marked `is_surge` when that fresh volume clears
        ETF_SURGE_MIN_VOLUME *and* one side owns at least ETF_SURGE_MIN_SKEW of it
        — i.e. a wave of calls with little put volume to match, or vice versa.
        Returned strongest-first (surges before merely-busy tickers).
        """
        by_ticker = {}
        for i in items:
            if not i["is_new"]:
                continue
            a = i["alert"]
            fresh = a.volume_delta or 0
            if fresh <= 0:
                continue
            r = by_ticker.setdefault(a.ticker, {
                "ticker": a.ticker, "call_fresh": 0, "put_fresh": 0,
                "premium": 0, "contracts": 0, "top": None,
            })
            r["contracts"] += 1
            r["premium"]  += _window_premium(a)
            if a.option_type == "call":
                r["call_fresh"] += fresh
            else:
                r["put_fresh"] += fresh
            # Biggest single fresh print — names the strike driving the wave.
            if r["top"] is None or fresh > (r["top"].volume_delta or 0):
                r["top"] = a

        rows = []
        for r in by_ticker.values():
            total = r["call_fresh"] + r["put_fresh"]
            if not total:
                continue
            call_share = r["call_fresh"] / total
            r["fresh_total"] = total
            r["call_share"]  = call_share
            r["skew"]        = max(call_share, 1 - call_share)
            r["direction"]   = "call" if call_share >= 0.5 else "put"
            r["is_surge"]    = (total >= ETF_SURGE_MIN_VOLUME
                                and r["skew"] >= ETF_SURGE_MIN_SKEW)
            rows.append(r)
        rows.sort(key=lambda r: (r["is_surge"], r["fresh_total"]), reverse=True)
        return rows

    def _eod_window():
        """
        The most recent end-of-day positioning window.

        Returns (start_utc, end_utc, session_date, start_et). `session_date` is
        the ET trading date the window belongs to. Before today's cutoff the
        window rolls back to the previous session, so a pre-market or weekend
        look still shows the last session's late positioning rather than
        nothing — and that read is precisely about the session now starting.

        The window closes at midnight ET on the session date — the scanner stops
        around 16:20 ET, so that bound only matters for the rolled-back case,
        where it keeps today's flow out of yesterday's positioning read.
        """
        h, m = ETF_EOD_START_HM
        session_date = _recent_session_date(ETF_EOD_START_HM)
        start_et = ET.localize(datetime.combine(session_date, time(h, m)))
        end_et   = ET.localize(datetime.combine(session_date + timedelta(days=1), time(0, 0)))
        return _to_utc(start_et), _to_utc(end_et), session_date, start_et

    def _eod_positioning(show_far_otm=False):
        """
        Next-session positioning: late-day flow into contracts that outlive the
        session's close.

        Same call-vs-put read as the fresh-flow waves, but over the whole EOD
        window instead of one scan cycle — so it works off the *un-collapsed*
        rows (`_notable_rows`), summing every flag's `volume_delta`. Collapsing
        to the latest flag per contract would drop everything a strike printed
        earlier in the afternoon.

        Contracts expiring on the session date are excluded: 0DTE is a bet on
        today, not on tomorrow. A ticker is marked `is_wave` when the window's
        volume clears ETF_EOD_MIN_VOLUME and one side owns at least
        ETF_EOD_MIN_SKEW of it. Far-OTM rows are dropped unless `show_far_otm`,
        matching the rest of the page.
        """
        start_utc, end_utc, session_date, start_et = _eod_window()
        rows = _notable_rows(tickers=ETF_FOCUS_TICKERS, since=start_utc, until=end_utc)
        if not show_far_otm:
            rows = [a for a in rows if not is_far_otm(a)]

        by_ticker = {}
        for a in rows:
            try:
                exp = date.fromisoformat(a.expiry)
            except (TypeError, ValueError):
                continue
            if exp <= session_date:       # expires with this session — not positioning
                continue
            vol = a.volume_delta or 0
            if vol <= 0:
                continue
            r = by_ticker.setdefault(a.ticker, {
                "ticker": a.ticker, "call_volume": 0, "put_volume": 0,
                "premium": 0, "contracts": 0, "top": None, "expiries": set(),
            })
            r["contracts"] += 1
            r["premium"]   += _window_premium(a)
            r["expiries"].add(a.expiry)
            if a.option_type == "call":
                r["call_volume"] += vol
            else:
                r["put_volume"] += vol
            if r["top"] is None or vol > (r["top"].volume_delta or 0):
                r["top"] = a

        rows_out = []
        for r in by_ticker.values():
            total = r["call_volume"] + r["put_volume"]
            if not total:
                continue
            call_share = r["call_volume"] / total
            r["total"]      = total
            r["call_share"] = call_share
            r["skew"]       = max(call_share, 1 - call_share)
            r["direction"]  = "call" if call_share >= 0.5 else "put"
            r["is_wave"]    = (total >= ETF_EOD_MIN_VOLUME
                               and r["skew"] >= ETF_EOD_MIN_SKEW)
            r["expiries"]   = sorted(r["expiries"])
            rows_out.append(r)
        # Keep the configured ticker order (SPY, QQQ, IWM) for a stable layout.
        rows_out.sort(key=lambda r: ETF_FOCUS_TICKERS.index(r["ticker"])
                      if r["ticker"] in ETF_FOCUS_TICKERS else 99)
        return rows_out, session_date, start_et, start_utc, end_utc

    def _show_far_otm():
        """The Index ETFs page's 'Far OTM' toggle (`?far_otm=1`), off by default."""
        return request.args.get("far_otm") == "1"

    def _index_etf_flow(show_far_otm=False):
        """
        Short-dated index-ETF entries, tagged with the waves they belong to.

        Scoped to the current session (see `_session_window`) rather than a
        rolling 24 hours: DTE buckets are computed against today's date, so
        yesterday's 1DTE prints would otherwise resurface each morning wearing
        a 0DTE label while carrying yesterday afternoon's detection time.

        Far-OTM entries (signals.is_far_otm) are dropped *before* anything is
        summed unless `show_far_otm`, so the cards, rollup, waves and EOD read
        always describe the same set of contracts. The toggle is server-side
        rather than a client-side hide for exactly that reason: hiding cards
        alone would leave the volume reads counting contracts that aren't shown.
        `far_otm_count` is how many cards the filter holds back (or, with it
        off, how many of those on screen are far OTM).
        """
        start_utc, end_utc, session_date = _session_window()
        items = _notable_ranked(tickers=ETF_FOCUS_TICKERS, min_score=0,
                                dte_max=ETF_MAX_DTE,
                                since=start_utc, until=end_utc)
        for i in items:
            i["far_otm"] = is_far_otm(i["alert"])
            if i["far_otm"]:
                i["flags"] = i["flags"] + [("farotm", "FAR OTM")]
        far_otm_count = sum(1 for i in items if i["far_otm"])
        if not show_far_otm:
            items = [i for i in items if not i["far_otm"]]
        fresh  = _fresh_flow(items)
        surges = [f for f in fresh if f["is_surge"]]
        eod, eod_session, eod_start_et, eod_start_utc, eod_end_utc = \
            _eod_positioning(show_far_otm)

        # Tag the individual cards that make up a wave so the banners and the
        # entries below them visibly connect.
        surge_side = {f["ticker"]: f["direction"] for f in surges}
        eod_side   = {e["ticker"]: e["direction"] for e in eod if e["is_wave"]}
        for i in items:
            a = i["alert"]
            if i["is_new"] and surge_side.get(a.ticker) == a.option_type:
                i["surge"] = a.option_type
            # An EOD leg: on the dominant side of its ticker's EOD wave, flagged
            # inside the window, and expiring after the session it was bought in.
            if (eod_side.get(a.ticker) == a.option_type
                    and a.detected_at and eod_start_utc <= a.detected_at < eod_end_utc
                    and a.expiry > eod_session.isoformat()):
                i["eod"] = a.option_type
        return {
            "items": items, "fresh": fresh, "surges": surges,
            "eod": eod, "eod_session": eod_session, "eod_start_et": eod_start_et,
            "session_date": session_date, "far_otm_count": far_otm_count,
        }

    def _rollup(items):
        """
        Per-ticker CALL vs PUT *volume* (not premium) — call/put skew is the read
        on these index names. Each chip also carries the last-N-minutes split,
        summed from the same subset so the two lines always describe the same
        set of contracts.
        """
        by_ticker = {}
        for i in items:
            a = i["alert"]
            r = by_ticker.setdefault(a.ticker, {
                "ticker": a.ticker, "call_volume": 0, "put_volume": 0, "contracts": 0,
                "call_fresh": 0, "put_fresh": 0,
            })
            r["contracts"] += 1
            fresh = (a.volume_delta or 0) if i["is_new"] else 0
            if a.option_type == "call":
                r["call_volume"] += a.volume or 0
                r["call_fresh"]  += fresh
            else:
                r["put_volume"] += a.volume or 0
                r["put_fresh"]  += fresh
        # Keep the configured ticker order (SPY, QQQ, IWM) for a stable layout.
        return sorted(by_ticker.values(),
                      key=lambda r: ETF_FOCUS_TICKERS.index(r["ticker"])
                      if r["ticker"] in ETF_FOCUS_TICKERS else 99)

    @app.route("/index-etfs")
    def index_etfs():
        show_far_otm = _show_far_otm()
        flow  = _index_etf_flow(show_far_otm)
        items = flow["items"]
        new_count = sum(1 for i in items if i["is_new"])

        # One rollup per expiry filter, so the chips recalculate when the user
        # switches tabs instead of describing contracts that are hidden. Built
        # server-side (rather than summed in JS) to keep the arithmetic in one
        # language and independent of the separate fresh-only card filter.
        rollups = {
            "all":  _rollup(items),
            "0dte": _rollup([i for i in items if i["bucket"] == "0dte"]),
            "1dte": _rollup([i for i in items if i["bucket"] == "1dte"]),
        }

        return render_template(
            "index_etfs.html",
            items=items,
            rollups=rollups,
            surges=flow["surges"],
            eod=flow["eod"],
            eod_session=flow["eod_session"],
            # True in the morning, when the card feed has already reset for the
            # new day but the EOD read still belongs to yesterday's close.
            eod_is_prior=(flow["eod_session"] != flow["session_date"]),
            # e.g. "3:00 PM" — %-I isn't portable, so strip the pad zero by hand
            eod_start=flow["eod_start_et"].strftime("%I:%M %p").lstrip("0"),
            new_count=new_count,
            # This page covers one session, not a rolling window — label which.
            session_label=(flow["session_date"].strftime("%a %b ")
                           + str(flow["session_date"].day)),
            session_start=("%d:%02d" % ETF_SESSION_START_HM),
            new_minutes=HIGHLIGHT_NEW_MINUTES,
            tickers=ETF_FOCUS_TICKERS,
            max_dte=ETF_MAX_DTE,
            show_far_otm=show_far_otm,
            far_otm_count=flow["far_otm_count"],
            min_delta=ETF_MIN_DELTA,
        )

    # The API routes honour the same `?far_otm=1` toggle as the page.
    @app.route("/api/index-etfs")
    def api_index_etfs():
        items = _index_etf_flow(_show_far_otm())["items"]
        return jsonify([{
            "id":                i["alert"].id,
            "ticker":            i["alert"].ticker,
            "option_ticker":     i["alert"].option_ticker,
            "expiry":            i["alert"].expiry,
            "strike":            i["alert"].strike,
            "option_type":       i["alert"].option_type,
            "volume":            i["alert"].volume,
            "volume_delta":      i["alert"].volume_delta,
            "side":              i["alert"].side,
            "open_interest":     i["alert"].open_interest,
            "vol_oi_ratio":      i["alert"].vol_oi_ratio,
            "estimated_premium": i["alert"].estimated_premium,
            "underlying_price":  i["alert"].underlying_price,
            "implied_volatility": i["alert"].implied_volatility,
            "delta":             i["alert"].delta,
            "detected_at":       i["alert"].detected_at.isoformat(),
            "score":             i["score"],
            "flags":             [label for _, label in i["flags"]],
            "dte":               i["dte"],
            "tdte":              i["tdte"],
            "bucket":            i["bucket"],
            "hits":              i["hits"],
            "first_seen":        i["first_seen"].isoformat(),
            "is_new":            i["is_new"],
            # "call"/"put" when this entry is part of a one-sided fresh wave
            # on its ticker, else null.
            "surge":             i.get("surge"),
            # Same, for its ticker's end-of-day positioning wave.
            "eod":               i.get("eod"),
            # Only ever true with ?far_otm=1 — otherwise these are filtered out.
            "far_otm":           i["far_otm"],
        } for i in items])

    @app.route("/api/index-etfs/eod")
    def api_index_etf_eod():
        """Per-ticker end-of-day positioning split for the next session."""
        flow = _index_etf_flow(_show_far_otm())
        return jsonify({
            "session_date":  flow["eod_session"].isoformat(),
            "window_start":  flow["eod_start_et"].isoformat(),
            "min_volume":    ETF_EOD_MIN_VOLUME,
            "min_skew":      ETF_EOD_MIN_SKEW,
            "tickers": [{
                "ticker":      e["ticker"],
                "call_volume": e["call_volume"],
                "put_volume":  e["put_volume"],
                "total":       e["total"],
                "call_share":  round(e["call_share"], 4),
                "skew":        round(e["skew"], 4),
                "direction":   e["direction"],
                "is_wave":     e["is_wave"],
                "contracts":   e["contracts"],
                "premium":     e["premium"],
                "expiries":    e["expiries"],
                "top_contract": {
                    "option_ticker": e["top"].option_ticker,
                    "strike":        e["top"].strike,
                    "expiry":        e["top"].expiry,
                    "option_type":   e["top"].option_type,
                    "volume_delta":  e["top"].volume_delta,
                } if e["top"] is not None else None,
            } for e in flow["eod"]],
        })

    @app.route("/api/index-etfs/waves")
    def api_index_etf_waves():
        """Per-ticker fresh call-vs-put split, with the one-sided waves flagged."""
        fresh = _index_etf_flow(_show_far_otm())["fresh"]
        return jsonify([{
            "ticker":      f["ticker"],
            "call_fresh":  f["call_fresh"],
            "put_fresh":   f["put_fresh"],
            "fresh_total": f["fresh_total"],
            "call_share":  round(f["call_share"], 4),
            "skew":        round(f["skew"], 4),
            "direction":   f["direction"],
            "is_surge":    f["is_surge"],
            "contracts":   f["contracts"],
            "premium":     f["premium"],
            "window_minutes": HIGHLIGHT_NEW_MINUTES,
            "top_contract": {
                "option_ticker": f["top"].option_ticker,
                "strike":        f["top"].strike,
                "expiry":        f["top"].expiry,
                "option_type":   f["top"].option_type,
                "volume_delta":  f["top"].volume_delta,
            } if f["top"] is not None else None,
        } for f in fresh])

    # ── Rotation (sector-level flow) ──────────────────────────────────────────
    # The per-ticker rollup one level up. Same call-vs-put arithmetic, but the
    # unit is a sector rather than a symbol, which is the only level at which
    # "money is leaving semis for healthcare" is a statement the data can make.
    #
    # Two sources, deliberately:
    #   level    — collapsed per-contract items, so cumulative day volume is
    #              counted once per contract (as on the per-ticker page).
    #   momentum — un-collapsed rows priced by volume_delta, so each slice sees
    #              only what printed inside it (as on the EOD / fresh-flow
    #              reads). Collapsing here would drop everything a contract
    #              printed earlier in the window, and using estimated_premium
    #              would credit the whole session's notional to one slice.

    def _premium_split(rows_in, price=None):
        """
        Call/put premium split of raw alert rows, under a chosen pricing.

        `price` defaults to each row's own estimated_premium (whole-day
        notional). The windowed reads pass _window_premium instead.
        """
        price = price or (lambda a: a.estimated_premium or 0)
        call = put = 0.0
        for a in rows_in:
            if a.option_type == "call":
                call += price(a)
            else:
                put += price(a)
        total = call + put
        return {
            "call_premium": call, "put_premium": put, "premium": total,
            "call_share": (call / total) if total else None,
        }

    def _ticker_breakdown(items):
        """Per-name call/put split inside a sector, biggest premium first."""
        by_ticker = {}
        for i in items:
            by_ticker.setdefault(i["alert"].ticker, []).append(i)
        rows_out = []
        for ticker, group in by_ticker.items():
            row = _side_totals(group)
            row["ticker"] = ticker
            row["top"] = _top_print(group)
            rows_out.append(row)
        rows_out.sort(key=lambda r: r["premium"], reverse=True)
        return rows_out

    def _breadth(ticker_rows):
        """
        How the sector's names lean individually — the guard against reading a
        single whale as a sector-wide move. A name counts as leaning only if it
        clears the same skew bar the per-ticker page uses.
        """
        call = put = flat = 0
        for r in ticker_rows:
            if not r["premium"]:
                flat += 1
            elif r["call_share"] >= TICKER_LEAN_MIN_SKEW:
                call += 1
            elif (1 - r["call_share"]) >= TICKER_LEAN_MIN_SKEW:
                put += 1
            else:
                flat += 1
        return {"call_names": call, "put_names": put, "flat_names": flat,
                "names": len(ticker_rows)}

    def _quadrant(edge, shift):
        """
        Where a sector sits on the rotation grid, or None when either axis is
        inside its dead band — an unlabelled sector is more useful than a label
        invented out of a 1pp drift.
        """
        if edge is None or shift is None:
            return None
        if abs(edge) < ROTATION_MIN_EDGE or abs(shift) < ROTATION_MIN_SHIFT:
            return None
        if edge > 0:
            return "leading" if shift > 0 else "weakening"
        return "improving" if shift > 0 else "lagging"

    QUADRANT_LABEL = {
        "leading":   ("LEADING",   "bullish tilt, still building"),
        "improving": ("IMPROVING", "bearish tilt, but turning — early rotation"),
        "weakening": ("WEAKENING", "bullish tilt, but fading"),
        "lagging":   ("LAGGING",   "bearish tilt, getting worse"),
    }

    # ── Movement, drawn two ways ──────────────────────────────────────────────
    # One chart per axis, which is also the clearest division of labour:
    #   heat strip — Edge through time, bucket by bucket (where each sector sat
    #                against the tape, and when that changed)
    #   slopegraph — Shift as two endpoints (how far the recent slice moved from
    #                the rest of the window)
    # An earlier version plotted both axes as one Edge/Shift scatter. It drew a
    # *position* rather than a movement, and with 21 sectors most of them piled
    # into the middle. These two read at a glance and scale to every sector.

    def _rotation_slope(sectors):
        """
        Endpoints for the slopegraph: each sector's call share before and
        during the recent slice, biggest upward turn first.

        Sectors without both endpoints are left out — no prior-window flow
        means no movement to draw, which is not the same as "did not move".
        """
        rows = [{
            "sector":  s["sector"],
            "etf":     s["etf"],
            "prior":   s["prior"]["call_share"],
            "recent":  s["recent"]["call_share"],
            "shift":   s["shift"],
            "premium": s["totals"]["premium"],
            "quadrant": s["quadrant"],
            # Enough movement to be worth reading as a turn rather than drift.
            "material": abs(s["shift"]) >= ROTATION_MIN_SHIFT,
        } for s in sectors if s["shift"] is not None]
        # Sorted by the movement itself rather than by premium: this chart is
        # the rotation leaderboard, and the table below already ranks by size.
        rows.sort(key=lambda r: r["shift"], reverse=True)
        return rows

    def _rotation_trend(member_rows, since, now, sectors):
        """
        Edge per sector per time bucket — the heat strip.

        Each cell is the sector's call share in that bucket minus the tape's in
        the *same* bucket, so a market-wide swing cancels out and what is left
        is the sector moving against its peers. Priced off `_window_premium`
        for the same reason Shift is: a bucket should see only what printed
        inside it.

        Cells with no flow carry `empty` rather than a zero. A sector that did
        not trade in an hour must not render as balanced.
        """
        n = max(1, ROTATION_TREND_BUCKETS)
        span = (now - since).total_seconds() or 1.0
        width = span / n

        cells = {}                                   # (sector, bucket) -> [call, put]
        tape  = [[0.0, 0.0] for _ in range(n)]       # per-bucket baseline
        for name, alerts in member_rows.items():
            for a in alerts:
                if not a.detected_at:
                    continue
                premium = _window_premium(a)
                if premium <= 0:
                    continue
                b = int((a.detected_at - since).total_seconds() / width)
                b = max(0, min(n - 1, b))
                side = 0 if a.option_type == "call" else 1
                cells.setdefault((name, b), [0.0, 0.0])[side] += premium
                tape[b][side] += premium

        # Colour ramp. A linear map from Edge to opacity leaves the middle of
        # the range muddy — a 5pp edge came out at 20% tint, near-invisible
        # against the cell base — so intensity is gamma-curved and given a
        # floor. Effect: 5pp reads at 44% instead of 20%, 12pp at 68% instead
        # of 48%, while the extremes still anchor at the same places. The floor
        # is what makes a small-but-real edge visibly coloured; a no-flow bucket
        # is told apart by its hatching, not by being paler.
        def intensity(edge):
            t = min(1.0, abs(edge) / ROTATION_TREND_FULL_SCALE)
            return ROTATION_TREND_FLOOR + (1 - ROTATION_TREND_FLOOR) * (t ** ROTATION_TREND_GAMMA)

        def share(pair):
            total = pair[0] + pair[1]
            return (pair[0] / total) if total else None

        tape_share = [share(p) for p in tape]

        rows = []
        for s in sectors:
            name = s["sector"]
            row = []
            for b in range(n):
                pair = cells.get((name, b))
                call_share = share(pair) if pair else None
                edge = (call_share - tape_share[b])                     if (call_share is not None and tape_share[b] is not None) else None
                row.append({
                    "bucket":     b,
                    "empty":      edge is None,
                    "edge":       edge,
                    "call_share": call_share,
                    "premium":    (pair[0] + pair[1]) if pair else 0.0,
                    # Signed ratio (clamped so one extreme bucket cannot wash
                    # out the strip) and the curved opacity actually rendered.
                    "tone":       (max(-1.0, min(1.0, edge / ROTATION_TREND_FULL_SCALE))
                                   if edge is not None else 0.0),
                    "intensity":  intensity(edge) if edge is not None else 0.0,
                })
            rows.append({"sector": name, "etf": s["etf"], "cells": row,
                         "quadrant": s["quadrant"]})

        # Bucket start times on the ET clock, for the axis beneath the strip.
        labels = []
        for b in range(n):
            start_et = _to_et(since + timedelta(seconds=b * width))
            labels.append({"bucket": b, "label": start_et.strftime("%H:%M"),
                           "hour": start_et.hour})
        return {"rows": rows, "buckets": labels, "count": n,
                "full_scale": ROTATION_TREND_FULL_SCALE}

    # ── Notes: the window's read, in words ────────────────────────────────────
    # The charts show everything; a note says which part of it mattered. Each
    # kind only fires when its own threshold is cleared, so a quiet window
    # produces no notes rather than a manufactured headline — the section
    # disappears entirely instead of saying "nothing much happened".
    #
    # Deliberately *not* covered here: the risk-on/risk-off stance and the
    # ETF-vs-constituents divergence, which already have a banner and a table
    # tag of their own. A note that restates something two inches above it
    # trains you to skip the notes.

    # One side must be this much bigger than the other before a rotation is
    # called lopsided — below it the two sectors are comparable in size.
    ROTATION_PAIR_SIZE_GAP = 4.0
    # "Broad" is measured among the names that lean *at all*. Counting balanced
    # names against it would bury the real cases: Financials at 6 call / 0 put
    # / 6 balanced is unanimous among everyone with a view, and a rule keyed to
    # the total name count scored it as narrow.
    ROTATION_BROAD_MIN_LEANERS = 3
    ROTATION_BROAD_MIN_SHARE   = 0.80

    def _rotation_notes(sectors):
        """
        Ranked plain-language observations about the window.

        Returns note dicts carrying the *objects*, not formatted strings, so
        the template renders numbers through the same filters as the rest of
        the page.
        """
        notes = []
        named = set()          # sectors a note has already pointed at

        # 1. The rotation pair. Only sectors whose Shift cleared the dead band
        #    qualify, so this never pairs two pieces of drift.
        movers = [s for s in sectors
                  if s["shift"] is not None and abs(s["shift"]) >= ROTATION_MIN_SHIFT]
        gaining = [s for s in movers if s["shift"] > 0]
        losing  = [s for s in movers if s["shift"] < 0]
        if gaining and losing:
            into   = max(gaining, key=lambda s: s["shift"])
            out_of = min(losing,  key=lambda s: s["shift"])
            big, small = sorted([into["totals"]["premium"], out_of["totals"]["premium"]],
                                reverse=True)
            notes.append({
                "kind": "rotation", "tag": "ROTATION", "tone": "info",
                "into": into, "out_of": out_of,
                # Same posture on both sides is not a rotation between risk
                # appetites, and saying "out of defensive into cyclical" when
                # both are cyclical would simply be wrong.
                "posture_shift": into["posture"] != out_of["posture"],
                # A $2B sector and a $50M one moving opposite ways are not two
                # halves of one trade, and the note must not imply they are.
                "lopsided": bool(small) and (big / small) >= ROTATION_PAIR_SIZE_GAP,
            })
            named |= {into["sector"], out_of["sector"]}
        elif gaining or losing:
            # One-directional: a tilt turning with nothing clearly giving it
            # up. Worth saying, but it is not a rotation.
            side = gaining or losing
            s = max(side, key=lambda s: abs(s["shift"]))
            notes.append({"kind": "one_way", "tag": "TURNING", "tone": "info", "sector": s})
            named.add(s["sector"])

        # 2. Breadth — is a lean the whole sector, or one desk?
        def broad_side(s):
            b = s["breadth"]
            leaners  = b["call_names"] + b["put_names"]
            dominant = max(b["call_names"], b["put_names"])
            if (leaners < ROTATION_BROAD_MIN_LEANERS or dominant < ROTATION_BROAD_MIN_LEANERS
                    or dominant / leaners < ROTATION_BROAD_MIN_SHARE):
                return None
            return "call" if b["call_names"] >= b["put_names"] else "put"

        broad = [(s, side) for s in sectors
                 if not s["concentrated"] and (side := broad_side(s))]
        if broad:
            # Prefer corroborating a sector a note already named — "the move
            # you just read about is the whole sector" is worth more than the
            # same fact about a sector nothing else mentioned.
            s, side = max(broad, key=lambda pair: (pair[0]["sector"] in named,
                                                   pair[0]["totals"]["premium"]))
            notes.append({"kind": "broad", "tag": "BROAD", "tone": "info",
                          "sector": s, "side": side,
                          "corroborates": s["sector"] in named})

        # 3. Concentration — collapsed into a single note. One line per
        #    concentrated sector read as three warnings about nothing in
        #    particular and crowded everything else out of the list.
        concentrated = [s for s in sectors if s["concentrated"] and s["top_name"]]
        if concentrated:
            concentrated.sort(key=lambda s: s["totals"]["premium"], reverse=True)
            hits = [s for s in concentrated if s["sector"] in named]
            notes.append({
                "kind": "concentration", "tag": "CAUTION", "tone": "warn",
                "sectors": concentrated[:ROTATION_NOTE_MAX_SECTORS],
                "more": max(0, len(concentrated) - ROTATION_NOTE_MAX_SECTORS),
                # Qualifying a sector another note just made a claim about
                # matters more than the same caution in isolation.
                "hits": hits,
            })

        def rank(n):
            if n["kind"] == "concentration":
                return 0 if n["hits"] else 3
            return {"rotation": 1, "one_way": 1, "broad": 2}[n["kind"]]

        notes.sort(key=rank)
        return notes[:ROTATION_MAX_NOTES]

    def _rotation(scope="all"):
        """
        Everything the rotation page needs, from one pass over the window.

        scope="notable" narrows to the Sweeps population. As on the per-ticker
        page the default is every alert the scanner kept: a premium cut-off
        biases the call/put split toward whichever side happened to print big,
        which is exactly the bias a sector read must not have.
        """
        now          = datetime.utcnow()
        since        = now - timedelta(hours=ROTATION_WINDOW_HOURS)
        recent_start = now - timedelta(hours=ROTATION_RECENT_HOURS)
        notable_only = (scope == "notable")

        # Whole-universe, no score floor: the heaviest query in the app. Loaded
        # `lite` for that reason — this page needs no greeks or quotes.
        rows_all = _notable_rows(since=since, notable_only=notable_only, lite=True)
        items    = _notable_ranked(min_score=0, notable_only=notable_only,
                                   rows=rows_all)

        # Bucket both views by sector. Proxies (the sector ETF itself, plus
        # leveraged/inverse baskets) are kept apart from the single names: a
        # sector is call-heavy when its *companies* are, not when someone
        # bought the ETF.
        members, proxies, context = {}, {}, {}
        for i in items:
            t = i["alert"].ticker
            group = sector_of(t)
            if group is None:
                continue
            if group in CONTEXT_GROUPS:
                context.setdefault(group, []).append(i)
            elif is_proxy(t):
                proxies.setdefault(group, []).append(i)
            else:
                members.setdefault(group, []).append(i)

        member_rows = {}
        for a in rows_all:
            group = sector_of(a.ticker)
            if group and group not in CONTEXT_GROUPS and not is_proxy(a.ticker):
                member_rows.setdefault(group, []).append(a)

        # The tape baseline every sector's Edge is measured against: all
        # single-name sector flow pooled. Using this rather than SPY's own flow
        # keeps the baseline from being one ticker's afternoon.
        tape = _side_totals([i for g in members.values() for i in g])

        sectors = []
        for name, (etf, posture) in SECTOR_META.items():
            group_items = members.get(name, [])
            if not group_items:
                continue
            totals = _side_totals(group_items)
            tickers = _ticker_breakdown(group_items)

            # Level: how this sector's tilt compares with the whole tape's.
            # _side_totals reports a 0.0 call share for a premium-less group, so
            # both sides need premium before the difference means anything.
            edge = (totals["call_share"] - tape["call_share"]) \
                if (tape["premium"] and totals["premium"]) else None

            # Momentum: the last ROTATION_RECENT_HOURS against everything
            # before it, priced per-slice off what actually traded in each.
            group_rows = member_rows.get(name, [])
            recent = _premium_split([a for a in group_rows
                                     if a.detected_at and a.detected_at >= recent_start],
                                    _window_premium)
            prior  = _premium_split([a for a in group_rows
                                     if a.detected_at and a.detected_at < recent_start],
                                    _window_premium)
            shift = (recent["call_share"] - prior["call_share"]) \
                if (recent["call_share"] is not None and prior["call_share"] is not None) else None

            # The ETF's own tilt beside its constituents'. A wide gap is usually
            # the basket being hedged against the single names, not disagreement.
            etf_items  = proxies.get(name, [])
            etf_totals = _side_totals([i for i in etf_items
                                       if i["alert"].ticker == etf]) if etf else None
            divergence = None
            if etf_totals and etf_totals["premium"] and totals["premium"]:
                divergence = etf_totals["call_share"] - totals["call_share"]

            top_name = tickers[0] if tickers else None
            concentration = (top_name["premium"] / totals["premium"]) \
                if top_name and totals["premium"] else None

            sectors.append({
                "sector":        name,
                "etf":           etf,
                "posture":       posture,
                "totals":        totals,
                "tickers":       tickers,
                "breadth":       _breadth(tickers),
                "edge":          edge,
                "shift":         shift,
                "quadrant":      _quadrant(edge, shift),
                "recent":        recent,
                "prior":         prior,
                "etf_totals":    etf_totals if (etf_totals and etf_totals["premium"]) else None,
                "divergence":    divergence,
                "diverging":     divergence is not None and abs(divergence) >= ROTATION_DIVERGE_MIN,
                "concentration": concentration,
                "concentrated":  concentration is not None and concentration >= ROTATION_CONC_WARN,
                "top_name":      top_name,
                # Thin sectors still get a row, but below the ranked table and
                # without a quadrant — 100% call share on one print is not a signal.
                "thin":          (totals["premium"] < ROTATION_MIN_PREMIUM
                                  or len(tickers) < ROTATION_MIN_TICKERS),
            })

        for s in sectors:
            if s["thin"]:
                s["quadrant"] = None

        ranked = [s for s in sectors if not s["thin"]]
        thin   = [s for s in sectors if s["thin"]]
        # Biggest premium first: a sector's weight in the tape is the first
        # thing that decides whether its tilt is worth acting on. Columns sort
        # client-side from there.
        ranked.sort(key=lambda s: s["totals"]["premium"], reverse=True)
        thin.sort(key=lambda s: s["totals"]["premium"], reverse=True)

        return {
            "sectors":   ranked,
            "thin":      thin,
            "notes":     _rotation_notes(ranked),
            "slope":     _rotation_slope(ranked),
            "trend":     _rotation_trend(member_rows, since, now, ranked),
            "tape":      tape,
            "risk":      _risk_appetite(members),
            "context":   [{"group": g,
                           "totals": _side_totals(context.get(g, [])),
                           "tickers": _ticker_breakdown(context.get(g, []))}
                          for g in CONTEXT_GROUPS if context.get(g)],
            "scope":     scope,
            "as_of":     max((i["alert"].detected_at for i in items), default=None),
            "alerts":    len(items),
        }

    def _risk_appetite(members):
        """
        One number for what the whole tape is positioning for.

        Cyclical sectors' call share minus defensive sectors'. Taking the
        *difference* is what makes it a risk read rather than a restatement of
        the day's direction: on a strong tape both rise together and the spread
        barely moves, while upside bought in growth against hedges bought in
        safety pulls them apart.
        """
        cyc = _side_totals([i for n in CYCLICAL for i in members.get(n, [])])
        dfn = _side_totals([i for n in DEFENSIVE for i in members.get(n, [])])

        # Both sides need enough premium to be worth differencing. Defensive
        # names are a small slice of a high-options-volume universe, and without
        # this a handful of XLP prints — 2% of the tape's premium — would set
        # the headline for the whole page.
        enough = (cyc["premium"] >= ROTATION_MIN_PREMIUM
                  and dfn["premium"] >= ROTATION_MIN_PREMIUM)
        spread = (cyc["call_share"] - dfn["call_share"]) if enough else None

        if spread is None:
            stance = None
        elif spread >= ROTATION_RISK_MIN:
            stance = "on"
        elif spread <= -ROTATION_RISK_MIN:
            stance = "off"
        else:
            stance = "mixed"

        total = cyc["premium"] + dfn["premium"]
        return {
            "cyclical": cyc, "defensive": dfn, "spread": spread, "stance": stance,
            # A defensive bid shows up as a rising share of total premium even
            # when the call/put spread hasn't moved yet.
            "defensive_share": (dfn["premium"] / total) if total else None,
        }

    @app.route("/rotation")
    def rotation():
        scope = "notable" if request.args.get("scope") == "notable" else "all"
        flow  = _rotation(scope)
        return render_template(
            "rotation.html",
            quadrant_label=QUADRANT_LABEL,   # `scope` arrives via **flow
            window_hours=ROTATION_WINDOW_HOURS,
            recent_hours=ROTATION_RECENT_HOURS,
            # "2" rather than "2.0", but "1.5" kept intact.
            recent_label=("%g" % ROTATION_RECENT_HOURS),
            min_premium=HIGHLIGHT_MIN_PREMIUM,
            min_vol_oi=HIGHLIGHT_MIN_VOL_OI,
            trend_full_scale=ROTATION_TREND_FULL_SCALE,
            thin_premium=ROTATION_MIN_PREMIUM,
            thin_tickers=ROTATION_MIN_TICKERS,
            min_edge=ROTATION_MIN_EDGE,
            min_shift=ROTATION_MIN_SHIFT,
            conc_warn=ROTATION_CONC_WARN,
            diverge_min=ROTATION_DIVERGE_MIN,
            risk_min=ROTATION_RISK_MIN,
            **flow,
        )

    @app.route("/api/rotation")
    def api_rotation():
        scope = "notable" if request.args.get("scope") == "notable" else "all"
        flow  = _rotation(scope)

        def totals_json(row):
            return {k: v for k, v in (row or {}).items() if k != "top"}

        def sector_json(s):
            return {
                "sector":        s["sector"],
                "etf":           s["etf"],
                "posture":       s["posture"],
                "totals":        totals_json(s["totals"]),
                "edge":          round(s["edge"], 4) if s["edge"] is not None else None,
                "shift":         round(s["shift"], 4) if s["shift"] is not None else None,
                "quadrant":      s["quadrant"],
                "breadth":       s["breadth"],
                "concentration": round(s["concentration"], 4) if s["concentration"] is not None else None,
                "concentrated":  s["concentrated"],
                "etf_call_share": (round(s["etf_totals"]["call_share"], 4)
                                   if s["etf_totals"] else None),
                "divergence":    round(s["divergence"], 4) if s["divergence"] is not None else None,
                "diverging":     s["diverging"],
                "thin":          s["thin"],
                "tickers":       [{"ticker": t["ticker"], "premium": t["premium"],
                                   "call_share": round(t["call_share"], 4),
                                   "lean": t["lean"]} for t in s["tickers"]],
            }

        risk = flow["risk"]
        return jsonify({
            "scope":        flow["scope"],
            "window_hours": ROTATION_WINDOW_HOURS,
            "recent_hours": ROTATION_RECENT_HOURS,
            "as_of":        flow["as_of"].isoformat() if flow["as_of"] else None,
            "alerts":       flow["alerts"],
            "tape":         totals_json(flow["tape"]),
            "risk": {
                "stance":          risk["stance"],
                "spread":          round(risk["spread"], 4) if risk["spread"] is not None else None,
                "defensive_share": (round(risk["defensive_share"], 4)
                                    if risk["defensive_share"] is not None else None),
                "cyclical":        totals_json(risk["cyclical"]),
                "defensive":       totals_json(risk["defensive"]),
            },
            "sectors": [sector_json(s) for s in flow["sectors"]],
            "thin":    [sector_json(s) for s in flow["thin"]],
            # The two movement views. `slope` is Shift as two endpoints;
            # `trend` is Edge per sector per time bucket (null where a sector
            # had no flow in that bucket — deliberately not zero).
            "slope":   [{"sector": r["sector"],
                         "prior":  round(r["prior"], 4),
                         "recent": round(r["recent"], 4),
                         "shift":  round(r["shift"], 4),
                         "material": r["material"]} for r in flow["slope"]],
            "trend": {
                "buckets": [b["label"] for b in flow["trend"]["buckets"]],
                "rows": [{"sector": r["sector"],
                          "edge": [None if c["empty"] else round(c["edge"], 4)
                                   for c in r["cells"]]}
                         for r in flow["trend"]["rows"]],
            },
            "context": [{"group": c["group"], "totals": totals_json(c["totals"])}
                        for c in flow["context"]],
        })

    # ── Watchlist ─────────────────────────────────────────────────────────────

    @app.route("/watchlist")
    def watchlist():
        session = Session()
        try:
            entries = (
                session.query(WatchlistEntry)
                .filter_by(is_active=True)
                .order_by(WatchlistEntry.added_at.desc())
                .all()
            )
            # One query for every entry's snapshots instead of two per entry.
            snaps_by_entry = {}
            entry_ids = [e.id for e in entries]
            if entry_ids:
                for s in (
                    session.query(PriceSnapshot)
                    .filter(PriceSnapshot.entry_id.in_(entry_ids))
                    .order_by(PriceSnapshot.captured_at.asc())
                    .all()
                ):
                    snaps_by_entry.setdefault(s.entry_id, []).append(s)

            entries_data = []
            for entry in entries:
                snapshots = snaps_by_entry.get(entry.id, [])
                latest = snapshots[-1] if snapshots else None
                # Downsample long histories — every point gets serialized into
                # the page, and the sparkline looks the same past ~150 points.
                if len(snapshots) > MAX_SPARK_POINTS:
                    step = -(-len(snapshots) // MAX_SPARK_POINTS)  # ceil div
                    sampled = snapshots[::step]
                    if sampled[-1] is not snapshots[-1]:
                        sampled.append(snapshots[-1])  # always keep the latest
                    snapshots = sampled
                entries_data.append({
                    "entry":     entry,
                    "latest":    latest,
                    "snapshots": snapshots,
                })
            return render_template("watchlist.html", entries=entries_data)
        finally:
            session.close()

    @app.route("/watchlist/add/<int:alert_id>", methods=["POST"])
    def add_to_watchlist(alert_id):
        session = Session()
        try:
            alert = session.get(SweepAlert, alert_id)
            if alert is None:
                abort(404)
            if not alert.on_watchlist:
                entry = WatchlistEntry(
                    alert_id         = alert.id,
                    ticker           = alert.ticker,
                    option_ticker    = alert.option_ticker,
                    expiry           = alert.expiry,
                    strike           = alert.strike,
                    option_type      = alert.option_type,
                    entry_price      = alert.ask,
                    entry_underlying = alert.underlying_price,
                )
                alert.on_watchlist = True
                session.add(entry)
                session.commit()
        finally:
            session.close()
        return redirect(url_for("alerts"))

    @app.route("/watchlist/remove/<int:entry_id>", methods=["POST"])
    def remove_from_watchlist(entry_id):
        session = Session()
        try:
            entry = session.get(WatchlistEntry, entry_id)
            if entry:
                entry.is_active = False
                if entry.alert_id:
                    alert = session.get(SweepAlert, entry.alert_id)
                    if alert:
                        alert.on_watchlist = False
                session.commit()
        finally:
            session.close()
        return redirect(url_for("watchlist"))

    @app.route("/watchlist/clear", methods=["POST"])
    def clear_watchlist():
        """Deactivate every active watchlist entry in one action."""
        session = Session()
        try:
            entries = session.query(WatchlistEntry).filter_by(is_active=True).all()
            alert_ids = [e.alert_id for e in entries if e.alert_id]
            for e in entries:
                e.is_active = False
            if alert_ids:
                # Re-arm the "+ Watch" button on the source alerts.
                session.query(SweepAlert).filter(SweepAlert.id.in_(alert_ids)).update(
                    {SweepAlert.on_watchlist: False}, synchronize_session=False
                )
            session.commit()
        finally:
            session.close()
        return redirect(url_for("watchlist"))

    # ── Health check (Railway / uptime monitors) ──────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


if __name__ == "__main__":
    init_db()
    app = create_app()
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Options Flow web UI on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
