"""Terminal output shim for the vendored OFS scanner.

The standalone scanner printed a Rich-formatted console feed (one line per alert).
Inside the desk that feed is noise and `rich` would be a needless dependency, so the
three functions the scanner imports are reduced to quiet logging — same signatures,
no console, no Rich. Alerts land in the database regardless; this only drops the
cosmetic stdout stream.
"""

import logging

log = logging.getLogger("engines.flow.scanner")


def print_alert(alert) -> None:
    log.debug("alert %s %s %s %s exp %s premium=%s",
              getattr(alert, "ticker", "?"), getattr(alert, "option_type", "?"),
              getattr(alert, "strike", "?"), getattr(alert, "volume_delta", "?"),
              getattr(alert, "expiry", "?"), getattr(alert, "estimated_premium", "?"))


def print_status(msg: str) -> None:
    log.info("%s", msg)


def print_error(msg: str) -> None:
    log.warning("%s", msg)
