"""Trade-print classification: which side was the customer on?

Priority order:
1. Print at/above the ask -> customer buy. At/below the bid -> customer sell.
2. Within MID_ZONE_FRAC of the spread around the midpoint -> unclassified.
   We do not guess; those contracts sit in a separate bucket.
3. No NBBO available -> tick test against the prior print at that contract,
   and everything built from it is flagged low-confidence.
"""
from .. import config


def classify_print(price: float, bid: float | None, ask: float | None,
                   prev_price: float | None) -> tuple[str, bool, bool | None]:
    """Returns (side, low_conf, at_ask).
    side in {'buy','sell','unclassified'};
    at_ask True/False for classified prints (None otherwise)."""
    if bid is not None and ask is not None and ask >= bid > 0:
        mid = (bid + ask) / 2.0
        half = (ask - bid) / 2.0
        if price >= ask:
            return "buy", False, True
        if price <= bid:
            return "sell", False, False
        if half > 0 and abs(price - mid) <= config.MID_ZONE_FRAC * half:
            return "unclassified", False, None
        # Between the mid-zone and the quote: lean on quote side.
        if price > mid:
            return "buy", False, True
        if price < mid:
            return "sell", False, False
        return "unclassified", False, None

    # No NBBO — tick test, low confidence.
    if prev_price is not None:
        if price > prev_price:
            return "buy", True, True
        if price < prev_price:
            return "sell", True, False
    return "unclassified", True, None
