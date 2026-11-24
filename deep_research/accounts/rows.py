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
