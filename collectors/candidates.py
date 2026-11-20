"""AI-suggested companies for a course + state, each checked on the web before it is shown.

This is how a chat assistant answers "companies where I can do SIWES for GIS in Lagos":
it names firms it knows. Here the LLM's names are only candidates. A candidate is kept
only when one web search for it finds a page that

1. names it (whole words),
2. says what it does: one of the course's work words, outside the company's own name
   ("geospatial" inside "Geospatial Research" proves nothing),
3. places it in the state (the state or one of its cities),
4. has 1-3 within about 300 characters of each other, and
5. is about that one company: its own website, a page titled with its name, or a
   single-company directory entry. A category page listing 20 firms next to "GIS" and
   "Lagos" would pass 1-4 for all of them.

No contacts are taken; the card links to the page and the website. Only run for course +
state pairs students actually searched (each check costs a search).
"""

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from core.companies import site_domain
from core.dedupe import normalize_org
from core.extract import _own_domain, state_places
from core.schema import Company, Evidence, RawItem

logger = logging.getLogger(__name__)

SUGGEST_SYSTEM = """You help Nigerian university and polytechnic students find organisations where they could do
their SIWES / IT (industrial training) placement.

You get JSON: {"field": ..., "state": ..., "max": N, "known": [names already listed]}.
Reply with JSON only: {"work_terms": [...], "companies": [{"name": ..., "city": ..., "does": ...}]}
- companies: up to N real organisations in that Nigerian state that do work in that field and could host a
  student (firms, consultancies, labs, hospitals, agencies, manufacturers). Only ones you are confident exist.
  Use the organisation's usual full name. Skip any in "known". No schools or training centres.
  - city: the city or area, or null
  - does: a few words on what they do
- work_terms: 3 to 8 short words or phrases an organisation doing this work would use on its own website,
  e.g. for Remote Sensing and GIS: "GIS", "geospatial", "mapping", "surveying", "remote sensing", "drone"."""

GROUNDED_PROMPT = """List up to {limit} real organisations in {state} State, Nigeria that do {field} work and could
host a SIWES / IT (industrial training) student: firms, consultancies, labs, hospitals, agencies, manufacturers.
Only ones you find on the web. Skip these, already known: {known}. No schools or training centres.
Reply with JSON only, no other text:
{{"work_terms": [3 to 8 short words an organisation doing this work would use on its own website],
 "companies": [{{"name": "usual full name", "city": "city or area", "does": "a few words",
                 "website": "https://... or null"}}]}}"""
GROUNDED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROXIMITY = 300          # characters around the name in which work word and place must appear
RESULTS_CHECKED = 5
# Words too general to show what a company does.
_GENERIC = {"services", "service", "solutions", "company", "companies", "limited", "nigeria", "business",
            "consulting", "consultants", "consultancy", "technology", "technologies", "engineering", "science",
            "management", "group", "international", "global", "industry", "industries", "work", "projects"}
# Pages about one business in a directory.
_SINGLE_LISTING = re.compile(
    r"finelib\.com/listing/|connectnigeria\.com/businesses/|vconnect\.com/|linkedin\.com/company/"
    r"|businesslist\.com\.ng/company/|kompass\.com/c/|esri\.com/partners/[^/?#]+",
    re.IGNORECASE,
)


def _words(text: str | None) -> str:
    return f" {normalize_org(text)} "


def work_terms(field: str, suggested: list[Any]) -> list[str]:
    """The LLM's work words for the field, minus generic ones, plus the field's own name."""
    terms = {normalize_org(str(t)) for t in suggested if isinstance(t, str) and 2 < len(t) <= 30}
    terms.add(normalize_org(field))
    return sorted(t for t in terms if t and t not in _GENERIC)


def suggest(llm: Any, field: str, state: str, known: list[str], limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    """(candidates, work terms) from one LLM call; ([], []) if the reply is unusable."""
    user = json.dumps({"field": field, "state": state, "max": limit, "known": known[:60]})
    try:
        reply, _ = llm.complete(SUGGEST_SYSTEM, user)
        text = reply.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        data = json.loads(fenced.group(1) if fenced else text)
    except Exception:  # noqa: BLE001 - a bad reply just means no candidates this run
        logger.warning("candidate suggestion failed for %s in %s", field, state, exc_info=True)
        return [], []
    if not isinstance(data, dict):
        return [], []
    companies = [c for c in data.get("companies", []) if isinstance(c, dict) and isinstance(c.get("name"), str)]
    return companies[:limit], work_terms(field, data.get("work_terms") or [])


def _json_from(text: str) -> Any:
    """JSON from a model reply that may wrap it in ``` fences or add words around it."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1] if start >= 0 else text)


def grounded_suggest(
    field: str, state: str, known: list[str], limit: int, *, model: str, api_key: str | None, post=None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str]]] | None:
    """Gemini with Google Search grounding: names found on the web, plus the pages it used
    as (domain, url) pairs. None when grounding isn't available (no key, quota, error)."""
    if not api_key:
        return None
    import httpx

    post = post or httpx.post
    prompt = GROUNDED_PROMPT.format(limit=limit, state=state, field=field, known=", ".join(known[:40]) or "none")
    try:
        for attempt in range(3):   # Google answers 503 "busy" now and then
            response = post(GROUNDED_URL.format(model=model), params={"key": api_key}, timeout=120,
                            json={"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]})
            if response.status_code not in (500, 502, 503, 504):
                break
            logger.warning("grounded suggestion busy (HTTP %s), attempt %d", response.status_code, attempt + 1)
            sleep(5 * (attempt + 1))
        if response.status_code != 200:
            logger.warning("grounded suggestion failed: HTTP %s", response.status_code)
            return None
        candidate = response.json()["candidates"][0]
        data = _json_from("".join(p.get("text", "") for p in candidate["content"]["parts"]))
    except Exception:  # noqa: BLE001
        logger.warning("grounded suggestion failed for %s in %s", field, state, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    chunks = candidate.get("groundingMetadata", {}).get("groundingChunks", [])
    sources = [(str(c["web"].get("title", "")).lower(), c["web"]["uri"]) for c in chunks if c.get("web", {}).get("uri")]
    companies = [c for c in data.get("companies", []) if isinstance(c, dict) and isinstance(c.get("name"), str)]
    return companies[:limit], work_terms(field, data.get("work_terms") or []), sources


def _passes(text: str, key: str, terms: list[str], places: list[str]) -> str | None:
    """The normalized window around the name if name, work word and place sit together, else None."""
    t = _words(text)
    for m in re.finditer(rf"(?<= ){re.escape(key)}(?= )", t):
        window = f" {t[max(0, m.start() - PROXIMITY): m.end() + PROXIMITY].strip()} "
        # The work word must not be part of the name: drop every copy of the name first.
        outside = re.sub(rf"(?<= ){re.escape(key)}(?= )", " ", window)
        if any(f" {w} " in outside for w in terms) and any(f" {p} " in window for p in places):
            return window.strip()
    return None


def _about_one_company(item: RawItem, key: str) -> bool:
    domain = site_domain(item.url)
    return _own_domain(domain, key) or normalize_org(item.title).startswith(key) or bool(_SINGLE_LISTING.search(item.url))


def verify(name: str, terms: list[str], state: str, results: list[RawItem],
           fetch: Callable[[str], str | None] | None = None) -> tuple[RawItem, str] | None:
    """(the page that proves it, a short quote from it), or None."""
    key = normalize_org(name)
    if len(key) < 3:
        return None
    places = state_places(state)
    for item in results[:RESULTS_CHECKED]:
        if not _about_one_company(item, key):
            continue
        window = _passes(f"{item.title}\n{item.text}", key, terms, places)
        if window is None and fetch:
            page = fetch(item.url) or ""
            window = _passes(page, key, terms, places)
        if window:
            words = window.split()
            return item, " ".join(words[:25])
    return None


def _best_source(sources: list[tuple[str, str]], key: str) -> tuple[str, str] | None:
    """The (domain, link) grounding source whose domain shares the most name words: for
    "Firstline Geospatial", firstlinegeospatial.com beats geospatial.ng."""
    def score(domain: str) -> int:
        host = domain.split(".")[0]
        return sum(len(w) for w in key.split() if len(w) >= 4 and w in host)
    best = max(sources, key=lambda s: score(s[0]), default=None)
    return best if best and score(best[0]) > 0 and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", best[0]) else None


def _follow(url: str, getter=None) -> str | None:
    """The final address behind a grounding redirect link."""
    import httpx

    get = getter or httpx.get
    try:
        return str(get(url, follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).url)
    except Exception:  # noqa: BLE001
        return None


def candidate_companies(
    llm: Any, search: Any, field: str, state: str, *, known: list[str], max_checks: int, today: date,
    fetch: Callable[[str], str | None] | None = None, grounded_model: str | None = None,
    api_key: str | None = None, post=None, follow: Callable[[str], str | None] | None = None,
) -> tuple[list[Company], dict[str, int]]:
    """Suggest companies, then check each one before it can be shown.

    Grounded suggestions (Gemini searching Google) come with the pages they used: a firm
    whose own website is among them is checked on that page, at no search cost. Otherwise
    one web search per firm (``max_checks`` of them). With no ``grounded_model`` the plain
    LLM suggests from memory and every firm needs a search. If grounding is configured but
    fails, nothing is suggested (``stats["grounding_failed"]``), so the caller can retry the
    pair next run instead of spending searches on a weaker model's guesses.
    """
    grounded = None
    if grounded_model:
        grounded = grounded_suggest(field, state, known, max_checks + 5, model=grounded_model, api_key=api_key,
                                    post=post)
        if grounded is None:
            return [], {"suggested": 0, "grounded": 0, "grounding_failed": 1, "checked": 0, "verified": 0}
        suggested, terms, sources = grounded
    else:
        (suggested, terms), sources = suggest(llm, field, state, known, max_checks), []
    known_keys = {normalize_org(n) for n in known}
    stats = {"suggested": len(suggested), "grounded": int(bool(grounded)), "checked": 0, "verified": 0}
    if not terms:
        return [], stats
    follow = follow or _follow
    out: list[Company] = []
    for cand in suggested:
        name = cand["name"].strip()
        key = normalize_org(name)
        if not key or key in known_keys:
            continue
        found = None
        # 1. The company's own site among the grounding sources: check it there, no search.
        own = _best_source(sources, key)
        # The source link is a Google redirect; if following it fails, the source's title is the
        # bare domain (e.g. kingsgeoconsult.com), so open the site directly.
        final = (follow(own[1]) or f"https://{own[0]}/") if own else None
        if final and fetch:
            page = RawItem(source="web", url=final, title=site_domain(final), text=fetch(final) or "")
            found = verify(name, terms, state, [page])
        # 2. Otherwise one web search, within the budget.
        if not found and stats["checked"] < max_checks:
            stats["checked"] += 1
            found = verify(name, terms, state, search.search(f'"{name}" {state}', recent=False), fetch)
        if not found:
            logger.info("candidate %r not verified for %s in %s", name, field, state)
            continue
        page, quote = found
        domain = site_domain(page.url)
        known_keys.add(key)
        stats["verified"] += 1
        out.append(Company(
            name=name[:120],
            name_key=key,
            fields=[field],
            states=[state],
            location=cand.get("city") if isinstance(cand.get("city"), str) else None,
            website=f"https://{domain}" if _own_domain(domain, key) else None,
            evidence=[Evidence(url=page.url, kind="profile", seen_at=today, quote=quote[:200])],
            origin="discovery",
        ))
    return out, stats
