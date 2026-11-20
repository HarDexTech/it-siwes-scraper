"""Companies students can apply to: from listings, lists of companies that take students,
student SIWES reports, business directories and company pages, plus a seed list.

Shown in three tiers: open now; has taken SIWES/IT students; does the field's work
(no SIWES record found - "ask before you go").

Rules that keep students from being misled:
- A company is shown only with evidence: a page we actually collected.
- Evidence says what the page showed ("advertised places", "a 2021 SIWES report names it",
  "does GIS work in Lagos"), dated by the page itself, never by when we crawled it.
- Contacts come only from listings, never from directory pages.
- Two companies with the same short name merge only when something else agrees
  (seed alias, website, state), so "Greenway" in Oyo and in Lagos stay apart.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .dedupe import fold_accents, normalize_org
from .fields import canonical_names
from .schema import Company, Evidence, Placement

_SEEDS = Path(__file__).resolve().parents[1] / "config" / "companies.yaml"

DATED_EVIDENCE_DAYS = 730      # a dated page counts for 2 years
UNDATED_EVIDENCE_DAYS = 365    # an undated page counts for 1 year from when we last saw it
REPORT_EVIDENCE_DAYS = 1826    # a dated student SIWES report counts for 5 years


def norm_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]", " ", fold_accents(text or "").lower())).strip()


def contains_words(haystack: str, needle: str) -> bool:
    """Whole-word match on normalized text, so "ge" doesn't match "general"."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def site_domain(url: str | None) -> str:
    host = (urlsplit(url).hostname or "") if url else ""
    return host.removeprefix("www.")


def keys(c: Company) -> set[str]:
    return {c.name_key, *c.aliases}


def load_seeds(path: Path = _SEEDS) -> list[Company]:
    """config/companies.yaml as invisible seed companies (no evidence until a collector finds some)."""
    allowed = set(canonical_names())
    seeds = []
    for entry in yaml.safe_load(path.read_text("utf-8"))["companies"]:
        unknown = set(entry.get("fields", [])) - allowed
        if unknown:
            raise ValueError(f"{entry['name']}: unknown fields {sorted(unknown)} (see config/fields.yaml)")
        key = normalize_org(entry["name"])
        aliases = {normalize_org(a) for a in entry.get("aliases", [])} - {key, ""}
        seeds.append(Company(name=entry["name"], name_key=key, aliases=sorted(aliases),
                             fields=sorted(entry.get("fields", [])), website=entry.get("website"), origin="seed"))
    return seeds


def company_from_placement(p: Placement, today: date) -> Company | None:
    """Every listing with a named organization is evidence that it takes students."""
    key = normalize_org(p.organization)
    # A listing that asks students to pay isn't evidence of a placement, whatever else it says.
    if not key or len(key) < 2 or p.status == "rejected" or p.scam_warning:
        return None
    return Company(
        name=p.organization.strip(),
        name_key=key,
        fields=list(p.fields),
        open_to_all=p.open_to_all,
        states=[p.state] if p.state else [],
        location=p.location,
        contact=p.contact,
        how_to_apply=p.how_to_apply,
        evidence=[Evidence(url=s.url, kind="listing", page_date=p.posted_date, seen_at=today) for s in p.sources],
        origin="listing",
    )


def should_merge(existing: Company, new: Company, candidates: int) -> bool:
    """Same key or alias is necessary; something else must agree too."""
    if not keys(existing) & keys(new):
        return False
    if "seed" in (existing.origin, new.origin):
        return True
    if existing.website and new.website and site_domain(existing.website) == site_domain(new.website):
        return True
    if existing.states and new.states:
        return bool(set(existing.states) & set(new.states))
    # One side has no state: only safe when there's no other company it could be.
    return candidates == 1


def merge_company(existing: Company, new: Company) -> Company:
    # Same page seen again: keep one entry, with the latest sighting (keeps undated pages fresh).
    by_url = {e.url: e for e in existing.evidence}
    for e in new.evidence:
        old = by_url.get(e.url)
        by_url[e.url] = e if old is None else old.model_copy(update={"seen_at": max(old.seen_at, e.seen_at)})
    # A seed is curated: its name and seed status win, so the seed rotation keeps finding it.
    seeded = new.origin == "seed"
    return existing.model_copy(update={
        "name": new.name if seeded else existing.name,
        "origin": "seed" if seeded else existing.origin,
        "aliases": sorted(set(existing.aliases) | set(new.aliases)),
        "fields": sorted(set(existing.fields) | set(new.fields)),
        "open_to_all": existing.open_to_all or new.open_to_all,
        "states": sorted(set(existing.states) | set(new.states)),
        "location": existing.location or new.location,
        "website": existing.website or new.website,
        "contact": existing.contact or new.contact,
        "how_to_apply": existing.how_to_apply or new.how_to_apply,
        "evidence": list(by_url.values()),
    })


def drop_evidence(c: Company, urls: set[str]) -> Company:
    return c.model_copy(update={"evidence": [e for e in c.evidence if e.url not in urls]})


def evidence_date(e: Evidence) -> date:
    return e.page_date or e.seen_at


def is_fresh(e: Evidence, today: date) -> bool:
    if e.page_date:
        limit = REPORT_EVIDENCE_DAYS if e.kind == "report" else DATED_EVIDENCE_DAYS
        return (today - e.page_date).days <= limit
    return (today - e.seen_at).days <= UNDATED_EVIDENCE_DAYS


# Tier 1 is "open now" (an open listing, not evidence). Tier 2: has taken SIWES/IT students.
# Tier 3: does the field's work, no SIWES record found.
_TIER = {"listing": 2, "directory": 2, "report": 2, "profile": 3}


def best_evidence(c: Company, today: date) -> Evidence | None:
    """The strongest fresh evidence: SIWES records beat "does the work"; listings beat
    lists and reports; then dated, then newest.

    A student's report is old by nature and the firm may have closed since, so it only
    counts when something recent shows the company still exists.
    """
    fresh = [e for e in c.evidence if is_fresh(e, today)]
    if not any(e.kind != "report" for e in fresh) and not c.website:
        fresh = []
    if not fresh:
        return None
    return max(fresh, key=lambda e: (-_TIER[e.kind], e.kind == "listing", e.page_date is not None, evidence_date(e)))


def evidence_text(e: Evidence, field: str | None = None, state: str | None = None) -> str:
    when = e.page_date.strftime("%b %Y") if e.page_date else None
    if e.kind == "listing":
        return f"Advertised SIWES/IT places ({when or 'date not stated'})"
    if e.kind == "report":
        year = e.page_date.year if e.page_date else None
        return f"A {year} student SIWES/IT report names this company" if year else \
            "A student's SIWES/IT report names this company (undated)"
    if e.kind == "profile":
        work = f"Does {field} work" if field else "Works in this field"
        where = f" in {state}" if state else ""
        return (f"{work}{where} ({site_domain(e.url)}, seen {e.seen_at.strftime('%b %Y')}). "
                "We found no record of them taking SIWES students. Ask before you go.")
    # Directory pages range from SIWES-specific lists to "companies that hire interns".
    return f"Named on a list of companies that take students ({when or 'undated'})"


def links_open_listing(c: Company, p: Placement) -> bool:
    """Is this open placement really this company's? Same name is not enough on its own."""
    if normalize_org(p.organization) not in keys(c):
        return False
    if c.origin == "seed":
        return True
    urls = {s.url for s in p.sources}
    if any(e.url in urls for e in c.evidence):
        return True
    if p.state and p.state in c.states:
        return True
    domains = {site_domain(p.apply_link), *(site_domain(u) for u in urls)} - {""}
    return bool(c.website and site_domain(c.website) in domains)


@dataclass
class CompanyCard:
    company: Company
    open_listing: Placement | None
    in_state: bool
    evidence: Evidence
    evidence_label: str
    tier: int            # 1 open now | 2 has taken SIWES/IT students | 3 does the work, no SIWES record


def rank_companies(
    companies: list[Company], open_placements: list[Placement], state: str | None, today: date,
    field: str | None = None, per_tier: int = 15,
) -> list[CompanyCard]:
    """Tier first (open now, has taken students, does the work), then the chosen state,
    then newest evidence, then reachable. At most ``per_tier`` cards in each tier."""
    open_placements = [p for p in open_placements if not p.scam_warning]
    cards: list[CompanyCard] = []
    for c in companies:
        ev = best_evidence(c, today) if c.status == "active" else None
        if ev is None:
            continue
        listing = next((p for p in open_placements if links_open_listing(c, p)), None)
        in_state = bool(state and state in c.states)
        tier = 1 if listing else _TIER[ev.kind]
        cards.append(CompanyCard(c, listing, in_state, ev, evidence_text(ev, field, state if in_state else None), tier))
    cards.sort(key=lambda k: (
        k.tier,
        not k.in_state,
        -evidence_date(k.evidence).toordinal(),
        not (k.company.website or k.company.contact),
    ))
    kept: list[CompanyCard] = []
    for tier in (1, 2, 3):
        kept += [k for k in cards if k.tier == tier][:per_tier]
    return kept
