EXTRACT_SYSTEM = """You read posts and web pages collected for Nigerian university students looking for
SIWES / IT (Industrial Training) placements, also called industrial attachment or student internships.

You get JSON: {"today": ..., "canonical_fields": [...], "fields_hint": [...], "items": [{"id", "source", "url", "title", "text", "posted_at"}]}.

For EVERY item return one object. Reply with JSON only: {"items": [ ... ]}. Each object has:
- id: the item's id, unchanged
- isPlacementListing: true only if the item advertises placements/internships that students can apply to now or soon
  (an organization taking SIWES/IT students, an internship for undergraduates). false for news, advice articles,
  lists of "how to find SIWES", people asking for placements, full-time jobs for graduates, NYSC-only posts.
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
- scamWarning: true if students are asked to pay any fee (acceptance, registration, form, processing) or money
- summary: one neutral sentence describing the opportunity

Use only what the item says. Do not invent organizations, dates, links or contacts."""
