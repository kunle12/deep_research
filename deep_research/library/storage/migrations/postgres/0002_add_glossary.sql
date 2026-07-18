-- 0002_add_glossary: glossary table

CREATE TABLE IF NOT EXISTS glossary (
    term_id      SERIAL PRIMARY KEY,
    term         TEXT NOT NULL,
    term_canonical TEXT NOT NULL,
    kind         TEXT NOT NULL,
    short_def    TEXT,
    long_def     TEXT,
    acronym_expansion TEXT,
    related_terms TEXT,
    domain_tags  TEXT,
    confidence   REAL,
    first_seen_run_id TEXT REFERENCES reports(run_id),
    first_seen_artifact_id TEXT REFERENCES artifacts(artifact_id),
    last_updated TEXT NOT NULL,
    UNIQUE(term_canonical),
    CHECK (kind IN ('concept','acronym','method','metric','dataset','model','tool'))
);

CREATE INDEX IF NOT EXISTS idx_glossary_fts ON glossary USING gin(
    to_tsvector('english', coalesce(term, '') || ' ' || coalesce(short_def, '') || ' ' || coalesce(long_def, ''))
);

-- Update schema version
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = '2';
