"""
Static reference data for the single-stock deep-dive tool.

Bundled here (rather than scraped at runtime) so the tool has no fragile network
dependency for index membership and related-instrument lookups:

  * Index constituent sets — S&P 500, Nasdaq-100, Dow 30.
  * A curated map of single-stock leveraged / inverse ETFs for popular underlyings.
  * A sector -> leveraged/inverse/sector-ETF fallback used when an underlying has
    no dedicated single-stock products.

Snapshot date: 2026-06 (refresh periodically; tickers can change as funds launch/close).
"""

# ---------------------------------------------------------------------------
# Index membership
# ---------------------------------------------------------------------------

DOW_30 = {
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
}

NASDAQ_100 = {
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC",
    "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP",
    "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MSTR", "MU", "NFLX",
    "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP",
    "PLTR", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS",
    "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
}

# Representative large-cap S&P 500 set. Not exhaustive (the full index is ~500
# names); covers the most commonly looked-up tickers. Easy to extend.
SP_500 = NASDAQ_100 | DOW_30 | {
    "ABBV", "ABT", "ACN", "AIG", "ALL", "AMT", "APD", "BAC", "BBY", "BDX",
    "BLK", "BMY", "BSX", "C", "CB", "CCI", "CI", "CL", "CMG", "COF",
    "COP", "CVS", "D", "DE", "DHR", "DUK", "ELV", "EMR", "EOG", "EQIX",
    "ETN", "F", "FDX", "GD", "GE", "GHC", "GM", "HCA", "HUM", "ICE",
    "ITW", "KMB", "KMI", "LLY", "LMT", "LOW", "MA", "MCK", "MDT", "MET",
    "MMC", "MO", "MPC", "MS", "NEE", "NEM", "NOC", "NSC", "NUE", "OXY",
    "PFE", "PGR", "PH", "PLD", "PM", "PNC", "PSA", "PSX", "RTX", "SCHW",
    "SLB", "SO", "SPG", "SYK", "T", "TGT", "TJX", "TMO", "TFC", "UNP",
    "UPS", "USB", "VLO", "WFC", "WM", "XOM", "ZTS",
}


def index_memberships(symbol: str) -> list[str]:
    """Return the human-readable indices a ticker belongs to."""
    sym = symbol.upper()
    out = []
    if sym in SP_500:
        out.append("S&P 500")
    if sym in NASDAQ_100:
        out.append("NASDAQ-100")
    if sym in DOW_30:
        out.append("Dow 30")
    return out


# ---------------------------------------------------------------------------
# Curated single-stock leveraged / inverse ETFs
# ---------------------------------------------------------------------------
# Each underlying maps to a list of (ticker, label, kind) where kind is one of:
#   "long"     — leveraged long (2x/3x)
#   "inverse"  — leveraged short / inverse
# The label carries the leverage factor for display.

LEVERAGED_MAP: dict[str, list[tuple[str, str, str]]] = {
    "NVDA": [
        ("NVDL", "2x Long", "long"),
        ("NVDU", "2x Long", "long"),
        ("NVDX", "2x Long", "long"),
        ("NVD",  "2x Inverse", "inverse"),
        ("NVDQ", "2x Inverse", "inverse"),
        ("NVDS", "Short",      "inverse"),
    ],
    "TSLA": [
        ("TSLL", "2x Long", "long"),
        ("TSLR", "2x Long", "long"),
        ("TSLT", "2x Long", "long"),
        ("TSLQ", "2x Inverse", "inverse"),
        ("TSLS", "Short",      "inverse"),
        ("TSDD", "2x Inverse", "inverse"),
    ],
    "AAPL": [
        ("AAPU", "2x Long", "long"),
        ("AAPD", "1x Inverse", "inverse"),
    ],
    "MSFT": [
        ("MSFU", "2x Long", "long"),
        ("MSFD", "1x Inverse", "inverse"),
    ],
    "AMZN": [
        ("AMZU", "2x Long", "long"),
        ("AMZD", "1x Inverse", "inverse"),
    ],
    "GOOGL": [
        ("GGLL", "2x Long", "long"),
        ("GGLS", "2x Inverse", "inverse"),
    ],
    "GOOG": [
        ("GGLL", "2x Long", "long"),
        ("GGLS", "2x Inverse", "inverse"),
    ],
    "META": [
        ("METU", "2x Long", "long"),
        ("METD", "2x Inverse", "inverse"),
    ],
    "AMD": [
        ("AMDL", "2x Long", "long"),
        ("AMDU", "2x Long", "long"),
        ("AMDS", "2x Inverse", "inverse"),
    ],
    "COIN": [
        ("CONL", "2x Long", "long"),
        ("CONI", "1x Inverse", "inverse"),
    ],
    "MSTR": [
        ("MSTU", "2x Long", "long"),
        ("MSTX", "2x Long", "long"),
        ("MSTZ", "2x Inverse", "inverse"),
        ("SMST", "2x Inverse", "inverse"),
    ],
    "NFLX": [
        ("NFXL", "2x Long", "long"),
        ("NFXS", "2x Inverse", "inverse"),
    ],
    "PLTR": [
        ("PLTU", "2x Long", "long"),
        ("PLTD", "2x Inverse", "inverse"),
    ],
    "MU": [
        ("MUU", "2x Long", "long"),
    ],
    "AVGO": [
        ("AVL", "2x Long", "long"),
        ("AVS", "2x Inverse", "inverse"),
    ],
    "MRVL": [
        ("MRVU", "2x Long", "long"),
    ],
    "SMCI": [
        ("SMCL", "2x Long", "long"),
        ("SMCZ", "2x Inverse", "inverse"),
    ],
}


# ---------------------------------------------------------------------------
# Sector fallback — broad / leveraged / inverse ETFs by sector
# ---------------------------------------------------------------------------
# Keyed by lowercase substrings found in yfinance's `sector` / `industry`.

SECTOR_ETFS: dict[str, list[tuple[str, str, str]]] = {
    "semiconductor": [
        ("SMH",  "Semis ETF",   "sector"),
        ("SOXX", "Semis ETF",   "sector"),
        ("SOXL", "3x Long Semis",  "long"),
        ("SOXS", "3x Inverse Semis", "inverse"),
    ],
    "technology": [
        ("XLK",  "Tech ETF",    "sector"),
        ("QQQ",  "Nasdaq-100",  "sector"),
        ("TQQQ", "3x Long QQQ",  "long"),
        ("SQQQ", "3x Inverse QQQ", "inverse"),
    ],
    "energy": [
        ("XLE", "Energy ETF",  "sector"),
        ("ERX", "2x Long Energy",  "long"),
        ("ERY", "2x Inverse Energy", "inverse"),
    ],
    "financial": [
        ("XLF", "Financials ETF", "sector"),
        ("FAS", "3x Long Financials",  "long"),
        ("FAZ", "3x Inverse Financials", "inverse"),
    ],
    "health": [
        ("XLV",  "Healthcare ETF", "sector"),
        ("CURE", "3x Long Health",  "long"),
        ("LABU", "3x Long Biotech", "long"),
        ("LABD", "3x Inverse Biotech", "inverse"),
    ],
    "consumer": [
        ("XLY", "Consumer Disc ETF", "sector"),
        ("XLP", "Consumer Staples ETF", "sector"),
    ],
    "industrial": [
        ("XLI", "Industrials ETF", "sector"),
        ("DUSL", "3x Long Industrials", "long"),
    ],
    "real estate": [
        ("XLRE", "Real Estate ETF", "sector"),
        ("DRN",  "3x Long Real Estate", "long"),
        ("DRV",  "3x Inverse Real Estate", "inverse"),
    ],
    "utilit": [
        ("XLU", "Utilities ETF", "sector"),
    ],
    "material": [
        ("XLB", "Materials ETF", "sector"),
    ],
    "communication": [
        ("XLC", "Comm Services ETF", "sector"),
    ],
}


def sector_fallback(sector: str | None, industry: str | None) -> list[tuple[str, str, str]]:
    """Pick sector-level ETFs by matching keywords in sector/industry strings."""
    hay = f"{(sector or '').lower()} {(industry or '').lower()}"
    for key, etfs in SECTOR_ETFS.items():
        if key in hay:
            return etfs
    return []
