"""Central configuration. Everything tunable lives here or in env vars."""
import os
from datetime import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()]

# --- Massive API (option data, T-15 delayed) ---
MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
# How far behind real time the Massive feed runs. Used by the two-clock logic.
OPTION_FEED_DELAY_S = int(os.getenv("OPTION_FEED_DELAY_S", str(15 * 60)))
# Requests allowed per rolling minute, account-wide. The Starter plan answers
# ~5/min then 429s; raise via env if the subscription tier allows more.
MASSIVE_RPM = int(os.getenv("MASSIVE_RPM", "5"))
# Only fetch chain strikes within this window of spot — keeps each chain
# snapshot to one page so three tickers fit inside the request budget.
CHAIN_STRIKE_WINDOW_PCT = float(os.getenv("CHAIN_STRIKE_WINDOW_PCT", "0.08"))
# How often to refresh the next-expiry ATM IV (for the front/next IV spread).
NEXT_EXPIRY_REFRESH_S = int(os.getenv("NEXT_EXPIRY_REFRESH_S", "300"))

# --- Polling cadence ---
OPTION_POLL_SECONDS = int(os.getenv("OPTION_POLL_SECONDS", "60"))
UNDERLYING_POLL_SECONDS = int(os.getenv("UNDERLYING_POLL_SECONDS", "15"))

# Any input older than this marks its panel degraded.
DEGRADED_AFTER_S = 20 * 60

# --- Session clock (ET) ---
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

# Pollers only run inside this window (ET, weekdays). Starting before the open
# warms the chain/quote history so the 09:30 baseline freeze has data; running
# past the close lets the final T-15 flow land and touch classification finish.
# Outside the window the app still serves /api/state — it just stops burning
# request budget against Massive and Yahoo 24/7.
POLL_WINDOW_START = time(9, 0)
POLL_WINDOW_END = time(16, 20)
# Set ALWAYS_POLL=1 to ignore the window (useful for testing off-hours).
ALWAYS_POLL = os.getenv("ALWAYS_POLL", "").strip() in ("1", "true", "yes")

# --- Model constants ---
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.04"))

# Trade classification: prints within this fraction of the half-spread around
# the midpoint are held as unclassified rather than guessed.
MID_ZONE_FRAC = 0.20

# Trade-print fetching is per-contract; cap how many contracts we pull prints
# for each poll (most active first). Only used when the plan is entitled to
# trade prints. Unfetched volume lands in the unclassified bucket and is
# reported as reduced flow coverage.
MAX_TRADE_CONTRACTS_PER_POLL = int(os.getenv("MAX_TRADE_CONTRACTS", "6"))

# --- Request budget accounting -------------------------------------------
# The limiter queues rather than fails, so oversubscription shows up as polls
# silently arriving late, not as errors. Observed 2026-08-04 with trade-print
# entitlement live: throttled_pct 50.9%, avg wait 64.9s against a 60s poll —
# the chain fetch, which is the app's core input, was competing in one FIFO
# queue against print fetches and losing.
#
# CRITICAL load is the part that must never be starved: one chain snapshot per
# ticker per poll, plus the next-expiry refresh (2 calls per ticker per
# window). Trade prints are best-effort and get only what is left over.
_POLLS_PER_MIN = 60.0 / max(OPTION_POLL_SECONDS, 1)
CRITICAL_RPM = len(TICKERS) * _POLLS_PER_MIN \
    + len(TICKERS) * 2 * (60.0 / max(NEXT_EXPIRY_REFRESH_S, 1))
PRINT_BUDGET_RPM = max(0.0, MASSIVE_RPM - CRITICAL_RPM)

# How many contracts per ticker per poll the leftover budget actually affords.
# Falls to 0 when the plan cannot support prints at all, in which case flow
# classification uses the snapshot-delta path instead of quietly starving the
# chain fetch. At MASSIVE_RPM=5 with 3 tickers this IS 0: prints cost ~18
# req/min on top of ~4.2 critical, so they need roughly 25+ RPM to be viable.
MAX_TRADE_CONTRACTS_EFFECTIVE = min(
    MAX_TRADE_CONTRACTS_PER_POLL,
    int(PRINT_BUDGET_RPM / max(len(TICKERS), 1) / max(_POLLS_PER_MIN, 0.001)),
)

# Slots held back from best-effort (non-critical) calls so a burst of print
# fetches can never delay a chain snapshot.
CRITICAL_RESERVE = min(MASSIVE_RPM - 1, max(1, int(round(CRITICAL_RPM))))
# A non-critical call that cannot get a slot this quickly is dropped for this
# poll rather than queued — late prints are worth less than a timely chain.
NONCRITICAL_MAX_WAIT_S = float(os.getenv("NONCRITICAL_MAX_WAIT_S", "10"))

# --- Flip sweep ---
FLIP_SWEEP_PCT = 0.05          # primary sweep: spot * (1 +/- 5%)
FLIP_SWEEP_WIDE_PCT = 0.10     # widened sweep before reporting "none"
FLIP_SWEEP_STEP = 0.001        # 0.1% steps

# --- Regime state machine ---
TRANSITION_FLIP_DIST_PCT = 0.0015   # spot within 0.15% of a flip
TRANSITION_FLIP_MOVE_PCT = 0.003    # flip moved > 0.3% in the last 15 min
FLIP_MOVE_WINDOW_S = 15 * 60

# --- Touch detection ---
# Band = max(min_pct * level, atr_mult * ATR14(1m)). Backtested over 21 real
# sessions (tools/backtest_touches.py, Jul 2026): at these price levels the
# original 0.15 ATR multiplier NEVER bound — the band was always the 0.05%
# floor, so it couldn't widen on violent tape. At 0.75 the ATR term takes
# over only when 1-min ATR exceeds ~0.07% of price (fast tape), cutting
# false breach confirms from 27.6% to ~23% at the same 2-min confirm
# latency. 0.75 and 1.0 tested within noise of each other; 0.75 kept as the
# smaller deviation. Calm-tape behavior is unchanged (floor still governs).
TOUCH_BAND_MIN_PCT = 0.0005      # 0.05%
TOUCH_BAND_ATR_MULT = 0.75       # 0.75 * ATR14(1m) — see note above
TOUCH_REENTRY_WINDOW_S = 15 * 60
TOUCH_BREACH_BARS = 3            # confirmed best trade-off vs 2 (more false
                                 # breaks) and 4 (+1 min latency, ~no gain)

# --- Flow-classification thresholds for touch labels ---
WALL_GREW_RATIO = 1.10       # magnitude after > 1.10x before  -> "grew"
WALL_SHRANK_RATIO = 0.90     # after < 0.90x before            -> "shrank"
WALL_COLLAPSED_RATIO = 0.50  # after < 0.50x before            -> "collapsed"
WALL_INTACT_RATIO = 0.80     # after >= 0.80x before           -> "intact"
HEAVY_ASK_AGGRESSION = 0.60  # share of classified volume at the ask

# --- Level decay ---
DECAY_DROP_RATIO = 0.40      # magnitude fell > 40%...
DECAY_WINDOW_S = 30 * 60     # ...within 30 minutes, with no touch -> DECAYED

# --- Expected move ---
EM_STRADDLE_MULT = 0.85

# --- Access control ---
# Optional shared-secret gate for a hosted deployment. Unset (default) leaves
# the app fully open, which is the right thing for localhost. Set it on
# Railway and the public URL stops serving the map, the replay archive and the
# session's accumulated flow to anyone who finds it. /healthz stays open so
# the platform health check keeps working.
APP_TOKEN = os.getenv("APP_TOKEN", "").strip()

# --- Entitlement probing ---
# HTTP 403 from Massive means either "your plan lacks this" or a transient
# quota refusal, and the two are indistinguishable from the response. Treating
# the first 403 as a verdict disabled the underlying bar fallback for a whole
# session. Require this many consecutive 403s before believing it.
AGGS_403_VERDICT_STREAK = int(os.getenv("AGGS_403_VERDICT_STREAK", "5"))

# --- Storage ---
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "0dte.sqlite3"))

# Retention (days), pruned daily. Sized so the Railway volume stays flat while
# keeping the learning loop intact:
#   prints    — bulkiest table (per-contract flow rows); only needed for
#               post-close classifier evaluation and deep replay of recent days
#   snapshots — power /replay
#   bars      — 1-minute underlying context for replay
#   events    — session narrative; small
#   touches   — the classifier's scored outcomes; THE learning record, keep long
#   daily_option_volume — tiny, kept forever (20-day baseline input)
# Sized against a real print-entitled session: 208 MB accumulated in roughly
# one day (2026-08-04), i.e. ~20x the estimate made when the fallback
# snapshot-delta path was assumed. At 10 days that is ~2 GB and fills the
# Railway volume. Prints are the bulkiest table by far and are only needed
# for post-close classifier evaluation and deep replay of recent days — the
# touches table, not this one, is the long-term learning record.
RETAIN_PRINT_DAYS = int(os.getenv("RETAIN_PRINT_DAYS", "3"))
# Log a warning past this size so the volume filling up is noticed before it
# becomes an outage rather than after.
DB_SIZE_WARN_MB = float(os.getenv("DB_SIZE_WARN_MB", "1500"))
RETAIN_SNAPSHOT_DAYS = int(os.getenv("RETAIN_SNAPSHOT_DAYS", "30"))
RETAIN_BAR_DAYS = int(os.getenv("RETAIN_BAR_DAYS", "30"))
RETAIN_EVENT_DAYS = int(os.getenv("RETAIN_EVENT_DAYS", "90"))
RETAIN_TOUCH_DAYS = int(os.getenv("RETAIN_TOUCH_DAYS", "365"))
