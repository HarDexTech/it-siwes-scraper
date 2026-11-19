import re

# Legitimate SIWES placements never charge students. These phrases show up in
# the "pay N5,000 acceptance fee to secure your slot" posts.
_FEE = r"(?:acceptance|registration|processing|application|form|training|course|tuition)\s+fees?"
_NAIRA = r"(?:₦|\bngn\s?|\bn)\s?\d"
_PATTERNS = [
    _FEE,
    r"pay\s+(?:the\s+)?(?:sum|fee|amount)",
    r"(?:make|send)\s+(?:a\s+)?payment",
    # A naira amount only counts next to pay/fee words: "stipend of N30,000" is fine.
    rf"\b(?:pay|paying|fee|payment)\b[^.\n]{{0,40}}{_NAIRA}",
    r"(?:₦|\bngn\s?|\bn)\s?\d[\d,]*k?\b[^.\n]{0,30}\b(?:fee|payment)\b",
    r"\b\d+k\s+(?:fee|only)\b",
    r"account\s+(?:number|no\.?)\s*[:\-]?\s*\d{10}",
    # Paid training sold as a placement: "Course cost N30,000", "SIWES training N25k", "Tuition: 50k".
    rf"\b(?:training|course|tuition|bootcamp|enrol(?:l?ment)?|cost)\b[^.\n]{{0,25}}{_NAIRA}",
    r"\b(?:tuition|cost)\s*[:\-]\s*(?:₦|ngn\s?|n)?\s?\d[\d,]*k?\b",
    rf"{_NAIRA}[\d,]*k?\s+for\s+(?:the\s+|our\s+)?(?:training|course|bootcamp|programme|program)\b",
    r"siwes\s+(?:package|bundle)",
]
_SCAM = re.compile("|".join(_PATTERNS), re.IGNORECASE)

# Honest posts often say what they DON'T charge: "no training fee", "course fee waived".
_NEGATION_BEFORE = re.compile(r"\b(?:no|not|without|free|never|zero|don'?t|doesn'?t|won'?t)\b[^.\n]{0,20}$", re.I)
_NEGATION_AFTER = re.compile(r"^[^.\n]{0,15}\b(?:waived|sponsored|covered|none|nil|free|not required)\b", re.I)
# Money paid TO the student: "stipend of N30,000 during training".
_PAID_TO_STUDENT = re.compile(r"\b(?:stipend|allowance|salary|earn|paid\s+to)\b", re.I)


def _is_scam_match(text: str, match: re.Match) -> bool:
    before = text[max(0, match.start() - 30):match.start()]
    after = text[match.end():match.end() + 25]
    if _NEGATION_BEFORE.search(before) or _NEGATION_AFTER.search(after):
        return False
    return not _PAID_TO_STUDENT.search(before + match.group(0))


def looks_like_scam(*texts: str | None) -> bool:
    return any(_is_scam_match(t, m) for t in texts if t for m in _SCAM.finditer(t))
