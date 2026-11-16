"""Shared learning layer — the one place every loop's evidence is read and judged.

Not a tool engine: it serves no desk source and runs no jobs. It sits beside the
engines because it reads their stores.

  stats.py      one method (date-clustered, overlap- and multiplicity-corrected)
                and one verdict vocabulary: paying / inverted / noise / accumulating
  ledger.py     read-only projection of each engine's learning store into one
                common observation shape
  scorecard.py  every loop's cohorts, judged with stats over the ledger

Registered route (in-process, via engines.produce):
  /api/scorecard.json   the full scorecard; ?loop=<name> for one loop
"""

from .. import route
from . import ledger, scorecard, stats  # noqa: F401  (re-exported)


def _produce_scorecard(params: dict) -> dict:
    card = scorecard.cached()
    want = (params or {}).get("loop")
    if want:
        return {**card, "loops": [lp for lp in card["loops"] if lp["loop"] == want]}
    return card


route("/api/scorecard.json")(_produce_scorecard)
