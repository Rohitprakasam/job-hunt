-- 0001: initial schema. Never edit after it has run on real data; add 0002_*.sql instead.
-- All timestamps are UTC ISO-8601 text ending in 'Z'.

CREATE TABLE companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    domain      TEXT,
    ats_type    TEXT,
    ats_slug    TEXT,
    watchlist   INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
    notes       TEXT,
    UNIQUE (ats_type, ats_slug)
);

CREATE TABLE jobs (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    external_id    TEXT,
    url            TEXT,
    apply_url      TEXT,
    company        TEXT NOT NULL,
    company_id     INTEGER REFERENCES companies(id),
    title          TEXT NOT NULL,
    location       TEXT,
    remote         INTEGER CHECK (remote IN (0, 1)),
    salary_min     REAL,
    salary_max     REAL,
    currency       TEXT,
    posted_at      TEXT,
    first_seen_at  TEXT NOT NULL,
    description    TEXT,
    dedupe_key     TEXT NOT NULL UNIQUE,
    ats_type       TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX idx_jobs_company ON jobs(company);

CREATE TABLE scores (
    job_id       INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    fit          REAL NOT NULL,
    semantic     REAL,
    skills       REAL,
    llm_rubric   REAL,
    seniority    REAL,
    freshness    REAL,
    reasons_json TEXT,
    gaps_json    TEXT,
    model        TEXT,
    scored_at    TEXT NOT NULL
);

CREATE TABLE applications (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs(id),
    status            TEXT NOT NULL CHECK (status IN (
                          'prepared', 'skipped', 'submitted', 'applied', 'assessment',
                          'interview', 'rejected', 'offer', 'no_response')),
    origin            TEXT NOT NULL CHECK (origin IN ('agent', 'external')),
    resume_variant    TEXT,
    resume_path       TEXT,
    cover_path        TEXT,
    answers_json      TEXT,
    gate_results_json TEXT,
    skip_reason       TEXT,
    submitted_at      TEXT,
    confirmed_at      TEXT,
    last_event_at     TEXT,
    follow_up_due     TEXT
);
CREATE INDEX idx_applications_job ON applications(job_id);
CREATE INDEX idx_applications_status ON applications(status);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id),
    type            TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('gmail', 'agent', 'manual')),
    gmail_msg_id    TEXT UNIQUE,
    gmail_thread_id TEXT,
    sender_domain   TEXT,
    confidence      REAL,
    occurred_at     TEXT NOT NULL,
    details_json    TEXT
);
CREATE INDEX idx_events_application ON events(application_id);

CREATE TABLE answers_bank (
    id            INTEGER PRIMARY KEY,
    question_norm TEXT NOT NULL UNIQUE,
    question_raw  TEXT NOT NULL,
    answer        TEXT NOT NULL,
    category      TEXT NOT NULL CHECK (category IN ('legal', 'factual', 'preference', 'other')),
    approved      INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
    used_count    INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE notifications (
    id            INTEGER PRIMARY KEY,
    channel       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    ref_id        TEXT,
    body          TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'failed', 'dropped')),
    error_code    TEXT,
    queued_at     TEXT NOT NULL,
    sent_at       TEXT,
    delivered     INTEGER CHECK (delivered IN (0, 1)),
    cost_estimate REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_notifications_status ON notifications(status, priority);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER CHECK (ok IN (0, 1)),
    items       INTEGER,
    error       TEXT
);

CREATE TABLE experiments (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    variant        TEXT NOT NULL,
    application_id INTEGER REFERENCES applications(id)
);

-- One row per attempt. `sent` = the request reached the provider and used quota.
CREATE TABLE llm_calls (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER REFERENCES runs(id),
    stage             TEXT NOT NULL,
    tier              TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    list_cost_usd     REAL,
    sent              INTEGER NOT NULL CHECK (sent IN (0, 1)),
    ok                INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error_code        TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_llm_calls_model_time ON llm_calls(model, created_at);
CREATE INDEX idx_llm_calls_time ON llm_calls(created_at);

CREATE TABLE flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
