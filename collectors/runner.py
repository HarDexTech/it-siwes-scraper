"""collect -> dedupe -> skip seen -> keyword filter -> fetch -> LLM -> store -> expire.

Unlike the Need-Finder runner there is no PII stripping: phone numbers, emails,
handles and links are how students apply to these placements.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.companies import company_from_placement
from core.dedupe import normalize_url
from core.extract import LLM, extract, passes_prefilter
from core.schema import RawItem

logger = logging.getLogger(__name__)

# Sources whose pages are worth fetching. Social posts sit behind logins, so
# their search snippet (or the scraped post text) is all we use.
FETCH_SOURCES = {"web", "jobsite"}
LAGOS = timezone(timedelta(hours=1))


def run_pipeline(
    store: Any,
    llm: LLM,
    collectors: dict[str, Callable[[], list[RawItem]]],
    *,
    fetch_text: Callable[[str], str | None] | None = None,
    fields_hint: list[str] | None = None,
    max_llm_calls: int = 25,
    batch_size: int = 5,
    today: date | None = None,
) -> dict[str, Any]:
    counts: dict[str, Any] = {"collected": 0, "new": 0, "prefiltered": 0, "fetched": 0, "llm_calls": 0,
                              "inserted": 0, "merged": 0, "expired": 0, "errors": {}}
    items: dict[str, RawItem] = {}
    for name, collect in collectors.items():
        try:
            found = collect()
        except Exception as exc:  # one broken collector must not stop the others
            logger.exception("collector %s failed", name)
            counts["errors"][name] = str(exc)[:300]
            continue
        counts[f"from_{name}"] = len(found)
        counts["collected"] += len(found)
        for item in found:
            url = normalize_url(item.url)
            if url not in items or item.purpose == "discovery":
                items[url] = item.model_copy(update={"url": url})

    # Discovery pages (company directories, company sites, SIWES reports) never mention
    # SIWES the way adverts do, and one page can serve several fields, so they skip both
    # the seen-url skip and the keyword filter; each (field, state) is searched at most every 90 days.
    def discovery(it: RawItem) -> bool:
        return it.purpose == "discovery"

    seen = store.seen([u for u, it in items.items() if not discovery(it)])
    fresh = [it for url, it in items.items() if discovery(it) or url not in seen]
    counts["new"] = len(fresh)

    relevant = [it for it in fresh if discovery(it) or passes_prefilter(it)]
    irrelevant = [it.url for it in fresh if not discovery(it) and not passes_prefilter(it)]
    counts["prefiltered"] = len(relevant)
    store.mark_seen(irrelevant)
    relevant.sort(key=lambda it: not discovery(it))   # first, so the LLM call cap can't starve them

    if fetch_text:
        to_fetch = [it for it in relevant if it.source in FETCH_SOURCES]
        with ThreadPoolExecutor(max_workers=5) as pool:
            texts = list(pool.map(fetch_text, [it.url for it in to_fetch]))
        fetched = {it.url: text for it, text in zip(to_fetch, texts) if text}
        counts["fetched"] = len(fetched)
        relevant = [
            it.model_copy(update={"text": f"{it.text}\n\n{fetched[it.url]}"}) if it.url in fetched else it
            for it in relevant
        ]

    today = today or datetime.now(LAGOS).date()
    result = extract(
        relevant, llm, fields_hint=fields_hint, batch_size=batch_size, max_calls=max_llm_calls, today=today
    )
    counts["llm_calls"] = result.calls
    counts["placements"] = len(result.placements)
    counts["left_for_next_run"] = result.left_over
    store.mark_seen([it.url for it in result.processed if not discovery(it)])
    kept: list = []
    for placement in result.placements:
        outcome = store.upsert_placement(placement)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome != "rejected":
            kept.append(placement)
    # Every named organization that advertised places is a company students can apply to later.
    companies = [c for p in kept if (c := company_from_placement(p, today))] + result.companies
    counts["companies_from_lists"] = len(result.companies)
    for company in companies:
        outcome = store.upsert_company(company)
        counts[f"companies_{outcome}"] = counts.get(f"companies_{outcome}", 0) + 1
    counts["expired"] = store.expire()
    logger.info("pipeline done: %s", counts)
    return counts
