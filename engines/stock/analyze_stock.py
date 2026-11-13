"""
Analysis layer — turns raw fetched data into a decision-ready deep dive.

Two independent products, so the tool is useful with or without an API key:

  1. heuristic_scorecard()  — deterministic 0-100 sub-scores (valuation / growth /
     financial health / momentum) + an overall rating. Pure data, always available.

  2. claude_analysis()      — a long-term investment narrative (thesis, bull/bear,
     risks, moat, sharp-move explanations, sector news) synthesized by Claude with
     the web-search tool. Skipped gracefully when ANTHROPIC_API_KEY is absent.

run_analysis() orchestrates every fetch module + both products into one payload.
"""

import json
import os
import re

from .fetch_fundamentals import fetch_fundamentals
from .fetch_price_history import fetch_price_history
from .fetch_analysts import fetch_analysts
from .fetch_news import fetch_news
from .related_stocks import get_related

MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Heuristic scorecard (no API key required)
# ---------------------------------------------------------------------------

def _band(value, thresholds, scores):
    """Map a value to a score given ascending thresholds. None -> None."""
    if value is None:
        return None
    for t, s in zip(thresholds, scores):
        if value <= t:
            return s
    return scores[-1]


def heuristic_scorecard(f: dict, perf: dict) -> dict:
    """Return {sub_scores: {...}, overall: int, rating: str, notes: {...}}."""
    notes = {}

    # --- Valuation: cheaper = higher score ---
    val_parts = []
    pe = f.get("forward_pe") or f.get("trailing_pe")
    if pe is not None and pe > 0:
        val_parts.append(_band(pe, [10, 18, 25, 40, 60], [100, 80, 60, 40, 25]) or 15)
    peg = f.get("peg_ratio")
    if peg is not None and peg > 0:
        val_parts.append(_band(peg, [1, 1.5, 2, 3], [100, 75, 55, 35]) or 20)
    ps = f.get("price_to_sales")
    if ps is not None and ps > 0:
        val_parts.append(_band(ps, [2, 5, 10, 20], [100, 70, 45, 25]) or 15)
    valuation = round(sum(val_parts) / len(val_parts)) if val_parts else None
    if valuation is not None:
        notes["valuation"] = (
            "Attractively valued" if valuation >= 70 else
            "Reasonably valued" if valuation >= 45 else "Richly valued"
        )

    # --- Growth: faster = higher ---
    g_parts = []
    rg = f.get("revenue_growth")
    if rg is not None:
        g_parts.append(_band(rg, [0, 5, 15, 30, 50], [20, 40, 60, 80, 95]) or 95)
    eg = f.get("earnings_growth")
    if eg is not None:
        g_parts.append(_band(eg, [0, 10, 25, 50, 80], [20, 45, 65, 85, 95]) or 95)
    growth = round(sum(g_parts) / len(g_parts)) if g_parts else None
    if growth is not None:
        notes["growth"] = (
            "Strong growth" if growth >= 70 else
            "Moderate growth" if growth >= 45 else "Slow / declining"
        )

    # --- Financial health ---
    h_parts = []
    margin = f.get("profit_margin")
    if margin is not None:
        h_parts.append(_band(margin, [0, 5, 12, 20, 30], [20, 45, 65, 82, 95]) or 95)
    roe = f.get("roe")
    if roe is not None:
        h_parts.append(_band(roe, [0, 8, 15, 25, 40], [20, 45, 65, 82, 95]) or 95)
    de = f.get("debt_to_equity")
    if de is not None:
        # lower debt = healthier
        h_parts.append(_band(de, [30, 80, 150, 250], [95, 75, 50, 30]) or 20)
    if f.get("free_cashflow") is not None:
        h_parts.append(75 if f["free_cashflow"] > 0 else 35)
    health = round(sum(h_parts) / len(h_parts)) if h_parts else None
    if health is not None:
        notes["health"] = (
            "Robust balance sheet" if health >= 70 else
            "Adequate" if health >= 45 else "Stretched"
        )

    # --- Momentum / trend ---
    m_parts = []
    price = f.get("price")
    ma200 = f.get("two_hundred_day_avg")
    if price and ma200:
        above = (price - ma200) / ma200 * 100
        m_parts.append(_band(above, [-20, -5, 5, 20, 40], [25, 45, 60, 80, 90]) or 90)
    r1y = perf.get("return_1y")
    if r1y is not None:
        m_parts.append(_band(r1y, [-20, 0, 15, 40, 80], [20, 45, 60, 80, 92]) or 92)
    hi = f.get("fifty_two_week_high")
    if price and hi:
        off_high = (price - hi) / hi * 100  # negative = below high
        m_parts.append(_band(off_high, [-40, -20, -10, -3, 0], [35, 55, 70, 85, 95]) or 95)
    momentum = round(sum(m_parts) / len(m_parts)) if m_parts else None
    if momentum is not None:
        notes["momentum"] = (
            "Strong uptrend" if momentum >= 70 else
            "Neutral trend" if momentum >= 45 else "Weak / downtrend"
        )

    sub = {
        "valuation": valuation,
        "growth": growth,
        "health": health,
        "momentum": momentum,
    }
    present = [v for v in sub.values() if v is not None]
    # Long-term investing leans on quality > momentum — weight accordingly.
    weights = {"valuation": 0.3, "growth": 0.25, "health": 0.3, "momentum": 0.15}
    if present:
        wsum = sum(weights[k] for k, v in sub.items() if v is not None)
        overall = round(sum(weights[k] * v for k, v in sub.items() if v is not None) / wsum)
    else:
        overall = None

    rating = None
    if overall is not None:
        rating = (
            "Strong" if overall >= 72 else
            "Solid" if overall >= 58 else
            "Mixed" if overall >= 45 else
            "Weak"
        )

    return {"sub_scores": sub, "overall": overall, "rating": rating, "notes": notes}


# ---------------------------------------------------------------------------
# Claude narrative (requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a buy-side equity research analyst writing a LONG-TERM \
(multi-year, 1+ year holding period) investment deep dive for a single stock. \
You are NOT giving short-term trading calls.

You will receive a JSON bundle of fundamentals, multi-year performance, analyst \
coverage, recent headlines, and a list of historical "sharp move" days (large \
single-day price changes). Use the web_search tool to:
  - explain what drove the flagged sharp-move days (earnings beats/misses, product \
    announcements, guidance cuts, analyst actions, macro/sector events);
  - surface current sector/industry context relevant to the thesis (e.g. for a memory \
    chipmaker, DRAM/NAND pricing and shortages; for an automaker, EV demand, etc.).

Search a few high-quality, recent sources. Then respond with ONLY a single JSON \
object (no prose, no markdown fences) with EXACTLY these keys:

{
  "thesis": "3-4 sentence long-term investment thesis",
  "verdict": "one of: Accumulate | Buy on weakness | Hold | Trim | Avoid",
  "conviction": "High | Medium | Low",
  "time_horizon": "e.g. 3-5 years",
  "moat": "2-3 sentences on competitive position / durability",
  "bull_case": ["3-4 concise bullet strings"],
  "bear_case": ["3-4 concise bullet strings"],
  "valuation_read": "2-3 sentences on whether valuation is justified for a long-term holder",
  "key_risks": ["3-4 concise risk bullet strings"],
  "catalysts": ["2-4 forward catalysts to watch"],
  "sharp_move_explanations": [
    {"date": "YYYY-MM-DD", "pct": -12.3, "reason": "one-sentence cause"}
  ],
  "sector_context": "1 short paragraph on the current sector backdrop and what it means for this stock",
  "sector_news": [
    {"headline": "short headline", "source": "publisher", "takeaway": "one line on why it matters"}
  ]
}

Only explain the (at most 6) most significant dates from the provided "sharp_moves" \
list that you can credibly attribute — do not invent dates. Keep EVERY string tight \
(under ~30 words) so the full JSON fits comfortably. Output valid JSON only — no prose \
before or after, and make sure the JSON is complete and closed."""


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        return None
    # Strip code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Last-ditch repair for a response truncated mid-JSON: trim to the last
    # complete top-level item and close the open brackets.
    repaired = _repair_truncated_json(candidate)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None
    return None


def _repair_truncated_json(s: str) -> str | None:
    """Best-effort: cut to the last complete key/value, then close brackets."""
    cut = max(s.rfind('"'), s.rfind("}"), s.rfind("]"))
    if cut == -1:
        return None
    s = s[:cut + 1]
    # Balance braces/brackets by counting (ignoring those inside strings is
    # imperfect but adequate for our well-structured output).
    depth_obj = s.count("{") - s.count("}")
    depth_arr = s.count("[") - s.count("]")
    if depth_obj < 0 or depth_arr < 0:
        return None
    return s + "]" * depth_arr + "}" * depth_obj


def claude_analysis(combined: dict, api_key: str | None = None) -> dict | None:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    # Trim the payload sent to the model (drop the heavy raw price series; cap
    # the sharp-move list to the most significant so explanations map to markers)
    perf = combined["performance"]
    payload = {
        "fundamentals": combined["fundamentals"],
        "performance": {k: v for k, v in perf.items() if k not in ("series", "sharp_moves")},
        "sharp_moves": perf.get("sharp_moves", [])[:8],
        "analysts": combined["analysts"],
        "related": combined["related"],
        "recent_news": [
            {"title": n["title"], "publisher": n["publisher"], "date": n["published_date"]}
            for n in combined["news"][:10]
        ],
    }
    user_msg = (
        "Produce the long-term deep-dive JSON for this stock.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )

    client = anthropic.Anthropic(api_key=api_key)

    def _call(use_search: bool):
        kwargs = dict(
            model=MODEL,
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        if use_search:
            kwargs["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }]
        return client.messages.create(**kwargs)

    msg = None
    try:
        msg = _call(use_search=True)
    except Exception:
        try:
            msg = _call(use_search=False)  # account may lack web search
        except Exception:
            return None

    # Concatenate all text blocks (web-search responses interleave tool blocks)
    text = "".join(
        block.text for block in msg.content if getattr(block, "type", None) == "text"
    )
    result = _extract_json(text)
    if result is not None:
        result["_generated"] = True
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_analysis(symbol: str, include_ai: bool = True) -> dict:
    """Fetch everything for one ticker and assemble the full payload."""
    sym = symbol.upper().strip()

    fundamentals = fetch_fundamentals(sym)
    # Guard: a totally unknown ticker yields no price and no name match
    performance = fetch_price_history(sym)
    if performance.get("error") == "no_price_history" and fundamentals.get("price") is None:
        raise ValueError(f"No data found for ticker '{sym}'. Check the symbol and try again.")

    analysts = fetch_analysts(sym, fundamentals.get("price"))
    news = fetch_news(sym)
    related = get_related(sym, fundamentals.get("sector"), fundamentals.get("industry"))

    combined = {
        "symbol": sym,
        "fundamentals": fundamentals,
        "performance": performance,
        "analysts": analysts,
        "news": news,
        "related": related,
        "scorecard": heuristic_scorecard(fundamentals, performance),
        "ai": None,
    }

    if include_ai:
        combined["ai"] = claude_analysis(combined)

    return combined


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    out = run_analysis(sym, include_ai=False)
    out["performance"]["series"] = f"<{len(out['performance']['series'])} pts>"
    print(json.dumps(out, indent=2, default=str))
