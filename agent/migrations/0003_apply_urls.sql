-- Migration 0003: Greenhouse embed apply urls, company discovery probes, digest items, and captcha outcome

-- 1. Probing log for automatic ATS company discovery
CREATE TABLE IF NOT EXISTS company_probes (
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL,
    ats          TEXT NOT NULL,
    http_status  INTEGER NOT NULL,
    checked_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_probes_name ON company_probes(name);
CREATE INDEX IF NOT EXISTS idx_company_probes_checked ON company_probes(checked_at);

-- 2. Daily morning digest numbering for interactive WhatsApp /applied and /skip commands
CREATE TABLE IF NOT EXISTS digest_items (
    id       INTEGER PRIMARY KEY,
    sent_at  TEXT NOT NULL,
    n        INTEGER NOT NULL,
    job_id   INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_digest_items_sent ON digest_items(sent_at);

-- 3. Update apply_attempts CHECK constraint to allow 'captcha' outcome
CREATE TABLE IF NOT EXISTS apply_attempts_v3 (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    adapter         TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('dry_run', 'live')),
    outcome         TEXT NOT NULL CHECK (outcome IN (
                        'filled', 'submitted', 'confirmed', 'blocked', 'failed', 'skipped', 'captcha')),
    error           TEXT,
    screenshot_path TEXT,
    values_json     TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
INSERT INTO apply_attempts_v3 SELECT * FROM apply_attempts;
DROP TABLE apply_attempts;
ALTER TABLE apply_attempts_v3 RENAME TO apply_attempts;
CREATE INDEX IF NOT EXISTS idx_apply_attempts_app ON apply_attempts(application_id);

-- 4. Update applications CHECK constraint to allow origin IN ('agent', 'external', 'manual')
CREATE TABLE IF NOT EXISTS applications_v3 (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs(id),
    status            TEXT NOT NULL CHECK (status IN (
                          'prepared', 'skipped', 'submitted', 'applied', 'assessment',
                          'interview', 'rejected', 'offer', 'no_response')),
    origin            TEXT NOT NULL CHECK (origin IN ('agent', 'external', 'manual')),
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
INSERT INTO applications_v3 SELECT * FROM applications;
DROP TABLE applications;
ALTER TABLE applications_v3 RENAME TO applications;
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

-- 5. Rewrite apply_url for existing Greenhouse jobs using companies.ats_slug and external_id
UPDATE jobs
SET apply_url = 'https://job-boards.greenhouse.io/embed/job_app?for=' || (
    SELECT c.ats_slug FROM companies c WHERE c.id = jobs.company_id
) || '&token=' || jobs.external_id
WHERE jobs.ats_type = 'greenhouse'
  AND jobs.company_id IN (SELECT id FROM companies WHERE ats_type = 'greenhouse' AND ats_slug IS NOT NULL)
  AND jobs.external_id IS NOT NULL;
