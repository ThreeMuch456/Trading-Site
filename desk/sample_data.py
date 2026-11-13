"""
Phase-0 sample data for the Trading Desk.

This is the single source of mock data the templates render from. It is shaped
like the real `DeskSnapshot` view-model (see BUILD-PLAN.md §3) so that Phase 1+
can replace `snapshot()` with live adapters (`desk/sources/*`) without touching
the templates — the templates only ever see this structure.

Nothing here calls the network. Everything is a plausible single session
(Thu Nov 12, power hour) so the whole site renders coherently.
"""

from __future__ import annotations


def snapshot() -> dict:
    """Return the full composed desk snapshot (mock)."""
    return {
        "mock": True,
        "session": {
            "date_label": "Thu Nov 12",
            "phase": "Power Hour",
            "phase_class": "power",
            "clock": "14:42:08 ET · close in 1h 18m",
        },
        "meta": "Massive · 15-min delayed",

        # ---- top context bar (shared, from brief + 0DTE) ----
        "context": {
            "tickers": [
                {"sym": "SPY", "price": "681.42", "chg": "+0.34%", "chg_class": "up",
                 "note": "EM ±3.8 · in balance", "note_class": "flat"},
                {"sym": "QQQ", "price": "604.88", "chg": "+0.51%", "chg_class": "up",
                 "note": "above zero-γ", "note_class": "up"},
                {"sym": "IWM", "price": "243.17", "chg": "-0.22%", "chg_class": "down",
                 "note": "below put wall", "note_class": "down"},
            ],
            "vix": "14.8", "vix_chg": "-0.6", "vix_class": "down",
            "risk_rating": "Moderate", "risk_class": "on",
        },

        # ---- Morning Brief (Trading-Agent) ----
        "brief": {
            "synthesized_at": "Synthesized 08:52 ET · graded at close",
            "structure_html": (
                "<b>Market structure:</b> Indices open constructive but two-sided — SPY/QQQ above their "
                "50d with positive-gamma pinning expected, IWM lagging below its 50d. Overnight tape "
                "risk-on but mild; no tier-1 macro until tomorrow's <span class='lv'>CPI</span>. "
                "<b>Gamma:</b> structurally long but 0DTE short on SPY → pinning early, unstable into the close."
            ),
            "globals": [
                {"region": "Asia", "name": "Nikkei", "chg": "+0.8%", "cls": "up"},
                {"region": None, "name": "Hang Seng", "chg": "+1.2%", "cls": "up"},
                {"region": None, "name": "Shanghai", "chg": "-0.3%", "cls": "down"},
                {"region": "Europe", "name": "DAX", "chg": "+0.4%", "cls": "up"},
                {"region": None, "name": "FTSE", "chg": "+0.1%", "cls": "flat"},
            ],
            "calendar": [
                {"time": "08:30", "imp": "Med", "imp_class": "med", "text": "PPI (Oct)"},
                {"time": "10:30", "imp": "Low", "imp_class": "low", "text": "EIA Nat Gas"},
                {"time": "13:00", "imp": "Low", "imp_class": "low", "text": "30Y Bond Auction"},
                {"time": "—", "imp": "High", "imp_class": "high", "text": "CPI <b>tomorrow</b> 08:30"},
            ],
            "watch": [
                "SPY 684 call wall — pin magnet into Fri OPEX",
                "QQQ 602 flip — the long trigger if it holds",
                "IWM 244 put wall — laggard, reclaim or avoid",
                "NVDA/AMD thin-resistance breakouts (semis leading)",
            ],
        },

        # ---- Gameplan: fused AI read + mechanical levels, per ticker ----
        "gameplan_summary_html": (
            "Constructive but range-bound open. SPY pinned between the <span class='lv'>681 zero-gamma</span> "
            "flip and the <span class='lv'>684 call wall</span> — positive-gamma <b>PIN</b> regime says fade "
            "the edges unless 684 breaks on volume. QQQ is the cleaner long: sitting above its "
            "<span class='lv'>602 flip</span> with a thin pocket up to <span class='lv'>609</span>. IWM is the "
            "laggard, under its <span class='lv'>244 put wall</span> — avoid longs there until it reclaims. "
            "Macro is quiet (no tier-1 prints until tomorrow's CPI), so expect level-respecting chop into the "
            "afternoon. <b>Bias: long QQQ pullbacks, fade SPY 684, stand aside IWM.</b>"
        ),
        "tickers": [
            {
                "sym": "SPY", "regime": "PIN", "regime_class": "pin",
                "spot": "678.90", "chg": "+0.18%", "chg_class": "up",
                "bias": "Neutral", "bias_class": "neutral",
                "conviction": "0.45", "conviction_pct": 45, "conviction_color": "var(--muted)",
                "bull": "Break &amp; hold 684 on volume → 686",
                "bear": "Lose 677.5 vol trigger → 675 put wall",
                "risk_flags": ["0DTE short gamma", "Fri OPEX pin"],
                "calib": {"acc": 61, "n": 82},
                "levels": [
                    {"name": "Call Wall", "kind": "callwall", "price": "684.00", "dist": "+0.75%", "val_class": "up"},
                    {"name": "EM High", "kind": "em", "price": "682.70", "dist": "+0.56%", "val_class": ""},
                    {"name": "Spot", "kind": "spot", "price": "678.90", "dist": "—", "val_class": "", "is_spot": True},
                    {"name": "Zero Gamma", "kind": "zero", "price": "681.00", "dist": "+0.31%", "val_class": "amber"},
                    {"name": "Vol Trigger", "kind": "vol", "price": "677.50", "dist": "-0.21%", "val_class": "purple"},
                    {"name": "Put Wall", "kind": "putwall", "price": "675.00", "dist": "-0.57%", "val_class": "down"},
                ],
                "observed": 71, "observed_class": "var(--green)", "flow_age": "flow T-15 · 14:27",
            },
            {
                "sym": "QQQ", "regime": "TREND", "regime_class": "trend",
                "spot": "601.40", "chg": "+0.44%", "chg_class": "up",
                "bias": "Bullish", "bias_class": "bullish",
                "conviction": "0.68", "conviction_pct": 68, "conviction_color": "var(--blue)",
                "bull": "Hold 599.5 flip → 605, then thin air to 609",
                "bear": "Lose 599.5 → stand aside, long invalid",
                "risk_flags": ["extended vs 50d"],
                "calib": {"acc": 59, "n": 82},
                "levels": [
                    {"name": "Call Wall", "kind": "callwall", "price": "609.00", "dist": "+1.26%", "val_class": "up"},
                    {"name": "EM High", "kind": "em", "price": "605.10", "dist": "+0.62%", "val_class": ""},
                    {"name": "Spot", "kind": "spot", "price": "601.40", "dist": "—", "val_class": "", "is_spot": True},
                    {"name": "Zero Gamma", "kind": "zero", "price": "599.50", "dist": "-0.32%", "val_class": "amber"},
                    {"name": "Put Wall", "kind": "putwall", "price": "595.00", "dist": "-1.06%", "val_class": "down"},
                ],
                "observed": 64, "observed_class": "var(--green)", "flow_age": "flow T-15 · 14:27",
            },
            {
                "sym": "IWM", "regime": "TRANSITION", "regime_class": "transition",
                "spot": "244.60", "chg": "-0.11%", "chg_class": "down",
                "bias": "Bearish", "bias_class": "bearish",
                "conviction": "0.40", "conviction_pct": 40, "conviction_color": "var(--red)",
                "bull": "Reclaim 245.5 flip → 247 call wall",
                "bear": "Lose 244 put wall → 242.3 EM low",
                "risk_flags": ["below 50d", "low observed% 48"],
                "calib": {"acc": 54, "n": 81},
                "levels": [
                    {"name": "Call Wall", "kind": "callwall", "price": "247.00", "dist": "+0.98%", "val_class": "up"},
                    {"name": "Zero Gamma", "kind": "zero", "price": "245.50", "dist": "+0.37%", "val_class": "amber"},
                    {"name": "Spot", "kind": "spot", "price": "244.60", "dist": "—", "val_class": "", "is_spot": True},
                    {"name": "Put Wall", "kind": "putwall", "price": "244.00", "dist": "-0.25%", "val_class": "down"},
                    {"name": "EM Low", "kind": "em", "price": "242.30", "dist": "-0.94%", "val_class": ""},
                ],
                "observed": 48, "observed_class": "var(--amber)", "flow_age": "flow T-15 · 14:27",
            },
        ],
        "scenarios": {
            "spy": [
                {"arrow": "▲", "arrow_class": "up", "html": "Break &amp; hold <b>684</b> on volume → PIN→TREND, target EM-high <b>682.7</b>→<b>686</b>"},
                {"arrow": "◆", "arrow_class": "flat", "html": "Chop <b>681–684</b> → fade both edges, this is the base case"},
                {"arrow": "▼", "arrow_class": "down", "html": "Lose <b>677.5</b> vol trigger → downside opens to put wall <b>675</b>"},
            ],
            "qqq_iwm": [
                {"arrow": "▲", "arrow_class": "up", "html": "QQQ holds <b>599.5</b> flip → longs toward <b>605</b> then thin air to <b>609</b>"},
                {"arrow": "▼", "arrow_class": "down", "html": "QQQ loses <b>599.5</b> → stand aside, flip regime invalidates the long"},
                {"arrow": "◆", "arrow_class": "flat", "html": "IWM reclaim <b>245.5</b> flips TRANSITION bullish — until then no-trade"},
            ],
        },

        # ---- Intraday ----
        "intraday": {
            "updated": "Auto · last 14:40 ET",
            "summary_html": (
                "Morning plan is <b>holding</b>. SPY defended 681 zero-gamma twice and the 684 call wall capped "
                "the 11:50 push — PIN intact, keep fading edges. QQQ triggered the long: broke 603 at 12:20 on a "
                "<b>call wave</b> and is extending toward 605 EM-high. IWM reclaimed 244 put wall but can't hold "
                "245.5 — still no-trade. Power-hour flow is leaning calls on SPY/QQQ; watch for a 684 SPY break "
                "to flip PIN→TREND."
            ),
            "touches": [
                {"time": "13:58", "tag": "Breached", "tag_class": "breached", "html": "<b>QQQ 603</b> call wall — cleared on volume, flow confirmed in-direction"},
                {"time": "11:50", "tag": "Defended", "tag_class": "defended", "html": "<b>SPY 684</b> call wall — rejected, −0.4% fade off the level"},
                {"time": "11:12", "tag": "Defended", "tag_class": "defended", "html": "<b>SPY 681</b> zero-γ — 2nd test held, dealers buying the dip"},
                {"time": "10:35", "tag": "Contested", "tag_class": "contested", "html": "<b>IWM 244</b> put wall — wick below, reclaimed, no follow-through"},
                {"time": "10:02", "tag": "Defended", "tag_class": "defended", "html": "<b>QQQ 599.5</b> flip — held as support, confirmed the TREND read"},
            ],
            "skew": [
                {"sym": "SPY", "call_pct": 63, "label": "63% calls"},
                {"sym": "QQQ", "call_pct": 71, "label": "71% calls"},
                {"sym": "IWM", "call_pct": 44, "label": "56% puts"},
            ],
            "position": [
                {"sym": "SPY", "spot": "682.10", "regime": "PIN", "regime_class": "pin",
                 "above": "684.00 CW", "above_dist": "+0.28%", "below": "681.00 0γ", "below_dist": "-0.16%", "flip": "+0.16%"},
                {"sym": "QQQ", "spot": "604.30", "regime": "TREND", "regime_class": "trend",
                 "above": "605.10 EM", "above_dist": "+0.13%", "below": "599.50 0γ", "below_dist": "-0.79%", "flip": "+0.79%"},
                {"sym": "IWM", "spot": "244.40", "regime": "TRANSITION", "regime_class": "transition",
                 "above": "245.50 0γ", "above_dist": "+0.45%", "below": "244.00 PW", "below_dist": "-0.16%", "flip": "-0.45%"},
            ],
        },

        # ---- Flow ----
        "flow": {
            "session_label": "0DTE / 1DTE · session of Thu Nov 12",
            "waves": [
                {"sym": "QQQ", "side": "call", "call_n": "8,420", "put_n": "2,110", "skew_pct": 80, "call_pct": 80,
                 "lead": "led by $604C exp today (+3,900)", "meta": "$1.9M fresh · 14 contracts · last 15m"},
                {"sym": "SPY", "side": "call", "call_n": "11,260", "put_n": "6,540", "skew_pct": 63, "call_pct": 63,
                 "lead": "led by $682C exp today (+5,100)", "meta": "$2.7M fresh · 21 contracts · last 15m"},
            ],
            "session_skew": [
                {"sym": "SPY", "call_pct": 57, "label": "57% calls"},
                {"sym": "QQQ", "call_pct": 68, "label": "68% calls"},
                {"sym": "IWM", "call_pct": 46, "label": "54% puts"},
            ],
            "eod": [
                {"sym": "SPY", "lean": "call", "call_pct": 58, "detail": "58% calls · $4.1M into 683–685 · exp Fri"},
                {"sym": "QQQ", "lean": "call", "call_pct": 66, "detail": "66% calls · $3.3M into 605–610 · exp Fri"},
                {"sym": "IWM", "lean": "put", "call_pct": 39, "detail": "61% puts · $1.2M into 242–244 · exp Fri"},
            ],
            "all": [
                {"sym": "NVDA", "cp": "C", "cp_class": "call", "strike": "190", "expiry": "2026-11-14", "premium": "$3.2M", "vol": "12,400", "voi": "4.1", "side": "buy", "score": "88", "flags": ["whale", "unusual"], "is_new": True},
                {"sym": "AMD", "cp": "C", "cp_class": "call", "strike": "175", "expiry": "2026-11-21", "premium": "$1.6M", "vol": "8,100", "voi": "2.8", "side": "buy", "score": "74", "flags": ["big"], "is_new": False},
                {"sym": "TSLA", "cp": "P", "cp_class": "put", "strike": "390", "expiry": "2026-11-14", "premium": "$2.1M", "vol": "9,900", "voi": "3.3", "side": "buy", "score": "69", "flags": ["unusual"], "is_new": True},
                {"sym": "META", "cp": "P", "cp_class": "put", "strike": "630", "expiry": "2026-11-21", "premium": "$1.2M", "vol": "5,400", "voi": "2.1", "side": "sell", "score": "61", "flags": ["new OI"], "is_new": False},
                {"sym": "AAPL", "cp": "C", "cp_class": "call", "strike": "240", "expiry": "2026-12-19", "premium": "$900k", "vol": "6,200", "voi": "1.9", "side": "buy", "score": "55", "flags": [], "is_new": False},
            ],
            "sample_ticker": {
                "sym": "NVDA", "underlying": "187.4", "as_of": "14:30", "call_pct": 68,
                "lean": "call", "lean_class": "call", "call_vol": "142,000", "put_vol": "66,000", "premium": "$18.4M",
                "by_expiry": [
                    {"expiry": "2026-11-14", "call_vol": "60,000", "put_vol": "22,000", "call_pct": 73, "premium": "$7.1M", "top": "$190C"},
                    {"expiry": "2026-11-21", "call_vol": "52,000", "put_vol": "28,000", "call_pct": 65, "premium": "$6.0M", "top": "$195C"},
                    {"expiry": "2026-12-19", "call_vol": "30,000", "put_vol": "16,000", "call_pct": 65, "premium": "$5.3M", "top": "$200C"},
                ],
                "by_strike": [
                    {"strike": "190", "call_vol": "48,000", "put_vol": "8,000", "call_pct": 86, "premium": "$5.2M"},
                    {"strike": "195", "call_vol": "33,000", "put_vol": "7,000", "call_pct": 82, "premium": "$4.1M"},
                    {"strike": "185", "call_vol": "18,000", "put_vol": "22,000", "call_pct": 45, "premium": "$3.0M"},
                    {"strike": "200", "call_vol": "25,000", "put_vol": "6,000", "call_pct": 81, "premium": "$2.8M"},
                ],
            },
        },

        # ---- Structure recs ----
        "structure": {
            "as_of": "Profiles 17:00 ET Wed · re-priced 09:15 · gate on",
            "learning": {"horizon": 5, "n_outcomes": 310, "rows": [
                {"type": "Resistance", "win_pct": "61%", "n": 48, "enough": True},
                {"type": "Support", "win_pct": "54%", "n": 39, "enough": False},
                {"type": "Flag", "win_pct": "57%", "n": 52, "enough": True},
            ]},
            "recs": [
                {"sym": "NVDA", "price": "187.40", "dir": "▲ Up", "dir_class": "dir-up",
                 "zone": "Thin resistance", "dist": "+0.9%",
                 "comp": {"z": True, "f": True, "g": "amp", "r": "amp"},
                 "rotation": "Leading", "rotation_class": "q-leading", "quant": "72", "quant_class": "up",
                 "live": "Firing", "live_class": "live", "score": "0.88", "score_pct": 88, "score_color": "var(--green-hot)", "score_class": "up"},
                {"sym": "AMD", "price": "168.22", "dir": "▲ Up", "dir_class": "dir-up",
                 "zone": "Thin resistance", "dist": "+1.4%",
                 "comp": {"z": True, "f": True, "g": "amp", "r": False},
                 "rotation": "Leading", "rotation_class": "q-leading", "quant": "68", "quant_class": "up",
                 "live": "Firing", "live_class": "live", "score": "0.79", "score_pct": 79, "score_color": "var(--green-hot)", "score_class": "up"},
                {"sym": "META", "price": "642.10", "dir": "▼ Down", "dir_class": "dir-dn",
                 "zone": "Thin support", "dist": "-1.1%",
                 "comp": {"z": True, "f": True, "g": False, "r": "amp"},
                 "rotation": "Weakening", "rotation_class": "q-weakening", "quant": "54", "quant_class": "flat",
                 "live": "Watch", "live_class": "watch", "score": "0.71", "score_pct": 71, "score_color": "var(--amber)", "score_class": "amber"},
                {"sym": "XOM", "price": "118.95", "dir": "▲ Up", "dir_class": "dir-up",
                 "zone": "Thin resistance", "dist": "+0.7%",
                 "comp": {"z": True, "f": False, "g": "amp", "r": "amp"},
                 "rotation": "Improving", "rotation_class": "q-improving", "quant": "63", "quant_class": "up",
                 "live": "Watch", "live_class": "watch", "score": "0.66", "score_pct": 66, "score_color": "var(--amber)", "score_class": "amber"},
                {"sym": "TSLA", "price": "398.60", "dir": "▼ Down", "dir_class": "dir-dn",
                 "zone": "Thin support", "dist": "-1.8%",
                 "comp": {"z": True, "f": True, "g": False, "r": False},
                 "rotation": "Lagging", "rotation_class": "q-lagging", "quant": "41", "quant_class": "down",
                 "live": "Watch", "live_class": "watch", "score": "0.58", "score_pct": 58, "score_color": "var(--amber)", "score_class": "amber"},
            ],
        },

        # ---- EOD ----
        "eod": {
            "summary_html": (
                "<b>Plan graded: good.</b> QQQ long worked (603 break → 605, +0.4R to EM-high); SPY 684 fade "
                "paid; IWM stand-aside avoided a chop loss. Index closed with a <b>call lean</b> on SPY/QQQ into "
                "Friday expiry — dealers positioned for drift-up, so <span class='lv'>684 SPY</span> / "
                "<span class='lv'>609 QQQ</span> call walls become tomorrow's magnets if CPI cooperates. "
                "<b>Tomorrow:</b> CPI 08:30 is the pivot — a hot print flips the call lean fast. Carry NVDA/AMD "
                "longs (structure + rotation + live all aligned); drop TSLA (lagging, no confirm)."
            ),
            "grades": [
                {"sym": "SPY", "bias": "Neutral", "bias_class": "neutral", "conv": "0.45",
                 "open": "678.90", "close": "682.40", "day_pct": "+0.52%", "day_class": "up",
                 "correct": "Yes", "correct_class": "yes", "note": "Held range, closed up inside EM"},
                {"sym": "QQQ", "bias": "Bullish", "bias_class": "bullish", "conv": "0.68",
                 "open": "601.40", "close": "605.60", "day_pct": "+0.70%", "day_class": "up",
                 "correct": "Yes", "correct_class": "yes", "note": "Broke flip, ran to EM-high"},
                {"sym": "IWM", "bias": "Bearish", "bias_class": "bearish", "conv": "0.40",
                 "open": "244.60", "close": "243.90", "day_pct": "-0.29%", "day_class": "down",
                 "correct": "No", "correct_class": "no", "note": "Chopped flat — low conviction paid"},
            ],
            "performance": [
                {"label": "Today", "value": "2 / 3", "value_class": "up", "sub": "bias correct"},
                {"label": "30-session acc.", "value": "58%", "value_class": "", "sub": "142 / 245 trades"},
                {"label": "Brier", "value": "0.21", "value_class": "", "sub": "lower = better calibrated"},
                {"label": "Neg-γ sessions", "value": "63%", "value_class": "up", "sub": "best regime"},
                {"label": "Baseline", "value": "54%", "value_class": "flat", "sub": "always-bullish"},
            ],
            "watchlist": [
                {"sym": "NVDA", "sym_class": "up", "tag": "structure+rot+live", "tag_class": "live", "note": "thin resist +0.9% · leading"},
                {"sym": "AMD", "sym_class": "up", "tag": "structure+rot+live", "tag_class": "live", "note": "thin resist +1.4% · leading"},
                {"sym": "QQQ", "sym_class": "up", "tag": "momentum", "tag_class": "call", "note": "605→609 open room if holds 602"},
                {"sym": "META", "sym_class": "down", "tag": "watch only", "tag_class": "watch", "note": "thin support -1.1% · weakening"},
            ],
            "calendar": [
                {"time": "08:30", "imp": "High", "imp_class": "high", "text": "<b>CPI</b> (Oct) — core m/m exp +0.3%"},
                {"time": "08:30", "imp": "High", "imp_class": "high", "text": "Initial Jobless Claims"},
                {"time": "10:00", "imp": "Med", "imp_class": "med", "text": "U. Michigan Sentiment (prelim)"},
                {"time": "13:00", "imp": "Low", "imp_class": "low", "text": "Baker Hughes Rig Count"},
                {"time": "—", "imp": "Med", "imp_class": "med", "text": "Fri OPEX — elevated pin risk at walls"},
            ],
            "track_record": {"lines": [
                {"src": "Morning Brief", "text": "Gameplan bias 142/245 (58%) over recent sessions · Brier 0.21"},
                {"src": "Morning Brief", "text": "beats the always-bullish baseline (54%)"},
                {"src": "Structure", "text": "resistance setups win 61% at the 5-day horizon"},
                {"src": "Desk", "text": "Desk journal: 1 session logged"},
            ]},
            "risk_next": "Mixed · event-gated",
            "risk_note": (
                "CPI at 08:30 dominates — the call lean is the base case, but a hot core print voids it and the "
                "684/609 magnets flip to resistance. Size down into the number; let the first 15 minutes set the "
                "tone before acting on the carried watchlist."
            ),
        },

        # ---- Overview snapshot cards ----
        "overview_cards": [
            {"n": "01 · MORNING GAMEPLAN", "accent": "ac-blue", "href": "gameplan", "title": "Pre-Market Gameplan",
             "sub": "0DTE levels · regime · bias",
             "stats": [{"l": "SPY regime", "badge": ("PIN", "var(--blue-dim)", "var(--blue)")},
                       {"l": "QQQ regime", "badge": ("TREND", "var(--amber-dim)", "var(--amber)")},
                       {"l": "Observed", "v": "61%", "v_class": "up"}],
             "line_html": "Long QQQ pullbacks · fade SPY <span class='lv'>684</span> call wall · stand aside IWM.",
             "foot_l": "Frozen 09:30 baseline · built 09:34", "foot_r": "0DTE-Assistant"},
            {"n": "02 · INTRADAY CHECK-IN", "accent": "ac-green", "href": "intraday", "title": "Intraday Check-In",
             "sub": "level touches · live flow skew",
             "stats": [{"l": "Plan status", "v": "Holding", "v_class": "up"},
                       {"l": "Touches today", "v": "4", "v_class": ""},
                       {"l": "Flow lean", "v": "Calls", "v_class": "up"}],
             "line_html": "QQQ triggered long on <span class='lv'>603</span> break · SPY <span class='lv'>684</span> fade paid · IWM still no-trade.",
             "foot_l": "Auto · last 14:40 ET", "foot_r": "0DTE-Assistant"},
            {"n": "03 · SHORT-DATED FLOW", "accent": "ac-amber", "href": "flow", "title": "Short-Dated Index Flow",
             "sub": "0DTE / 1DTE waves · EOD lean",
             "stats": [{"l": "Active waves", "v": "2", "v_class": "", "badge_after": ("call", "call")},
                       {"l": "Fresh premium", "v": "$4.6M", "v_class": ""},
                       {"l": "EOD lean", "v": "Calls 2/3", "v_class": "up"}],
             "line_html": "QQQ call wave 80% · SPY call wave 63% · IWM put-leaning into Fri expiry.",
             "foot_l": "Session of Thu Nov 12", "foot_r": "Options-Flow-Scanner"},
            {"n": "04 · STRUCTURE RECS", "accent": "ac-purple", "href": "structure", "title": "Structure Recs — Scored",
             "sub": "thin zones · agreement gate · scores",
             "stats": [{"l": "Passing gate", "v": "5", "v_class": ""},
                       {"l": "Top score", "v": "0.88", "v_class": "up"},
                       {"l": "Firing live", "v": "2", "v_class": ""}],
             "line_html": "NVDA · AMD leading + live-confirmed · META/TSLA watch only.",
             "foot_l": "Profiles 17:00 Wed · re-priced 09:15", "foot_r": "Structure-Identifier"},
            {"n": "05 · EOD SUMMARY", "accent": "ac-red", "href": "eod", "title": "EOD — Next-Day Positioning",
             "sub": "plan grade · tomorrow's watchlist",
             "stats": [{"l": "Plan grade", "v": "Good", "v_class": "up"},
                       {"l": "Next risk", "badge": ("Mixed", "var(--surface2)", "var(--muted)")},
                       {"l": "Carried", "v": "4", "v_class": ""}],
             "line_html": "Call lean into Fri OPEX · <span class='lv'>CPI 08:30</span> is the pivot · carry NVDA/AMD.",
             "foot_l": "Generates 16:10 ET · preview", "foot_r": "synthesis"},
        ],
    }
