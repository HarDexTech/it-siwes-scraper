import hashlib
import re
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from .schema import Placement

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|igshid$|mc_|ref$|ref_src$|s$|t$|si$|trk|ref_)", re.I)
_ORG_NOISE = re.compile(
    r"\b(limited|ltd|plc|nig|nigeria|inc|llc|company|co|the|group|services|enterprises?)\b"
)


def normalize_url(url: str) -> str:
    """Lowercase host, drop tracking params and fragments, and unify x/twitter hosts."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    if host == "twitter.com":
        host = "x.com"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def normalize_org(name: str | None) -> str:
    if not name:
        return ""
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", _ORG_NOISE.sub(" ", name)).strip()


def fingerprint(organization: str | None, state: str | None, deadline: date | None) -> str | None:
    """Identity for the same placement posted on several sites.

    No organization means we can't tell two posts apart safely, so they only
    merge on URL.
    """
    org = normalize_org(organization)
    if not org:
        return None
    key = "|".join([org, (state or "").strip().lower(), deadline.strftime("%Y-%m") if deadline else "-"])
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def merge(existing: "Placement", new: "Placement") -> "Placement":
    """Fold a newly seen copy of a placement into the stored one.

    Stored values win; the new copy only fills blanks, adds source links and
    fields, and can raise (never clear) the scam flag.
    """
    data = existing.model_dump()
    for key, value in new.model_dump().items():
        if data.get(key) in (None, "", []) and value not in (None, "", []):
            data[key] = value
    seen = {s.url for s in existing.sources}
    data["sources"] = [s.model_dump() for s in existing.sources] + [
        s.model_dump() for s in new.sources if s.url not in seen
    ]
    data["fields"] = sorted(set(existing.fields) | set(new.fields))
    data["open_to_all"] = existing.open_to_all or new.open_to_all
    data["scam_warning"] = existing.scam_warning or new.scam_warning
    if existing.status == "expired" and new.status == "open":
        data["status"] = "open"
    return type(existing).model_validate(data)
