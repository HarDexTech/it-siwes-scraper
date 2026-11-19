"""The public site. It only reads the database (plus queueing requested fields).

Nothing here may import collectors.x_browser or scripts/run_x.py - students can
never trigger X scraping (tests/test_app.py checks this).
"""

import hashlib
import hmac
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from core.db import MemoryStore, open_store
from core.extract import STATES
from core.fields import VALID_FIELD, canonical_names, clean_requested, match_field

load_dotenv()
logger = logging.getLogger("app")

MIN_RESULTS = 5
STATIC = Path(__file__).parent / "static"
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Built by `npm run build`; the hash in the URL makes browsers refetch it after a rebuild.
_css = STATIC / "app.css"
TEMPLATES.env.globals["css_version"] = (
    hashlib.sha1(_css.read_bytes()).hexdigest()[:10] if _css.exists() else "dev"
)
LAGOS = timezone(timedelta(hours=1))


def site_name(url: str) -> str:
    """"https://www.myjobmag.com/job/..." -> "myjobmag.com", for source link labels."""
    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")


TEMPLATES.env.filters["site_name"] = site_name


def client_ip(request: Request) -> str:
    """The visitor's IP, read from X-Forwarded-For when behind a proxy.

    Render's proxy appends the real client IP to X-Forwarded-For, so the entry
    TRUSTED_PROXY_HOPS from the right is the one a visitor can't forge (anything
    further left is whatever the client sent).
    """
    hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
    forwarded = request.headers.get("x-forwarded-for")
    if hops > 0 and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else "unknown"


class RequestQuota:
    """Caps on queueing requested fields: per IP per day, and site-wide per day.

    A visitor counts once per field per day, so repeating a search can't push a
    field up the queue. In memory, like slowapi's store - fine for one instance.
    """

    def __init__(self, per_ip: int = 10, site_wide: int = 200) -> None:
        self.per_ip = per_ip
        self.site_wide = site_wide
        self._day: date | None = None
        self._by_ip: dict[str, set[str]] = {}
        self._total = 0
        self._lock = threading.Lock()

    def allow(self, ip: str, name: str, today: date | None = None) -> bool:
        today = today or datetime.now(LAGOS).date()
        with self._lock:
            if self._day != today:
                self._day, self._by_ip, self._total = today, {}, 0
            names = self._by_ip.setdefault(ip, set())
            if name in names or len(names) >= self.per_ip or self._total >= self.site_wide:
                return False
            names.add(name)
            self._total += 1
            return True


limiter = Limiter(key_func=client_ip, default_limits=["60/minute"])
app = FastAPI(title="SIWES Placement Finder", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.state.store = open_store(os.environ.get("DATABASE_URL"))
app.state.quota = RequestQuota()
if not os.environ.get("DATABASE_URL"):
    logger.warning("DATABASE_URL is not set: serving an empty in-memory store, no real listings")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _message(request: Request, text: str, status: int) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "message.html", {"text": text}, status_code=status)


@app.exception_handler(RateLimitExceeded)
def _rate_limited(request: Request, exc: RateLimitExceeded) -> Response:
    logger.warning("rate limit hit: %s %s (%s)", client_ip(request), request.url.path, exc.detail)
    if request.url.path.startswith("/admin"):
        return Response(status_code=404)
    return _message(request, "Too many searches, please wait a minute and try again.", 429)


def _base_context() -> dict[str, Any]:
    return {
        "fields": canonical_names(),
        "states": sorted(STATES - {"FCT"}) + ["FCT"],
        "contact_email": os.environ.get("CONTACT_EMAIL", ""),
        "today": datetime.now(LAGOS).date(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", _base_context())


@app.get("/search", response_class=HTMLResponse)
@limiter.limit("20/minute;300/day")
def search(request: Request, field: str = "", state: str = "") -> HTMLResponse:
    field = " ".join(field.split())
    if not VALID_FIELD.match(field):
        return _message(request, "Please type your course or field using letters only, e.g. Computer Science.", 400)
    if state and state not in STATES:
        return _message(request, "Please choose a state from the list.", 400)

    store = request.app.state.store
    canonical = match_field(field)
    results = store.search(canonical, field, state or None)
    queued = None
    if len(results) < MIN_RESULTS:
        name = canonical or clean_requested(field)
        if name and request.app.state.quota.allow(client_ip(request), name):
            store.request_field(name, canonical is not None)
        queued = name

    context = {**_base_context(), "field": field, "state": state, "canonical": canonical,
               "results": results, "queued": queued}
    template = "results.html" if _is_htmx(request) else "index.html"
    return TEMPLATES.TemplateResponse(request, template, context)


@app.get("/admin/health", response_class=HTMLResponse)
@limiter.limit("5/minute")
def health(request: Request, token: str = "") -> Response:
    expected = os.environ.get("ADMIN_TOKEN", "")
    given = request.headers.get("x-admin-token") or token
    # A missing or wrong token looks exactly like a page that doesn't exist.
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return Response(status_code=404)
    data = request.app.state.store.health()
    return TEMPLATES.TemplateResponse(request, "health.html", {"data": data, "now": datetime.now(timezone.utc)})


@app.get("/healthz")
@limiter.exempt
def healthz(request: Request) -> dict[str, str]:
    # "memory" on a deployed site means DATABASE_URL is missing and searches show nothing.
    store = request.app.state.store
    return {"status": "ok", "database": "memory" if isinstance(store, MemoryStore) else "postgres"}
