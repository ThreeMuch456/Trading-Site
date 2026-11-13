"""
Sector taxonomy for the rotation view.

Maps the ticker universe onto ~21 groups so the scanner's per-contract flow can
be rolled up into "which corner of the market is money positioning in".

Why not plain GICS: this is a *high-options-volume* universe, not the S&P 500.
Lumping NVDA/MU/AVGO in with MSFT/CRM (both "Technology") hides the one rotation
that matters most here, and GICS has no bucket at all for the crypto proxies and
retail-speculative names that make up a real share of this tape. So the map is
GICS-shaped with the buckets this universe actually trades split out — Semis,
Biotech, Crypto, China/EM, Speculative, Rates, Metals.

Four deliberate departures, worth knowing when reading a rollup:
  - AVGO / QCOM / TXN sit in Semis, not Megacap Tech: their flow tracks the SOX
    far more closely than it tracks enterprise software.
  - "Megacap Tech" is XLK's actual composition — AAPL and MSFT beside the
    enterprise-software names — rather than a pure software bucket, because
    AAPL is neither a semi nor software and trades with that cohort.
  - TSLA is in Autos & EV rather than Consumer Discretionary. By GICS it is
    discretionary; by flow it is its own weather system and would swamp XLY.
  - HOOD and XYZ/SQ sit in Crypto, not Financials — their option flow tracks
    crypto beta. This follows the repo's own grouping in tickers.py.
  - AMGN / GILD stay in Healthcare while MRNA / BIIB / REGN / VRTX go to
    Biotech. The split is "trades on trial-result binaries" vs "trades on
    earnings", not the IBB membership list.

Index, leveraged-index and volatility products map to CONTEXT_GROUPS instead.
They are not a sector — SPY flow is the tape itself and VXX flow is a hedge on
it — so they are reported as context beside the sector table, never ranked in it.
"""

# ── Sector definitions ────────────────────────────────────────────────────────
# name -> (representative ETF or None, risk posture)
#
# `posture` drives the risk-on/risk-off gauge:
#   "cyclical"  — growth, high-beta, economically sensitive
#   "defensive" — bought for safety, income, or as a hedge
# It describes what buying *the sector* usually expresses, not what any single
# print means.
SECTOR_META = {
    "Semis & AI Infra":    ("SMH",  "cyclical"),
    "Megacap Tech":        ("XLK",  "cyclical"),
    "Comm & Internet":     ("XLC",  "cyclical"),
    "Consumer Disc.":      ("XLY",  "cyclical"),
    "Consumer Staples":    ("XLP",  "defensive"),
    "Financials":          ("XLF",  "cyclical"),
    "Energy":              ("XLE",  "cyclical"),
    "Healthcare":          ("XLV",  "defensive"),
    "Biotech":             ("XBI",  "cyclical"),
    "Industrials":         ("XLI",  "cyclical"),
    "Transports & Travel": ("JETS", "cyclical"),
    "Autos & EV":          (None,   "cyclical"),
    "Clean Energy":        ("TAN",  "cyclical"),
    "Materials & Miners":  ("XLB",  "cyclical"),
    "Crypto":              ("IBIT", "cyclical"),
    "China & EM":          ("FXI",  "cyclical"),
    "Speculative":         (None,   "cyclical"),
    "Real Estate":         ("XLRE", "defensive"),
    "Utilities":           ("XLU",  "defensive"),
    "Rates & Credit":      ("TLT",  "defensive"),
    "Precious Metals":     ("GLD",  "defensive"),
}

# Context groups — reported beside the sector table, never ranked inside it.
CONTEXT_GROUPS = ("Broad Market", "Volatility")

# ── Ticker -> group ───────────────────────────────────────────────────────────
# Sector ETFs are listed under their own sector so ETF-vs-constituent divergence
# can be measured, but they are excluded from the constituent aggregate via
# SECTOR_PROXIES below.
_MEMBERS = {
    "Semis & AI Infra": [
        "NVDA", "AMD", "INTC", "MU", "TSM", "ASML", "SMCI", "MRVL", "AMAT",
        "LRCX", "KLAC", "ARM", "ANET", "VRT", "DELL", "HPE", "ON", "ADI",
        "WDC", "STX", "NXPI", "MCHP", "GFS", "AVGO", "QCOM", "TXN",
        "SMH", "SOXX", "SOXL", "SOXS", "NVDL",
    ],
    "Megacap Tech": [
        "AAPL", "MSFT", "ORCL", "ADBE", "CRM", "IBM", "CSCO", "CRWD", "PANW", "SNOW",
        "NET", "DDOG", "MDB", "ZS", "PLTR", "NOW", "U", "TWLO", "DOCU", "ZM",
        "XLK",
    ],
    "Comm & Internet": [
        "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "SPOT", "SNAP",
        "PINS", "RDDT", "TTD", "ROKU", "T", "VZ", "TMUS",
        "XLC",
    ],
    "Consumer Disc.": [
        "AMZN", "HD", "LOW", "NKE", "SBUX", "MCD", "TGT", "LULU", "CMG",
        "ELF", "CELH", "CVNA", "ULTA", "ABNB", "UBER", "LYFT", "DASH",
        "DKNG", "RBLX", "SHOP",
        "XLY", "XRT", "XHB", "ITB",
    ],
    "Consumer Staples": ["COST", "WMT", "DG", "DLTR", "XLP"],
    "Financials": [
        "JPM", "BAC", "GS", "MS", "C", "WFC", "BLK", "SCHW", "COF", "USB",
        "AXP", "V", "MA", "PYPL", "SOFI", "ALLY", "DFS", "AFRM",
        "XLF", "KRE", "KBE", "FAS", "FAZ",
    ],
    "Energy": [
        "XOM", "CVX", "OXY", "COP", "SLB", "MPC", "VLO", "PSX", "DVN",
        "HAL", "FANG", "KMI", "WMB", "ET",
        "XLE", "XOP", "OIH", "USO", "UNG",
    ],
    "Healthcare": [
        "UNH", "JNJ", "PFE", "ABBV", "LLY", "GILD", "AMGN", "BMY", "CVS",
        "MRK", "TMO", "ISRG", "NVO", "HIMS",
        "XLV",
    ],
    "Biotech": ["MRNA", "BIIB", "REGN", "VRTX", "XBI", "IBB", "ARKG", "LABU", "LABD"],
    "Industrials": [
        "CAT", "BA", "GE", "RTX", "LMT", "DE", "UPS", "FDX", "HON", "MMM",
        "EMR", "GEV", "XLI",
    ],
    "Transports & Travel": ["DAL", "AAL", "UAL", "LUV", "CCL", "NCLH", "RCL", "JETS"],
    "Autos & EV": ["TSLA", "F", "GM", "RIVN", "NIO", "LCID", "TSLL", "TSLQ"],
    "Clean Energy": ["ENPH", "FSLR", "PLUG", "RUN", "CHPT", "TAN", "LIT", "URA"],
    "Materials & Miners": ["FCX", "NEM", "CLF", "X", "AA", "NUE", "MOS", "CF", "XLB", "XME"],
    "Crypto": [
        "COIN", "MSTR", "HOOD", "MARA", "RIOT", "CLSK", "WULF", "BITF",
        "XYZ", "SQ", "IBIT", "BITO", "ETHU",
    ],
    "China & EM": [
        "BABA", "PDD", "JD", "MELI", "FXI", "KWEB", "ASHR", "INDA", "EEM",
        "EFA", "EWZ", "EWJ", "YINN",
    ],
    "Speculative": [
        "GME", "AMC", "DJT", "SOUN", "IONQ", "RGTI", "BBAI", "LUNR", "RKLB",
        "ASTS", "AI", "NKLA", "TLRY", "CGC", "ARKK",
    ],
    "Real Estate": ["AMT", "PLD", "SPG", "O", "XLRE"],
    "Utilities": ["XLU"],
    "Rates & Credit": ["TLT", "IEF", "HYG", "LQD", "SHY", "TMF", "TBT", "UUP"],
    "Precious Metals": ["GLD", "SLV", "GDX", "GDXJ"],
    # ── Context groups (never ranked as sectors) ──────────────────────────────
    "Broad Market": [
        "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "MDY", "RSP",
        "TQQQ", "SQQQ", "TNA", "TZA", "SPXL", "SPXU", "SPXS", "UPRO", "SDOW",
    ],
    "Volatility": ["VXX", "UVXY", "SVXY", "VIXY"],
}

TICKER_SECTOR = {
    ticker: sector
    for sector, tickers in _MEMBERS.items()
    for ticker in tickers
}

# Sector -> its representative ETF, for the ETF-vs-constituents divergence read.
SECTOR_ETF = {name: etf for name, (etf, _) in SECTOR_META.items() if etf}

# Tickers that stand in for a whole sector rather than a single company. They
# are excluded from the constituent aggregate so that "Semis is call-heavy"
# means the chips are, not that someone bought SMH calls. Inverse products are
# excluded for a second reason: an SOXS put is a *bullish* bet, so counting it
# on the put side would invert the read.
SECTOR_PROXIES = frozenset(SECTOR_ETF.values()) | frozenset([
    "SOXX", "SOXL", "SOXS", "NVDL", "XRT", "XHB", "ITB", "KRE", "KBE",
    "FAS", "FAZ", "XOP", "OIH", "USO", "UNG", "IBB", "LABU", "LABD",
    "ARKG", "ARKK", "LIT", "URA", "XME", "TSLL", "TSLQ", "BITO", "ETHU",
    "KWEB", "ASHR", "INDA", "EEM", "EFA", "EWZ", "EWJ", "YINN",
    "IEF", "HYG", "LQD", "SHY", "TMF", "TBT", "UUP", "SLV", "GDX", "GDXJ",
])

CYCLICAL  = tuple(n for n, (_, p) in SECTOR_META.items() if p == "cyclical")
DEFENSIVE = tuple(n for n, (_, p) in SECTOR_META.items() if p == "defensive")


def sector_of(ticker):
    """Group name for a ticker (sector or context group), or None if unmapped."""
    return TICKER_SECTOR.get((ticker or "").upper())


def is_proxy(ticker):
    """True when the ticker represents its sector rather than a single company."""
    return (ticker or "").upper() in SECTOR_PROXIES


def unmapped(universe):
    """Universe tickers with no group — used by the coverage check in tests."""
    return sorted(t for t in universe if t.upper() not in TICKER_SECTOR)
