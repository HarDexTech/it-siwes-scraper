"""X (Twitter) keyword search through a saved, logged-in Chrome profile.

Adapted from the Need-Finder collector. Automated access violates X's terms of
service: throwaway account only. NEVER import this from app/ - only
scripts/run_x.py may run it, behind the 48-hour rule.

Changes from the Need-Finder version: returns RawItem, keeps the poster's handle
and any @mentions (they're the contact on "DM me for SIWES" posts), and scrolls
3 times per query instead of 25.
"""

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.schema import RawItem

logger = logging.getLogger(__name__)

_ARTICLE = 'article[data-testid="tweet"]'
_STATUS_LINK = 'a[href*="/status/"]'
_TWEET_TEXT = '[data-testid="tweetText"]'
_LOGIN_INPUT = 'input[autocomplete="username"]'
_EMPTY_STATE = '[data-testid="emptyState"]'
_X_HOST = re.compile(r"^https?://(?:www\.)?x\.com", re.IGNORECASE)


def _as_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def launch_chrome(pw: Any, profile_dir: str, headless: bool) -> Any:
    """Launch the installed Chrome over the given persistent profile.

    Real Chrome (channel="chrome") with the automation flag dropped - bundled
    Chromium with default flags gets a degraded/broken X login experience.
    Headless Chrome also announces itself as "HeadlessChrome" in the user
    agent, which X rejects at the HTTP level, so the UA is normalized to the
    installed version.
    """
    probe = pw.chromium.launch(channel="chrome", headless=True)
    version = probe.version
    probe.close()
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )
    return pw.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        channel="chrome",
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
        user_agent=user_agent,
        locale="en-NG",
        timezone_id="Africa/Lagos",
    )


def _parse_status_href(href: str) -> tuple[str, str] | None:
    """Return (post_id, handle) from a /<handle>/status/<id> permalink."""
    path = _X_HOST.sub("", (href or "").split("?")[0]).rstrip("/")
    parts = path.split("/")
    if len(parts) < 4 or parts[-2] != "status" or not parts[-1].isdigit():
        return None
    handle = parts[-3]
    if handle in ("i", "web", "intent"):
        return None
    return parts[-1], handle


class XBrowserCollector:
    def __init__(
        self,
        queries: list[str],
        max_scrolls: int = 3,
        profile_dir: str | Path = "data/browser-profiles/x",
        headless: bool = True,
        delay_seconds: float = 4.0,
        page: Any | None = None,
        debug_dir: str | Path = "data/x-debug",
    ) -> None:
        self._queries = queries
        self._max_scrolls = max_scrolls
        self._profile_dir = Path(profile_dir)
        self._headless = headless
        self._delay = delay_seconds
        self._page = page
        self._debug_dir = Path(debug_dir)

    def collect(self) -> list[RawItem]:
        if self._page is not None:
            return self._collect_page(self._page)
        from playwright.sync_api import sync_playwright

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        ctx = launch_chrome(pw, str(self._profile_dir), self._headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return self._collect_page(page)
        finally:
            ctx.close()
            pw.stop()

    def _login_wall(self, page: Any) -> bool:
        url = page.url
        # A stale/mismatched session lands on /i/jf/onboarding or ?mode=login
        # instead of /flow/login, so match all of them.
        return (
            "/flow/login" in url
            or "/i/jf/onboarding" in url
            or "mode=login" in url
            or page.locator(_LOGIN_INPUT).count() > 0
        )

    def _fail(self, page: Any, message: str) -> RuntimeError:
        """Snapshot what X actually served, so a failed run can be diagnosed."""
        base = self._debug_dir / f"x-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=f"{base}.png", full_page=True)
            Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            logger.warning("x: could not save failure snapshot", exc_info=True)
        return RuntimeError(f"{message} (url: {page.url}; snapshot: {base}.png/.html)")

    def _collect_page(self, page: Any) -> list[RawItem]:
        items: list[RawItem] = []
        for query in self._queries:
            items.extend(self._search(page, query))
            if self._delay:
                time.sleep(self._delay)
        return items

    def _search(self, page: Any, query: str) -> list[RawItem]:
        page.goto(
            f"https://x.com/search?q={quote(query)}&src=typed_query&f=live",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(4_000)
        if self._login_wall(page):
            raise self._fail(page, "X login required: run scripts/login_social.py x (throwaway account)")
        articles = page.locator(_ARTICLE)
        # Logged-out/blocked requests get a blank shell with no login form; a real
        # results page (even "No results") always has far more chrome than this.
        if articles.count() == 0 and len(page.locator("body").inner_text(timeout=5_000).strip()) < 200:
            raise self._fail(
                page, "X search came back nearly blank - session logged out or blocked, or the DOM changed"
            )
        # A restricted account gets the full page frame but an empty timeline. A
        # genuine "No results" shows X's empty state; anything else stops the run.
        if articles.count() == 0 and page.locator(_EMPTY_STATE).count() == 0:
            page.wait_for_timeout(6_000)
            if articles.count() == 0 and page.locator(_EMPTY_STATE).count() == 0:
                raise self._fail(
                    page,
                    "X search loaded but showed no tweets - search restricted for this account"
                    " (rest it 48-72h+), or the DOM changed",
                )
        found: list[RawItem] = []
        seen: set[str] = set()
        for round_no in range(self._max_scrolls):
            for i in range(articles.count()):
                try:
                    item = self._extract(articles.nth(i), query)
                except Exception:
                    logger.warning("x: skipped an article that failed to parse", exc_info=True)
                    continue
                if item is None or item.url in seen:
                    continue
                seen.add(item.url)
                found.append(item)
            if round_no == self._max_scrolls - 1:
                break
            before = articles.count()
            page.mouse.wheel(0, 4_800)
            page.wait_for_timeout(2_000)
            if self._delay:
                time.sleep(self._delay)
            if articles.count() == before:
                page.wait_for_timeout(2_500)
                if articles.count() == before:
                    break
        return found

    def _extract(self, article: Any, query: str) -> RawItem | None:
        link = article.locator(_STATUS_LINK).first
        if link.count() == 0:
            return None
        parsed = _parse_status_href(link.get_attribute("href") or "")
        if parsed is None:
            return None
        post_id, handle = parsed
        text_el = article.locator(_TWEET_TEXT).first
        if text_el.count() == 0:
            return None
        text = re.sub(r"\s+", " ", text_el.inner_text()).strip()
        if not text:
            return None
        posted_at = None
        time_el = article.locator("time").first
        if time_el.count() > 0:
            posted_at = _as_utc(time_el.get_attribute("datetime") or "")
        return RawItem(
            source="x",
            url=f"https://x.com/{handle}/status/{post_id}",
            title=f"@{handle} on X",
            text=text[:4000],
            posted_at=posted_at,
            author_handle=handle,
            query=query,
        )
