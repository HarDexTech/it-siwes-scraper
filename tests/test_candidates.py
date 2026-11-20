import json
from datetime import date

from collectors.candidates import candidate_companies, verify, work_terms
from core.schema import RawItem

TODAY = date(2026, 11, 20)
TERMS = work_terms("Remote Sensing and GIS", ["GIS", "geospatial", "mapping", "surveying", "services"])


def _r(url, title, text):
    return RawItem(source="web", url=url, title=title, text=text)


def test_work_terms_drop_generic_words():
    assert "services" not in TERMS and {"gis", "geospatial", "mapping", "remote sensing and gis"} <= set(TERMS)


def test_company_on_its_own_site_is_verified():
    page = _r("https://geoinfotech.ng/", "Geoinfotech Resources Ltd",
              "Geoinfotech Resources Ltd provides GIS, drone mapping and surveying from Ikeja, Lagos.")
    found = verify("Geoinfotech Resources Ltd", TERMS, "Lagos", [page])
    assert found and found[0].url == "https://geoinfotech.ng/" and "geoinfotech" in found[1]


def test_similar_name_is_not_enough():
    # "Polaris Digitech" must not pass on a page about Polaris Bank.
    bank = _r("https://polarisbanklimited.com/", "Polaris Bank", "Polaris Bank, Lagos. GIS-enabled branch mapping.")
    assert verify("Polaris Digitech", TERMS, "Lagos", [bank]) is None


def test_work_word_inside_the_name_proves_nothing():
    page = _r("https://www.geospatialresearch.example/", "Geospatial Research Ltd", "Geospatial Research Ltd, Lagos.")
    assert verify("Geospatial Research Ltd", TERMS, "Lagos", [page]) is None


def test_wrong_state_fails():
    page = _r("https://www.geofirm.example/", "Geo Firm Ltd", "Geo Firm Ltd does GIS mapping in Abuja.")
    assert verify("Geo Firm Ltd", TERMS, "Lagos", [page]) is None
    assert verify("Geo Firm Ltd", TERMS, "FCT", [page])


def test_name_work_and_place_must_be_close():
    far = "Geo Firm Ltd. " + "x " * 400 + "GIS mapping in Lagos."
    page = _r("https://www.geofirm.example/", "Geo Firm Ltd", far)
    assert verify("Geo Firm Ltd", TERMS, "Lagos", [page]) is None


def test_category_page_with_many_firms_is_not_proof_for_one():
    category = _r("https://www.finelib.com/cities/lagos/business/mapping-gis", "Mapping & GIS companies in Lagos",
                  "Geo Firm Ltd, Ikeja. Map Co, Yaba. GIS and surveying firms in Lagos.")
    assert verify("Geo Firm Ltd", TERMS, "Lagos", [category]) is None
    listing = category.model_copy(update={"url": "https://www.finelib.com/listing/Geo-Firm-Ltd/1/"})
    assert verify("Geo Firm Ltd", TERMS, "Lagos", [listing])


def test_fetched_page_is_used_when_the_snippet_is_thin():
    page = _r("https://geofirm.ng/", "Geo Firm", "Welcome")
    fetch = lambda url: "Geo Firm Ltd offers GIS and drone mapping from Lekki, Lagos."
    assert verify("Geo Firm Ltd", TERMS, "Lagos", [page], fetch)


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, json_mode=True):
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply), "fake"


class FakeSearch:
    def __init__(self, pages):
        self.pages = pages
        self.queries = []

    def search(self, query, recent=True):
        assert recent is False
        self.queries.append(query)
        return [p for name, p in self.pages.items() if f'"{name}"' in query]


def test_end_to_end_keeps_only_verified_and_skips_known():
    llm = FakeLLM({"work_terms": ["GIS", "mapping"], "companies": [
        {"name": "Geo Firm Ltd", "city": "Ikeja", "does": "GIS"},
        {"name": "Made Up Spatial", "city": "Yaba", "does": "GIS"},
        {"name": "Known Co", "city": "Lekki", "does": "GIS"},
    ]})
    search = FakeSearch({"Geo Firm Ltd": _r("https://geofirm.ng/", "Geo Firm Ltd",
                                            "Geo Firm Ltd: GIS and mapping, Ikeja, Lagos.")})
    found, stats = candidate_companies(llm, search, "Remote Sensing and GIS", "Lagos", known=["Known Co"],
                                       max_checks=10, today=TODAY)
    [c] = found
    assert c.name == "Geo Firm Ltd" and c.website == "https://geofirm.ng" and c.evidence[0].kind == "profile"
    assert c.fields == ["Remote Sensing and GIS"] and c.states == ["Lagos"] and c.contact is None
    assert stats == {"suggested": 3, "grounded": 0, "checked": 2, "verified": 1}
    assert not any("Known Co" in q for q in search.queries)          # already listed: no search spent


def test_check_budget_is_respected():
    llm = FakeLLM({"work_terms": ["GIS"], "companies": [{"name": f"Firm {i} Ltd"} for i in range(10)]})
    search = FakeSearch({})
    _, stats = candidate_companies(llm, search, "Remote Sensing and GIS", "Lagos", known=[], max_checks=3,
                                   today=TODAY)
    assert stats["checked"] == 3 and len(search.queries) == 3


def test_bad_llm_reply_means_no_candidates():
    found, stats = candidate_companies(FakeLLM("sorry, I can't"), FakeSearch({}), "Geology", "Lagos", known=[],
                                       max_checks=5, today=TODAY)
    assert found == [] and stats["suggested"] == 0


# --- grounded suggestions (Gemini searching Google) ---------------------------------
class _Resp:
    def __init__(self, status, payload=None, url=None):
        self.status_code, self._payload, self.url = status, payload, url

    def json(self):
        return self._payload


def _grounded_reply(companies, sources):
    text = "Here you go:\n```json\n" + json.dumps({"work_terms": ["GIS", "mapping"], "companies": companies}) + "\n```"
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "groundingMetadata": {
        "groundingChunks": [{"web": {"title": d, "uri": u}} for d, u in sources]}}]})


def test_grounded_firm_is_checked_on_its_own_site_without_a_search():
    post = lambda *a, **k: _grounded_reply(
        [{"name": "Firstline Geospatial Services Limited", "city": "Ikeja"}],
        [("geospatial.ng", "https://redirect/1"), ("firstlinegeospatial.com", "https://redirect/2")])
    follow = lambda url: {"https://redirect/2": "https://firstlinegeospatial.com/about"}.get(url)
    fetch = lambda url: "Firstline Geospatial Services Limited: GIS and mapping surveys, Ikeja, Lagos."
    search = FakeSearch({})
    found, stats = candidate_companies(None, search, "Remote Sensing and GIS", "Lagos", known=[], max_checks=5,
                                       today=TODAY, fetch=fetch, grounded_model="gemini-2.5-flash", api_key="k",
                                       post=post, follow=follow)
    [c] = found
    assert c.evidence[0].url == "https://firstlinegeospatial.com/about" and c.website == "https://firstlinegeospatial.com"
    assert search.queries == [] and stats["grounded"] == 1 and stats["checked"] == 0


def test_grounded_firm_without_its_own_source_falls_back_to_a_search():
    post = lambda *a, **k: _grounded_reply([{"name": "Geo Firm Ltd"}], [("someblog.com", "https://redirect/9")])
    search = FakeSearch({"Geo Firm Ltd": _r("https://geofirm.ng/", "Geo Firm Ltd", "Geo Firm Ltd: GIS, Ikeja, Lagos.")})
    found, stats = candidate_companies(None, search, "Remote Sensing and GIS", "Lagos", known=[], max_checks=5,
                                       today=TODAY, fetch=lambda u: "", grounded_model="m", api_key="k", post=post,
                                       follow=lambda u: None)
    assert [c.name for c in found] == ["Geo Firm Ltd"] and stats["checked"] == 1


def test_failed_grounding_spends_nothing_and_asks_for_a_retry():
    post = lambda *a, **k: _Resp(429)
    llm = FakeLLM({"work_terms": ["GIS"], "companies": [{"name": "Geo Firm Ltd"}]})
    search = FakeSearch({})
    found, stats = candidate_companies(llm, search, "Remote Sensing and GIS", "Lagos", known=[], max_checks=5,
                                       today=TODAY, grounded_model="m", api_key="k", post=post)
    assert found == [] and stats["grounding_failed"] == 1 and search.queries == []


def test_grounding_retries_when_google_is_busy():
    from collectors.candidates import grounded_suggest

    replies = [_Resp(503), _grounded_reply([{"name": "Geo Firm Ltd"}], [])]
    out = grounded_suggest("Remote Sensing and GIS", "Lagos", [], 5, model="m", api_key="k",
                           post=lambda *a, **k: replies.pop(0), sleep=lambda s: None)
    assert out and out[0][0]["name"] == "Geo Firm Ltd"


def test_serper_hiccup_is_retried_not_abandoned():
    from collectors.search import WebSearch

    calls = []

    class Ok:
        def raise_for_status(self):
            pass

        def json(self):
            return {"organic": [{"link": "https://a.example/1", "title": "t", "snippet": "s"}]}

    def post(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("Server disconnected without sending a response.")
        return Ok()

    search = WebSearch(serper_key="k", getter=post, ddgs_factory=lambda: (_ for _ in ()).throw(AssertionError))
    assert [it.url for it in search.search("q")] == ["https://a.example/1"]
    assert search._serper_key == "k" and len(calls) == 2


def test_grounded_site_opened_directly_when_the_redirect_fails():
    post = lambda *a, **k: _grounded_reply([{"name": "Kings Geo Consult"}], [("kingsgeoconsult.com", "https://redirect/3")])
    opened = []

    def fetch(url):
        opened.append(url)
        return "Kings Geo Consult: licensed land surveyors in Lagos, GIS and drone mapping."

    search = FakeSearch({})
    found, stats = candidate_companies(None, search, "Remote Sensing and GIS", "Lagos", known=[], max_checks=5,
                                       today=TODAY, fetch=fetch, grounded_model="m", api_key="k", post=post,
                                       follow=lambda u: None)
    assert opened == ["https://kingsgeoconsult.com/"] and len(found) == 1 and search.queries == []
