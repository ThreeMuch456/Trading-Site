"""
Related & inverse instruments for a given underlying.

Combines the curated single-stock leveraged/inverse map with a sector-ETF
fallback, then attaches a live price + day-change snapshot for each so the UI
can show how the geared products are trading right now.
"""

import yfinance as yf

from .index_data import LEVERAGED_MAP, sector_fallback, index_memberships


def _quote(symbol: str) -> dict:
    """Lightweight last-price / day-change snapshot for one ticker."""
    try:
        fi = yf.Ticker(symbol).fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        chg = None
        if last is not None and prev:
            chg = round((last - prev) / prev * 100, 2)
        return {
            "price": round(float(last), 2) if last is not None else None,
            "change_pct": chg,
        }
    except Exception:
        return {"price": None, "change_pct": None}


def get_related(symbol: str, sector: str | None, industry: str | None,
                with_quotes: bool = True) -> dict:
    """
    Return grouped related instruments for `symbol`.

    Shape:
      {
        "indices": ["S&P 500", "NASDAQ-100"],
        "direct": [ {symbol,label,kind,price,change_pct}, ... ],  # single-stock geared
        "sector": [ {symbol,label,kind,price,change_pct}, ... ],  # sector ETFs
      }
    """
    sym = symbol.upper()

    direct_specs = LEVERAGED_MAP.get(sym, [])
    sector_specs = sector_fallback(sector, industry)

    def build(specs):
        rows = []
        for tkr, label, kind in specs:
            row = {"symbol": tkr, "label": label, "kind": kind}
            if with_quotes:
                row.update(_quote(tkr))
            else:
                row.update({"price": None, "change_pct": None})
            rows.append(row)
        return rows

    return {
        "indices": index_memberships(sym),
        "direct": build(direct_specs),
        "sector": build(sector_specs),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_related("NVDA", "Technology", "Semiconductors"), indent=2))
