"""ShareService — view-only report sharing between accounts.

One authorization core for both access paths (docs/MULTI_USER_PLAN.md §14):

- **secret link**: anyone holding the raw token (``#/share/<token>``);
- **named grant**: an account listed in ``share_grants`` (``#/shared/…``).

Access requires a live share (not revoked, not expired). Reads always open the
OWNER's library through ``LibraryRegistry`` — grantees never write into it, so
sharing stays read-only by construction.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email

from deep_research.accounts.rows import ShareRow
from deep_research.accounts.service import AuthError, normalize_email
from deep_research.accounts.store import AccountStore

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 32
_MAX_SHARES_PER_OWNER = 100
_MAX_GRANTEES_PER_SHARE = 25


@dataclass
class CreatedShare:
    share: ShareRow
    token: str  # raw token, shown ONCE at creation


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def share_is_live(share: ShareRow) -> bool:
    """Not revoked and not expired — the single liveness rule."""
    if share.revoked_at is not None:
        return False
    expires = _parse_iso(share.expires_at)
    return not (expires is not None and expires.timestamp() <= datetime.now(UTC).timestamp())


class ShareService:
    """Share lifecycle + authorization on top of the account registry."""

    def __init__(self, store: AccountStore) -> None:
        self._store = store

    async def create_share(
        self,
        owner_user_id: str,
        run_id: str,
        *,
        title: str | None = None,
        with_emails: list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> CreatedShare:
        count = len(await self._store.list_shares_for_owner(owner_user_id))
        if count >= _MAX_SHARES_PER_OWNER:
            raise AuthError(
                f"share limit reached ({_MAX_SHARES_PER_OWNER}); revoke some first", 409
            )
        expires_at = None
        if expires_in_days is not None:
            if expires_in_days < 1 or expires_in_days > 365:
                raise AuthError("expires_in_days must be between 1 and 365", 422)
            expires_at = (datetime.now(UTC) + timedelta(days=expires_in_days)).isoformat(
                timespec="seconds"
            )
        # Resolve grantees BEFORE creating the row: a failed request must not
        # leave an orphan share behind.
        grantee_ids = await self._resolve_grantee_ids(with_emails or [])
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        share = await self._store.create_share(
            share_id=uuid.uuid4().hex,
            token_hash=hash_share_token(token),
            owner_user_id=owner_user_id,
            run_id=run_id,
            title=title,
            expires_at=expires_at,
        )
        for gid in grantee_ids:
            await self._store.add_grant(share.share_id, gid)
        logger.info(
            "share created: %s for run %s (%d grants)", share.share_id, run_id, len(grantee_ids)
        )
        return CreatedShare(share=share, token=token)

    async def _resolve_grantee_ids(self, emails: list[str]) -> list[str]:
        """Emails -> existing account ids (deduped); unknown addresses raise 404."""
        ids: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = normalize_email(raw)
            grantee = await self._store.get_user_by_email(email)
            if grantee is None:
                raise AuthError(f"no account for {email} — ask them to register first", 404)
            if grantee.user_id in seen:
                continue
            seen.add(grantee.user_id)
            ids.append(grantee.user_id)
        return ids

    async def add_grantees(self, share_id: str, owner_user_id: str, emails: list[str]) -> int:
        """Resolve emails to accounts and grant them; returns count added.

        Unknown emails raise AuthError(404) with the offending address — the
        owner shares with colleagues who have already registered. All emails
        are resolved first, so a failed call adds nothing.
        """
        existing = await self._store.list_grants(share_id)
        if len(existing) + len(emails) > _MAX_GRANTEES_PER_SHARE:
            raise AuthError(f"too many grantees (max {_MAX_GRANTEES_PER_SHARE} per share)", 422)
        grantee_ids = await self._resolve_grantee_ids(emails)
        added = 0
        for gid in grantee_ids:
            if gid == owner_user_id:
                continue  # sharing with yourself is a no-op
            if any(g.user_id == gid for g in existing):
                continue
            if await self._store.add_grant(share_id, gid):
                added += 1
        return added

    async def resolve_for_token(self, token: str) -> ShareRow:
        """Link path: raw token -> live share."""
        if not token:
            raise AuthError("share not found", 404)
        share = await self._store.get_share_by_token(hash_share_token(token))
        if share is None or not share_is_live(share):
            raise AuthError("share not found", 404)
        return share

    async def resolve_for_user(self, share_id: str, user_id: str) -> ShareRow:
        """Named path: authenticated user must hold a grant (or be the owner)."""
        share = await self._store.get_share(share_id)
        if share is None or not share_is_live(share):
            raise AuthError("share not found", 404)
        if share.owner_user_id == user_id:
            return share
        grants = await self._store.list_grants(share_id)
        if not any(g.user_id == user_id for g in grants):
            # Indistinguishable from nonexistent — share ids don't leak.
            raise AuthError("share not found", 404)
        return share

    async def resolve_owner_share(self, share_id: str, user_id: str) -> ShareRow:
        """Owner-only management access to a share."""
        share = await self._store.get_share(share_id)
        if share is None or share.owner_user_id != user_id:
            raise AuthError("share not found", 404)
        return share

    async def revoke(self, share_id: str, owner_user_id: str) -> None:
        await self.resolve_owner_share(share_id, owner_user_id)
        await self._store.revoke_share(share_id)
        logger.info("share revoked: %s", share_id)


def validate_grantee_email(raw: str) -> str:
    """Syntax-only check with the house normalizer (used for early errors)."""
    try:
        return validate_email(raw, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise AuthError(f"invalid email address: {exc}", 422) from exc


__all__ = [
    "CreatedShare",
    "ShareService",
    "hash_share_token",
    "share_is_live",
    "validate_grantee_email",
]
