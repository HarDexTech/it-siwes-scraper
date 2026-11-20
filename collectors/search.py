"""Web search: Serper (Google results) when a key is set, ddgs (DuckDuckGo) otherwise.

Serper's free credits are a one-time 2,500 queries; once it fails, the rest of
the run uses ddgs.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from core.schema import RawItem

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_HOST_SOURCES = {
    "linkedin.com": "linkedin",
    "x.com": "x",
    "twitter.com": "x",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "t.me": "telegram",
    "nairaland.com": "nairaland",
    "myjobmag.com": "jobsite",
    "jobberman.com": "jobsite",
    "hotnigerianjobs.com": "jobsite",
    "ngcareers.com": "jobsite",
}
_RELATIVE = re.compile(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", re.IGNORECASE)


def source_for(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    for domain, source in _HOST_SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return source
    return "web"


_GOOGLE_REDIRECT = re.compile(r"^https?://(?:www\.)?google\.[a-z.]+/(?:goto|url)\?", re.IGNORECASE)


def resolve_redirect(url: str, getter=None) -> str | None:
    """Google sometimes hands back google.com/goto?url=<token> links. Follow them to the
    real page so the card can name the site and dedupe works; None if that fails."""
    if not _GOOGLE_REDIRECT.match(url):
        return url
    import httpx

    get = getter or httpx.get
    try:
        response = get(url, follow_redirects=True, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        final = str(response.url)
    except Exception:  # noqa: BLE001
        logger.info("could not resolve %s", url[:80])
        return None
    return None if _GOOGLE_REDIRECT.match(final) or "google." in (urlsplit(final).hostname or "") else final


def _parse_date(value: str | None, now: datetime) -> datetime | None:
    """Serper gives "3 days ago" or "Nov 2, 2026"."""
    if not value:
        return None
    match = _RELATIVE.search(value)
    if match:
        n, unit = int(match.group(1)), match.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return now - timedelta(days=days)
    for fmt in ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class WebSearch:
    def __init__(self, serper_key: str | None = None, max_results: int = 10, getter=None, ddgs_factory=None,
                 redirect_getter=None):
        self._serper_key = serper_key if serper_key is not None else os.environ.get("SERPER_API_KEY")
        self._max = max_results
        self._getter = getter
        self._ddgs_factory = ddgs_factory
        self._redirect_getter = redirect_getter
        self.queries_used = 0

    def search(self, query: str, *, recent: bool = True) -> list[RawItem]:
        """``recent`` limits results to the last few months: right for adverts, wrong for
        company sites, business directories and old SIWES reports."""
        self.queries_used += 1
        items: list[RawItem] = []
        if self._serper_key:
            try:
                items = self._serper(query, recent)
            except Exception as exc:  # noqa: BLE001
                logger.warning("serper failed (%s); using ddgs for the rest of the run", exc)
                self._serper_key = None
        if not self._serper_key:
            try:
                items = self._ddgs(query, recent)
            except Exception:
                logger.warning("ddgs failed for %r", query, exc_info=True)
        return [it for it in map(self._resolved, items) if it]

    def _resolved(self, item: RawItem) -> RawItem | None:
        url = resolve_redirect(item.url, self._redirect_getter)
        if url is None:
            return None
        return item if url == item.url else item.model_copy(update={"url": url, "source": source_for(url)})

    def _serper(self, query: str, recent: bool = True) -> list[RawItem]:
        import httpx

        post = self._getter or httpx.post
        response = post(
            _SERPER_URL,
            headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "ng", "hl": "en", "num": self._max, **({"tbs": "qdr:m3"} if recent else {})},
            timeout=20,
        )
        response.raise_for_status()
        now = datetime.now(timezone.utc)
        return [
            RawItem(
                source=source_for(r["link"]),
                url=r["link"],
                title=r.get("title", ""),
                text=r.get("snippet", ""),
                posted_at=_parse_date(r.get("date"), now),
                query=query,
            )
            for r in response.json().get("organic", [])
            if r.get("link")
        ]

    def _ddgs(self, query: str, recent: bool = True) -> list[RawItem]:
        if self._ddgs_factory:
            client = self._ddgs_factory()
        else:
            from ddgs import DDGS

            client = DDGS()
        results = client.text(query, region="ng-en", timelimit="m" if recent else None, max_results=self._max) or []
        return [
            RawItem(source=source_for(r["href"]), url=r["href"], title=r.get("title", ""),
                    text=r.get("body", ""), query=query)
            for r in results
            if r.get("href")
        ]
