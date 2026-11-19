"""Companies students can apply to: built from listings, SIWES directory pages and a seed list.

Rules that keep students from being misled:
- A company is shown only with evidence: a page we actually collected.
- Evidence says what the page showed ("advertised places", "listed on a directory"),
  dated by the page itself, never by when we crawled it.
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
UNDATED_EVIDENCE_DAYS = 365    # an undated page counts for 1 year from when we saw it


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
    known = {e.url for e in existing.evidence}
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
        "evidence": existing.evidence + [e for e in new.evidence if e.url not in known],
    })


def drop_evidence(c: Company, urls: set[str]) -> Company:
    return c.model_copy(update={"evidence": [e for e in c.evidence if e.url not in urls]})


def evidence_date(e: Evidence) -> date:
    return e.page_date or e.seen_at


def is_fresh(e: Evidence, today: date) -> bool:
    if e.page_date:
        return (today - e.page_date).days <= DATED_EVIDENCE_DAYS
    return (today - e.seen_at).days <= UNDATED_EVIDENCE_DAYS


def best_evidence(c: Company, today: date) -> Evidence | None:
    """A listing beats a directory mention; then the newest."""
    fresh = [e for e in c.evidence if is_fresh(e, today)]
    if not fresh:
        return None
    return max(fresh, key=lambda e: (e.kind == "listing", e.page_date is not None, evidence_date(e)))


def evidence_text(e: Evidence) -> str:
    when = e.page_date.strftime("%b %Y") if e.page_date else None
    if e.kind == "listing":
        return f"Advertised SIWES/IT places ({when or 'date not stated'})"
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


def rank_companies(
    companies: list[Company], open_placements: list[Placement], state: str | None, today: date, limit: int = 30
) -> list[CompanyCard]:
    """Open listing now, then the chosen state, then newest evidence, then reachable."""
    open_placements = [p for p in open_placements if not p.scam_warning]
    cards: list[CompanyCard] = []
    for c in companies:
        ev = best_evidence(c, today) if c.status == "active" else None
        if ev is None:
            continue
        listing = next((p for p in open_placements if links_open_listing(c, p)), None)
        cards.append(CompanyCard(c, listing, bool(state and state in c.states), ev, evidence_text(ev)))
    cards.sort(key=lambda k: (
        k.open_listing is None,
        not k.in_state,
        -evidence_date(k.evidence).toordinal(),
        not (k.company.website or k.company.contact),
    ))
    return cards[:limit]
