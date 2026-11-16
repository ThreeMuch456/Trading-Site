"""Display model for the learning scorecard (the /learning page and its digests).

`engines.learning.scorecard` decides what is true; this module only decides how
to say it. Like every desk adapter it hands the templates finished strings and
geometry, so the templates stay free of logic.

Three consumers:
  page()            the full /learning page
  digest()          one line per loop — the EOD Track Record
  structure_tiles() the learned strip on the Structure page

All three are fail-soft: with no readable scorecard they return empty shapes.
"""
from __future__ import annotations

import math

# One glyph + one word per verdict, everywhere. Status is never colour alone.
VERDICT = {
    "paying":       {"label": "Paying",       "glyph": "▲", "cls": "v-paying"},
    "inverted":     {"label": "Inverted",     "glyph": "▼", "cls": "v-inverted"},
    "noise":        {"label": "Noise",        "glyph": "○", "cls": "v-noise"},
    "accumulating": {"label": "Accumulating", "glyph": "…", "cls": "v-accumulating"},
}
_ORDER = ("paying", "inverted", "noise", "accumulating")

_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# What one observation is called, per loop (singular, plural).
_UNIT_NAME = {"brief": ("call", "calls"), "structure": ("labeled setup", "labeled setups"),
              "flow": ("flow lean", "flow leans"), "zerodte": ("level touch", "level touches")}


def _scorecard() -> dict | None:
    try:
        from engines import produce
        return produce("local://learning/api/scorecard.json")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _is_pp(unit: str) -> bool:
    return "percentage" in (unit or "")


def _signed(v, unit: str) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}" if _is_pp(unit) else f"{v:+.2f}"


def _suffix(unit: str) -> str:
    return " pp" if _is_pp(unit) else "%"


def _day(iso: str | None) -> str:
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        return f"{_MONTHS[m]} {d}"
    except Exception:
        return "—"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _nice_ceiling(x: float) -> float:
    """Round up to a clean number so the shared axis ends on a readable tick —
    in steps fine enough that the marks still fill most of the track."""
    if not x or x <= 0 or not math.isfinite(x):
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if x <= step * mag * (1 + 1e-9):
            return step * mag
    return 10 * mag


def _needs_text(need: dict | None) -> str:
    if not need:
        return ""
    parts = []
    if need.get("dates"):
        parts.append(_plural(need["dates"], "more session"))
    if need.get("observations"):
        parts.append(_plural(need["observations"], "more observation"))
    return " · ".join(parts)


def _progress(c: dict) -> int | None:
    """How far an accumulating cohort is toward being judged, 0-100, by the
    binding requirement (sessions, usually)."""
    need = c.get("needs")
    if not need:
        return None
    fracs = []
    if need.get("dates") is not None:
        have = c.get("n_clusters") or 0
        fracs.append(have / (have + need["dates"]) if have + need["dates"] else 0.0)
    if need.get("observations"):
        have = c.get("n") or 0
        fracs.append(have / (have + need["observations"]))
    return int(round(100 * min(fracs))) if fracs else None


# ---------------------------------------------------------------------------
# Rows and families
# ---------------------------------------------------------------------------

def _axis_max(cohorts: list) -> float:
    """Half-width of the family's shared axis. Set by the JUDGED cohorts'
    intervals: an unjudged cohort can have an enormous interval (a handful of
    independent windows) or a wild estimate (four observations), and letting it
    set the scale would flatten every row that matters. Unjudged marks that fall
    outside are drawn clipped at the edge; their numbers are in the table. With
    nothing judged yet, the estimates set the scale."""
    spans = [max(abs(c["ci_low"]), abs(c["ci_high"])) for c in cohorts
             if c["verdict"] != "accumulating"
             and c.get("ci_low") is not None and c.get("ci_high") is not None]
    if not spans:
        spans = [abs(c["effect"]) for c in cohorts if c.get("effect") is not None]
    return _nice_ceiling(max(spans)) if spans else 1.0


def _pos(v: float, half: float) -> float:
    return round(max(0.0, min(100.0, 50.0 + 50.0 * v / half)), 2)


def _row(c: dict, unit: str, half: float) -> dict:
    v = VERDICT[c["verdict"]]
    eff, lo, hi = c.get("effect"), c.get("ci_low"), c.get("ci_high")
    judged = c["verdict"] != "accumulating"
    mark = None
    if eff is not None:
        mark = {"dot": _pos(eff, half), "clipped": abs(eff) > half}
        if lo is not None and hi is not None:
            mark["lo"], mark["width"] = _pos(lo, half), round(_pos(hi, half) - _pos(lo, half), 2)
    interval = (f"{_signed(lo, unit)} to {_signed(hi, unit)}"
                if lo is not None and hi is not None else "—")
    sfx = _suffix(unit)
    tip = f"{c['cohort']}: {_signed(eff, unit)}{sfx}"
    if interval != "—":
        tip += f", 90% interval {interval}{sfx}"
    tip += f" · {v['label'].lower()}"
    windows = c.get("eff_clusters")
    # Its own interval clears zero, yet it is not paying/inverted: the family
    # correction is what held it back. Flagged so the two do not look contradictory.
    dagger = (c["verdict"] == "noise" and lo is not None and hi is not None
              and (lo > 0 or hi < 0))
    return {
        "dagger": dagger,
        "cohort": c["cohort"], "verdict": c["verdict"], "v": v, "judged": judged,
        "effect": _signed(eff, unit), "interval": interval, "mark": mark, "tip": tip,
        "n": f"{c.get('n') or 0:,}", "dates": c.get("n_clusters") or 0,
        "windows": "—" if not windows else (f"{windows:.0f}" if windows >= 20 else f"{windows:.1f}"),
        "overlapping": bool(windows and c.get("n_clusters") and windows < c["n_clusters"] - 0.5),
        "hit": f"{c['hit_rate']:.1f}%" if c.get("hit_rate") is not None else "—",
        "needs": _needs_text(c.get("needs")), "progress": _progress(c),
    }


def _family(f: dict, lead: bool) -> dict:
    cohorts = f.get("cohorts") or []
    half = _axis_max(cohorts)
    unit = f.get("unit") or ""
    rows = [_row(c, unit, half) for c in cohorts]
    tally = {k: sum(1 for r in rows if r["verdict"] == k) for k in _ORDER}
    label = f"{half:g}"
    return {
        "key": f["family"], "question": f["question"], "unit": unit, "note": f.get("note"),
        "rows": rows, "tally": tally, "tests": f.get("tests") or 0,
        "axis": {"lo": f"−{label}", "hi": f"+{label}", "suffix": _suffix(unit).strip()},
        "has_hit": any(r["hit"] != "—" for r in rows),
        "has_overlap": any(r["overlapping"] for r in rows),
        "has_dagger": any(r["dagger"] for r in rows),
        # The headline family is open; diagnostics fold away until wanted.
        "open": lead,
        "summary": _tally_text(tally),
    }


def _tally_text(tally: dict) -> str:
    parts = [f"{tally[k]} {VERDICT[k]['label'].lower()}" for k in _ORDER if tally.get(k)]
    return " · ".join(parts) if parts else "no cohorts yet"


# ---------------------------------------------------------------------------
# Per-loop reading
# ---------------------------------------------------------------------------

def _read(loop: dict) -> tuple:
    """(verdict key for the chip, one plain sentence) for a loop's headline family."""
    fams = loop.get("families") or []
    cohorts = fams[0]["cohorts"] if fams else []
    sessions = (loop.get("coverage") or {}).get("dates") or 0
    if loop.get("error"):
        return "accumulating", "Store could not be read."
    if not cohorts or not sessions:
        return "accumulating", "No labelled history yet."
    by = {k: [c for c in cohorts if c["verdict"] == k] for k in _ORDER}
    judged = len(cohorts) - len(by["accumulating"])
    names = lambda cs: ", ".join(c["cohort"] for c in cs[:3]) + ("…" if len(cs) > 3 else "")  # noqa: E731
    bits = []
    if by["paying"]:
        bits.append(f"Paying: {names(by['paying'])}.")
    if by["inverted"]:
        bits.append(f"Reliably wrong: {names(by['inverted'])}.")
    if judged and not by["paying"] and not by["inverted"]:
        bits.append(f"{_plural(judged, 'cohort')} judged over {_plural(sessions, 'session')}: "
                    "no edge detected.")
    if by["accumulating"]:
        soonest = min((c.get("needs") or {}).get("dates") or 0 for c in by["accumulating"])
        tail = f" (soonest needs {_plural(soonest, 'more session')})" if soonest else ""
        bits.append(f"{len(by['accumulating'])} still accumulating{tail}.")
    # A real finding in a diagnostic family must not hide behind a quiet headline.
    deeper = [c for f in fams[1:] for c in f["cohorts"] if c["verdict"] in ("paying", "inverted")]
    if deeper:
        found = "; ".join(f"{c.get('reading') or c['cohort']} "
                          f"({VERDICT[c['verdict']]['label'].lower()})" for c in deeper[:3])
        bits.append(f"In the diagnostics: {found}{'…' if len(deeper) > 3 else ''}.")
    key = next((k for k in _ORDER if by[k]), "accumulating")
    return key, " ".join(bits)


def _loop(loop: dict) -> dict:
    cov = loop.get("coverage") or {}
    one, many = _UNIT_NAME.get(loop["loop"], ("observation", "observations"))
    key, sentence = _read(loop)
    span = (f"{_day(cov.get('first'))} → {_day(cov.get('last'))}"
            if cov.get("first") else "no history yet")
    desc = loop.get("descriptive")
    return {
        "key": loop["loop"], "label": loop.get("label") or loop["loop"].title(),
        "observation": loop.get("observation") or "",
        "sessions": cov.get("dates") or 0,
        "observations": _plural(cov.get("observations") or 0, one, many),
        "span": span, "store": loop.get("store"),
        "read": sentence, "v": VERDICT[key], "verdict": key,
        "tally": loop.get("headline") or {},
        "families": [_family(f, i == 0) for i, f in enumerate(loop.get("families") or [])],
        "descriptive": ({
            "tiles": [
                {"label": "Held", "value": f"{desc['hold_rate']:.1f}%" if desc.get("hold_rate") is not None else "—",
                 "sub": f"{desc.get('held', 0):,} of {desc.get('held', 0) + desc.get('broke', 0):,} resolved touches"},
                {"label": "Broke", "value": f"{desc.get('broke', 0):,}", "sub": "resolved touches"},
                {"label": "Churned", "value": f"{desc['churn_share']:.1f}%" if desc.get("churn_share") is not None else "—",
                 "sub": f"{desc.get('churned', 0):,} of {desc.get('touches', 0):,} touches, unresolved"},
            ],
            "note": desc.get("note"),
        } if desc else None),
        "error": loop.get("error"),
    }


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def page() -> dict:
    """Everything the /learning template renders."""
    card = _scorecard()
    if not card:
        return {"available": False, "loops": [], "legend": [], "method": {}}
    m = card.get("method") or {}
    return {
        "available": True,
        "generated_at": (card.get("generated_at") or "")[:16].replace("T", " ") + " UTC",
        "loops": [_loop(lp) for lp in card.get("loops") or []],
        "legend": [{**VERDICT[k], "key": k, "text": (card.get("vocabulary") or {}).get(k, "")}
                   for k in _ORDER],
        "method": {
            "min_obs": m.get("min_observations"), "min_dates": m.get("min_dates"),
            "min_windows": int(m.get("min_independent_windows") or 0),
            "fdr": f"{(m.get('false_discovery_rate') or 0) * 100:.0f}%",
            "ci": f"{(m.get('confidence_level') or 0) * 100:.0f}%",
        },
    }


def digest() -> list:
    """One Track Record line per loop that has history: source, sentence, verdict."""
    card = _scorecard()
    if not card:
        return []
    out = []
    for lp in card.get("loops") or []:
        if not (lp.get("coverage") or {}).get("observations"):
            continue
        key, sentence = _read(lp)
        out.append({"src": lp.get("label") or lp["loop"], "text": sentence,
                    "verdict": key, "v": VERDICT[key]})
    return out


def _brief_feedback() -> dict:
    card = _scorecard()
    if not card:
        return {"available": False}
    try:
        from engines.learning import feedback
        return feedback.brief(card)
    except Exception:
        return {"available": False}


def brief_calls(tickers: list) -> dict:
    """Per ticker, what the Brief's history says about TODAY's call — computed in
    code from the scorecard, replacing two things on the Gameplan card: the
    model's self-reported conviction as the headline number (it has not been
    informative) and the per-ticker recent-accuracy chip (no error bar).

    tickers: [{"sym", "bias", "conviction"}]. Returns {sym: learned} or {}."""
    fb = _brief_feedback()
    if not fb.get("available"):
        return {}
    from engines.learning import feedback
    d = fb["directional"]
    out = {}
    for t in tickers:
        call = feedback.brief_call(fb, t.get("bias"), t.get("sym"))
        stated = t.get("conviction")
        stated_txt = f" · model stated {stated}" if stated not in (None, "", "—") else ""
        if call["kind"] == "directional":
            pct = int(round(100 * call["p"]))
            rng = f"{100 * call['low']:.0f}–{100 * call['high']:.0f}%"
            if call["own"]:
                why = (f"{call['basis']} calls have verified {pct}% over {call['n']} graded, "
                       f"judged {VERDICT[call['verdict']]['label'].lower()}")
            else:
                why = (f"directional calls have verified {d['hits']} of {d['graded']} graded; "
                       "no bias, ticker or gamma split differs reliably")
            out[t["sym"]] = {"kind": "directional", "pct": pct, "value": f"{pct}%", "range": rng,
                             "text": f"{why}{stated_txt}"}
        elif call["kind"] == "no_call":
            out[t["sym"]] = {
                "kind": "no_call", "value": "No call",
                "text": (f"neutral calls have verified {call['hit_rate']:.0f}% against "
                         f"{call['base_rate']:.0f}% of all days staying in the band, so neutral "
                         f"has carried no information yet{stated_txt}")}
        elif call["kind"] == "range":
            pct = int(round(100 * call["p"]))
            out[t["sym"]] = {"kind": "range", "pct": pct, "value": f"{pct}%",
                             "text": f"neutral calls have verified {pct}% over {call['n']} graded"}
    return out


def brief_tiles(today: dict | None = None) -> list:
    """EOD performance tiles from the full history. Replaces the recent-30-session
    accuracy (which mixed in neutral calls), the 'best regime' tile (the maximum
    of several noisy buckets, so biased upward by construction) and the
    always-bullish baseline (drift is now removed inside the edge itself)."""
    fb = _brief_feedback()
    if not fb.get("available"):
        return []
    d, neu, conv = fb["directional"], fb["neutral"], fb["conviction"]
    tiles = []
    if today:
        tiles.append(today)
    tiles.append({"label": "Directional calls", "value": f"{d['hit_rate']:.1f}%", "value_class": "",
                  "sub": f"{d['hits']} / {d['graded']} verified · {fb['sessions']} sessions"})
    tiles.append({"label": "Timing edge", "value": f"{d['edge']:+.2f}%", "value_class": "",
                  "sub": f"{d['ci_low']:+.2f} to {d['ci_high']:+.2f} per call",
                  "v": VERDICT[d["verdict"]]})
    if neu.get("graded"):
        tiles.append({"label": "Neutral calls", "value": f"{neu['hit_rate']:.0f}%", "value_class": "",
                      "sub": f"vs {neu['base_rate']:.0f}% of all days · {neu['graded']} graded",
                      "v": VERDICT[neu["verdict"]] if neu.get("verdict") in VERDICT else None})
    if conv.get("gap") is not None:
        tiles.append({"label": "Conviction", "value": f"{conv['gap']:+.1f} pp", "value_class": "",
                      "sub": "high vs low conviction"
                             + (f" · Brier {conv['brier']:.2f}" if conv.get("brier") is not None else ""),
                      "v": VERDICT[conv["verdict"]] if conv.get("verdict") in VERDICT else None})
    return tiles


def neutral_is_a_call() -> bool | None:
    """True once neutral calls have earned `paying`; False while they have not;
    None when there is no history to say."""
    fb = _brief_feedback()
    return fb["neutral"]["informative"] if fb.get("available") else None


def zerodte_panel() -> dict | None:
    """The Intraday page's level record. Replaces the per-label hold rates, which
    could only ever read 100% or 0% because a touch's label is assigned after its
    outcome. Shows the plain hold rate, then each reading taken while price was
    at the level: how often levels held when it was high versus low, and whether
    that gap is real."""
    card = _scorecard()
    loop = next((lp for lp in (card or {}).get("loops") or [] if lp["loop"] == "zerodte"), None)
    desc = (loop or {}).get("descriptive")
    if not desc or not desc.get("touches"):
        return None
    view = _loop(loop)
    fam = next((f for f in loop["families"] if f["family"] == "during_touch"), None)
    rows = []
    for c in (fam or {}).get("cohorts") or []:
        if c.get("hit_rate") is None or c.get("other_hit_rate") is None:
            continue
        rows.append({
            "reading": c.get("reading") or c["cohort"],
            "high": c.get("high"), "high_rate": f"{c['hit_rate']:.1f}%",
            "low": c.get("versus"), "low_rate": f"{c['other_hit_rate']:.1f}%",
            "gap": f"{_signed(c.get('effect'), 'percentage')} pp",
            "v": VERDICT[c["verdict"]], "verdict": c["verdict"],
            "needs": _needs_text(c.get("needs")),
        })
    return {"tiles": view["descriptive"]["tiles"], "rows": rows,
            "sessions": view["sessions"], "touches": f"{desc['touches']:,}",
            "note": (fam or {}).get("note")}


def structure_tiles() -> dict | None:
    """The Structure page's learned strip: each setup type and horizon as its
    edge over the benchmark with a verdict — replacing the raw win rates, which
    can be high for a setup that still trails the benchmark."""
    card = _scorecard()
    loop = next((lp for lp in (card or {}).get("loops") or [] if lp["loop"] == "structure"), None)
    if not loop or not loop.get("families"):
        return None
    fam = loop["families"][0]
    cohorts = [c for c in fam["cohorts"] if c.get("n")]
    if not cohorts:
        return None
    unit = fam.get("unit") or ""
    tiles = []
    for c in cohorts:
        lo, hi = c.get("ci_low"), c.get("ci_high")
        tiles.append({
            "label": c["cohort"], "value": f"{_signed(c.get('effect'), unit)}%",
            "v": VERDICT[c["verdict"]], "verdict": c["verdict"],
            "sub": (f"{_signed(lo, unit)} to {_signed(hi, unit)} · win {c['hit_rate']:.0f}%"
                    if lo is not None and c.get("hit_rate") is not None else f"n = {c.get('n', 0):,}"),
            "needs": _needs_text(c.get("needs")),
        })
    # Per setup type, one chip per horizon — stamped onto each recommendation so the
    # history of its kind of setup travels with it.
    edges: dict = {}
    for c in cohorts:
        typ, _, horizon = c["cohort"].partition(" · ")
        lo, hi = c.get("ci_low"), c.get("ci_high")
        v = VERDICT[c["verdict"]]
        tip = f"{c['cohort']}: {_signed(c.get('effect'), unit)}% vs benchmark"
        if lo is not None and hi is not None:
            tip += f" (90% interval {_signed(lo, unit)} to {_signed(hi, unit)})"
        tip += f" over {c.get('n_clusters') or 0} sessions — {v['label'].lower()}"
        if c.get("needs"):
            tip += f"; needs {_needs_text(c['needs'])}"
        edges.setdefault(typ, []).append({"horizon": horizon, "v": v, "verdict": c["verdict"],
                                          "value": f"{_signed(c.get('effect'), unit)}%", "tip": tip})
    # Does the combined score — the column the table is sorted by — rank outcomes?
    score = (loop["families"][1]["cohorts"] if len(loop["families"]) > 1 else [])
    judged = [c for c in score if c["verdict"] != "accumulating"]
    if any(c["verdict"] == "paying" for c in judged):
        score_note = "Higher-scored setups have done reliably better than lower-scored ones."
    elif any(c["verdict"] == "inverted" for c in judged):
        score_note = "Higher-scored setups have done reliably WORSE than lower-scored ones."
    elif judged:
        score_note = ("So far the score has not ranked outcomes: higher-scored setups have done "
                      "no better than lower-scored ones.")
    else:
        score_note = ""
    return {"tiles": tiles, "edges": edges, "score_note": score_note,
            "sessions": (loop.get("coverage") or {}).get("dates") or 0,
            "outcomes": (loop.get("coverage") or {}).get("observations") or 0}


def rec_setup(row: dict) -> str:
    """The scorecard setup type a Structure rec belongs to. Rows from the live
    adapter carry it; sample rows are read from their zone text and components."""
    if row.get("setup"):
        return row["setup"]
    if (row.get("comp") or {}).get("z"):
        return "resistance" if "resist" in str(row.get("zone", "")).lower() else "support"
    return "flag"
