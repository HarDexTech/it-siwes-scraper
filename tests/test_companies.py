import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as web
from collectors.search import WebSearch, resolve_redirect
from core.companies import (
    company_from_placement, evidence_text, load_seeds, rank_companies, should_merge,
)
from core.db import MemoryStore
from core.extract import extract, state_from_text
from core.schema import Company, Evidence, Extracted, Placement, RawItem, SourceLink

TODAY = date(2026, 11, 20)
ROOT = Path(__file__).resolve().parents[1]


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user, json_mode=True):
        self.calls += 1
        return (self.reply if isinstance(self.reply, str) else json.dumps({"items": self.reply})), "fake/model"


def _company(name, key, *, states=(), website=None, origin="listing", aliases=(), evidence_url="https://e.com/1",
             page_date=TODAY, fields=("Remote Sensing and GIS",)):
    return Company(name=name, name_key=key, states=list(states), website=website, origin=origin, aliases=list(aliases),
                   fields=list(fields), evidence=[Evidence(url=evidence_url, kind="listing", page_date=page_date,
                                                           seen_at=TODAY)] if evidence_url else [])


def _placement(org, state=None, url="https://jobs.example/1", status="open", fields=("Remote Sensing and GIS",)):
    return Placement(title=f"IT at {org}", organization=org, state=state, fields=list(fields), status=status,
                     posted_date=date(2026, 11, 1), sources=[SourceLink(source="web", url=url)])


DIRECTORY = RawItem(
    source="web", url="https://siwesfinder.example/gis/lagos", title="Companies that accept SIWES students",
    text="Companies that accept SIWES students for Remote Sensing and GIS in Lagos:\n"
         "1. Geo Mapping Ltd, Ikeja - takes GIS and Surveying students every year.\n"
         "2. Blue Fields Services, Lekki - call 08031234567.\n",
)


def _directory_reply(companies):
    return [{"id": 0, "isPlacementListing": False, "pageType": "company_list", "companies": companies}]


# --- extraction: company lists ------------------------------------------------
def test_directory_page_gives_companies_not_placements():
    llm = FakeLLM(_directory_reply([
        {"name": "Geo Mapping Ltd", "location": "Ikeja", "fields": ["Remote Sensing and GIS", "Geology"],
         "quote": "Geo Mapping Ltd, Ikeja - takes GIS and Surveying students", "website": None},
    ]))
    r = extract([DIRECTORY], llm, today=TODAY)
    assert r.placements == []
    [c] = r.companies
    assert c.name_key == "geo mapping" and c.states == ["Lagos"]     # Ikeja -> Lagos
    assert c.fields == ["Remote Sensing and GIS"]                    # Geology isn't named on the page
    assert c.contact is None and c.evidence[0].kind == "directory"


def test_company_not_on_the_page_is_dropped():
    # Invented name, a quote that isn't on the page, and a name that only matches inside another word.
    llm = FakeLLM(_directory_reply([
        {"name": "Shell Nigeria", "quote": "Shell takes GIS students"},
        {"name": "Blue Fields Services", "quote": "Blue Fields Services is the best"},
        {"name": "Map", "quote": "Geo Mapping Ltd, Ikeja"},             # only inside "Mapping"
    ]))
    assert extract([DIRECTORY], llm, today=TODAY).companies == []


def test_directory_contacts_are_never_kept():
    llm = FakeLLM(_directory_reply([
        {"name": "Blue Fields Services", "quote": "Blue Fields Services, Lekki", "contact": "08031234567"},
    ]))
    [c] = extract([DIRECTORY], llm, today=TODAY).companies
    assert c.contact is None and "0803" not in c.model_dump_json()


def test_reply_without_page_type_still_validates():
    ext = Extracted.model_validate({"id": 0, "isPlacementListing": True})
    assert ext.pageType is None and ext.companies == []


def test_unreadable_reply_is_not_retried_forever():
    # A reply cut off at max_tokens: the items count as processed so the next run doesn't pay again.
    llm = FakeLLM('{"items": [{"id": 0, "isPlacementListing": false, "companies": [{"name": "Ge')
    r = extract([DIRECTORY], llm, today=TODAY)
    assert r.processed == [DIRECTORY] and r.companies == [] and llm.calls == 1


# --- merging --------------------------------------------------------------------
def test_same_short_name_in_different_states_stays_apart():
    store = MemoryStore()
    store.upsert_company(_company("Greenway Services", "greenway", states=["Oyo"], evidence_url="https://a/1"))
    assert store.upsert_company(_company("Greenway Group", "greenway", states=["Lagos"],
                                         evidence_url="https://b/1")) == "inserted"
    assert len(store.companies) == 2


def test_listing_merges_into_seed_through_alias():
    store = MemoryStore()
    for seed in load_seeds():
        store.upsert_company(seed)
    spdc = company_from_placement(_placement("SPDC", state="Rivers", url="https://jobs.example/spdc"), TODAY)
    assert store.upsert_company(spdc) == "merged"
    shell = next(c for c in store.companies if c.name == "Shell Nigeria")
    assert shell.origin == "seed" and shell.states == ["Rivers"] and len(shell.evidence) == 1


def test_merge_dedupes_evidence_and_fills_gaps():
    store = MemoryStore()
    store.upsert_company(_company("Acme", "acme", states=["Lagos"]))
    store.upsert_company(_company("Acme Ltd", "acme", states=["Lagos"], website="https://acme.ng"))
    [c] = store.companies
    assert len(c.evidence) == 1 and c.website == "https://acme.ng"


def test_state_less_company_merges_only_when_unambiguous():
    a = _company("Acme", "acme", states=["Lagos"])
    new = _company("Acme", "acme", states=[], evidence_url="https://e.com/2")
    assert should_merge(a, new, candidates=1)
    assert not should_merge(a, new, candidates=2)


def test_scam_flagged_listing_is_not_evidence_or_open_now():
    paid = _placement("IMT Computers", "Lagos").model_copy(update={"scam_warning": True})
    assert company_from_placement(paid, TODAY) is None
    imt = _company("IMT Computers", "imt computers", states=["Lagos"])
    [card] = rank_companies([imt], [paid], "Lagos", TODAY)
    assert card.open_listing is None


def test_rejecting_a_placement_removes_its_evidence():
    store = MemoryStore()
    store.upsert_placement(_placement("Edall Systems", url="https://fb.example/edall"))
    store.upsert_company(company_from_placement(store.placements[0], TODAY))
    assert store.reject_placement(store.placements[0].id)
    assert store.companies[0].evidence == []
    assert store.companies_for("Remote Sensing and GIS") == []      # no evidence left, so not shown


# --- ranking and freshness ----------------------------------------------------
def test_open_listing_first_then_state_then_newest():
    old = _company("Old Co", "old", states=["Lagos"], page_date=date(2025, 6, 1), evidence_url="https://o/1")
    new = _company("New Co", "new", states=["Oyo"], evidence_url="https://n/1")
    lagos = _company("Lagos Co", "lagos co", states=["Lagos"], evidence_url="https://l/1")
    hiring = _company("Hiring Co", "hiring", states=["Kano"], evidence_url="https://h/1")
    cards = rank_companies([old, new, lagos, hiring], [_placement("Hiring Co", "Kano")], "Lagos", TODAY)
    assert [c.company.name for c in cards] == ["Hiring Co", "Lagos Co", "Old Co", "New Co"]
    assert cards[0].open_listing is not None


def test_open_now_needs_more_than_the_same_name():
    greenway_lagos = _company("Greenway Group", "greenway", states=["Lagos"], evidence_url="https://g/1")
    [card] = rank_companies([greenway_lagos], [_placement("Greenway Services", "Oyo")], None, TODAY)
    assert card.open_listing is None


def test_stale_and_undated_evidence_ages_out():
    dated_2023 = _company("Old", "old", page_date=date(2023, 1, 1))
    undated_old = Company(name="U", name_key="u", fields=["Geology"], evidence=[
        Evidence(url="https://u/1", kind="directory", page_date=None, seen_at=date(2025, 6, 1))])
    assert rank_companies([dated_2023, undated_old], [], None, TODAY) == []


def test_evidence_labels_say_what_the_page_showed():
    listing = Evidence(url="u", kind="listing", page_date=date(2026, 3, 4), seen_at=TODAY)
    directory = Evidence(url="u", kind="directory", page_date=None, seen_at=TODAY)
    assert evidence_text(listing) == "Advertised SIWES/IT places (Mar 2026)"
    assert evidence_text(directory) == "Named on a list of companies that take students (undated)"


def test_accented_names_match_plain_spelling():
    from core.dedupe import normalize_org
    assert normalize_org("Nestlé Nigeria Plc") == normalize_org("Nestle Nigeria") == "nestle"


# --- seeds ----------------------------------------------------------------------
def test_seed_file_is_valid_and_seeds_are_invisible_until_evidence():
    seeds = load_seeds()
    assert any(s.name == "Chevron Nigeria" and "spdc" not in s.aliases for s in seeds)
    store = MemoryStore()
    for s in seeds:
        store.upsert_company(s)
    assert store.companies_for("Petroleum Engineering") == []


def test_seed_with_unknown_field_is_rejected(tmp_path):
    bad = tmp_path / "companies.yaml"
    bad.write_text("companies:\n  - name: X Ltd\n    fields: [Basket Weaving]\n", "utf-8")
    with pytest.raises(ValueError, match="Basket Weaving"):
        load_seeds(bad)


def test_seeds_rotate():
    store = MemoryStore(clock=lambda: datetime(2026, 11, 20, tzinfo=timezone.utc))
    for s in load_seeds():
        store.upsert_company(s)
    first = store.seeds_due(3)
    store.mark_checked([c.id for c in first])
    assert {c.id for c in store.seeds_due(3)}.isdisjoint({c.id for c in first})


# --- places and redirects -----------------------------------------------------
@pytest.mark.parametrize("text,state", [
    ("Ibadan", "Oyo"), ("Lekki, Lagos", "Lagos"), ("Port Harcourt", "Rivers"), ("Ile-Ife", "Osun"),
    ("Abuja, Delta, Lagos", None), ("Remote", None), ("Republic of Benin", None),
])
def test_state_from_text(text, state):
    assert state_from_text(text) == state


class _Resp:
    def __init__(self, url):
        self.url = url


def test_google_redirects_are_followed_or_dropped():
    assert resolve_redirect("https://x.com/a") == "https://x.com/a"
    to_fb = lambda url, **kw: _Resp("https://web.facebook.com/groups/1/posts/2")
    assert resolve_redirect("https://google.com/goto?url=CAES", to_fb) == "https://web.facebook.com/groups/1/posts/2"
    stuck = lambda url, **kw: _Resp("https://www.google.com/sorry")
    assert resolve_redirect("https://google.com/goto?url=CAES", stuck) is None

    class FakeDdgs:
        def text(self, *a, **k):
            return [{"href": "https://google.com/goto?url=CAES", "title": "t", "body": "SIWES"}]

    search = WebSearch(serper_key="", ddgs_factory=FakeDdgs, redirect_getter=to_fb)
    [item] = search.search("q")
    assert item.source == "facebook" and item.url.startswith("https://web.facebook.com/")


# --- website --------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    web.limiter.reset()
    store = MemoryStore()
    for p in [_placement("Lagos Maps", "Lagos", url="https://a/1"), _placement("Oyo Maps", "Oyo", url="https://b/1"),
              _placement("Somewhere Maps", None, url="https://c/1")]:
        store.upsert_placement(p)
    store.upsert_company(_company("Sambus Geospatial", "sambus geospatial", states=["FCT"], evidence_url="https://s/1"))
    store.upsert_company(_company("Hidden Co", "hidden", evidence_url=None))
    web.app.state.store = store
    web.app.state.quota = web.RequestQuota()
    return TestClient(web.app)


def _get(client, **params):
    return client.get("/search", params=params, headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1", "HX-Request": "true"})


def test_search_labels_listings_outside_the_chosen_state(client):
    html = _get(client, field="GIS", state="Lagos").text
    assert html.index("In Lagos") < html.index("Elsewhere in Nigeria") < html.index("Location not stated")
    assert "Lagos first" not in html


def test_search_shows_companies_section(client):
    html = _get(client, field="GIS").text
    assert "Companies that take Remote Sensing and GIS students" in html
    assert "Sambus Geospatial" in html and "Advertised SIWES/IT places (Nov 2026)" in html
    assert "Hidden Co" not in html                      # no evidence, not shown
    assert "Not confirmed: call or email before you go" in html


def test_companies_failure_does_not_break_listings(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(client.app.state.store, "companies_for", boom)
    r = _get(client, field="GIS")
    assert r.status_code == 200 and "Lagos Maps" in r.text


def test_web_role_can_read_companies():
    assert "GRANT SELECT ON companies TO siwes_web" in (ROOT / "db" / "web_role.sql").read_text("utf-8")


# --- fixes after the first live GIS search -------------------------------------
def test_any_course_listings_are_kept_apart_from_the_field(client):
    store = client.app.state.store
    store.upsert_placement(Placement(title="Apple-style programme", organization="Big Co", open_to_all=True,
                                     fields=[], sources=[SourceLink(source="telegram", url="https://t.me/x/1")]))
    store.upsert_company(Company(name="Big Co", name_key="big", open_to_all=True, fields=[], evidence=[
        Evidence(url="https://t.me/x/1", kind="listing", page_date=TODAY, seen_at=TODAY)]))
    html = _get(client, field="GIS").text
    field_part, rest = html.split("more open to any course")
    assert "Apple-style programme" not in field_part and "Apple-style programme" in rest
    assert "Big Co" not in html.split("Companies that take")[1]


def test_rejected_listing_found_again_does_not_come_back():
    from collectors.runner import run_pipeline

    store = MemoryStore()
    store.upsert_placement(_placement("Edall Systems", url="https://fb.example/edall"))
    store.reject_placement(store.placements[0].id)
    llm = FakeLLM([{"id": 0, "isPlacementListing": True, "title": "GIS Intern", "organization": "Edall Systems",
                    "fields": ["Remote Sensing and GIS"]}])
    item = RawItem(source="facebook", url="https://fb.example/edall", title="GIS intern", text="SIWES GIS intern")
    counts = run_pipeline(store, llm, {"t": lambda: [item]}, today=TODAY)
    assert counts.get("rejected") == 1 and store.placements[0].status == "rejected"
    assert store.companies == []
