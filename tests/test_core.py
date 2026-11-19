import json
from datetime import date, datetime, timedelta, timezone

import pytest

from collectors.queries import field_queries, plan_fields, search_terms
from collectors.runner import run_pipeline
from collectors.search import source_for
from core.dedupe import fingerprint, merge, normalize_url
from core.extract import _parse_json, extract, is_expired, passes_prefilter
from core.fields import clean_requested, match_field
from core.llm_client import MockLLMClient
from core.db import MemoryStore
from core.scam import looks_like_scam
from core.schema import Placement, RawItem, SourceLink

TODAY = date(2026, 11, 18)


# --- field matching ----------------------------------------------------------
@pytest.mark.parametrize(
    "typed, expected",
    [
        ("CSC", "Computer Science"),
        ("computer science", "Computer Science"),
        ("Comp Sci", "Computer Science"),
        ("Geophysics", "Applied Geophysics"),
        ("applied geophysic", "Applied Geophysics"),
        ("RSG", "Remote Sensing and GIS"),
        ("GIS", "Remote Sensing and GIS"),
        ("Remote Sensing", "Remote Sensing and GIS"),
        ("Applied Geology", "Geology"),
        ("Fisheries", "Fisheries and Aquaculture"),
        ("Underwater Basket Weaving", None),
        ("xq", None),
    ],
)
def test_match_field(typed, expected):
    assert match_field(typed) == expected


def test_clean_requested_rejects_junk():
    assert clean_requested("marine biology") == "Marine Biology"
    assert clean_requested("<script>") is None
    assert clean_requested("a" * 80) is None
    assert clean_requested("xkcdqwrtzpl") is None


# --- scam rules --------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Pay N5,000 acceptance fee to secure your slot",
        "registration fee of ₦3000 applies",
        "Interested students should make payment to account number: 0123456789",
        "Form fee 2k only",
        "pay ngn 10000 before resumption",
        "Training fee covers logbook support",
        "Tuition: 50k",
        "Course cost N30,000 for SIWES students",
        "Enrol now: SIWES training N25k",
        "N15,000 for the training, limited slots",
        "Buy our SIWES package today",
    ],
)
def test_scam_flagged(text):
    assert looks_like_scam(text)


@pytest.mark.parametrize(
    "text",
    [
        "Monthly stipend of N30,000 for IT students",
        "During the training, students get a stipend of N30,000",
        "Transport allowance of N20,000 for training",
        "SIWES students in Computer Science should send CV to hr@example.com",
        "No fees required. Apply via the link.",
        "No registration fee. Apply via the link.",
        # Honest posts saying what they DON'T charge must not get a scam badge.
        "No training fee required",
        "Free training, no course fee",
        "course fee waived",
        "Training fee: none",
        "We do not charge any training fee",
        "Training fees are fully sponsored",
        "Never pay a training fee to anyone",
        "Tuition-free programme",
    ],
)
def test_scam_not_flagged(text):
    assert not looks_like_scam(text)


# --- dedupe ------------------------------------------------------------------
def test_normalize_url_strips_tracking_and_unifies_x():
    assert normalize_url("https://twitter.com/acme/status/1?utm_source=a&s=20") == "https://x.com/acme/status/1"
    assert normalize_url("https://www.Example.com/job/?id=4&fbclid=x#top") == "https://example.com/job?id=4"


def test_fingerprint_ignores_company_suffixes():
    a = fingerprint("Acme Nigeria Limited", "Lagos", date(2026, 12, 1))
    b = fingerprint("ACME Ltd.", "lagos", date(2026, 12, 20))
    assert a == b
    assert fingerprint(None, "Lagos", None) is None
    assert fingerprint("Acme", "Oyo", None) != fingerprint("Acme", "Lagos", None)


def _placement(url, **kw):
    base = dict(title="SIWES at Acme", organization="Acme Ltd", state="Lagos", fields=["Computer Science"],
                sources=[SourceLink(source="web", url=url)])
    base.update(kw)
    p = Placement(**base)
    p.fingerprint = fingerprint(p.organization, p.state, p.deadline)
    return p


def test_merge_keeps_stored_values_and_adds_sources():
    a = _placement("https://a.com/1", contact=None)
    b = _placement("https://x.com/acme/status/2", contact="hr@acme.ng", fields=["Physics"], scam_warning=True,
                   sources=[SourceLink(source="x", url="https://x.com/acme/status/2")])
    m = merge(a, b)
    assert m.contact == "hr@acme.ng"
    assert [s.source for s in m.sources] == ["web", "x"]
    assert m.fields == ["Computer Science", "Physics"]
    assert m.scam_warning


def test_store_merges_same_placement_from_two_sites():
    store = MemoryStore()
    assert store.upsert_placement(_placement("https://a.com/1")) == "inserted"
    assert store.upsert_placement(_placement("https://nairaland.com/2",
                                             sources=[SourceLink(source="nairaland", url="https://nairaland.com/2")])) == "merged"
    assert len(store.placements) == 1
    assert len(store.placements[0].sources) == 2


# --- expiry ------------------------------------------------------------------
def test_expiry_rules():
    assert is_expired(TODAY - timedelta(days=1), None, None, TODAY)
    assert not is_expired(TODAY, None, None, TODAY)
    assert not is_expired(None, TODAY - timedelta(days=60), None, TODAY)
    assert is_expired(None, TODAY - timedelta(days=61), None, TODAY)
    old_seen = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert is_expired(None, None, old_seen, TODAY)
    assert not is_expired(None, None, None, TODAY)


def test_store_expire_uses_first_seen_when_no_dates():
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    store = MemoryStore(clock=lambda: now)
    store.upsert_placement(_placement("https://a.com/1"))
    store.placements[0].first_seen_at = now - timedelta(days=61)
    store.upsert_placement(_placement("https://b.com/2", organization="Other Co"))
    assert store.expire() == 1
    assert [p.status for p in store.placements] == ["expired", "open"]


# --- extraction --------------------------------------------------------------
class FakeLLM:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def complete(self, system, user, json_mode=True):
        self.calls += 1
        return "```json\n" + json.dumps({"items": self.records}) + "\n```", "fake/model"


def _item(n, text="SIWES placement for Computer Science students", source="web"):
    return RawItem(source=source, url=f"https://site.com/{n}", title=f"post {n}", text=text)


def test_parse_json_accepts_bare_list_and_object():
    assert _parse_json('[{"id": 0}]') == [{"id": 0}]
    assert _parse_json('{"items": [{"id": 1}]}') == [{"id": 1}]


def test_extract_validates_and_cleans():
    llm = FakeLLM([
        {"id": 0, "isPlacementListing": True, "title": "IT at Acme", "organization": "Acme", "state": "Abuja",
         "fields": ["Computer Science", "Basket Weaving"], "deadline": "2030-01-01", "postedDate": "unknown",
         "applyLink": "javascript:alert(1)", "howToApply": "Pay N2,000 registration fee then apply"},
        {"id": 1, "isPlacementListing": False},
        {"id": 7, "isPlacementListing": True, "title": "out of range id"},
        {"id": "bad"},
    ])
    placements, processed, calls = extract([_item(0), _item(1)], llm, today=TODAY)
    assert calls == 1 and len(processed) == 2
    [p] = placements
    assert p.state == "FCT"
    assert p.fields == ["Computer Science"]          # non-canonical field dropped
    assert p.deadline is None                         # > 1 year away is a misread
    assert p.apply_link is None                       # non-http link dropped
    assert p.scam_warning                             # rule-based, even though the LLM said no
    assert p.sources[0].url == "https://site.com/0"


def test_extract_drops_listing_with_nobody_to_apply_to():
    # A results page the model wrongly kept: no organization, no contact. A social
    # post with no organization is kept, because the poster's handle is the contact.
    llm = FakeLLM([
        {"id": 0, "isPlacementListing": True, "title": "SIWES jobs in Lagos (477)", "organization": None},
        {"id": 1, "isPlacementListing": True, "title": "Taking IT students, DM me", "organization": None},
    ])
    x_post = RawItem(source="x", url="https://x.com/ade/status/9", title="", text="Taking IT students",
                     author_handle="ade")
    placements, processed, _ = extract([_item(0), x_post], llm, today=TODAY)
    assert len(processed) == 2
    [p] = placements
    assert p.contact == "@ade on X"


def test_extract_drops_telegram_repost_and_aggregator_pages():
    # A channel reposting a roundup is not a contact; a results page is dropped even
    # when the model names the job board as the organization.
    llm = FakeLLM([
        {"id": 0, "isPlacementListing": True, "title": "50 companies taking SIWES students", "organization": None},
        {"id": 1, "isPlacementListing": True, "title": "SIWES jobs in Lagos", "organization": "Indeed"},
        {"id": 2, "isPlacementListing": True, "title": "IT jobs", "organization": "Acme"},
        {"id": 3, "isPlacementListing": True, "title": "IT at Beta", "organization": "Beta"},
    ])
    items = [
        RawItem(source="telegram", url="https://t.me/jobsregion/5", title="t.me/jobsregion", text="SIWES",
                author_handle="jobsregion"),
        RawItem(source="web", url="https://ng.indeed.com/jobs?q=siwes", title="", text="SIWES"),
        RawItem(source="web", url="https://ng.indeed.com/q-siwes-l-lagos-jobs.html", title="", text="SIWES"),
        RawItem(source="telegram", url="https://t.me/jobsregion/6", title="t.me/jobsregion", text="SIWES",
                author_handle="jobsregion"),
    ]
    placements, processed, _ = extract(items, llm, today=TODAY)
    assert len(processed) == 4
    [p] = placements
    assert p.organization == "Beta" and p.contact is None   # kept, but the channel isn't its contact


def test_extract_respects_call_cap_and_leaves_rest_unprocessed():
    llm = MockLLMClient()
    items = [_item(i) for i in range(12)]
    placements, processed, calls = extract(items, llm, batch_size=5, max_calls=2, today=TODAY)
    assert calls == 2
    assert len(processed) == 10


def test_prefilter():
    assert passes_prefilter(_item(0, "We are accepting IT students for industrial training"))
    assert passes_prefilter(_item(0, "Internship for undergraduates"))
    assert not passes_prefilter(_item(0, "Buy cheap phones in Ikeja"))


# --- queries / search --------------------------------------------------------
def test_queries_skip_short_codes():
    assert search_terms("Computer Science") == '("Computer Science" OR "Comp Sci" OR "Computer Sci")'
    assert search_terms("Fisheries Science") == '"Fisheries Science"'
    assert len(field_queries("Geology")) == 4


def test_plan_fields_puts_requested_first_within_budget():
    fields = plan_fields(["Fisheries Science", "Geology"], query_budget=24)
    assert fields[:2] == ["Fisheries Science", "Geology"]
    assert len(fields) == 5    # (24 - 4 general) // 4 per field


def test_source_for():
    assert source_for("https://www.linkedin.com/posts/abc") == "linkedin"
    assert source_for("https://twitter.com/a/status/1") == "x"
    assert source_for("https://ng.jobberman.com/x") == "jobsite"
    assert source_for("https://acme.ng/careers") == "web"


# --- pipeline ----------------------------------------------------------------
def test_pipeline_end_to_end_skips_seen_and_survives_broken_collector():
    store = MemoryStore()
    items = [_item(0), _item(1, "Buy cheap phones"), _item(0)]  # duplicate + irrelevant

    def broken():
        raise RuntimeError("site changed")

    counts = run_pipeline(store, MockLLMClient(), {"good": lambda: items, "bad": broken},
                          fields_hint=["Computer Science"], today=TODAY)
    assert counts["errors"] == {"bad": "site changed"}
    assert counts["new"] == 2 and counts["prefiltered"] == 1 and counts["inserted"] == 1
    assert store.seen_urls == {"https://site.com/0", "https://site.com/1"}

    again = run_pipeline(store, MockLLMClient(), {"good": lambda: items}, today=TODAY)
    assert again["new"] == 0 and again["llm_calls"] == 0
