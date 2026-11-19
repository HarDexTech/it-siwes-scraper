"""X collection run - the ONLY way X is ever scraped.

Runs at most once every 48 hours, counted from the last *start* (a failed or
restricted run still counts). Windows Task Scheduler calls this daily; most
days it just logs "X skipped" and exits.

Usage:
    python scripts/run_x.py            # normal (scheduled) run
    python scripts/run_x.py --show     # watch the browser while it runs
    python scripts/run_x.py --force    # ignore the 48h rule (debugging only; logged)
"""

import argparse
import logging
from datetime import datetime, timezone

from _common import ROOT, database_url, setup_logging, sources_config

from collectors.runner import run_pipeline
from collectors.x_browser import XBrowserCollector
from core.db import PgStore
from core.llm_client import LLMClient
from core.xgate import local_next_allowed, record_local_start

logger = logging.getLogger("run_x")
LOCAL_STAMP = ROOT / "data" / "x_last_start.txt"


def build_queries(requested: list[str], general: list[str], limit: int) -> list[str]:
    queries = [f'SIWES "{field}"' for field in requested[: limit // 2]]
    return (queries + general)[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore the 48h rule (debugging only)")
    parser.add_argument("--show", action="store_true", help="run Chrome with a visible window")
    args = parser.parse_args()
    setup_logging("x.log")
    cfg = sources_config()["x"]
    now = datetime.now(timezone.utc)

    if args.force:
        logger.warning("--force: ignoring the 48-hour rule. This raises the ban risk on the X account.")
    else:
        local_block = local_next_allowed(LOCAL_STAMP, now)
        if local_block:
            logger.info("X skipped: next run allowed at %s (local record)", local_block.isoformat())
            return 0

    dsn = database_url()
    if not dsn:
        logger.info("X skipped: DATABASE_URL is not set")
        return 0
    try:
        store = PgStore(dsn)
        gate = store.try_start_x_run(forced=args.force)
    except Exception as exc:
        # No database means no reliable 48h check: don't risk the account.
        logger.warning("X skipped: database unreachable (%s)", exc)
        return 0
    if not gate.allowed:
        logger.info("X skipped: next run allowed at %s", gate.next_allowed.isoformat())
        store.close()
        return 0
    record_local_start(LOCAL_STAMP, now)

    try:
        queries = build_queries(store.top_requested(cfg["max_queries"]), cfg["general_queries"], cfg["max_queries"])
        logger.info("X run %s starting with %d queries: %s", gate.run_id, len(queries), queries)
        collector = XBrowserCollector(
            queries, max_scrolls=cfg["max_scrolls"], delay_seconds=cfg["delay_seconds"],
            profile_dir=ROOT / "data" / "browser-profiles" / "x", debug_dir=ROOT / "data" / "x-debug",
            headless=not args.show,
        )
        counts = run_pipeline(store, LLMClient(), {"x": collector.collect})
        error = counts["errors"].get("x")
        store.finish_run(gate.run_id, "failed" if error else "ok", counts, error)
        if error:
            logger.error("X run failed: %s", error)
            return 1
        return 0
    except Exception as exc:
        logger.exception("X run failed")
        store.finish_run(gate.run_id, "failed", {}, str(exc)[:1000])
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
