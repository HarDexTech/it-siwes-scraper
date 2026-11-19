import re

# Legitimate SIWES placements never charge students. These phrases show up in
# the "pay N5,000 acceptance fee to secure your slot" posts.
_PATTERNS = [
    r"acceptance\s+fee",
    r"registration\s+fee",
    r"processing\s+fee",
    r"application\s+fee",
    r"form\s+fee",
    r"pay\s+(?:the\s+)?(?:sum|fee|amount)",
    r"(?:make|send)\s+(?:a\s+)?payment",
    # A naira amount only counts next to pay/fee words: "stipend of N30,000" is fine.
    r"\b(?:pay|paying|fee|payment)\b[^.\n]{0,40}(?:₦|\bngn\s?|\bn)\s?\d",
    r"(?:₦|\bngn\s?|\bn)\s?\d[\d,]*k?\b[^.\n]{0,30}\b(?:fee|payment)\b",
    r"\b\d+k\s+(?:fee|only)\b",
    r"account\s+(?:number|no\.?)\s*[:\-]?\s*\d{10}",
]
_SCAM = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def looks_like_scam(*texts: str | None) -> bool:
    return any(_SCAM.search(t) for t in texts if t)
