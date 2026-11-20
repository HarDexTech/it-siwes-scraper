"""Storage for placements, seen urls, requested fields and collector runs.

``PgStore`` talks to Neon Postgres; ``MemoryStore`` has the same methods for
tests and dry runs. All "now"/"today" values in ``PgStore`` come from the
database clock, never the machine running the code.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .companies import drop_evidence, keys, merge_company, should_merge
from .dedupe import merge
from .extract import is_expired
from .schema import Company, Placement

LAGOS = timezone(timedelta(hours=1))
X_MIN_GAP = timedelta(hours=48)
_X_LOCK_KEY = 4_829_311  # pg_advisory lock id for the X gate
_SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
_COLUMNS = (
    "title, organization, location, state, fields, open_to_all, deadline, posted_date, how_to_apply, "
    "contact, apply_link, scam_warning, summary, sources, source_urls, fingerprint, status"
)


@dataclass
class GateResult:
    allowed: bool
    run_id: int | None
    last_start: datetime | None
    next_allowed: datetime | None


def x_gate_decision(
    last_start: datetime | None, now: datetime, gap: timedelta = X_MIN_GAP, forced: bool = False
) -> tuple[bool, datetime | None]:
    """(allowed, next allowed time). Counted from the last *start*, success or not."""
    if last_start is None:
        return True, None
    next_allowed = last_start + gap
    return forced or now >= next_allowed, next_allowed


_COMPANY_COLUMNS = (
    "name, name_key, aliases, fields, open_to_all, states, location, website, contact, how_to_apply, "
    "evidence, origin, status"
)
_COMPANY_LOCK_KEY = 4_829_312  # serializes company merges, so two writers can't both insert


def _company_values(c: Company) -> tuple[Any, ...]:
    return (
        c.name, c.name_key, c.aliases, c.fields, c.open_to_all, c.states, c.location, c.website, c.contact,
        c.how_to_apply, json.dumps([e.model_dump(mode="json") for e in c.evidence]), c.origin, c.status,
    )


def _row_values(p: Placement) -> tuple[Any, ...]:
    return (
        p.title, p.organization, p.location, p.state, p.fields, p.open_to_all, p.deadline,
        p.posted_date, p.how_to_apply, p.contact, p.apply_link, p.scam_warning, p.summary,
        json.dumps([s.model_dump() for s in p.sources]), [s.url for s in p.sources],
        p.fingerprint, p.status,
    )


class PgStore:
    def __init__(self, dsn: str, pool_size: int = 5) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None: Neon's pooled (-pooler) URL is PgBouncer in
        # transaction mode, where server-side prepared statements can land on
        # another backend. min_size=0 lets an idle Neon compute scale to zero.
        # Neon drops idle connections (a collector run can sit 10+ minutes in AI
        # calls), so close them after 60s idle and test each one on checkout.
        self._pool = ConnectionPool(
            dsn, min_size=0, max_size=pool_size,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            max_idle=60, check=ConnectionPool.check_connection,
            open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def init_schema(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA.read_text("utf-8"))

    # --- collectors -------------------------------------------------------
    def seen(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT url FROM seen_urls WHERE url = ANY(%s)", (urls,)).fetchall()
        return {r["url"] for r in rows}

    def mark_seen(self, urls: list[str]) -> None:
        if not urls:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO seen_urls (url) VALUES (%s) ON CONFLICT DO NOTHING", [(u,) for u in urls]
            )

    def upsert_placement(self, p: Placement) -> str:
        """Insert, or merge into the stored copy with a shared url or fingerprint."""
        urls = [s.url for s in p.sources]
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM placements WHERE source_urls && %s OR (%s::text IS NOT NULL AND fingerprint = %s) "
                "ORDER BY id LIMIT 1 FOR UPDATE",
                (urls, p.fingerprint, p.fingerprint),
            ).fetchone()
            if row is None:
                conn.execute(
                    f"INSERT INTO placements ({_COLUMNS}) VALUES ({', '.join(['%s'] * 17)}) "
                    "ON CONFLICT (fingerprint) DO NOTHING",
                    _row_values(p),
                )
                return "inserted"
            if row["status"] == "rejected":
                return "rejected"   # hidden by hand: a fresh copy must not bring it (or its company) back
            merged = merge(Placement.model_validate(row), p)
            # Keep the stored fingerprint unless it had none, so the unique index can't clash.
            merged.fingerprint = row["fingerprint"] or p.fingerprint
            assignments = ", ".join(f"{c.strip()} = %s" for c in _COLUMNS.split(","))
            conn.execute(
                f"UPDATE placements SET {assignments}, last_seen_at = now() WHERE id = %s",
                (*_row_values(merged), row["id"]),
            )
            return "merged"

    def upsert_company(self, c: Company) -> str:
        """Merge into a stored company only when ``should_merge`` agrees; else a new row."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_COMPANY_LOCK_KEY,))
            ks = sorted(keys(c))
            rows = conn.execute(
                "SELECT * FROM companies WHERE name_key = ANY(%s) OR aliases && %s ORDER BY id FOR UPDATE",
                (ks, ks),
            ).fetchall()
            candidates = [Company.model_validate(r) for r in rows]
            target = next((e for e in candidates if should_merge(e, c, len(candidates))), None)
            if target is None:
                conn.execute(
                    f"INSERT INTO companies ({_COMPANY_COLUMNS}) VALUES ({', '.join(['%s'] * 13)})",
                    _company_values(c),
                )
                return "inserted"
            merged = merge_company(target, c)
            assignments = ", ".join(f"{col.strip()} = %s" for col in _COMPANY_COLUMNS.split(","))
            conn.execute(
                f"UPDATE companies SET {assignments}, updated_at = now() WHERE id = %s",
                (*_company_values(merged), target.id),
            )
            return "merged"

    def reject_placement(self, placement_id: int) -> bool:
        """Hide a bad listing for good, and stop it counting as evidence for its company."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE placements SET status = 'rejected' WHERE id = %s RETURNING source_urls", (placement_id,)
            ).fetchone()
            if row is None:
                return False
            urls = row["source_urls"]
            rows = conn.execute(
                "SELECT * FROM companies WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) e "
                "WHERE e->>'url' = ANY(%s)) FOR UPDATE",
                (urls,),
            ).fetchall()
            for r in rows:
                c = drop_evidence(Company.model_validate(r), set(urls))
                conn.execute(
                    "UPDATE companies SET evidence = %s, updated_at = now() WHERE id = %s",
                    (json.dumps([e.model_dump(mode="json") for e in c.evidence]), c.id),
                )
            return True

    def seeds_due(self, limit: int, older_than_days: int = 30) -> list[Company]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM companies WHERE origin = 'seed' AND status = 'active' "
                "AND (last_checked_at IS NULL OR last_checked_at < now() - make_interval(days => %s)) "
                "ORDER BY last_checked_at NULLS FIRST, id LIMIT %s",
                (older_than_days, limit),
            ).fetchall()
        return [Company.model_validate(r) for r in rows]

    def mark_checked(self, company_ids: list[int]) -> None:
        if company_ids:
            with self._pool.connection() as conn:
                conn.execute("UPDATE companies SET last_checked_at = now() WHERE id = ANY(%s)", (company_ids,))

    def top_requested(self, limit: int = 10) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM requested_fields "
                "WHERE last_collected_at IS NULL OR last_requested_at > last_collected_at "
                "ORDER BY request_count DESC, first_requested_at LIMIT %s",
                (limit,),
            ).fetchall()
        return [r["name"] for r in rows]

    def mark_requested_collected(self, names: list[str]) -> None:
        if names:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE requested_fields SET last_collected_at = now() WHERE name = ANY(%s)", (names,)
                )

    def expire(self) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE placements SET status = 'expired' WHERE status = 'open' AND ("
                " (deadline IS NOT NULL AND deadline < (now() AT TIME ZONE 'Africa/Lagos')::date)"
                " OR (deadline IS NULL AND COALESCE(posted_date, (first_seen_at AT TIME ZONE 'Africa/Lagos')::date)"
                "     < (now() AT TIME ZONE 'Africa/Lagos')::date - 60))"
            )
            return cur.rowcount

    def start_run(self, collector: str, forced: bool = False) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO collector_runs (collector, forced) VALUES (%s, %s) RETURNING id", (collector, forced)
            ).fetchone()
        return row["id"]

    def finish_run(self, run_id: int, status: str, counts: dict[str, Any], error: str | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE collector_runs SET finished_at = now(), status = %s, counts = %s, error = %s WHERE id = %s",
                (status, json.dumps(counts), error, run_id),
            )

    def try_start_x_run(self, gap: timedelta = X_MIN_GAP, forced: bool = False) -> GateResult:
        """Atomically check the 48h rule and record the start before X is touched.

        The advisory lock serializes concurrent callers, so two overlapping runs
        can't both read "no run in 48h" and both go ahead.
        """
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_X_LOCK_KEY,))
            row = conn.execute(
                "SELECT now() AS now, max(started_at) AS last FROM collector_runs WHERE collector = 'x'"
            ).fetchone()
            allowed, next_allowed = x_gate_decision(row["last"], row["now"], gap, forced)
            if not allowed:
                return GateResult(False, None, row["last"], next_allowed)
            run = conn.execute(
                "INSERT INTO collector_runs (collector, forced) VALUES ('x', %s) RETURNING id", (forced,)
            ).fetchone()
            return GateResult(True, run["id"], row["last"], next_allowed)

    # --- website ----------------------------------------------------------
    def search(self, field: str | None, text: str, state: str | None, limit: int = 100) -> list[Placement]:
        if field:
            where, params = "(%s = ANY(fields) OR open_to_all)", [field]
        else:
            like = f"%{text}%"
            where, params = "(open_to_all OR title ILIKE %s OR summary ILIKE %s)", [like, like]
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM placements WHERE status = 'open' AND {where} "
                "ORDER BY (state IS NOT DISTINCT FROM %s) DESC, (deadline IS NULL), "
                "COALESCE(posted_date, first_seen_at::date) DESC LIMIT %s",
                (*params, state, limit),
            ).fetchall()
        return [Placement.model_validate(r) for r in rows]

    def companies_for(self, field: str, limit: int = 300) -> list[Company]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                # Field-tagged only: an "open to any course" advert says nothing about GIS work.
                "SELECT * FROM companies WHERE status = 'active' AND jsonb_array_length(evidence) > 0 "
                "AND %s = ANY(fields) ORDER BY updated_at DESC LIMIT %s",
                (field, limit),
            ).fetchall()
        return [Company.model_validate(r) for r in rows]

    def open_placements(self) -> list[Placement]:
        """Every open listing, to mark companies "open now" (small table: hundreds of rows)."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM placements WHERE status = 'open'").fetchall()
        return [Placement.model_validate(r) for r in rows]

    def request_field(self, name: str, canonical: bool) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO requested_fields (name, canonical) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET request_count = requested_fields.request_count + 1, "
                "last_requested_at = now()",
                (name, canonical),
            )

    def health(self) -> dict[str, Any]:
        with self._pool.connection() as conn:
            runs = conn.execute(
                "SELECT DISTINCT ON (collector) collector, started_at, finished_at, status, counts, error, forced "
                "FROM collector_runs ORDER BY collector, started_at DESC"
            ).fetchall()
            last_x = conn.execute(
                "SELECT max(started_at) AS last FROM collector_runs WHERE collector = 'x'"
            ).fetchone()["last"]
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE status = 'open') AS open, count(*) AS total FROM placements"
            ).fetchone()
            requested = conn.execute(
                "SELECT name, request_count, last_requested_at, last_collected_at FROM requested_fields "
                "ORDER BY request_count DESC LIMIT 20"
            ).fetchall()
        return {
            "runs": runs,
            "next_x_allowed": last_x + X_MIN_GAP if last_x else None,
            "placements": counts,
            "requested": requested,
        }


class MemoryStore:
    """In-process stand-in for ``PgStore`` (tests, dry runs without a database)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.placements: list[Placement] = []
        self.seen_urls: set[str] = set()
        self.requested: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.companies: list[Company] = []

    def close(self) -> None:
        pass

    def init_schema(self) -> None:
        pass

    def seen(self, urls: list[str]) -> set[str]:
        return {u for u in urls if u in self.seen_urls}

    def mark_seen(self, urls: list[str]) -> None:
        self.seen_urls.update(urls)

    def upsert_placement(self, p: Placement) -> str:
        with self._lock:
            urls = {s.url for s in p.sources}
            for i, existing in enumerate(self.placements):
                if urls & {s.url for s in existing.sources} or (
                    p.fingerprint and existing.fingerprint == p.fingerprint
                ):
                    if existing.status == "rejected":
                        return "rejected"
                    merged = merge(existing, p)
                    merged.fingerprint = existing.fingerprint or p.fingerprint
                    merged.last_seen_at = self._clock()
                    self.placements[i] = merged
                    return "merged"
            p = p.model_copy(update={"id": len(self.placements) + 1, "first_seen_at": self._clock(),
                                     "last_seen_at": self._clock()})
            self.placements.append(p)
            return "inserted"

    def upsert_company(self, c: Company) -> str:
        with self._lock:
            candidates = [e for e in self.companies if keys(e) & keys(c)]
            target = next((e for e in candidates if should_merge(e, c, len(candidates))), None)
            if target is None:
                self.companies.append(c.model_copy(update={"id": len(self.companies) + 1}))
                return "inserted"
            i = self.companies.index(target)
            self.companies[i] = merge_company(target, c)
            return "merged"

    def reject_placement(self, placement_id: int) -> bool:
        with self._lock:
            p = next((p for p in self.placements if p.id == placement_id), None)
            if p is None:
                return False
            p.status = "rejected"
            urls = {s.url for s in p.sources}
            self.companies = [drop_evidence(c, urls) for c in self.companies]
            return True

    def seeds_due(self, limit: int, older_than_days: int = 30) -> list[Company]:
        cutoff = self._clock() - timedelta(days=older_than_days)
        due = [c for c in self.companies if c.origin == "seed" and c.status == "active"
               and (c.last_checked_at is None or c.last_checked_at < cutoff)]
        due.sort(key=lambda c: (c.last_checked_at is not None, c.last_checked_at or self._clock(), c.id))
        return due[:limit]

    def mark_checked(self, company_ids: list[int]) -> None:
        for c in self.companies:
            if c.id in company_ids:
                c.last_checked_at = self._clock()

    def top_requested(self, limit: int = 10) -> list[str]:
        pending = [
            r for r in self.requested.values()
            if r["last_collected_at"] is None or r["last_requested_at"] > r["last_collected_at"]
        ]
        pending.sort(key=lambda r: (-r["request_count"], r["first_requested_at"]))
        return [r["name"] for r in pending[:limit]]

    def mark_requested_collected(self, names: list[str]) -> None:
        for name in names:
            if name in self.requested:
                self.requested[name]["last_collected_at"] = self._clock()

    def expire(self) -> int:
        today = self._clock().astimezone(LAGOS).date()
        count = 0
        for p in self.placements:
            if p.status == "open" and is_expired(p.deadline, p.posted_date, p.first_seen_at, today):
                p.status = "expired"
                count += 1
        return count

    def start_run(self, collector: str, forced: bool = False) -> int:
        with self._lock:
            self.runs.append({"id": len(self.runs) + 1, "collector": collector, "started_at": self._clock(),
                              "status": "running", "forced": forced, "counts": {}, "error": None})
            return len(self.runs)

    def finish_run(self, run_id: int, status: str, counts: dict[str, Any], error: str | None = None) -> None:
        self.runs[run_id - 1].update(status=status, counts=counts, error=error, finished_at=self._clock())

    def try_start_x_run(self, gap: timedelta = X_MIN_GAP, forced: bool = False) -> GateResult:
        with self._lock:
            starts = [r["started_at"] for r in self.runs if r["collector"] == "x"]
            last = max(starts) if starts else None
            allowed, next_allowed = x_gate_decision(last, self._clock(), gap, forced)
            if not allowed:
                return GateResult(False, None, last, next_allowed)
            self.runs.append({"id": len(self.runs) + 1, "collector": "x", "started_at": self._clock(),
                              "status": "running", "forced": forced, "counts": {}, "error": None})
            return GateResult(True, len(self.runs), last, next_allowed)

    def search(self, field: str | None, text: str, state: str | None, limit: int = 100) -> list[Placement]:
        def matches(p: Placement) -> bool:
            if field:
                return field in p.fields or p.open_to_all
            needle = text.lower()
            return p.open_to_all or needle in p.title.lower() or needle in (p.summary or "").lower()

        hits = [p for p in self.placements if p.status == "open" and matches(p)]
        hits.sort(key=lambda p: (
            p.state != state,
            p.deadline is None,
            -(p.posted_date or (p.first_seen_at or self._clock()).date()).toordinal(),
        ))
        return hits[:limit]

    def companies_for(self, field: str, limit: int = 300) -> list[Company]:
        return [c for c in self.companies if c.status == "active" and c.evidence and field in c.fields][:limit]

    def open_placements(self) -> list[Placement]:
        return [p for p in self.placements if p.status == "open"]

    def request_field(self, name: str, canonical: bool) -> None:
        now = self._clock()
        row = self.requested.setdefault(name, {
            "name": name, "canonical": canonical, "request_count": 0,
            "first_requested_at": now, "last_requested_at": now, "last_collected_at": None,
        })
        row["request_count"] += 1
        row["last_requested_at"] = now

    def health(self) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        for run in self.runs:
            latest[run["collector"]] = run
        x_starts = [r["started_at"] for r in self.runs if r["collector"] == "x"]
        return {
            "runs": list(latest.values()),
            "next_x_allowed": max(x_starts) + X_MIN_GAP if x_starts else None,
            "placements": {"open": sum(p.status == "open" for p in self.placements), "total": len(self.placements)},
            "requested": sorted(self.requested.values(), key=lambda r: -r["request_count"])[:20],
        }


def open_store(dsn: str | None) -> "PgStore | MemoryStore":
    return PgStore(dsn) if dsn else MemoryStore()


def lagos_today() -> date:
    return datetime.now(LAGOS).date()
