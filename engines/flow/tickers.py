"""
Ticker universe for the options flow scanner.

Covers the ~250 most actively traded names by daily options *and* share
volume across:
  - Broad-market, sector, thematic and leveraged ETFs
  - Mega-cap tech, semis & AI infrastructure
  - Financials, energy, healthcare, consumer, industrials
  - Crypto-adjacent, EV/clean-energy, and high-volatility retail favorites

Scanned in order — ETFs and mega-caps are first so the most important
signals appear early in each cycle.
"""

TICKER_UNIVERSE = [
    # ── Broad market & volatility ETFs ───────────────────────────────────
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "MDY", "RSP",
    "VXX", "UVXY", "SVXY", "VIXY",

    # ── Sector ETFs ──────────────────────────────────────────────────────
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLRE", "XLP", "XLY", "XLU", "XLB", "XLC",
    "SMH", "SOXX", "XBI", "IBB", "KRE", "KBE", "XRT", "XHB", "XOP", "OIH", "ITB",

    # ── International & thematic ETFs ─────────────────────────────────────
    "EEM", "EFA", "FXI", "EWZ", "KWEB", "ASHR", "INDA", "EWJ",
    "ARKK", "ARKG", "JETS", "TAN", "LIT", "URA", "XME",

    # ── Commodity, rates & crypto ETFs ───────────────────────────────────
    "GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "UUP",
    "TLT", "IEF", "HYG", "LQD", "SHY", "TMF", "TBT",
    "IBIT", "BITO", "ETHU",

    # ── Leveraged / inverse ETFs ─────────────────────────────────────────
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "SPXL", "SPXU", "SPXS",
    "UPRO", "SDOW", "LABU", "LABD", "FAS", "FAZ", "NVDL", "TSLL", "TSLQ", "YINN",

    # ── Mega-cap tech ────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "AVGO", "ORCL", "ADBE", "CRM", "NFLX", "CSCO", "IBM", "QCOM", "TXN",

    # ── Semiconductors & AI infrastructure ───────────────────────────────
    "AMD", "INTC", "MU", "TSM", "ASML", "SMCI", "MRVL", "AMAT",
    "LRCX", "KLAC", "ARM", "ANET", "VRT", "DELL", "HPE", "ON", "ADI",
    "WDC", "STX", "NXPI", "MCHP", "GFS", "ASTS",

    # ── Software & internet ──────────────────────────────────────────────
    "CRWD", "PANW", "SNOW", "NET", "DDOG", "MDB", "ZS", "PLTR", "NOW",
    "SHOP", "U", "ROKU", "DKNG", "AFRM", "SQ", "ABNB", "UBER", "LYFT",
    "DASH", "SNAP", "PINS", "RBLX", "SPOT", "TTD", "ZM", "DOCU", "TWLO",

    # ── Financials ───────────────────────────────────────────────────────
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BLK", "SCHW", "COF", "USB",
    "AXP", "V", "MA", "PYPL", "SOFI", "ALLY", "DFS",

    # ── Energy ───────────────────────────────────────────────────────────
    "XOM", "CVX", "OXY", "COP", "SLB", "MPC", "VLO", "PSX", "DVN",
    "HAL", "FANG", "KMI", "WMB", "ET",

    # ── Healthcare & biotech ─────────────────────────────────────────────
    "UNH", "JNJ", "PFE", "MRNA", "ABBV", "LLY", "GILD", "BIIB",
    "AMGN", "REGN", "VRTX", "BMY", "CVS", "MRK", "TMO", "ISRG",
    "HIMS", "MRK", "NVO",

    # ── Consumer & retail ────────────────────────────────────────────────
    "COST", "WMT", "HD", "LOW", "NKE", "SBUX", "MCD", "TGT",
    "DIS", "CMCSA", "LULU", "CMG", "ELF", "CELH", "CVNA", "ULTA",
    "DG", "DLTR", "BABA", "PDD", "JD", "MELI",

    # ── Industrials & aerospace ──────────────────────────────────────────
    "CAT", "BA", "GE", "RTX", "LMT", "DE", "UPS", "FDX",
    "HON", "MMM", "EMR", "GEV",

    # ── Transports & travel ──────────────────────────────────────────────
    "DAL", "AAL", "UAL", "LUV", "CCL", "NCLH", "RCL",

    # ── Autos, EV & clean energy ─────────────────────────────────────────
    "F", "GM", "RIVN", "NIO", "LCID", "ENPH", "FSLR", "PLUG", "RUN", "CHPT",

    # ── Materials & miners ───────────────────────────────────────────────
    "FCX", "NEM", "CLF", "X", "AA", "NUE", "MOS", "CF",

    # ── Crypto-adjacent ──────────────────────────────────────────────────
    "COIN", "MSTR", "HOOD", "MARA", "RIOT", "CLSK", "WULF", "BITF", "XYZ",

    # ── High-volatility / retail / speculative ───────────────────────────
    "GME", "AMC", "DJT", "RDDT", "SOUN", "IONQ", "RGTI", "BBAI",
    "LUNR", "RKLB", "AI", "NKLA", "TLRY", "CGC",

    # ── Telecom, real estate & misc ──────────────────────────────────────
    "T", "VZ", "TMUS", "AMT", "PLD", "SPG", "O",
]

# Deduplicate while preserving order
_seen = set()
TICKER_UNIVERSE = [
    t for t in TICKER_UNIVERSE
    if not (t in _seen or _seen.add(t))
]
