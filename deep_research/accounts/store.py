"""AccountStore — global account registry (users, sessions, email tokens).

Owns a single shared aiosqlite connection to ``<pdl.root_dir>/registry.db`` and
follows the storage-layer concurrency pattern from docs/AGENTS.md: every public
method is serialized through an asyncio op-lock so multi-statement operations
stay atomic on the shared connection. Never call a public (serialized) method
from inside another public method — extract a private ``_`` helper instead.

This store is deliberately SQLite-only: the registry is a tiny, low-write DB
and per-user library isolation is handled by LibraryRegistry (SQLite files or
Postgres schemas). A Postgres-backed registry is a documented future option.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from deep_research.accounts.rows import (
    EmailTokenRow,
    SessionRow,
    ShareGrantRow,
    ShareRow,
    UserRow,
)

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent / "library" / "storage" / "migrations" / "sqlite"
)
# 0001_initial.sql is the PER-LIBRARY schema (never applied to the registry);
# every other numbered file is a registry migration (0002_registry, 0003_sharing, …).
_REGISTRY_MIGRATIONS = sorted(
    p for p in _MIGRATIONS_DIR.glob("0*.sql") if p.name != "0001_initial.sql"
)


def _now_iso() -> str:
    """Uniform UTC timestamp (second precision) so string compares stay valid."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def session_expiry(ttl_days: int) -> str:
    """ISO expiry timestamp for a session issued now."""
    return _iso(datetime.now(UTC) + timedelta(days=ttl_days))


def email_token_expiry(ttl_hours: float = 24.0) -> str:
    """ISO expiry timestamp for an emailed token issued now."""
    return _iso(datetime.now(UTC) + timedelta(hours=ttl_hours))


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _serialized(func):
    """Serialize an async AccountStore method through the instance's op lock."""

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with self._op_lock:
            return await func(self, *args, **kwargs)

    return wrapper


def _user_from_row(row) -> UserRow:
    return UserRow(
        user_id=row[0],
        email=row[1],
        display_name=row[2],
        password_hash=row[3],
        status=row[4],
        created_at=row[5],
        last_login_at=row[6],
    )


def _session_from_row(row) -> SessionRow:
    return SessionRow(
        token_hash=row[0],
        user_id=row[1],
        auth_method=row[2],
        created_at=row[3],
        expires_at=row[4],
        last_seen_at=row[5],
    )


class DuplicateEmailError(Exception):
    """Raised when creating a user with an email that already exists."""


class AccountStore:
    """Async access to the global account registry DB."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._op_lock = asyncio.Lock()

    # -- Lifecycle --

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # sqlite3's timeout kwarg is SECONDS (the busy_timeout PRAGMA below is
        # milliseconds — keep them consistent at 5s).
        self._conn = await aiosqlite.connect(self._db_path, timeout=5)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.ensure_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    async def ensure_schema(self) -> None:
        if self._conn is None:
            raise RuntimeError("AccountStore not connected")
        for migration in _REGISTRY_MIGRATIONS:
            sql = migration.read_text(encoding="utf-8")
            await self._conn.executescript(sql)
        await self._conn.commit()
        logger.info(
            "registry schema initialized (%d migrations): %s",
            len(_REGISTRY_MIGRATIONS),
            ", ".join(m.name for m in _REGISTRY_MIGRATIONS),
        )

    # -- Meta (migration markers etc.) --

    @_serialized
    async def meta_get(self, key: str) -> str | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM registry_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    @_serialized
    async def meta_set(self, key: str, value: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO registry_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    @_serialized
    async def meta_claim(self, key: str, value: str) -> bool:
        """Atomically claim *key* (INSERT OR IGNORE); True when acquired.

        Used as a lock for cross-process one-time work (legacy migration).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO registry_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    @_serialized
    async def meta_delete(self, key: str) -> None:
        assert self._conn is not None
        await self._conn.execute("DELETE FROM registry_meta WHERE key = ?", (key,))
        await self._conn.commit()

    # -- Users --

    @_serialized
    async def create_user(
        self,
        email: str,
        *,
        password_hash: str | None = None,
        display_name: str | None = None,
        status: str = "pending",
        user_id: str | None = None,
    ) -> UserRow:
        """Insert a user. Raises DuplicateEmailError on a conflicting email."""
        assert self._conn is not None
        uid = user_id or uuid.uuid4().hex
        now = _now_iso()
        try:
            await self._conn.execute(
                "INSERT INTO users (user_id, email, display_name, password_hash, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, email, display_name, password_hash, status, now),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateEmailError(email) from exc
        await self._conn.commit()
        return UserRow(
            user_id=uid,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            status=status,
            created_at=now,
        )

    @_serialized
    async def get_user(self, user_id: str) -> UserRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT user_id, email, display_name, password_hash, status, created_at, "
            "last_login_at FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return _user_from_row(row) if row else None

    @_serialized
    async def get_user_by_email(self, email: str) -> UserRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT user_id, email, display_name, password_hash, status, created_at, "
            "last_login_at FROM users WHERE email = ?",
            (email,),
        ) as cur:
            row = await cur.fetchone()
        return _user_from_row(row) if row else None

    @_serialized
    async def list_users(self) -> list[UserRow]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT user_id, email, display_name, password_hash, status, created_at, "
            "last_login_at FROM users ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [_user_from_row(r) for r in rows]

    @_serialized
    async def set_user_status(self, user_id: str, status: str) -> None:
        assert self._conn is not None
        if status not in ("pending", "active", "disabled"):
            raise ValueError(f"invalid user status: {status!r}")
        await self._conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await self._conn.commit()

    @_serialized
    async def set_password(self, user_id: str, password_hash: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        )
        await self._conn.commit()

    @_serialized
    async def record_login(self, user_id: str, *, auth_method: str = "password") -> None:
        assert self._conn is not None
        now = _now_iso()
        await self._conn.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?", (now, user_id)
        )
        # Opportunistic cleanup keeps the sessions table bounded without a
        # dedicated maintenance task.
        await self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now_iso(),))
        await self._conn.execute("DELETE FROM email_tokens WHERE expires_at < ?", (now,))
        await self._conn.commit()

    # -- Sessions --

    @_serialized
    async def create_session(
        self,
        user_id: str,
        token_hash: str,
        *,
        expires_at: str,
        auth_method: str = "password",
    ) -> SessionRow:
        assert self._conn is not None
        now = _now_iso()
        await self._conn.execute(
            "INSERT INTO sessions (token_hash, user_id, auth_method, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, auth_method, now, expires_at),
        )
        await self._conn.commit()
        return SessionRow(
            token_hash=token_hash,
            user_id=user_id,
            auth_method=auth_method,
            created_at=now,
            expires_at=expires_at,
        )

    @_serialized
    async def get_session(self, token_hash: str) -> SessionRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT token_hash, user_id, auth_method, created_at, expires_at, last_seen_at "
            "FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
        return _session_from_row(row) if row else None

    @_serialized
    async def touch_session(self, token_hash: str, *, expires_at: str | None = None) -> None:
        """Update last_seen_at (and optionally slide the expiry forward)."""
        assert self._conn is not None
        now = _now_iso()
        if expires_at is not None:
            await self._conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (now, expires_at, token_hash),
            )
        else:
            await self._conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
        await self._conn.commit()

    @_serialized
    async def revoke_session(self, token_hash: str) -> None:
        assert self._conn is not None
        await self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        await self._conn.commit()

    @_serialized
    async def revoke_user_sessions(self, user_id: str) -> None:
        assert self._conn is not None
        await self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    # -- Email tokens --

    @_serialized
    async def create_email_token(
        self, user_id: str, token_hash: str, *, purpose: str, expires_at: str
    ) -> None:
        assert self._conn is not None
        # Single active token per (user, purpose): issuing a new one invalidates
        # the old (prevents an inbox-full of still-valid links).
        await self._conn.execute(
            "DELETE FROM email_tokens WHERE user_id = ? AND purpose = ?",
            (user_id, purpose),
        )
        await self._conn.execute(
            "INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, user_id, purpose, expires_at),
        )
        await self._conn.commit()

    @_serialized
    async def get_email_token(self, token_hash: str, *, purpose: str) -> EmailTokenRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT token_hash, user_id, purpose, expires_at, used_at "
            "FROM email_tokens WHERE token_hash = ? AND purpose = ?",
            (token_hash, purpose),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return EmailTokenRow(
            token_hash=row[0],
            user_id=row[1],
            purpose=row[2],
            expires_at=row[3],
            used_at=row[4],
        )

    @_serialized
    async def mark_email_token_used(self, token_hash: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE token_hash = ?",
            (_now_iso(), token_hash),
        )
        await self._conn.commit()

    @_serialized
    async def consume_email_token(self, token_hash: str, *, purpose: str) -> bool:
        """Atomically consume a token: single-use + expiry in one UPDATE.

        Returns False when the token is unknown, already used, or expired.
        The conditional UPDATE (not read-then-write) keeps concurrent submits
        of the same token from minting two sessions.
        """
        assert self._conn is not None
        now = _now_iso()
        cursor = await self._conn.execute(
            "UPDATE email_tokens SET used_at = ? "
            "WHERE token_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ?",
            (now, token_hash, purpose, now),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    # -- Shares (view-only report sharing) --

    @staticmethod
    def _share_from_row(row) -> ShareRow:
        from deep_research.accounts.rows import ShareRow

        return ShareRow(
            share_id=row[0],
            token_hash=row[1],
            owner_user_id=row[2],
            run_id=row[3],
            title=row[4],
            created_at=row[5],
            expires_at=row[6],
            revoked_at=row[7],
        )

    @_serialized
    async def create_share(
        self,
        *,
        share_id: str,
        token_hash: str,
        owner_user_id: str,
        run_id: str,
        title: str | None,
        expires_at: str | None,
    ) -> ShareRow:
        assert self._conn is not None
        now = _now_iso()
        await self._conn.execute(
            "INSERT INTO shares (share_id, token_hash, owner_user_id, run_id, title, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (share_id, token_hash, owner_user_id, run_id, title, now, expires_at),
        )
        await self._conn.commit()
        return ShareRow(
            share_id=share_id,
            token_hash=token_hash,
            owner_user_id=owner_user_id,
            run_id=run_id,
            title=title,
            created_at=now,
            expires_at=expires_at,
        )

    @_serialized
    async def get_share(self, share_id: str) -> ShareRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT share_id, token_hash, owner_user_id, run_id, title, created_at, "
            "expires_at, revoked_at FROM shares WHERE share_id = ?",
            (share_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._share_from_row(row) if row else None

    @_serialized
    async def get_share_by_token(self, token_hash: str) -> ShareRow | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT share_id, token_hash, owner_user_id, run_id, title, created_at, "
            "expires_at, revoked_at FROM shares WHERE token_hash = ?",
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
        return self._share_from_row(row) if row else None

    @_serialized
    async def list_shares_for_owner(self, owner_user_id: str) -> list[ShareRow]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT share_id, token_hash, owner_user_id, run_id, title, created_at, "
            "expires_at, revoked_at FROM shares WHERE owner_user_id = ? "
            "ORDER BY created_at DESC",
            (owner_user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._share_from_row(r) for r in rows]

    @_serialized
    async def revoke_share(self, share_id: str) -> bool:
        """Hard-delete a share (cascades its grants). True when it existed.

        Revocation is deliberately a DELETE (not a revoked_at update): the
        share disappears from every listing and every access path at once,
        and grant rows cascade away with it.
        """
        assert self._conn is not None
        cursor = await self._conn.execute("DELETE FROM shares WHERE share_id = ?", (share_id,))
        await self._conn.commit()
        return bool(cursor.rowcount)

    @_serialized
    async def delete_shares_for_run(self, owner_user_id: str, run_id: str) -> int:
        """Drop all shares of one report (report-deletion cascade, F4)."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM shares WHERE owner_user_id = ? AND run_id = ?",
            (owner_user_id, run_id),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    @_serialized
    async def rotate_share_token(self, share_id: str, token_hash: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE shares SET token_hash = ? WHERE share_id = ?",
            (token_hash, share_id),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    @_serialized
    async def add_grant(self, share_id: str, user_id: str) -> bool:
        """Grant a named account access; idempotent. True when newly added."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO share_grants (share_id, user_id, created_at) VALUES (?, ?, ?)",
            (share_id, user_id, _now_iso()),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    @_serialized
    async def remove_grant(self, share_id: str, user_id: str) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM share_grants WHERE share_id = ? AND user_id = ?",
            (share_id, user_id),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    @_serialized
    async def list_grants(self, share_id: str) -> list[ShareGrantRow]:
        """Grants of a share with the grantee's email joined for display."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT g.share_id, g.user_id, g.created_at, u.email "
            "FROM share_grants g JOIN users u ON u.user_id = g.user_id "
            "WHERE g.share_id = ? ORDER BY g.created_at",
            (share_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            ShareGrantRow(share_id=r[0], user_id=r[1], created_at=r[2], email=r[3]) for r in rows
        ]

    @_serialized
    async def list_shares_for_grantee(self, user_id: str) -> list[tuple[ShareRow, str]]:
        """(share, owner_email) for every live share granted to *user_id*.

        Shares of disabled owners are filtered here as well as in the service
        resolver (F14) — disabling an account kills its outgoing shares.
        """
        assert self._conn is not None
        now = _now_iso()
        async with self._conn.execute(
            "SELECT s.share_id, s.token_hash, s.owner_user_id, s.run_id, s.title, "
            "s.created_at, s.expires_at, s.revoked_at, u.email "
            "FROM shares s "
            "JOIN share_grants g ON g.share_id = s.share_id "
            "JOIN users u ON u.user_id = s.owner_user_id "
            "WHERE g.user_id = ? AND s.revoked_at IS NULL "
            "AND u.status = 'active' "
            "AND (s.expires_at IS NULL OR s.expires_at > ?) "
            "ORDER BY s.created_at DESC",
            (user_id, now),
        ) as cur:
            rows = await cur.fetchall()
        return [(self._share_from_row(r), r[8]) for r in rows]
