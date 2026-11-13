"""
Fundamental snapshot for a single ticker via yfinance `.info` / `.fast_info`.

Returns a flat dict of the headline metrics a long-term investor cares about:
identity, valuation, profitability, balance-sheet health, and price context.
All values are best-effort — any field can come back None if yfinance lacks it.
"""

import yfinance as yf


def _num(v):
    """Coerce to float or None."""
    try:
        if v is None:
            return None
        f = float(v)
        # yfinance occasionally returns sentinel 'Infinity'
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _fi_get(fi, attr):
    """Safely read a fast_info attribute — its properties can raise (not just
    AttributeError) for delisted/invalid symbols, so getattr alone isn't enough."""
    try:
        return getattr(fi, attr, None)
    except Exception:
        return None


def fetch_fundamentals(symbol: str) -> dict:
    sym = symbol.upper()
    t = yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception:
        info = {}

    # fast_info gives a reliable live price even when .info is sparse
    fi = t.fast_info
    last_price = _num(_fi_get(fi, "last_price")) or _num(info.get("currentPrice"))
    prev_close = _num(_fi_get(fi, "previous_close")) or _num(info.get("previousClose"))
    day_change_pct = None
    if last_price is not None and prev_close:
        day_change_pct = round((last_price - prev_close) / prev_close * 100, 2)

    quote_type = (info.get("quoteType") or "").upper()  # EQUITY / ETF / etc.

    div_yield = _num(info.get("dividendYield"))
    # yfinance returns dividendYield as a fraction in newer versions; normalize to %
    if div_yield is not None and div_yield < 1:
        div_yield = round(div_yield * 100, 2)

    def pct(field):
        v = _num(info.get(field))
        return round(v * 100, 2) if v is not None else None

    return {
        "symbol": sym,
        "name": info.get("longName") or info.get("shortName") or sym,
        "quote_type": quote_type,
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "currency": info.get("currency") or _fi_get(fi, "currency") or "USD",
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        "website": info.get("website"),
        "summary": info.get("longBusinessSummary"),

        "price": last_price,
        "prev_close": prev_close,
        "day_change_pct": day_change_pct,

        # Size / valuation
        "market_cap": _num(info.get("marketCap")),
        "enterprise_value": _num(info.get("enterpriseValue")),
        "shares_outstanding": _num(info.get("sharesOutstanding")),
        "float_shares": _num(info.get("floatShares")),
        "trailing_pe": _num(info.get("trailingPE")),
        "forward_pe": _num(info.get("forwardPE")),
        "peg_ratio": _num(info.get("trailingPegRatio") or info.get("pegRatio")),
        "price_to_book": _num(info.get("priceToBook")),
        "price_to_sales": _num(info.get("priceToSalesTrailing12Months")),
        "ev_to_ebitda": _num(info.get("enterpriseToEbitda")),

        # Profitability / growth
        "profit_margin": pct("profitMargins"),
        "operating_margin": pct("operatingMargins"),
        "roe": pct("returnOnEquity"),
        "roa": pct("returnOnAssets"),
        "revenue_growth": pct("revenueGrowth"),
        "earnings_growth": pct("earningsGrowth"),
        "gross_margin": pct("grossMargins"),

        # Balance sheet
        "debt_to_equity": _num(info.get("debtToEquity")),
        "current_ratio": _num(info.get("currentRatio")),
        "total_cash": _num(info.get("totalCash")),
        "total_debt": _num(info.get("totalDebt")),
        "free_cashflow": _num(info.get("freeCashflow")),

        # Income for shareholders
        "dividend_yield": div_yield,
        "payout_ratio": pct("payoutRatio"),

        # Risk / price context
        "beta": _num(info.get("beta")),
        "fifty_two_week_high": _num(info.get("fiftyTwoWeekHigh")) or _num(_fi_get(fi, "year_high")),
        "fifty_two_week_low": _num(info.get("fiftyTwoWeekLow")) or _num(_fi_get(fi, "year_low")),
        "fifty_day_avg": _num(info.get("fiftyDayAverage")) or _num(_fi_get(fi, "fifty_day_average")),
        "two_hundred_day_avg": _num(info.get("twoHundredDayAverage")) or _num(_fi_get(fi, "two_hundred_day_average")),
        "avg_volume": _num(info.get("averageVolume")),

        # Analyst-target convenience (full breakdown lives in fetch_analysts)
        "target_mean": _num(info.get("targetMeanPrice")),
        "recommendation_key": info.get("recommendationKey"),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_fundamentals("NVDA"), indent=2, default=str))
