"""
Synthesis layer — step 3 of the trading agent.
Takes combined market data, macro calendar, and options Greeks and calls
Claude to produce a structured morning gameplan.

Requires ANTHROPIC_API_KEY environment variable.
"""

import os
import json
from datetime import date

import anthropic

MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5-5")

SYSTEM_PROMPT = """\
You are a seasoned equities trader specializing in SPY, QQQ, and IWM. \
Every morning before market open you synthesize market structure, macro catalysts, \
and options positioning into a concise, actionable gameplan.

Your analysis framework:
- TREND: price relative to 50d and 200d MAs; which side of the MA stack is price on?
- MOMENTUM: recent day change, distance from 52-week high/low
- VOLUME: current vs. 10-day average — confirms or questions the move. If a \
volume_baseline_warning field is present, the two sides of that comparison are scoped \
differently and the ratio is an artifact: say so once and do not use volume as \
confirmation anywhere in the gameplan that day.
- GAMMA ENVIRONMENT: positive gamma → dealers dampen moves (mean-reversion bias); \
negative gamma → dealers amplify moves (trend/breakout bias). Gamma sets the day's \
VOLATILITY CHARACTER, never its direction: negative gamma amplifies moves in BOTH \
directions — if price sits above a rising 50d MA, negative gamma favors upside \
acceleration just as much as downside. Take direction from trend, momentum, and the \
pre-market tape; take trend-vs-chop expectations from gamma. When both are present, \
net_gex aggregates dealer positioning across ~1 month of expiries (the structural regime) \
while net_gex_front_expiry isolates same-day/0DTE positioning that dominates intraday flows — \
note when the two disagree (e.g. structurally long gamma but 0DTE short gamma → \
pinning early, unstable into the close).
- KEY LEVELS: max pain (likely pin into expiry), call walls (resistance), put walls (support), gamma flip
- EXPECTED MOVE: when expected_move is present it is the options-implied ±band for the \
front expiry — anchor bull/bear triggers and targets at or inside band_low/band_high; \
a target beyond the band needs an explicit catalyst. method "atm_straddle" is live-quote \
derived; "atm_straddle_indicative" or "atm_iv" are built from prior prints — treat those \
bands as approximate rather than precise lines.
- VOL STRUCTURE: iv_skew_25d (25Δ put IV − 25Δ call IV) — rich/steepening skew = heavy \
downside hedging demand; flat or inverted = complacency or call chasing. iv_term_structure \
spread > 0 (front ATM IV above back) = event premium priced into the near term — \
cross-reference with the macro calendar and flag the implied catalyst.
- PRE-MARKET TAPE: when index_futures and pre-market ETF quotes are present, they show \
where the market is actually opening relative to yesterday's close and today's key levels — \
anchor each ticker's bias on this, not on yesterday's close alone. A gap through a key level \
changes that level's role (a call wall gapped over becomes support to hold, not resistance); \
futures flat-to-mixed overnight argues for a range day regardless of yesterday's trend. \
Once market_session.extended_hours is present, the ETFs themselves have traded through a \
full overnight session rather than only the futures proxying them, so a pre-market ETF quote \
is a real print and not an indication — but it is a THIN one, struck without dynamic LULD \
bands, and an overnight level is weaker evidence than the same level made in regular hours. \
A large overnight move that has already retraced by the pre-market print is a liquidity \
event, not a trend; say so rather than extrapolating it into the session.
- MACRO RISK: today's and upcoming releases — size the event risk
- SENTIMENT: put/call ratios — elevated put/call = fear/hedging; low = complacency. \
unusual_strikes (volume ≫ open interest) shows where new positioning is concentrated today.
- GLOBAL/OVERNIGHT TONE: how Asian (Nikkei, KOSPI, TAIEX, Shanghai, Hang Seng) and \
European (DAX, CAC, FTSE, Euro Stoxx) markets traded ahead of the US open — a uniformly \
risk-on or risk-off overnight tape biases the US open; sharp divergence (e.g. Asia up, \
Europe down) signals an indecisive, headline-driven session. Reference this in \
market_structure and macro_context where it informs the read.

MARKET SESSION: the user message includes today's NYSE session status.
- If the market is CLOSED today (holiday/weekend), open market_structure by stating markets \
are closed and for what, frame the entire gameplan as preparation for the NEXT trading session \
rather than today, and set overall_risk_rating to "low".
- If today is an EARLY CLOSE (1:00 PM ET half-day), note the shortened session up front — \
expect thinner liquidity, lower volume, and faster moves into the 1pm close; flag this in \
risk_flags and factor it into intraday level expectations.
- If market_session.extended_hours is present, equities run 23/5: a night session \
21:00-04:00 ET, pre-market from 04:00, regular hours 09:30-16:00, post-market to 20:00. \
The gameplan is still for REGULAR HOURS — the opening and closing auctions and the official \
closing price are unchanged, and every level you name should be read as a regular-hours level. \
This matters most for the options read: listed options on SPY/QQQ/IWM do NOT trade overnight, \
so gamma flip, call/put walls and max pain describe dealer behaviour from 09:30 only. Price \
can travel through a wall overnight with no dealer hedging flow behind it, and that level is \
NOT invalidated — it re-arms at the open, which is often where the day's first move comes \
from. Call that out when the overnight tape has already crossed a key level.

If a HISTORICAL PERFORMANCE CONTEXT block is included in the user message, use it to \
calibrate your output: reduce conviction on bias types or conditions with poor track records, \
surface those patterns in risk_flags, and adjust overall_risk_rating upward when entering \
conditions where past accuracy was below 50%.

Output a JSON object with exactly this schema (no extra keys):
{
  "market_structure": "<2-3 sentences on overall trend and regime for each ticker>",
  "gameplan": {
    "SPY": {
      "bias": "<bullish | bearish | neutral>",
      "conviction": <0.0-1.0 — your probability that the bias verifies open-to-close. \
0.5 means genuine coin-flip; only go above 0.7 with strong confluence across trend, \
gamma, and the pre-market tape. Express low confidence with a low number, NOT by \
picking neutral — neutral is a real forecast that the day stays inside a tight range>,
      "bull_case": "<trigger and target level>",
      "bear_case": "<trigger and target level>",
      "key_levels": ["<level: role>", ...],
      "risk_flags": ["<flag>", ...]
    },
    "QQQ": { ... },
    "IWM": { ... }
  },
  "macro_context": "<1-2 sentences on today's events and near-term calendar risk>",
  "gamma_summary": "<1-2 sentences on dealer positioning and its intraday implication>",
  "overall_risk_rating": "<low | moderate | elevated | high>",
  "watch_list": ["<the 3-5 most important things to monitor today>"]
}

Be specific with price levels. Use the data provided — do not invent numbers.
"""


def _build_historical_context(macro_data: dict, options_data: dict) -> str:
    """Pull recent scored outcomes from the journal and format for prompt injection."""
    try:
        from .journal import get_performance_stats
        stats = get_performance_stats(n_sessions=30)
        if stats["total"] == 0:
            return ""

        total, correct = stats["total"], stats["correct"]
        lines = [
            "=== HISTORICAL PERFORMANCE CONTEXT ===",
            f"Scored sessions: {stats['sessions_scored']}  |  "
            f"Total trades: {total}  |  "
            f"Overall accuracy: {correct}/{total} ({correct/total*100:.0f}%)",
        ]

        def _fmt(label: str, bucket: dict) -> str:
            parts = [f"{k}: {v['correct']}/{v['total']} ({v['accuracy']*100:.0f}%)"
                     for k, v in sorted(bucket.items())]
            return f"{label}: " + "  |  ".join(parts)

        if stats["by_ticker"]:
            lines.append(_fmt("By ticker",   stats["by_ticker"]))
        if stats["by_bias"]:
            lines.append(_fmt("By bias",     stats["by_bias"]))
        if stats["by_gamma"]:
            lines.append(_fmt("By gamma env", stats["by_gamma"]))
        if stats["by_risk"]:
            lines.append(_fmt("By risk",     stats["by_risk"]))

        base = stats.get("baselines") or {}
        ab, pe = base.get("always_bullish", {}), base.get("persistence", {})
        if ab.get("total"):
            b_line = (f"Baselines to beat — always-bullish: "
                      f"{ab['correct']}/{ab['total']} ({ab['accuracy']*100:.0f}%)")
            if pe.get("total"):
                b_line += f"  |  persistence: {pe['correct']}/{pe['total']} ({pe['accuracy']*100:.0f}%)"
            lines.append(b_line)
        if stats.get("brier") is not None:
            lines.append(f"Conviction calibration (Brier, lower is better; "
                         f"0.25 = always saying 50/50): {stats['brier']}")

        # Surface low-accuracy conditions as calibration warnings
        warnings = []
        for ticker, s in stats["by_ticker"].items():
            if s["total"] >= 10 and s["accuracy"] < 0.45:
                warnings.append(
                    f"{ticker} bias accuracy only {s['accuracy']*100:.0f}% over {s['total']} trades "
                    f"— reduce conviction, widen scenarios"
                )
        for bias, s in stats["by_bias"].items():
            if s["total"] >= 10 and s["accuracy"] < 0.40:
                warnings.append(
                    f"'{bias}' bias has been wrong >{100-s['accuracy']*100:.0f}% of the time "
                    f"— avoid or heavily qualify"
                )
        for gamma, s in stats["by_gamma"].items():
            if s["total"] >= 10 and s["accuracy"] < 0.45:
                warnings.append(
                    f"{gamma} gamma sessions: only {s['accuracy']*100:.0f}% accurate "
                    f"— widen bull/bear cases"
                )

        econ = macro_data.get("economic_releases", [])
        if any("FOMC" in e.get("event", "") and e.get("days_away", 1) == 0 for e in econ):
            warnings.append(
                "TODAY IS FOMC — binary event risk makes directional bias unreliable; "
                "raise overall_risk_rating and widen scenarios"
            )

        if warnings:
            lines.append("CALIBRATION NOTES (apply directly to today's output):")
            for w in warnings:
                lines.append(f"  • {w}")

        lines.append("=== END HISTORICAL CONTEXT ===")
        return "\n".join(lines)

    except Exception:
        return ""


def build_user_message(market_data: dict, macro_data: dict, options_data: dict) -> str:
    payload = {
        "market_data": market_data.get("watchlist", {}),
        "index_futures": market_data.get("futures", []),
        "global_overnight": market_data.get("global_markets", {}).get("indices", []),
        "market_session": macro_data.get("market_session", {}),
        "macro_calendar": {
            "today_events": [e for e in macro_data.get("economic_releases", []) if e.get("days_away") == 0],
            "upcoming_events": [e for e in macro_data.get("economic_releases", []) if e.get("days_away", 1) > 0],
            "earnings": macro_data.get("earnings", []),
        },
        "options_positioning": options_data.get("options", {}),
    }
    # Volume is the one input whose meaning the 23/5 switchover changes without
    # changing its shape, so the caveat has to travel with the data rather than
    # live only in the system prompt.
    # Keyed off the session being briefed so a re-run for a past date stays
    # consistent, falling back to the live trading day rather than letting a
    # malformed date take the whole brief down.
    from .market_calendar import volume_baseline_note
    try:
        session_date = (macro_data.get("market_session") or {}).get("date")
        note = volume_baseline_note(date.fromisoformat(session_date) if session_date else None)
    except (TypeError, ValueError):
        note = volume_baseline_note()
    if note:
        payload["volume_baseline_warning"] = note
    # Without this the model cannot tell "quiet calendar" from "calendar feed broke",
    # and confidently reports an empty calendar on a day that may have live catalysts.
    if macro_data.get("economic_error"):
        payload["macro_calendar"]["feed_status"] = (
            "DEGRADED — the FRED economic-release feed failed this run, so the "
            "release list below is incomplete (FOMC dates are unaffected and remain "
            "authoritative). Do NOT state the calendar is empty or quiet; say macro "
            "release data was unavailable and treat unknown catalyst risk as elevated."
        )
    hist = _build_historical_context(macro_data, options_data)
    return (
        "Here is today's pre-market data. Produce the morning gameplan JSON.\n\n"
        + json.dumps(payload, indent=2)
        + (f"\n\n{hist}" if hist else "")
    )


def synthesize(market_data: dict, macro_data: dict, options_data: dict,
               api_key: str | None = None) -> dict:
    # .strip() defends against a trailing space/newline on the env var (a common
    # paste artifact in hosting dashboards), which otherwise 401s as an invalid token.
    api_key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set.")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(market_data, macro_data, options_data)}],
    )

    # Concatenate the text blocks. Claude 5 models can emit a ThinkingBlock first,
    # so message.content[0] is not necessarily the text answer (content[0].text
    # then raises). Take every text block; fall back to any block exposing .text.
    raw = "".join(getattr(b, "text", "") for b in message.content
                  if getattr(b, "type", None) == "text").strip()
    if not raw:
        raw = "".join(getattr(b, "text", "") for b in message.content).strip()

    # Strip markdown code fences if model wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


if __name__ == "__main__":
    # Quick smoke test with dummy data
    dummy_market = {"watchlist": {"SPY": {"last_close": 750, "fifty_day_avg": 724}}}
    dummy_macro  = {"economic_releases": [], "earnings": []}
    dummy_opts   = {"options": {}}
    result = synthesize(dummy_market, dummy_macro, dummy_opts)
    print(json.dumps(result, indent=2))
