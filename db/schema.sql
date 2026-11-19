-- Run once against Neon: python scripts/init_db.py
CREATE TABLE IF NOT EXISTS placements (
    id            BIGSERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    organization  TEXT,
    location      TEXT,
    state         TEXT,
    fields        TEXT[] NOT NULL DEFAULT '{}',
    open_to_all   BOOLEAN NOT NULL DEFAULT false,
    deadline      DATE,
    posted_date   DATE,
    how_to_apply  TEXT,
    contact       TEXT,
    apply_link    TEXT,
    scam_warning  BOOLEAN NOT NULL DEFAULT false,
    summary       TEXT,
    sources       JSONB NOT NULL DEFAULT '[]',      -- [{source, url}, ...]
    source_urls   TEXT[] NOT NULL DEFAULT '{}',     -- normalized urls, for dedupe
    fingerprint   TEXT UNIQUE,                      -- org + state + deadline month
    status        TEXT NOT NULL DEFAULT 'open',     -- open | expired | rejected (hidden by hand; never reopened)
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS placements_fields_idx ON placements USING GIN (fields);
CREATE INDEX IF NOT EXISTS placements_urls_idx ON placements USING GIN (source_urls);
CREATE INDEX IF NOT EXISTS placements_status_idx ON placements (status);

-- Every url the LLM has already looked at, placement or not, so it never pays twice.
CREATE TABLE IF NOT EXISTS seen_urls (
    url     TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fields students searched for and found little; collectors query these first.
CREATE TABLE IF NOT EXISTS requested_fields (
    name               TEXT PRIMARY KEY,
    canonical          BOOLEAN NOT NULL DEFAULT false,
    request_count      INTEGER NOT NULL DEFAULT 1,
    first_requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_collected_at  TIMESTAMPTZ
);

-- One row per collector run. The X 48h rule reads the latest 'x' started_at.
CREATE TABLE IF NOT EXISTS collector_runs (
    id          BIGSERIAL PRIMARY KEY,
    collector   TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | skipped
    counts      JSONB NOT NULL DEFAULT '{}',
    error       TEXT,
    forced      BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS collector_runs_idx ON collector_runs (collector, started_at DESC);

-- Companies students can apply to, with or without an advert open now (core/companies.py).
-- Shown only with evidence: pages we collected (a listing, or a SIWES company list).
CREATE TABLE IF NOT EXISTS companies (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    name_key        TEXT NOT NULL,                  -- normalize_org(name); not unique on purpose
    aliases         TEXT[] NOT NULL DEFAULT '{}',   -- other normalized keys (seed list: "spdc" for Shell)
    fields          TEXT[] NOT NULL DEFAULT '{}',
    open_to_all     BOOLEAN NOT NULL DEFAULT false,
    states          TEXT[] NOT NULL DEFAULT '{}',
    location        TEXT,
    website         TEXT,
    contact         TEXT,                           -- from listings only
    how_to_apply    TEXT,
    evidence        JSONB NOT NULL DEFAULT '[]',    -- [{url, kind, page_date, seen_at, quote}]
    origin          TEXT NOT NULL,                  -- listing | directory | seed
    status          TEXT NOT NULL DEFAULT 'active', -- active | hidden
    last_checked_at TIMESTAMPTZ,                    -- seeds: last searched
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS companies_key_idx ON companies (name_key);
CREATE INDEX IF NOT EXISTS companies_aliases_idx ON companies USING GIN (aliases);
CREATE INDEX IF NOT EXISTS companies_fields_idx ON companies USING GIN (fields);