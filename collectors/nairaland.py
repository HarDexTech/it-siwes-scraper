"""Nairaland SIWES/IT threads. Adapted from the Need-Finder collector.

Board and thread parsing and the robots.txt handling are unchanged. What
changed: threads are picked by SIWES keywords, only each thread's opening post
is kept (that's the advert; replies are mostly "please is it still available"),
and authors are kept as-is rather than hashed.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from core.schema import RawItem

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nairaland.com"
_USER_AGENT = "SIWESFinderBot/0.1 (student placement search)"
_TIMEOUT = 25.0
_THREAD_HREF = re.compile(r"^/(\d+)/")
_QUOTE = re.compile(r"<blockquote\b[^>]*>.*?</blockquote>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(
    r"\bsiwes\b|industrial\s+training|\bi\.?t\.?\s+(?:placement|students?)|\bintern(?:ship)?s?\b|attachment",
    re.IGNORECASE,
)


def _adapt(html: str, url: str) -> Any:
    from scrapling.parser import Selector

    return Selector(html, url=url)


def _text(node: Any) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_all_text(separator=" ")).strip()


def parse_board(html: str, base_url: str = BASE_URL) -> list[dict[str, str]]:
    """Return the thread links on a board page, in page order, deduped."""
    page = _adapt(html, base_url)
    threads: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in page.css("a"):
        match = _THREAD_HREF.match(link.attrib.get("href", ""))
        if not match:
            continue
        thread_id = match.group(1)
        title = (link.text or "").strip()
        if not title or thread_id in seen:
            continue
        seen.add(thread_id)
        threads.append({"thread_id": thread_id, "title": title, "url": urljoin(base_url, link.attrib["href"])})
    return threads


def parse_opening_post(html: str, thread_url: str) -> RawItem | None:
    page = _adapt(_QUOTE.sub(" ", html), thread_url)
    title = next((_text(h) for h in page.css("h1") if _text(h)), "") or next(
        (_text(h) for h in page.css("h2")), ""
    )
    body = None
    for cell in page.css("td.l.w.pd"):
        if cell.attrib.get("id", "").startswith("pb"):
            narrow = cell.css("div.narrow")
            body = _text(narrow[0] if narrow else cell)
            break
    if not body:
        return None
    posted_at = None
    published = page.css('meta[itemprop="datePublished"]')
    if published:
        try:
            posted_at = datetime.fromisoformat(published[0].attrib.get("content", "").replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
    header = page.css("td.bold.l.pu")
    author = header[0].css("a.user") if header else []
    handle = author[0].attrib.get("href", "").lstrip("/") if author else None
    return RawItem(source="nairaland", url=thread_url, title=title, text=body[:4000],
                   posted_at=posted_at, author_handle=handle or None)


class NairalandCollector:
    def __init__(
        self,
        boards: list[str],
        max_pages_per_board: int = 3,
        max_threads_per_board: int = 10,
        delay_seconds: float = 4.0,
        user_agent: str = _USER_AGENT,
        base_url: str = BASE_URL,
        getter: Callable[[str, float], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._boards = boards
        self._max_pages = max_pages_per_board
        self._max_threads = max_threads_per_board
        self._delay = delay_seconds
        self._user_agent = user_agent
        self._base = base_url.rstrip("/")
        self._getter = getter
        self._sleeper = sleeper
        self._requests = 0

    def _fetch(self, url: str) -> Any:
        if self._requests:
            self._sleeper(self._delay)
        self._requests += 1
        return (self._getter or self._default_getter)(url, _TIMEOUT)

    def _default_getter(self, url: str, timeout: float) -> Any:
        from scrapling.fetchers import Fetcher

        # Nairaland sits behind Cloudflare, which rejects the plain client TLS
        # fingerprint. `Fetcher` with a Chrome fingerprint passes; we keep our
        # own UA and stay within robots.txt.
        return Fetcher.get(
            url, headers={"User-Agent": self._user_agent}, stealthy_headers=False,
            impersonate="chrome", timeout=timeout,
        )

    def _robots(self) -> RobotFileParser | None:
        """Fail closed: None when robots.txt cannot be read."""
        try:
            response = self._fetch(f"{self._base}/robots.txt")
        except Exception:
            logger.warning("could not read nairaland robots.txt; failing closed")
            return None
        if (getattr(response, "status", 200) or 200) >= 400:
            return None
        parser = RobotFileParser()
        parser.parse((getattr(response, "body", b"") or b"").decode("utf-8", "ignore").splitlines())
        return parser

    def _html(self, url: str) -> str | None:
        try:
            response = self._fetch(url)
        except Exception:
            logger.warning("nairaland request failed: %s", url, exc_info=True)
            return None
        if (getattr(response, "status", 200) or 200) >= 400:
            return None
        return getattr(response, "html_content", "") or ""

    def collect(self) -> list[RawItem]:
        robots = self._robots()
        if robots is None:
            logger.error("nairaland robots.txt unavailable; skipping")
            return []
        items: list[RawItem] = []
        seen: set[str] = set()
        for board in self._boards:
            board_url = f"{self._base}/{board}"
            if not robots.can_fetch(self._user_agent, board_url):
                continue
            fetched = 0
            for page_no in range(self._max_pages):
                html = self._html(board_url if page_no == 0 else f"{board_url}/{page_no}")
                if not html:
                    break
                for thread in parse_board(html, self._base):
                    if fetched >= self._max_threads:
                        break
                    if thread["thread_id"] in seen or not _TITLE.search(thread["title"]):
                        continue
                    seen.add(thread["thread_id"])
                    if not robots.can_fetch(self._user_agent, thread["url"]):
                        continue
                    fetched += 1
                    thread_html = self._html(thread["url"])
                    item = parse_opening_post(thread_html, thread["url"]) if thread_html else None
                    if item:
                        items.append(item)
                if fetched >= self._max_threads:
                    break
        return items
