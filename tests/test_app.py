import ast
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as web
from core.db import MemoryStore
from core.schema import Placement, SourceLink

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-token")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    web.limiter.reset()
    store = MemoryStore()
    store.upsert_placement(Placement(
        title="IT placement at Acme", organization="Acme", state="Lagos", fields=["Computer Science"],
        deadline=date(2099, 1, 1), sources=[SourceLink(source="x", url="https://x.com/acme/status/1")],
    ))
    web.app.state.store = store
    web.app.state.quota = web.RequestQuota(per_ip=10, site_wide=200)
    return TestClient(web.app)


def ip(n):
    # Client-sent junk on the left, Render's appended real IP on the right.
    return {"X-Forwarded-For": f"6.6.6.6, 10.0.0.{n}", "HX-Request": "true"}


def test_search_finds_by_alias(client):
    r = client.get("/search", params={"field": "CSC", "state": "Lagos"}, headers=ip(1))
    assert r.status_code == 200
    assert "IT placement at Acme" in r.text
    assert "View on X post" in r.text


def test_full_page_without_htmx(client):
    r = client.get("/search", params={"field": "Computer Science"})
    assert "<html" in r.text and "IT placement at Acme" in r.text


def test_few_results_queues_field_once_per_ip(client):
    store = client.app.state.store
    for _ in range(3):
        r = client.get("/search", params={"field": "Fisheries"}, headers=ip(1))
        assert "added it to our next search" in r.text
    client.get("/search", params={"field": "Fisheries"}, headers=ip(2))
    assert store.requested["Fisheries and Aquaculture"]["request_count"] == 2


def test_per_ip_daily_cap_on_new_fields(client):
    store = client.app.state.store
    names = ["Marine Biology", "Ocean Studies", "Brewing Science", "Textile Science", "Leather Technology",
             "Glass Technology", "Printing Technology", "Fashion Design", "Sports Science", "Dance Studies",
             "Puppetry Arts"]
    for name in names:
        client.get("/search", params={"field": name}, headers=ip(3))
    assert len(store.requested) == 10
    assert "Puppetry Arts" not in store.requested


def test_site_wide_cap(client):
    client.app.state.quota = web.RequestQuota(per_ip=10, site_wide=2)
    for n, name in enumerate(["Marine Biology", "Ocean Studies", "Brewing Science"]):
        client.get("/search", params={"field": name}, headers=ip(10 + n))
    assert len(client.app.state.store.requested) == 2


@pytest.mark.parametrize("field", ["<script>alert(1)</script>", "a" * 500, "", "DROP TABLE;--"])
def test_bad_input_400(client, field):
    r = client.get("/search", params={"field": field}, headers=ip(4))
    assert r.status_code == 400
    assert "letters only" in r.text


def test_bad_state_400(client):
    r = client.get("/search", params={"field": "Geology", "state": "Atlantis"}, headers=ip(4))
    assert r.status_code == 400


def test_rate_limit_21st_search_gets_429(client):
    for _ in range(20):
        assert client.get("/search", params={"field": "Geology"}, headers=ip(5)).status_code == 200
    r = client.get("/search", params={"field": "Geology"}, headers=ip(5))
    assert r.status_code == 429
    assert "Too many searches" in r.text
    # A different visitor is unaffected; spoofing the left of X-Forwarded-For doesn't help.
    assert client.get("/search", params={"field": "Geology"}, headers=ip(6)).status_code == 200
    spoof = {"X-Forwarded-For": "1.2.3.4, 10.0.0.5", "HX-Request": "true"}
    assert client.get("/search", params={"field": "Geology"}, headers=spoof).status_code == 429


def test_admin_needs_token(client):
    assert client.get("/admin/health", headers=ip(7)).status_code == 404
    assert client.get("/admin/health", headers={**ip(7), "X-Admin-Token": "wrong"}).status_code == 404
    r = client.get("/admin/health", headers={**ip(7), "X-Admin-Token": "s3cret-token"})
    assert r.status_code == 200 and "Collector health" in r.text


def test_admin_rate_limited(client):
    for _ in range(5):
        client.get("/admin/health", headers={**ip(8), "X-Admin-Token": "wrong"})
    r = client.get("/admin/health", headers={**ip(8), "X-Admin-Token": "s3cret-token"})
    assert r.status_code == 404  # limited, and still indistinguishable from "no such page"


def test_website_never_imports_x_code():
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            assert not any("x_browser" in n or "run_x" in n for n in names), f"{path} imports X code"


def test_stylesheet_is_built_and_served(client):
    # The page uses a CSS file built by `npm run build`, not the Tailwind CDN script.
    page = client.get("/")
    assert "/static/app.css?v=" in page.text and "cdn.tailwindcss.com" not in page.text
    css = client.get("/static/app.css")
    assert css.status_code == 200 and ".bg-emerald-700" in css.text


def test_web_sources_are_labelled_by_site(client):
    client.app.state.store.upsert_placement(Placement(
        title="IT at Beta", organization="Beta", fields=["Computer Science"], deadline=date(2099, 1, 1),
        sources=[SourceLink(source="jobsite", url="https://www.myjobmag.com/job/1"),
                 SourceLink(source="jobsite", url="https://jobberman.com/j/2")],
    ))
    r = client.get("/search", params={"field": "CSC"}, headers=ip(9))
    assert "View on myjobmag.com" in r.text and "View on jobberman.com" in r.text


def test_healthz_says_which_database(client):
    # The fixture uses the in-memory store, as a deploy with no DATABASE_URL would.
    assert client.get("/healthz").json() == {"status": "ok", "database": "memory"}
