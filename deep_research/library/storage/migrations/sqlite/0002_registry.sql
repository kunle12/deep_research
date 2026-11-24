-- Registry schema: global account state (users, sessions, email tokens).
-- This DB is NOT a per-user library; it lives at <pdl.root_dir>/registry.db
-- and is owned by deep_research.accounts.store.AccountStore.
-- (0001_initial.sql is the per-user library schema; never mix the two.)

CREATE TABLE IF NOT EXISTS registry_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,          -- uuid4 hex; the ONLY on-disk identifier
    email         TEXT NOT NULL UNIQUE,      -- normalized (validated, lowercased domain)
    display_name  TEXT,
    password_hash TEXT,                      -- Argon2id; NULL until first password is set
    status        TEXT NOT NULL CHECK (status IN ('pending','active','disabled')),
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,           -- SHA-256 hex of the cookie token
    user_id      TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    auth_method  TEXT NOT NULL DEFAULT 'password',  -- audit only (future SSO)
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,             -- SHA-256 hex of the emailed token
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('verify','reset')),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_tokens_user ON email_tokens(user_id);
