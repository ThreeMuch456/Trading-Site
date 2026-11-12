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

    # Performance / EOD grading is a second (best-effort) endpoint — the learning
    # loop. A miss here never fails the brief; the EOD section just stays sample.
    perf = None
    try:
        perf = http_get_json(cfg["url"] + "/api/performance.json", cfg.get("token", ""),
                             cfg.get("header", "Authorization"), timeout)
    except Exception:
        perf = None

    return {
        "brief": brief,
        "biases": biases,
        "risk_rating": risk.capitalize(),
        "risk_class": _RISK_CLASS.get(risk, "mixed"),
        "gameplan_summary_html": _summary_html(gp),
        "eod": _eod_from_perf(perf, biases) if perf else None,
        "track_record": _track_record(perf) if perf else None,
        "calibration": _calibration(perf) if perf else None,
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
    e = c.get("eod")
    if e:
        if e.get("grades"):
            s["eod"]["grades"] = e["grades"]
        if e.get("performance"):
            s["eod"]["performance"] = e["performance"]
        if e.get("summary_html"):
            s["eod"]["summary_html"] = e["summary_html"]
    if c.get("track_record"):
        # Seed the synthesized Track Record; other sources append their own items.
        s["eod"]["track_record"] = c["track_record"]
    cal = c.get("calibration")
    if cal:
        for t in s.get("tickers", []):
            d = cal.get(t["sym"])
            if d:
                t["calib"] = d
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


def _num(x) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else (escape(str(x)) if x not in (None, "") else "—")


def _eod_from_perf(perf: dict, biases: dict) -> dict:
    """Join the day's graded outcomes with this morning's biases → the EOD
    'Today, graded' table + performance tiles + a deterministic one-line read."""
    outcomes = perf.get("outcomes") or []
    grades, today_correct, today_total = [], 0, 0
    for o in outcomes:
        sym = (o.get("ticker") or "").upper()
        b = biases.get(sym, {})
        bc = o.get("bias_correct")
        dp = o.get("day_pct")
        grades.append({
            "sym": sym,
            "bias": b.get("bias", "—"), "bias_class": b.get("bias_class", "neutral"),
            "conv": b.get("conviction", "—"),
            "open": _num(o.get("open")), "close": _num(o.get("close")),
            "day_pct": f"{dp:+.2f}%" if isinstance(dp, (int, float)) else "—",
            "day_class": "up" if (isinstance(dp, (int, float)) and dp >= 0) else "down",
            "correct": "Yes" if bc == 1 else ("No" if bc == 0 else "—"),
            "correct_class": "yes" if bc == 1 else ("no" if bc == 0 else ""),
            "note": escape(str(o.get("notes") or "")),
        })
        if bc in (0, 1):
            today_total += 1
            today_correct += int(bc)
    return {
        "grades": grades,
        "performance": _perf_tiles(perf.get("stats") or {}, today_correct, today_total),
        "summary_html": _eod_summary(today_correct, today_total),
    }


def _perf_tiles(stats: dict, today_c: int, today_t: int) -> list:
    total, correct = stats.get("total") or 0, stats.get("correct") or 0
    brier = stats.get("brier")
    by_g = stats.get("by_gamma") or {}
    best = max(by_g.items(), key=lambda kv: (kv[1] or {}).get("accuracy", 0), default=None)
    base = (stats.get("baselines") or {}).get("always_bullish") or {}
    tiles = [
        {"label": "Today", "value": f"{today_c} / {today_t}" if today_t else "—",
         "value_class": "up" if (today_t and today_c == today_t) else "", "sub": "bias correct"},
        {"label": "30-session acc.", "value": f"{correct / total * 100:.0f}%" if total else "—",
         "value_class": "", "sub": f"{correct} / {total} trades"},
        {"label": "Brier", "value": f"{brier:.2f}" if isinstance(brier, (int, float)) else "—",
         "value_class": "", "sub": "lower = better calibrated"},
    ]
    if best:
        tiles.append({"label": escape(str(best[0]))[:14], "value": f"{(best[1] or {}).get('accuracy', 0) * 100:.0f}%",
                      "value_class": "up", "sub": "best regime"})
    if base.get("total"):
        tiles.append({"label": "Baseline", "value": f"{base.get('accuracy', 0) * 100:.0f}%",
                      "value_class": "flat", "sub": "always-bullish"})
    return tiles


def _eod_summary(c: int, t: int) -> str:
    if not t:
        return "Session not yet graded — outcomes land after the close."
    verdict = "good" if c >= t - c else "mixed"
    return (f"<b>Plan graded: {verdict}.</b> {c}/{t} morning biases verified open→close. "
            "The scored outcome feeds the learning loop that tempers tomorrow's conviction.")


def _calibration(perf: dict) -> dict:
    """Per-ticker bias accuracy, for the inline Gameplan 'track record' chip."""
    by_t = (perf.get("stats") or {}).get("by_ticker") or {}
    out = {}
    for sym, d in by_t.items():
        acc, n = (d or {}).get("accuracy"), (d or {}).get("total")
        if isinstance(acc, (int, float)) and n:
            out[sym.upper()] = {"acc": round(acc * 100), "n": n}
    return out


def _track_record(perf: dict) -> dict:
    """A synthesized, source-tagged learning summary. Seeded here from the Morning
    Brief's calibration; the desk store and other sources append their own items."""
    stats = perf.get("stats") or {}
    total, correct = stats.get("total") or 0, stats.get("correct") or 0
    brier = stats.get("brier")
    items = []
    if total:
        line = f"Gameplan bias {correct}/{total} ({correct / total * 100:.0f}%) over recent sessions"
        if isinstance(brier, (int, float)):
            line += f" · Brier {brier:.2f}"
        items.append({"src": "Morning Brief", "text": line})
        base = (stats.get("baselines") or {}).get("always_bullish") or {}
        if base.get("total"):
            verdict = "beats" if correct / total > base.get("accuracy", 0) else "lags"
            items.append({"src": "Morning Brief",
                          "text": f"{verdict} the always-bullish baseline ({base.get('accuracy', 0) * 100:.0f}%)"})
        by_g = stats.get("by_gamma") or {}
        best = max(by_g.items(), key=lambda kv: (kv[1] or {}).get("accuracy", 0), default=None)
        if best:
            items.append({"src": "Morning Brief",
                          "text": f"strongest in {str(best[0]).lower()} sessions ({(best[1] or {}).get('accuracy', 0) * 100:.0f}%)"})
    return {"lines": items}


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
