"""Typed row dataclasses for the account registry DB.

Mirrors the style of ``library/storage/rows.py`` (one dataclass per table).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UserRow:
    """Row from the registry ``users`` table."""

    user_id: str  # uuid4 hex — the only on-disk identifier
    email: str  # normalized (email-validator normalized form)
    display_name: str | None = None
    password_hash: str | None = None  # Argon2id; None until first password set
    status: str = "pending"  # pending | active | disabled
    created_at: str = ""  # ISO 8601
    last_login_at: str | None = None


@dataclass
class SessionRow:
    """Row from the registry ``sessions`` table."""

    token_hash: str  # SHA-256 hex of the cookie token
    user_id: str = ""
    auth_method: str = "password"  # audit only (future SSO identities)
    created_at: str = ""
    expires_at: str = ""
    last_seen_at: str | None = None


@dataclass
class EmailTokenRow:
    """Row from the registry ``email_tokens`` table."""

    token_hash: str  # SHA-256 hex of the emailed token
    user_id: str = ""
    purpose: str = "verify"  # verify | reset
    expires_at: str = ""
    used_at: str | None = None


@dataclass
class ShareRow:
    """Row from the registry ``shares`` table — one shared report."""

    share_id: str  # uuid4 hex (public, non-secret)
    token_hash: str  # SHA-256 hex of the secret link token
    owner_user_id: str = ""
    run_id: str = ""
    title: str | None = None  # denormalized at creation for listing
    created_at: str = ""
    expires_at: str | None = None
    revoked_at: str | None = None


@dataclass
class ShareGrantRow:
    """Row from the registry ``share_grants`` table — named-account access."""

    share_id: str = ""
    user_id: str = ""
    created_at: str = ""
    email: str | None = None  # joined from users for display
