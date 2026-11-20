"""Which fields to search this run, and the search strings for each."""

from datetime import datetime, timezone

from core.fields import load_fields

_SOCIAL_SITES = "(site:linkedin.com OR site:x.com OR site:t.me OR site:nairaland.com)"
_JOB_SITES = "(site:myjobmag.com OR site:jobberman.com OR site:hotnigerianjobs.com OR site:ngcareers.com)"
_TERMS = '(SIWES OR "IT placement" OR "industrial training" OR "industrial attachment")'


def search_terms(field: str) -> str:
    """Quoted name plus up to two readable aliases (codes like "CSC" are too ambiguous)."""
    aliases = [a for a in load_fields().get(field, []) if len(a) > 3][:2]
    if not aliases:
        return f'"{field}"'
    return "(" + " OR ".join(f'"{n}"' for n in [field, *aliases]) + ")"


def field_queries(field: str) -> list[str]:
    terms = search_terms(field)
    return [
        f"SIWES {terms}",
        # Pages listing companies that take students in this field (core/extract.to_companies).
        f'("companies that accept" OR "places for" OR "list of companies") SIWES {terms}',
        f"{_TERMS} {terms} {_SOCIAL_SITES}",
        f"{_TERMS} {terms} {_JOB_SITES}",
    ]


_DIRECTORIES = "(site:finelib.com OR site:connectnigeria.com OR site:vconnect.com OR site:esri.com)"


def discovery_queries(field: str, state: str) -> list[str]:
    """Companies doing this field's work in this state, and SIWES reports naming where students
    trained. Run without the recent-only date filter (these pages are old by nature)."""
    terms = search_terms(field)
    return [
        f"{terms} (company OR firm OR consultants OR services) {state} Nigeria",
        f"{terms} {state} {_DIRECTORIES}",
        f'(SIWES OR "industrial training") ("technical report" OR report) {terms} {state}',
    ]


def rotation_pairs(states: list[str], recent: set[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """When no student searches are waiting: every course, one state at a time (the first
    state, usually Lagos, gets all courses before the next), skipping pairs done recently."""
    out: list[tuple[str, str]] = []
    for state in states:
        for field in load_fields():
            if len(out) >= limit:
                return out
            if (field, state) not in recent:
                out.append((field, state))
    return out


def seed_query(name: str) -> str:
    """Checks one company from config/companies.yaml. "Nigeria", not "internship", keeps
    out the company's global/US internship pages."""
    return f'"{name}" (SIWES OR "industrial training" OR "IT students") Nigeria'


def general_queries(year: int) -> list[str]:
    return [
        f'"SIWES placement" {year}',
        f'"IT placement" students Nigeria {year}',
        f'"SIWES students" "apply" {_SOCIAL_SITES}',
        f'"industrial training" students vacancy {_JOB_SITES}',
    ]


def rotation_slice(size: int, now: datetime | None = None) -> list[str]:
    """A different slice of the field list each 6-hour run, wrapping around."""
    names = list(load_fields())
    if size <= 0 or not names:
        return []
    now = now or datetime.now(timezone.utc)
    run_index = int(now.timestamp() // (6 * 3600))
    start = (run_index * size) % len(names)
    return [names[(start + i) % len(names)] for i in range(min(size, len(names)))]


def plan_fields(requested: list[str], query_budget: int, now: datetime | None = None) -> list[str]:
    """Requested fields first, then rotation fields, within the query budget."""
    per_field = len(field_queries("x"))
    room = max(0, query_budget - len(general_queries(2000))) // per_field
    chosen = list(dict.fromkeys(requested))[:room]
    for name in rotation_slice(room, now):
        if len(chosen) >= room:
            break
        if name not in chosen:
            chosen.append(name)
    return chosen
