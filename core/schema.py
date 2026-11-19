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

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if value not in SOURCES:
            raise ValueError(f"unknown source {value!r}")
        return value


class Extracted(BaseModel):
    """What the LLM returns for one item. Field names match the prompt."""

    id: int
    isPlacementListing: bool = False
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
