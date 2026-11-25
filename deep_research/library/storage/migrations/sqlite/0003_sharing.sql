-- Registry schema extension: view-only report sharing (docs/MULTI_USER_PLAN.md §14).
-- A share row is one report (owner_user_id + run_id) exposed either via a
-- secret link (token_hash) or to named accounts (share_grants). Read-only by
-- construction: share reads open the OWNER's library through the server;
-- grantees never write into it.

CREATE TABLE IF NOT EXISTS shares (
    share_id      TEXT PRIMARY KEY,      -- uuid4 hex (public, non-secret)
    token_hash    TEXT NOT NULL UNIQUE,  -- SHA-256 of the secret link token
    owner_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    run_id        TEXT NOT NULL,
    title         TEXT,                  -- denormalized at creation for listing
    created_at    TEXT NOT NULL,
    expires_at    TEXT,                  -- optional
    revoked_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_shares_owner ON shares(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_shares_run ON shares(owner_user_id, run_id);

CREATE TABLE IF NOT EXISTS share_grants (
    share_id   TEXT NOT NULL REFERENCES shares(share_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (share_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_share_grants_user ON share_grants(user_id);
