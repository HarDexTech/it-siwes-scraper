"""Public Telegram channels through the t.me/s/<channel> web preview (no login)."""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from core.schema import RawItem

logger = logging.getLogger(__name__)


def _get(url: str) -> Any:
    from scrapling.fetchers import Fetcher

    return Fetcher.get(url, impersonate="chrome", timeout=20)


def parse_channel(page: Any, channel: str) -> list[RawItem]:
    items: list[RawItem] = []
    for message in page.css(".tgme_widget_message_wrap"):
        link = message.css("a.tgme_widget_message_date")
        body = message.css(".tgme_widget_message_text")
        if not link or not body:
            continue
        url = link[0].attrib.get("href", "")
        posted_at = None
        stamp = message.css("time[datetime]")
        if stamp:
            try:
                posted_at = datetime.fromisoformat(stamp[0].attrib["datetime"]).astimezone(timezone.utc)
            except ValueError:
                posted_at = None
        text = str(body[0].get_all_text(separator="\n", strip=True))
        if url and text:
            items.append(
                RawItem(source="telegram", url=url, title=f"t.me/{channel}", text=text[:4000],
                        posted_at=posted_at, author_handle=channel)
            )
    return items


def telegram_items(channels: list[str], getter: Callable[[str], Any] | None = None) -> list[RawItem]:
    getter = getter or _get
    items: list[RawItem] = []
    for channel in channels:
        try:
            page = getter(f"https://t.me/s/{channel}")
        except Exception:
            logger.warning("telegram channel %s failed", channel, exc_info=True)
            continue
        if (getattr(page, "status", 200) or 200) >= 400:
            logger.warning("telegram channel %s returned %s", channel, page.status)
            continue
        items.extend(parse_channel(page, channel))
    return items
