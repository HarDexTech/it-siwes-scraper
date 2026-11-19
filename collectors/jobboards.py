"""Indeed Nigeria through JobSpy. A bonus source: coverage is thin and it can be blocked."""

import logging
from datetime import datetime, timezone

from core.schema import RawItem

logger = logging.getLogger(__name__)


def indeed_items(search_terms: list[str], results_wanted: int = 15, scraper=None) -> list[RawItem]:
    if scraper is None:
        from jobspy import scrape_jobs as scraper

    items: list[RawItem] = []
    for term in search_terms:
        try:
            jobs = scraper(
                site_name=["indeed"],
                search_term=term,
                location="Nigeria",
                country_indeed="Nigeria",
                results_wanted=results_wanted,
                hours_old=24 * 45,
            )
        except Exception:
            logger.warning("jobspy failed for %r", term, exc_info=True)
            continue
        for job in jobs.to_dict("records"):
            url = job.get("job_url")
            if not url:
                continue
            posted = job.get("date_posted")
            posted_at = (
                datetime(posted.year, posted.month, posted.day, tzinfo=timezone.utc)
                if hasattr(posted, "year")
                else None
            )
            company = job.get("company") or ""
            location = job.get("location") or ""
            items.append(
                RawItem(
                    source="indeed",
                    url=url,
                    title=f"{job.get('title') or ''} - {company}".strip(" -"),
                    text=f"{company} | {location}\n{job.get('description') or ''}",
                    posted_at=posted_at,
                    query=term,
                )
            )
    return items
