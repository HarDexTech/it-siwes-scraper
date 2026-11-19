import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from .dedupe import fingerprint
from .fields import canonical_names
from .prompts import EXTRACT_SYSTEM
from .scam import looks_like_scam
from .schema import Extracted, Placement, RawItem, SourceLink

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
    state = _normalize_state(ext.state)
    title = (ext.title or item.title or "IT placement").strip()[:200]
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


def extract(
    items: list[RawItem],
    llm: LLM,
    *,
    fields_hint: list[str] | None = None,
    batch_size: int = 5,
    max_calls: int = 25,
    today: date | None = None,
) -> tuple[list[Placement], list[RawItem], int]:
    """Run the LLM over ``items`` in batches.

    Returns (placements, items the LLM actually looked at, calls made). Items
    left over when ``max_calls`` runs out are not in the second list, so the
    caller doesn't mark them seen and they get another chance next run.
    """
    today = today or datetime.now(timezone(timedelta(hours=1))).date()  # Lagos
    placements: list[Placement] = []
    processed: list[RawItem] = []
    calls = 0
    for start in range(0, len(items), batch_size):
        if calls >= max_calls:
            logger.warning("LLM call cap (%d) reached; %d items left for next run", max_calls, len(items) - start)
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
        calls += 1
        try:
            reply, model = llm.complete(EXTRACT_SYSTEM, json.dumps(payload))
            records = _parse_json(reply)
        except Exception:
            logger.exception("LLM batch failed; leaving %d items for next run", len(batch))
            continue
        processed.extend(batch)
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
                placements.append(placement)
        logger.info("batch of %d via %s -> %d placements so far", len(batch), model, len(placements))
    return placements, processed, calls
