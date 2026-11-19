"""Fetch a page's readable text with Scrapling.

``Fetcher`` (HTTP with Chrome's TLS fingerprint) first; if the site serves a
block or challenge page and ``STEALTHY_FETCH=1``, retry once with
``StealthyFetcher`` (a real browser, needs ``scrapling install``).
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

MAX_CHARS = 4000
_IGNORE = ("script", "style", "noscript", "nav", "footer", "header", "svg", "form")
_BLOCKED = re.compile(r"just a moment|attention required|verify you are human|enable javascript", re.I)


def _readable(page) -> str:
    text = str(page.get_all_text(separator="\n", strip=True, ignore_tags=_IGNORE))
    lines = [line for line in (l.strip() for l in text.splitlines()) if len(line) > 2]
    return re.sub(r"\n{2,}", "\n", "\n".join(lines))[:MAX_CHARS]


def _blocked(page) -> bool:
    status = getattr(page, "status", 200) or 200
    if status in (403, 429, 503):
        return True
    return status < 400 and bool(_BLOCKED.search(str(page.get_all_text()[:2000])))


def fetch_text(url: str) -> str | None:
    from scrapling.fetchers import Fetcher

    try:
        page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.info("fetch failed %s: %s", url, exc)
        return None
    if _blocked(page) and os.environ.get("STEALTHY_FETCH") == "1":
        try:
            from scrapling.fetchers import StealthyFetcher

            page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45_000)
        except Exception as exc:  # noqa: BLE001
            logger.info("stealthy fetch failed %s: %s", url, exc)
            return None
    if (getattr(page, "status", 200) or 200) >= 400 or _blocked(page):
        return None
    return _readable(page) or None
