# SIWES Placement Finder

A website where Nigerian students type their course (e.g. *Computer Science*, *Remote Sensing*, *Applied Geophysics*), optionally pick a state, and see open SIWES / IT placements. The listings are collected from the web, Nigerian job sites, Indeed, Nairaland, Telegram and X.

## How it works

```text
Background collectors ──► keyword filter ──► AI extraction ──► Neon Postgres ◄── Website (read-only)
 (GitHub Actions, 6h)      (free, local)     (Gemini → NVIDIA)                    FastAPI + HTMX on Render
 X: your PC, ≤ every 48h
```

- **The website only reads the database.** Searches are instant and cost nothing. If a field has fewer than 5 results, it's added to `requested_fields`, and the next background runs search it first. Students can never start a scrape.
- **Sources:**
  - Serper (Google) or `ddgs` (DuckDuckGo) web search, including `site:` searches of LinkedIn, X, Telegram, Nairaland and Nigerian job sites
  - Scrapling for fetching pages
  - JobSpy for Indeed Nigeria
  - the Nairaland and X collectors adapted from Need-Finder
- **The X rule:** only `scripts/run_x.py` can run X, and it runs **at most once every 48 hours**.
  - The rule is checked in the database under a lock, using the database's clock, and counts from the last *start* (failed runs count too).
  - A local backup file blocks an early run even if the database was wiped.
  - The website never imports any X code (a test checks this).
- **AI:** Gemini (free) first. NVIDIA NIM (free) takes over at once on a quota error.
  - To stay inside free quotas: a keyword filter drops irrelevant posts first, URLs the AI has already seen are skipped, and each run is capped at 25 AI calls.
- **Scam warnings:** keyword rules plus the AI's own flag. Flagged posts are shown with a warning, not hidden.
- **Expiry:** a listing expires when its deadline passes. A listing with no deadline expires 60 days after it was posted, or after it was first seen if the post date is unknown.
- **Duplicates:** the same placement found on several sites becomes one card that links to every source.

## Setup (local)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then fill in the keys
pytest                           # offline tests, no keys needed
```

Free accounts you need:
- **Gemini key:** https://aistudio.google.com
- **NVIDIA key:** https://build.nvidia.com
- **Neon Postgres database:** https://neon.tech (use the *pooled* connection string)
- **Serper key** (optional): https://serper.dev. It gives 2,500 queries in total; without it the collector uses DuckDuckGo.

```powershell
python scripts/init_db.py                     # create tables (safe to re-run)
python scripts/run_collect.py --dry-run --fields "Computer Science"   # real search, fake AI, no DB writes
python scripts/run_collect.py                 # real run into Neon
uvicorn app.main:app --reload                 # open http://127.0.0.1:8000
```

Without `DATABASE_URL` the website runs on an empty in-memory store, which is handy for working on the page design.

**Styles:** the page uses `app/static/app.css`, a Tailwind build of the classes used in `app/templates/`. After you change classes in a template, rebuild it and commit the result. The Docker image and Render use the committed file and don't need Node.

```powershell
npm install
npm run build      # or `npm run watch` while editing templates
```

## X collector (your PC only)

1. Make a **throwaway** X account. Automated access is against X's terms and the account will eventually be restricted. Never use your personal account.
2. Log in once: `python scripts/login_social.py x`. If the login page stays blank, run `python scripts/login_social.py x --cookies` instead.
3. Test it: `python scripts/run_x.py --show`. Run it again straight away and it should log `X skipped: next run allowed at …`.
4. Schedule it: `powershell -ExecutionPolicy Bypass -File scripts\install_x_task.ps1`. This runs the script daily; the 48-hour rule decides whether X actually runs.

Logs are written to `data/logs/x.log`. When X fails, a screenshot and the page HTML are saved to `data/x-debug/`.

`--force` ignores the 48-hour rule. It's for debugging only, and every forced run is recorded in `collector_runs`.

## Deploy

1. **Neon:**
   - run `python scripts/init_db.py` with the owner connection string
   - create a role `siwes_web` in the Neon console, then run `db/web_role.sql`, which lets the website read placements and write only to `requested_fields`
2. **GitHub:** push the repo. Add the secrets `DATABASE_URL` (owner role), `GEMINI_API_KEY`, `NVIDIA_API_KEY` and optionally `SERPER_API_KEY`. Then run the **collect** workflow once by hand from the Actions tab.
   - After that it runs every 6 hours.
   - Turn on failed-workflow emails in your GitHub notification settings.
   - Optional: set the repository variable `STEALTHY_FETCH=1` to retry blocked pages in a real browser.
3. **Render:** New → Blueprint → this repo (it reads `render.yaml`). Set `DATABASE_URL` to the **siwes_web** pooled connection string, and set `CONTACT_EMAIL`.
4. **Health page:** open `/admin/health` and send the `ADMIN_TOKEN` value (from Render) in an `X-Admin-Token` header, or as `?token=` in the URL. A wrong token gets a 404. The page shows each collector's last run, errors, the next allowed X run, and the most-requested fields.

## Rate limits (website)

| What | Limit |
| --- | --- |
| `/search` | 20/minute and 300/day per IP |
| Adding requested fields | 10 fields/day per IP (each field counts once per IP per day), 200/day site-wide |
| `/admin/health` | 5/minute per IP |
| Everything else | 60/minute per IP |

- **Visitor IP:** the IP is read from `X-Forwarded-For`, counting `TRUSTED_PROXY_HOPS` entries from the right. Render's proxy adds the real IP there, so faking the header doesn't get around the limits.
- **Input checks:** field input must be letters, spaces, `&` and `-`, at most 60 characters; the state must be in the list.
- **Storage:** the limits are kept in memory, which is fine for one Render instance. Point slowapi at Redis if you scale out.

## Project layout

```
app/          website (FastAPI + Jinja + HTMX); never imports X code
collectors/   search.py, sites.py (Scrapling), telegram.py, jobboards.py (JobSpy),
              nairaland.py, x_browser.py, queries.py, runner.py (the pipeline)
core/         llm_client.py, extract.py, prompts.py, fields.py, scam.py, dedupe.py, db.py, xgate.py
config/       fields.yaml (courses + aliases), llm.yaml, sources.yaml (budgets, boards, Telegram channels)
db/           schema.sql, web_role.sql
scripts/      run_collect.py, run_x.py, login_social.py, init_db.py, install_x_task.ps1
```

**To add Telegram channels:** list them under `telegram.channels` in `config/sources.yaml`. Use the part of the link after `t.me/`; the channel has to be public.

**To add a course or a short form students use:** edit `config/fields.yaml`.
