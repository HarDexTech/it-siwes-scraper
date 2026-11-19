"""Background collection run: web search, job sites, Telegram, Indeed, Nairaland.

Never touches X - that's scripts/run_x.py, behind the 48-hour rule.

Usage:
    python scripts/run_collect.py                      # normal run (GitHub Actions, every 6h)
    python scripts/run_collect.py --fields "Computer Science,Remote Sensing and GIS"
    python scripts/run_collect.py --dry-run            # mock LLM + in-memory store, real searches
"""

import argparse
import logging
from datetime import datetime, timezone

from _common import database_url, setup_logging, sources_config

from collectors.jobboards import indeed_items
from collectors.nairaland import NairalandCollector
from collectors.queries import field_queries, general_queries, plan_fields
from collectors.runner import run_pipeline
from collectors.search import WebSearch
from collectors.sites import fetch_text
from collectors.telegram import telegram_items
from core.db import MemoryStore, PgStore
from core.llm_client import LLMClient, MockLLMClient

logger = logging.getLogger("run_collect")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fields", help="comma-separated fields to search instead of the queue/rotation")
    parser.add_argument("--dry-run", action="store_true", help="mock LLM and in-memory store")
    args = parser.parse_args()
    setup_logging("collect.log")
    cfg = sources_config()

    dsn = database_url()
    if not args.dry_run and not dsn:
        logger.error("DATABASE_URL is not set (use --dry-run to try without a database)")
        return 2
    store = MemoryStore() if args.dry_run else PgStore(dsn)
    llm = MockLLMClient() if args.dry_run else LLMClient()

    requested = [] if args.fields else store.top_requested(cfg["requested_fields_per_run"])
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    else:
        fields = plan_fields(requested, cfg["search"]["max_queries_per_run"])
    served_requests = [f for f in requested if f in fields]
    logger.info("fields this run: %s (requested: %s)", fields, served_requests)

    run_id = store.start_run("collect")
    search = WebSearch(max_results=cfg["search"]["results_per_query"])
    queries = [q for f in fields for q in field_queries(f)] + general_queries(datetime.now(timezone.utc).year)

    collectors = {
        "search": lambda: [item for q in queries for item in search.search(q)],
        "nairaland": lambda: NairalandCollector(
            boards=cfg["nairaland"]["boards"],
            max_pages_per_board=cfg["nairaland"]["max_pages_per_board"],
            max_threads_per_board=cfg["nairaland"]["max_threads_per_board"],
            delay_seconds=cfg["nairaland"]["delay_seconds"],
        ).collect(),
    }
    if cfg["telegram"]["channels"]:
        collectors["telegram"] = lambda: telegram_items(cfg["telegram"]["channels"])
    if cfg["indeed"]["enabled"]:
        terms = ["SIWES", "industrial training"] + [f"intern {f}" for f in fields[:3]]
        collectors["indeed"] = lambda: indeed_items(terms, cfg["indeed"]["results_per_term"])

    try:
        counts = run_pipeline(
            store, llm, collectors,
            fetch_text=fetch_text,
            fields_hint=fields,
            max_llm_calls=cfg["llm"]["max_calls_per_run"],
            batch_size=cfg["llm"]["batch_size"],
        )
        counts["search_queries"] = search.queries_used
        store.mark_requested_collected(served_requests)
        status = "failed" if counts["errors"] and counts["collected"] == 0 else "ok"
        store.finish_run(run_id, status, counts, "; ".join(f"{k}: {v}" for k, v in counts["errors"].items()) or None)
    except Exception as exc:
        logger.exception("collection run failed")
        store.finish_run(run_id, "failed", {}, str(exc)[:1000])
        return 1
    finally:
        store.close()
    if args.dry_run:
        for p in store.placements:
            print(f"- {p.title} | {p.organization} | {p.state} | {p.sources[0].url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
