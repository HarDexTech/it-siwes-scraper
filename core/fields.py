import re
from functools import lru_cache
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

_CONFIG = Path(__file__).resolve().parents[1] / "config" / "fields.yaml"
_FUZZY_CUTOFF = 85
# What a student may type as a field: letters, spaces, & and - only.
VALID_FIELD = re.compile(r"^[A-Za-z][A-Za-z &\-]{1,59}$")


def _norm(value: str) -> str:
    value = value.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", value)).strip()


@lru_cache(maxsize=1)
def load_fields() -> dict[str, list[str]]:
    data = yaml.safe_load(_CONFIG.read_text("utf-8"))["fields"]
    return {name: list(aliases or []) for name, aliases in data.items()}


@lru_cache(maxsize=1)
def _lookup() -> dict[str, str]:
    """Normalized name/alias -> canonical name."""
    table: dict[str, str] = {}
    for name, aliases in load_fields().items():
        for term in [name, *aliases]:
            table.setdefault(_norm(term), name)
    return table


def canonical_names() -> list[str]:
    return list(load_fields())


def match_field(text: str) -> str | None:
    """Return the canonical field for what a student typed, or None.

    Exact name/alias first, so short codes like "CSC" never fuzz into the wrong
    field; then a fuzzy match for typos and partial names ("Geophysic", "Comp Science").
    """
    key = _norm(text)
    if not key:
        return None
    table = _lookup()
    if key in table:
        return table[key]
    if len(key) < 4:
        return None
    best = process.extractOne(key, list(table), scorer=fuzz.ratio, score_cutoff=_FUZZY_CUTOFF)
    return table[best[0]] if best else None


def clean_requested(text: str) -> str | None:
    """Normalize a field nobody has tagged yet, or None if it isn't plausible."""
    text = re.sub(r"\s+", " ", text).strip()
    if not VALID_FIELD.match(text):
        return None
    words = text.split()
    # A real department name is 1-6 words and each word has a vowel.
    if len(words) > 6 or any(not re.search(r"[aeiouyAEIOUY]", w) and len(w) > 4 for w in words):
        return None
    return text.title()
