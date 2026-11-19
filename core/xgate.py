"""Local backup for the X 48-hour rule.

The database row in collector_runs is the real gate. This file only matters
if the database was wiped or replaced: it still stops X running early.
"""

from datetime import datetime, timedelta
from pathlib import Path

from .db import X_MIN_GAP


def local_next_allowed(path: Path, now: datetime, gap: timedelta = X_MIN_GAP) -> datetime | None:
    """When X may next run according to the local file, or None if it may run now."""
    try:
        last = datetime.fromisoformat(path.read_text("utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    next_allowed = last + gap
    return next_allowed if now < next_allowed else None


def record_local_start(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when.isoformat(), encoding="utf-8")
