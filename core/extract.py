import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from .companies import contains_words, norm_text, site_domain
from .dedupe import fingerprint, normalize_org
from .fields import canonical_names, load_fields
from .prompts import EXTRACT_SYSTEM
from .scam import looks_like_scam
from .schema import Company, Evidence, Extracted, Placement, RawItem, SourceLink

logger = logging.getLogger(__name__)

# Cheap local filter: an item that never mentions any of these is not a
# placement post, so it never costs an LLM call.
_KEYWORDS = re.compile(
    r"\bsiwes\b|industrial\s+training|\bi\.?t\.?\s+(?:placement|students?|slot)|industrial\s+attachment"
    r"|\bintern(?:ship)?s?\b|student\s+attachment|attachment\s+students?|\bplacement\b",
    re.IGNORECASE,
)
MAX_TEXT_CHARS = 4000
STATES = {
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno", "Cross River",
    "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "Gombe", "Imo", "Jigawa", "Kaduna", "Kano", "Katsina",
    "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo", "Plateau",
    "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "FCT",
}
# Cities and areas that posts name instead of the state ("SIWES in Ibadan").
_CITY_STATE = {
    "Lagos": "ikeja, lekki, victoria island, ikoyi, yaba, surulere, apapa, ajah, festac, maryland, ikorodu, epe, "
             "badagry, oshodi, agege, ogba, magodo, gbagada, lagos island, marina, ojota, mushin, isolo, ilupeju, "
             "alausa, oregun, sangotedo, ibeju lekki",
    "FCT": "abuja, garki, wuse, maitama, asokoro, gwarinpa, kubwa, jabi, utako, lugbe, gwagwalada",
    "Oyo": "ibadan, ogbomoso, iseyin, saki",
    "Rivers": "port harcourt, bonny, eleme, onne, obio akpor",
    "Delta": "warri, asaba, sapele, ughelli, effurun, agbor",
    "Edo": "benin city, auchi, ekpoma",
    "Ogun": "abeokuta, sagamu, shagamu, ijebu ode, sango ota, agbara, ifo, mowe",
    "Osun": "osogbo, oshogbo, ile ife, ilesa, ilesha, ede, iwo",
    "Ondo": "akure, owo, okitipupa",
    "Ekiti": "ado ekiti",
    "Kwara": "ilorin, offa",
    "Kaduna": "zaria",
    "Enugu": "nsukka",
    "Anambra": "awka, onitsha, nnewi",
    "Imo": "owerri, orlu",
    "Abia": "umuahia, aba",
    "Akwa Ibom": "uyo, eket, ikot ekpene",
    "Cross River": "calabar, ikom",
    "Plateau": "jos",
    "Borno": "maiduguri",
    "Niger": "minna, bida",
    "Kogi": "lokoja, ajaokuta",
    "Benue": "makurdi",
    "Nasarawa": "lafia, keffi",
    "Bayelsa": "yenagoa",
    "Ebonyi": "abakaliki",
    "Adamawa": "yola",
    "Taraba": "jalingo",
    "Kebbi": "birnin kebbi",
    "Zamfara": "gusau",
    "Jigawa": "dutse",
    "Yobe": "damaturu",
}
# Normalized place name -> state; state names map to themselves ("niger" only as a state name, too vague alone).
_PLACES = {s.lower(): s for s in STATES if s not in {"FCT", "Niger"}} | {
    place.strip(): state for state, names in _CITY_STATE.items() for place in names.split(",")
}
# Search-results and directory pages: they list other organizations' openings,
# which are collected one by one instead. Dropped whatever the model says.
# Search-results and directory pages: they list other organizations' openings,
# which are collected one by one instead. Dropped whatever the model says.
_AGGREGATOR_URL = re.compile(
    r"indeed\.com/(?:q-|jobs\?)|siwesfinder\.|treehousejobs\.ng/jobs/siwes"
    r"|myjobmag\.com/(?:search|jobs-by-|jobs-in-)|jobberman\.com/jobs\?",
    re.IGNORECASE,
)
_AGGREGATOR_ORGS = {
    "indeed", "jobberman", "myjobmag", "hotnigerianjobs", "hot nigerian jobs", "ngcareers", "siwes finder",
    "siwesfinder", "treehouse jobs", "treehousejobs", "linkedin",
}
# Sources where the post's author is the one offering the place. A Telegram
# channel usually reposts someone else's listing, so its name isn't a contact.
_POSTER_IS_CONTACT = {"x", "nairaland"}


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = True) -> tuple[str, str]: ...


def passes_prefilter(item: RawItem) -> bool:
    return bool(_KEYWORDS.search(f"{item.title}\n{item.text}"))


def _parse_json(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    # Some models wrap JSON in ```json fences despite json mode.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise ValueError("LLM reply is not a list of items")
    return [d for d in data if isinstance(d, dict)]


def _normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().removesuffix(" State").removesuffix(" state")
    if value.lower() in {"abuja", "fct abuja", "federal capital territory"}:
        return "FCT"
    return next((s for s in STATES if s.lower() == value.lower()), None)


def state_from_text(text: str | None) -> str | None:
    """The state a place name in ``text`` belongs to ("Ibadan" -> Oyo).

    Only when exactly one state is named: "Abuja, Delta, Lagos" stays unknown.
    """
    words = f" {norm_text(text)} "
    found = {state for place, state in _PLACES.items() if f" {place} " in words}
    return found.pop() if len(found) == 1 else None


def to_placement(item: RawItem, ext: Extracted, today: date) -> Placement | None:
    if not ext.isPlacementListing:
        return None
    allowed = set(canonical_names())
    posted = ext.postedDate or (item.posted_at.date() if item.posted_at else None)
    if posted and posted > today:
        posted = None
    deadline = ext.deadline
    # A deadline more than a year out, or before the post itself, is a model misread.
    if deadline and (deadline > today + timedelta(days=365) or (posted and deadline < posted)):
        deadline = None
    if _AGGREGATOR_URL.search(item.url) or (ext.organization or "").strip().lower() in _AGGREGATOR_ORGS:
        return None
    contact = ext.contact
    if not contact and item.author_handle and item.source in _POSTER_IS_CONTACT:
        contact = f"@{item.author_handle} on {item.source.upper() if item.source == 'x' else item.source}"
    if not ext.organization and not contact:
        # Nobody to apply to: a results page or a repost that slipped past the prompt.
        return None
    title = (ext.title or item.title or "IT placement").strip()[:200]
    state = _normalize_state(ext.state) or state_from_text(ext.location) or state_from_text(title)
    return Placement(
        title=title,
        organization=ext.organization,
        location=ext.location,
        state=state,
        fields=[f for f in ext.fields if f in allowed],
        open_to_all=ext.openToAllFields,
        deadline=deadline,
        posted_date=posted,
        how_to_apply=ext.howToApply,
        contact=contact,
        apply_link=ext.applyLink if ext.applyLink and ext.applyLink.startswith("http") else None,
        scam_warning=ext.scamWarning or looks_like_scam(item.text, item.title, ext.howToApply),
        summary=ext.summary,
        sources=[SourceLink(source=item.source, url=item.url)],
        fingerprint=fingerprint(ext.organization, state, deadline),
        status="expired" if is_expired(deadline, posted, None, today) else "open",
    )


MAX_COMPANIES_PER_PAGE = 8
_MAX_QUOTE_WORDS = 20


def _named_fields(page: str) -> set[str]:
    """Canonical fields the page itself mentions, by name or a readable alias."""
    return {
        name for name, aliases in load_fields().items()
        if any(contains_words(page, norm_text(term)) for term in [name, *aliases] if len(term) > 3)
    }


def to_companies(item: RawItem, ext: Extracted, today: date) -> list[Company]:
    """Companies named on a SIWES company-list page, checked against the page text.

    The model's answer is only trusted where the page backs it: the name and the
    quote must be on the page, and a field only counts if the page names it.
    """
    if ext.pageType != "company_list" or ext.isPlacementListing:
        return []
    seen_text = f"{item.title}\n{item.text[:MAX_TEXT_CHARS]}"   # exactly what the model was shown
    page, page_org = norm_text(seen_text), normalize_org(seen_text)
    named = _named_fields(page)
    page_date = ext.postedDate or (item.posted_at.date() if item.posted_at else None)
    if page_date and page_date > today:
        page_date = None
    out: list[Company] = []
    for c in ext.companies[:MAX_COMPANIES_PER_PAGE]:
        key = normalize_org(c.name)
        quote = norm_text(c.quote)
        if (len(key) < 3 or not contains_words(page_org, key) or not quote
                or len(quote.split()) > _MAX_QUOTE_WORDS or not contains_words(page, quote)):
            logger.info("dropping company %r: not backed by the page %s", c.name, item.url)
            continue
        if key in _AGGREGATOR_ORGS:   # a job board listed as if it were an employer
            continue
        website = c.website if c.website and c.website.startswith("http") else None
        if website and site_domain(website) == site_domain(item.url):
            website = None   # the directory's own link, not the company's
        state = _normalize_state(c.state) or state_from_text(c.location)
        out.append(Company(
            name=c.name.strip()[:120],
            name_key=key,
            fields=sorted(set(c.fields) & named),
            states=[state] if state else [],
            location=c.location,
            website=website,
            evidence=[Evidence(url=item.url, kind="directory", page_date=page_date, seen_at=today,
                               quote=c.quote.strip()[:200])],
            origin="directory",
        ))
    return out


def is_expired(
    deadline: date | None,
    posted: date | None,
    first_seen: datetime | None,
    today: date,
    max_age_days: int = 60,
) -> bool:
    """Deadline passed, or no deadline and older than ``max_age_days``."""
    if deadline:
        return deadline < today
    start = posted or (first_seen.date() if first_seen else None)
    return bool(start and (today - start).days > max_age_days)


@dataclass
class ExtractResult:
    placements: list[Placement] = field(default_factory=list)
    companies: list[Company] = field(default_factory=list)
    processed: list[RawItem] = field(default_factory=list)  # items the LLM actually looked at
    calls: int = 0
    left_over: int = 0                                       # items not reached before the call cap


def extract(
    items: list[RawItem],
    llm: LLM,
    *,
    fields_hint: list[str] | None = None,
    batch_size: int = 5,
    max_calls: int = 25,
    today: date | None = None,
) -> ExtractResult:
    """Run the LLM over ``items`` in batches.

    Items left over when ``max_calls`` runs out are not in ``processed``, so the
    caller doesn't mark them seen and they get another chance next run. A reply
    that isn't valid JSON (e.g. cut off at max_tokens) counts as processed, so a
    page that always breaks the reply can't eat calls run after run.
    """
    today = today or datetime.now(timezone(timedelta(hours=1))).date()  # Lagos
    result = ExtractResult()
    for start in range(0, len(items), batch_size):
        if result.calls >= max_calls:
            result.left_over = len(items) - start
            logger.warning("LLM call cap (%d) reached; %d items left for next run", max_calls, result.left_over)
            break
        batch = items[start : start + batch_size]
        payload = {
            "today": today.isoformat(),
            "canonical_fields": canonical_names(),
            "fields_hint": fields_hint or [],
            "items": [
                {
                    "id": i,
                    "source": it.source,
                    "url": it.url,
                    "title": it.title,
                    "text": it.text[:MAX_TEXT_CHARS],
                    "posted_at": it.posted_at.isoformat() if it.posted_at else None,
                }
                for i, it in enumerate(batch)
            ],
        }
        result.calls += 1
        try:
            reply, model = llm.complete(EXTRACT_SYSTEM, json.dumps(payload))
        except Exception:
            logger.exception("LLM batch failed; leaving %d items for next run", len(batch))
            continue
        result.processed.extend(batch)
        try:
            records = _parse_json(reply)
        except ValueError:   # includes json.JSONDecodeError
            logger.warning("unreadable LLM reply (%d chars); skipping %d items", len(reply or ""), len(batch))
            continue
        for record in records:
            try:
                ext = Extracted.model_validate(record)
            except ValidationError as exc:
                logger.warning("dropping malformed LLM record: %s", exc.errors()[:1])
                continue
            if not 0 <= ext.id < len(batch):
                continue
            placement = to_placement(batch[ext.id], ext, today)
            if placement:
                result.placements.append(placement)
            result.companies.extend(to_companies(batch[ext.id], ext, today))
        logger.info("batch of %d via %s -> %d placements, %d companies so far",
                    len(batch), model, len(result.placements), len(result.companies))
    return result
