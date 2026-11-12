"""Trading-Agent adapter — the narrative + learning brain.

Maps `GET /api/brief.json` into: the Overview morning-brief banner, the per-ticker
AI read fused onto the Gameplan cards, the unified risk rating, and the gameplan
summary. Defensive throughout — older briefs may lack `conviction`, and macro
releases carry no time/importance field.
"""

from html import escape

from . import http_get_json

# AI bias → the CSS class the templates use.
_BIAS_CLASS = {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral", "cautious": "cautious"}
# Conviction meter colour keys off the direction, not the magnitude.
_BIAS_COLOR = {"bullish": "var(--blue)", "bearish": "var(--red)"}
# overall_risk_rating → the risk-tag class (on=green, mixed=grey, off=red).
_RISK_CLASS = {"low": "on", "moderate": "on", "elevated": "mixed", "high": "off"}


def collect(cfg: dict, timeout: float) -> dict:
    p = http_get_json(cfg["url"] + "/api/brief.json", cfg.get("token", ""), cfg.get("header", "Authorization"), timeout)
    if not p.get("available", True):
        raise RuntimeError(p.get("error", "brief unavailable"))

    gp = p.get("gameplan", {}) or {}
    plans = gp.get("gameplan", {}) or {}

    biases = {}
    for sym, plan in plans.items():
        conv = plan.get("conviction")
        has_conv = isinstance(conv, (int, float))
        bias = (plan.get("bias") or "neutral").lower()
        biases[sym.upper()] = {
            "bias": bias.capitalize(),
            "bias_class": _BIAS_CLASS.get(bias, "neutral"),
            "conviction": f"{conv:.2f}" if has_conv else "—",
            "conviction_pct": int(round(conv * 100)) if has_conv else 0,
            "conviction_color": _BIAS_COLOR.get(bias, "var(--muted)"),
            "bull": escape(str(plan.get("bull_case", ""))),
            "bear": escape(str(plan.get("bear_case", ""))),
            "risk_flags": [escape(str(f)) for f in (plan.get("risk_flags") or [])],
        }

    risk = (gp.get("overall_risk_rating") or "moderate").lower()
    brief = {
        "synthesized_at": f"Synthesized {escape(str(p.get('fetched_at', p.get('date', ''))))}",
        "structure_html": _structure_html(gp),
        "globals": _globals(p.get("global_markets", {}).get("indices", [])),
        "calendar": _calendar(p.get("macro", {}).get("economic_releases", [])),
        "watch": [escape(str(w)) for w in (gp.get("watch_list") or [])],
    }
    return {
        "brief": brief,
        "biases": biases,
        "risk_rating": risk.capitalize(),
        "risk_class": _RISK_CLASS.get(risk, "mixed"),
        "gameplan_summary_html": _summary_html(gp),
    }


def merge(s: dict, c: dict) -> None:
    s["brief"] = c["brief"]
    s["context"]["risk_rating"] = c["risk_rating"]
    s["context"]["risk_class"] = c["risk_class"]
    s["gameplan_summary_html"] = c["gameplan_summary_html"]
    for t in s.get("tickers", []):
        b = c["biases"].get(t["sym"])
        if b:
            t.update(b)
    # Keep the Overview gameplan snapshot card's risk badge in sync too.
    for card in s.get("overview_cards", []):
        if card.get("href") == "eod":
            for st in card.get("stats", []):
                if st.get("l") == "Next risk":
                    st["badge"] = (c["risk_rating"], "var(--surface2)", "var(--muted)")


# ---- mapping helpers ----

def _structure_html(gp: dict) -> str:
    bits = []
    if gp.get("market_structure"):
        bits.append(f"<b>Market structure:</b> {escape(str(gp['market_structure']))}")
    if gp.get("gamma_summary"):
        bits.append(f"<b>Gamma:</b> {escape(str(gp['gamma_summary']))}")
    return " ".join(bits)


def _summary_html(gp: dict) -> str:
    bits = [gp.get("market_structure", ""), gp.get("macro_context", "")]
    return " ".join(escape(str(b)) for b in bits if b)


def _cls(pct) -> str:
    if not isinstance(pct, (int, float)):
        return "flat"
    return "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")


def _globals(indices: list) -> list:
    out, last_region = [], object()
    for ix in indices:
        if "error" in ix:
            continue
        region = ix.get("region")
        chg = ix.get("change_pct")
        out.append({
            "region": region if region != last_region else None,
            "name": escape(str(ix.get("name", "—"))),
            "chg": f"{chg:+.1f}%" if isinstance(chg, (int, float)) else "—",
            "cls": _cls(chg),
        })
        last_region = region
    return out


def _calendar(releases: list) -> list:
    today = [e for e in releases if e.get("days_away") == 0]
    rows = today or releases[:4]
    out = []
    for e in rows:
        imp = (e.get("importance") or "med").lower()
        imp = imp if imp in ("high", "med", "low") else "med"
        out.append({
            "time": escape(str(e.get("time") or e.get("release_time") or "—")),
            "imp": imp.capitalize(), "imp_class": imp,
            "text": escape(str(e.get("event", "—"))),
        })
    return out[:6]
