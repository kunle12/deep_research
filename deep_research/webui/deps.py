"""FastAPI dependencies for the library web UI.

The auth middleware (webui/app.py) resolves the caller's account and their
personal library backend *before* handlers run; these dependencies only read
that prepared state, so they stay synchronous and every existing handler
keeps working unchanged (docs/MULTI_USER_PLAN.md §5).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from deep_research.accounts.rows import UserRow
from deep_research.config import AgentTopConfig
from deep_research.library.storage.base import StorageBackend


def get_user(request: Request) -> UserRow | None:
    """The resolved account for this request (None = unauthenticated)."""
    return getattr(request.state, "user", None)


def require_user(request: Request) -> UserRow:
    """The resolved account, or 401 when unauthenticated."""
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def get_storage(request: Request) -> StorageBackend:
    """Resolve the caller's personal library backend.

    In multi_user mode this is the requesting user's own library; in
    single_user mode it is the shared backend (legacy behavior).
    """
    storage = getattr(request.state, "storage", None)
    if storage is None:
        # Fallback for single_user (shared backend from app state).
        storage = getattr(request.app.state, "backend", None)
    if storage is None:
        if get_user(request) is None:
            raise HTTPException(status_code=401, detail="authentication required")
        raise HTTPException(status_code=503, detail="storage backend not initialized")
    return storage


def get_config(request: Request) -> AgentTopConfig:
    """Resolve the loaded agent config from app state."""
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        raise HTTPException(status_code=503, detail="server config not initialized")
    return cfg


def get_root_dir(request: Request) -> Path:
    """Resolve the caller's library root directory.

    Per-user root in multi_user mode; the configured root in single_user mode.
    """
    root = getattr(request.state, "library_root", None)
    if root is not None:
        return Path(root)
    cfg = get_config(request)
    if not getattr(cfg, "pdl", None):
        raise HTTPException(status_code=503, detail="server config not initialized")
    return Path(cfg.pdl.root_dir)
