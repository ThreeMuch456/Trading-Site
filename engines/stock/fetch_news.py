"""
Recent headlines for a single ticker via yfinance `.news`.

yfinance has shipped two different `.news` shapes over time:
  * legacy: flat dicts with title/publisher/link/providerPublishTime
  * current: nested under a "content" key with pubDate / provider / canonicalUrl
This normalizes both into a consistent list, newest first.
"""

from datetime import datetime, timezone

import yfinance as yf


def _parse_epoch(ts) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = datetime.now(timezone.utc) - dt
    secs = delta.total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _normalize(item: dict) -> dict | None:
    # Current nested shape
    if "content" in item and isinstance(item["content"], dict):
        c = item["content"]
        dt = _parse_iso(c.get("pubDate") or c.get("displayTime"))
        url = ""
        if isinstance(c.get("canonicalUrl"), dict):
            url = c["canonicalUrl"].get("url", "")
        elif isinstance(c.get("clickThroughUrl"), dict):
            url = c["clickThroughUrl"].get("url", "")
        provider = ""
        if isinstance(c.get("provider"), dict):
            provider = c["provider"].get("displayName", "")
        title = c.get("title")
        if not title:
            return None
        return {
            "title": title,
            "publisher": provider,
            "link": url,
            "summary": c.get("summary") or c.get("description") or "",
            "published_iso": dt.isoformat() if dt else None,
            "published_date": dt.strftime("%Y-%m-%d") if dt else None,
            "time_ago": _time_ago(dt),
            "type": c.get("contentType", ""),
        }

    # Legacy flat shape
    title = item.get("title")
    if not title:
        return None
    dt = _parse_epoch(item.get("providerPublishTime"))
    return {
        "title": title,
        "publisher": item.get("publisher", ""),
        "link": item.get("link", ""),
        "summary": item.get("summary", ""),
        "published_iso": dt.isoformat() if dt else None,
        "published_date": dt.strftime("%Y-%m-%d") if dt else None,
        "time_ago": _time_ago(dt),
        "type": item.get("type", ""),
    }


def fetch_news(symbol: str, limit: int = 12) -> list[dict]:
    try:
        raw = yf.Ticker(symbol.upper()).news or []
    except Exception:
        return []

    items = []
    for it in raw:
        norm = _normalize(it)
        if norm:
            items.append(norm)

    # Newest first (items with a date sort ahead of undated ones)
    items.sort(key=lambda x: x["published_iso"] or "", reverse=True)
    return items[:limit]


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_news("NVDA"), indent=2))
