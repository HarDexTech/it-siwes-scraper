"""Hide a bad listing for good (wrong country, not a placement, scam...).

Also stops it counting as evidence for its company. Find the id on /admin/health
or in the database.

Usage:
    python scripts/reject_placement.py 19 23
"""

import sys

from _common import database_url

from core.db import PgStore


def main(ids: list[str]) -> int:
    if not ids:
        print(__doc__)
        return 2
    store = PgStore(database_url())
    try:
        for pid in ids:
            print(pid, "rejected" if store.reject_placement(int(pid)) else "not found")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
