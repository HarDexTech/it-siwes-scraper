"""Create the tables (safe to re-run).

Uses DATABASE_URL_UNPOOLED (Neon's direct connection) when set, since schema
changes shouldn't go through the PgBouncer pooler; otherwise DATABASE_URL.
"""

import os

from _common import database_url

from core.db import PgStore


def main() -> int:
    dsn = os.environ.get("DATABASE_URL_UNPOOLED") or database_url()
    if not dsn:
        print("DATABASE_URL is not set")
        return 2
    store = PgStore(dsn)
    store.init_schema()
    store.close()
    print("schema ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
