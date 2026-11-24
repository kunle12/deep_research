"""AuthService — registration, verification, login, sessions, password reset.

Design notes (docs/MULTI_USER_PLAN.md §3, §9):

- Email is the identity: validated syntactically (email-validator), stored in
  its normalized form, and *verified* by a single-use emailed token before the
  account can log in.
- Passwords are Argon2id (accounts/passwords.py).
- Session tokens are opaque 32-byte secrets; only their SHA-256 is stored.
  Sessions slide: when less than half the TTL remains, a resolved session is
  extended. ``last_seen_at`` writes are throttled to once per minute.
- Session minting is credential-agnostic: ``_mint_session`` is the single
  chokepoint every auth path funnels through, so a future SSO
  ``IdentityProvider`` becomes just another caller (plan §3).
- User enumeration is suppressed where cheap: registration and password-reset
  requests respond identically whether or not the account exists.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from email_validator import EmailNotValidError, validate_email

from deep_research.accounts.emails import EmailSender, OutgoingEmail
from deep_research.accounts.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from deep_research.accounts.rows import UserRow
from deep_research.accounts.store import (
    AccountStore,
    DuplicateEmailError,
    email_token_expiry,
    session_expiry,
)
from deep_research.config import PDLAuthConfig

logger = logging.getLogger(__name__)

#: Name of the session cookie (imported by the web layer — keep in sync there).
SESSION_COOKIE = "pdl_session"
_TOKEN_BYTES = 32
_SESSION_TOUCH_INTERVAL_S = 60.0
_EMAIL_TOKEN_TTL_HOURS = 24.0


class AuthError(Exception):
    """Auth failure with an HTTP status the router can map directly."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RegisterResult:
    status: str  # "pending" | "exists"
    email: str


def normalize_email(raw: str) -> str:
    """Validate + normalize an email address; raises AuthError(422).

    The full address (local part included) is lowercased: RFC allows
    case-sensitive local parts, but in practice every major provider treats
    them as case-insensitive, and account lookup must match what the user
    actually types (docs/MULTI_USER_PLAN.md §3: "lowercase + trim").
    """
    if not raw or len(raw) > 254:
        raise AuthError("a valid email address is required", 422)
    try:
        # Deliverability (DNS) checks are off: self-hosted instances may run
        # offline/air-gapped, and syntax + emailed verification is our
        # validity contract.
        result = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise AuthError(f"invalid email address: {exc}", 422) from exc
    return result.normalized.lower()


def hash_token(token: str) -> str:
    """SHA-256 hex of a raw token — the only form ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class RateLimiter:
    """In-memory sliding-window limiter (per process — fine for self-hosted)."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = {}

    def check(self, bucket: str, key: str, *, limit: int, window_s: float) -> bool:
        """True when the action is allowed (and records the attempt)."""
        now = time.monotonic()
        k = (bucket, key)
        dq = self._events.setdefault(k, deque())
        while dq and now - dq[0] > window_s:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def reset(self) -> None:
        self._events.clear()


class AuthService:
    """Account lifecycle + session minting/resolution on top of AccountStore."""

    def __init__(
        self,
        store: AccountStore,
        sender: EmailSender,
        auth_cfg: PDLAuthConfig,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._store = store
        self._sender = sender
        self._cfg = auth_cfg
        self._limiter = rate_limiter or RateLimiter()

    # -- Sessions --

    @property
    def session_ttl_days(self) -> int:
        return self._cfg.session_ttl_days

    async def get_user_by_email(self, email: str) -> UserRow | None:
        return await self._store.get_user_by_email(email)

    async def _mint_session(self, user: UserRow, *, auth_method: str = "password") -> str:
        """Single session-issuing chokepoint (a future SSO funnels through here)."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._store.create_session(
            user.user_id,
            hash_token(token),
            expires_at=session_expiry(self._cfg.session_ttl_days),
            auth_method=auth_method,
        )
        return token

    async def resolve_session(self, token: str | None) -> UserRow | None:
        """User for a raw session cookie token, or None (invalid/expired)."""
        if not token:
            return None
        session = await self._store.get_session(hash_token(token))
        if session is None:
            return None
        user = await self._store.get_user(session.user_id)
        if user is None or user.status != "active":
            return None
        expires = _parse_iso(session.expires_at)
        now = time.time()
        if expires is None or expires.timestamp() <= now:
            return None
        # Sliding expiry: extend once the session is past half its TTL.
        # last_seen writes are throttled so hot request paths stay read-only.
        ttl = max(self._cfg.session_ttl_days, 1) * 86400
        remaining = expires.timestamp() - now
        last_seen = _parse_iso(session.last_seen_at) if session.last_seen_at else None
        stale = last_seen is None or (now - last_seen.timestamp()) > _SESSION_TOUCH_INTERVAL_S
        if remaining < ttl / 2:
            await self._store.touch_session(
                session.token_hash, expires_at=session_expiry(self._cfg.session_ttl_days)
            )
        elif stale:
            await self._store.touch_session(session.token_hash)
        return user

    async def logout(self, token: str | None) -> None:
        if token:
            await self._store.revoke_session(hash_token(token))

    # -- Registration & verification --

    async def register(
        self, raw_email: str, password: str, *, display_name: str | None = None
    ) -> RegisterResult:
        email = normalize_email(raw_email)
        if self._cfg.registration != "open":
            raise AuthError("registration is disabled on this server", 403)
        if not self._limiter.check("register", email, limit=10, window_s=3600):
            raise AuthError("too many registration attempts; try again later", 429)
        reason = validate_password_strength(password)
        if reason:
            raise AuthError(reason, 422)

        existing = await self._store.get_user_by_email(email)
        if existing is not None:
            # No enumeration: same result shape as success. Tell the address
            # owner someone tried to register their email.
            await self._sender.send(
                OutgoingEmail(
                    to_address=email,
                    subject="Deep Research library: registration attempt",
                    body=(
                        "Someone tried to register an account with this email "
                        "address, but an account already exists. If this was you, "
                        "sign in or reset your password instead."
                    ),
                )
            )
            return RegisterResult(status="exists", email=email)

        try:
            user = await self._store.create_user(
                email,
                password_hash=hash_password(password),
                display_name=display_name or None,
                status="pending",
            )
        except DuplicateEmailError:
            return RegisterResult(status="exists", email=email)
        await self._send_verification(user)
        logger.info("registered new account %s (pending verification)", email)
        return RegisterResult(status="pending", email=email)

    async def _send_verification(self, user: UserRow) -> None:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._store.create_email_token(
            user.user_id,
            hash_token(token),
            purpose="verify",
            expires_at=email_token_expiry(_EMAIL_TOKEN_TTL_HOURS),
        )
        await self._sender.send(
            OutgoingEmail(
                to_address=user.email,
                subject="Verify your Deep Research library account",
                body=(
                    "Welcome to your Deep Research library.\n\n"
                    "Your email verification token (valid for 24 hours):\n\n"
                    f"    {token}\n\n"
                    "Enter it on the sign-in page to activate your account."
                ),
            )
        )

    async def resend_verification(self, raw_email: str) -> None:
        email = normalize_email(raw_email)
        if not self._limiter.check("resend", email, limit=5, window_s=3600):
            raise AuthError("too many requests; try again later", 429)
        user = await self._store.get_user_by_email(email)
        if user is None or user.status != "pending":
            # No enumeration; nothing to do for active/unknown accounts.
            return
        await self._send_verification(user)

    async def verify(self, raw_email: str, token: str) -> str:
        """Consume a verification token -> activate account -> session token."""
        email = normalize_email(raw_email)
        user = await self._store.get_user_by_email(email)
        row = await self._store.get_email_token(hash_token(token), purpose="verify")
        if user is None or row is None or row.user_id != user.user_id or row.used_at:
            raise AuthError("invalid or expired verification token", 400)
        expires = _parse_iso(row.expires_at)
        if expires is None or expires.timestamp() <= time.time():
            raise AuthError("invalid or expired verification token", 400)
        await self._store.mark_email_token_used(row.token_hash)
        if user.status == "pending":
            await self._store.set_user_status(user.user_id, "active")
        refreshed = await self._store.get_user(user.user_id)
        assert refreshed is not None
        logger.info("account verified: %s", refreshed.email)
        return await self._mint_session(refreshed)

    # -- Login --

    async def login(self, raw_email: str, password: str) -> str:
        email = normalize_email(raw_email)
        if not self._limiter.check("login", email, limit=10, window_s=300):
            raise AuthError("too many login attempts; try again later", 429)
        user = await self._store.get_user_by_email(email)
        if user is None or not verify_password(user.password_hash, password):
            raise AuthError("invalid email or password", 401)
        if user.status == "pending":
            raise AuthError("email not verified; check your inbox for the token", 403)
        if user.status == "disabled":
            raise AuthError("this account is disabled", 403)
        if user.password_hash and needs_rehash(user.password_hash):
            # Transparent parameter upgrade after a policy change.
            await self._store.set_password(user.user_id, hash_password(password))
        await self._store.record_login(user.user_id)
        return await self._mint_session(user)

    # -- Password reset --

    async def request_password_reset(self, raw_email: str) -> None:
        email = normalize_email(raw_email)
        if not self._limiter.check("reset-request", email, limit=5, window_s=3600):
            raise AuthError("too many requests; try again later", 429)
        user = await self._store.get_user_by_email(email)
        # Unknown accounts: respond silently (no enumeration).
        if user is None or user.status != "active":
            return
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await self._store.create_email_token(
            user.user_id,
            hash_token(token),
            purpose="reset",
            expires_at=email_token_expiry(2.0),
        )
        await self._sender.send(
            OutgoingEmail(
                to_address=user.email,
                subject="Reset your Deep Research library password",
                body=(
                    "A password reset was requested for your account.\n\n"
                    f"Your reset token (valid for 2 hours):\n\n    {token}\n\n"
                    "If you did not request this, ignore this email."
                ),
            )
        )

    async def reset_password(self, raw_email: str, token: str, new_password: str) -> str:
        email = normalize_email(raw_email)
        reason = validate_password_strength(new_password)
        if reason:
            raise AuthError(reason, 422)
        user = await self._store.get_user_by_email(email)
        row = await self._store.get_email_token(hash_token(token), purpose="reset")
        if user is None or row is None or row.user_id != user.user_id or row.used_at:
            raise AuthError("invalid or expired reset token", 400)
        expires = _parse_iso(row.expires_at)
        if expires is None or expires.timestamp() <= time.time():
            raise AuthError("invalid or expired reset token", 400)
        await self._store.mark_email_token_used(row.token_hash)
        await self._store.set_password(user.user_id, hash_password(new_password))
        # A reset invalidates existing sessions everywhere.
        await self._store.revoke_user_sessions(user.user_id)
        logger.info("password reset completed for %s", user.email)
        return await self._mint_session(user)

    # -- Admin / bootstrap (used by the library CLI, not the HTTP API) --

    async def set_password_direct(self, raw_email: str, new_password: str) -> UserRow:
        """Set a password without a token (server-side admin operation)."""
        email = normalize_email(raw_email)
        reason = validate_password_strength(new_password)
        if reason:
            raise AuthError(reason, 422)
        user = await self._store.get_user_by_email(email)
        if user is None:
            raise AuthError(f"no account for {email}", 404)
        await self._store.set_password(user.user_id, hash_password(new_password))
        await self._store.revoke_user_sessions(user.user_id)
        return user

    async def provision_owner(self, raw_email: str, *, display_name: str = "Owner") -> UserRow:
        """Get-or-create the active owner account (single_user + migration)."""
        email = normalize_email(raw_email) if raw_email else "owner@localhost"
        user = await self._store.get_user_by_email(email)
        if user is None:
            user = await self._store.create_user(email, display_name=display_name, status="active")
            logger.info("provisioned owner account %s (%s)", email, user.user_id)
        elif user.status != "active":
            await self._store.set_user_status(user.user_id, "active")
            refreshed = await self._store.get_user(user.user_id)
            assert refreshed is not None
            user = refreshed
        return user


__all__ = [
    "SESSION_COOKIE",
    "AuthError",
    "AuthService",
    "RateLimiter",
    "RegisterResult",
    "hash_token",
    "normalize_email",
]
