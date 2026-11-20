from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Sources a RawItem can come from. "web" covers search results on ordinary sites;
# social posts found via search keep their platform name so the card can say so.
SOURCES = {
    "web",
    "linkedin",
    "x",
    "facebook",
    "instagram",
    "telegram",
    "nairaland",
    "indeed",
    "jobsite",
}


class RawItem(BaseModel):
    """One candidate post or page, before the LLM has looked at it."""

    source: str
    url: str
    title: str = ""
    text: str = ""
    posted_at: datetime | None = None
    author_handle: str | None = None
    query: str = ""
    # "discovery" = found while looking for companies in a field + state (directories, company
    # pages, SIWES reports): skips the SIWES keyword filter and the seen-url skip.
    purpose: str = "listing"

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if value not in SOURCES:
            raise ValueError(f"unknown source {value!r}")
        return value


class ExtractedCompany(BaseModel):
    """One company named on a "companies that take SIWES students" page."""

    name: str
    location: str | None = None
    state: str | None = None
    fields: list[str] = Field(default_factory=list)
    website: str | None = None
    quote: str | None = None

    @field_validator("fields", mode="before")
    @classmethod
    def _fields_list(cls, value: Any) -> Any:
        return [] if value is None else [value] if isinstance(value, str) else value


class Extracted(BaseModel):
    """What the LLM returns for one item. Field names match the prompt."""

    id: int
    isPlacementListing: bool = False
    # listing | company_list | other. Optional so replies without it still validate.
    pageType: str | None = None
    companies: list[ExtractedCompany] = Field(default_factory=list)
    title: str | None = None
    organization: str | None = None
    location: str | None = None
    state: str | None = None
    fields: list[str] = Field(default_factory=list)
    openToAllFields: bool = False
    deadline: date | None = None
    postedDate: date | None = None
    howToApply: str | None = None
    contact: str | None = None
    applyLink: str | None = None
    scamWarning: bool = False
    summary: str | None = None

    @field_validator("deadline", "postedDate", mode="before")
    @classmethod
    def _blank_date(cls, value: Any) -> Any:
        # Models write "", "unknown" or "N/A" for missing dates.
        if isinstance(value, str):
            value = value.strip()
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return value

    @field_validator("fields", mode="before")
    @classmethod
    def _fields_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class SourceLink(BaseModel):
    source: str
    url: str


class Placement(BaseModel):
    """A cleaned listing as stored and shown on the site."""

    id: int | None = None
    title: str
    organization: str | None = None
    location: str | None = None
    state: str | None = None
    fields: list[str] = Field(default_factory=list)
    open_to_all: bool = False
    deadline: date | None = None
    posted_date: date | None = None
    how_to_apply: str | None = None
    contact: str | None = None
    apply_link: str | None = None
    scam_warning: bool = False
    summary: str | None = None
    sources: list[SourceLink] = Field(default_factory=list)
    fingerprint: str | None = None
    status: str = "open"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class Evidence(BaseModel):
    """Why we think a company takes SIWES/IT students: a page we actually collected."""

    url: str
    # listing: it advertised places | directory: a list of companies that take students named it
    # report: a student's SIWES/IT report names it | profile: a page shows it does the field's work
    kind: str
    page_date: date | None = None  # the page's own date; None = undated
    seen_at: date                  # when we collected it
    quote: str | None = None


class Company(BaseModel):
    """A company students can apply to, with or without an advert open right now."""

    id: int | None = None
    name: str
    name_key: str
    aliases: list[str] = Field(default_factory=list)   # normalized keys that mean the same company
    fields: list[str] = Field(default_factory=list)
    open_to_all: bool = False
    states: list[str] = Field(default_factory=list)
    location: str | None = None
    website: str | None = None
    contact: str | None = None        # only ever from a listing, never from a directory page
    how_to_apply: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    origin: str = "listing"           # listing | directory | seed
    status: str = "active"            # active | hidden
    last_checked_at: datetime | None = None
