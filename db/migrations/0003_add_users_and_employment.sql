-- 0003 — add the users (login) table and employment fields on applications.
-- Hand-tracked, as usual. Authoritative DDL lives in db/init/001_schema.sql.
-- Applied 2025-06.

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- sha256(password), unsalted (harden later)
    role          TEXT NOT NULL DEFAULT 'csr',
    display_name  TEXT,
    applicant_id  INTEGER,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE applicants   ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE applicants   ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS employer TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS job_title TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS employment_years DOUBLE PRECISION;
