-- 0002: pipeline state for discovery, scoring, apply and reporting. Additive only.

ALTER TABLE jobs ADD COLUMN last_seen_at TEXT;
ALTER TABLE jobs ADD COLUMN filter_reason TEXT;
ALTER TABLE jobs ADD COLUMN filtered_at TEXT;
ALTER TABLE jobs ADD COLUMN notified_at TEXT;
CREATE INDEX idx_jobs_first_seen ON jobs(first_seen_at);

ALTER TABLE scores ADD COLUMN priority REAL;
ALTER TABLE scores ADD COLUMN skills_found_json TEXT;

-- Embedding cache. key = 'job:<id>' or 'profile:<sha256>'; vector = float32 little-endian.
CREATE TABLE embeddings (
    key        TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL
);

-- Screening questions the agent could not answer; listed weekly so the user can add an answer once.
CREATE TABLE pending_questions (
    id             INTEGER PRIMARY KEY,
    question_norm  TEXT NOT NULL UNIQUE,
    question_raw   TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    example_job_id INTEGER REFERENCES jobs(id),
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);

CREATE TABLE apply_attempts (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    adapter         TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('dry_run', 'live')),
    outcome         TEXT NOT NULL CHECK (outcome IN (
                        'filled', 'submitted', 'confirmed', 'blocked', 'failed', 'skipped')),
    error           TEXT,
    screenshot_path TEXT,
    values_json     TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX idx_apply_attempts_app ON apply_attempts(application_id);

-- Per-source circuit breaker.
CREATE TABLE source_health (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_until       TEXT,
    last_ok_at           TEXT,
    last_error           TEXT
);
