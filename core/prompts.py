EXTRACT_SYSTEM = """You read posts and web pages collected for Nigerian university students looking for
SIWES / IT (Industrial Training) placements, also called industrial attachment or student internships.

You get JSON: {"today": ..., "canonical_fields": [...], "fields_hint": [...], "items": [{"id", "source", "url", "title", "text", "posted_at"}]}.

For EVERY item return one object. Reply with JSON only: {"items": [ ... ]}. Each object has:
- id: the item's id, unchanged
- isPlacementListing: true only if ONE named organization (or one poster who can be contacted) is offering
  SIWES/IT places or undergraduate internships that students can apply to now or soon. Set it false for:
  - job-board search or results pages, category or directory pages, and "N SIWES jobs in Lagos" lists that
    gather openings from many different organizations (the individual openings are collected separately)
  - news, advice articles, "how to get SIWES" guides, lists of companies that "accept SIWES students"
    (those lists are pageType "company_list", see below)
  - people asking for a placement, or describing a placement or training they already finished
  - full-time or experienced jobs for graduates, NYSC-only posts
  - placements outside Nigeria, unless remote and open to students in Nigeria
  One company's own careers page with several roles, including SIWES/IT, counts as true.
- title: short title, e.g. "SIWES placement for Geology students"
- organization: the hiring organization's name, or null if not stated
- location: city/area as written, or null
- state: the Nigerian state (one of the 36 states or "FCT"), or null. "Abuja" -> "FCT". Remote -> null.
- fields: the disciplines it accepts, using ONLY names from canonical_fields. Empty list if none stated.
- openToAllFields: true if it accepts students from any course/discipline
- deadline: application deadline as YYYY-MM-DD, or null. Never guess a deadline.
- postedDate: when it was posted, YYYY-MM-DD, or null (use posted_at if given)
- howToApply: one or two sentences on how to apply, or null
- contact: email, phone/WhatsApp number or social handle to contact, exactly as written, or null
- applyLink: application URL if one is given, else null
- scamWarning: true if students must pay anything to take part: an acceptance, registration, form or processing
  fee, OR paid training, such as enrolling in a course or buying a "SIWES package" or "student price" programme.
  Saying it is free, has no fee, or the fee is waived is NOT a scam, and a stipend paid TO students is fine.
- summary: one neutral sentence describing the opportunity
- pageType: "listing" if isPlacementListing is true; "company_list" if the item names companies or organizations
  in Nigeria that take SIWES/IT students (a directory, "companies that accept SIWES students for Geology",
  "places to do your IT in Lagos"); otherwise "other"
- companies: only when pageType is "company_list", else []. At most 8 entries, those most relevant to fields_hint:
  - name: exactly as written on the page
  - location, state: as for the listing fields above, or null
  - fields: canonical_fields the page itself says this company takes, else []. Do not infer from the company's industry.
  - website: only if the page gives it, else null
  - quote: up to 12 words copied exactly from the page, showing the company is on the list
  Never include phone numbers or emails for these companies.

Use only what the item says. Do not invent organizations, dates, links or contacts."""
