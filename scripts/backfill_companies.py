"""Bring rows stored before the companies feature up to date (safe to re-run).

1. Resolve google.com/goto links in stored listings to the real page.
2. Fill in missing states from the location or title ("Ibadan" -> Oyo).
3. Build companies from every listing that isn't rejected, and load the seed list.
4. Forget known SIWES company-list pages, so the next run reads them for companies.

Reject bad listings first (scripts/reject_placement.py), so they don't become companies.
"""

import json

from _common import database_url

from collectors.search import resolve_redirect, source_for
from core.companies import company_from_placement, drop_evidence, load_seeds
from core.db import PgStore, lagos_today
from core.extract import state_from_text
from core.schema import Company, Placement

# Pages that list companies taking SIWES students; marked seen before company lists were read.
DIRECTORY_URL_PATTERNS = ["%siwesfinder.%", "%profolio.ng/intern%", "%monoed.africa/blog%"]


def main() -> int:
    store = PgStore(database_url())
    today = lagos_today()
    try:
        with store._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM placements WHERE status <> 'rejected'").fetchall()
        fixed_links = fixed_states = 0
        for row in rows:
            p = Placement.model_validate(row)
            changes = {}
            sources = []
            for s in p.sources:
                url = resolve_redirect(s.url)
                sources.append(s if url in (None, s.url) else s.model_copy(update={"url": url, "source": source_for(url)}))
            if sources != p.sources:
                changes["sources"] = sources
                fixed_links += 1
            if not p.state:
                state = state_from_text(p.location) or state_from_text(p.title)
                if state:
                    changes["state"] = state
                    fixed_states += 1
            if changes:
                p = p.model_copy(update=changes)
                with store._pool.connection() as conn:
                    conn.execute(
                        "UPDATE placements SET sources = %s, source_urls = %s, state = %s WHERE id = %s",
                        (json.dumps([s.model_dump() for s in p.sources]), [s.url for s in p.sources], p.state, p.id),
                    )
        print(f"links resolved: {fixed_links}, states filled: {fixed_states}")

        for seed in load_seeds():
            store.upsert_company(seed)
        with store._pool.connection() as conn:   # re-read: states and links were just fixed
            rows = conn.execute("SELECT * FROM placements WHERE status <> 'rejected'").fetchall()
        outcomes: dict[str, int] = {}
        for row in rows:
            company = company_from_placement(Placement.model_validate(row), today)
            if company:
                outcome = store.upsert_company(company)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
        print("companies from listings:", outcomes)

        # Listings flagged as asking students to pay are not evidence (added after the first backfill).
        flagged = {u for r in rows if r["scam_warning"] for u in r["source_urls"]}
        with store._pool.connection() as conn:
            stale = conn.execute(
                "SELECT * FROM companies WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) e "
                "WHERE e->>'url' = ANY(%s))", (sorted(flagged),),
            ).fetchall()
            for r in stale:
                c = drop_evidence(Company.model_validate(r), flagged)
                conn.execute("UPDATE companies SET evidence = %s WHERE id = %s",
                             (json.dumps([e.model_dump(mode="json") for e in c.evidence]), c.id))
        print("companies cleared of scam-flagged evidence:", len(stale))

        with store._pool.connection() as conn:
            forgotten = conn.execute(
                "DELETE FROM seen_urls WHERE url LIKE ANY(%s)", (DIRECTORY_URL_PATTERNS,)
            ).rowcount
        print("company-list pages to read again:", forgotten)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
