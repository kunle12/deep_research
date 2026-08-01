"""FastAPI dependencies for the library web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from deep_research.library.storage.base import StorageBackend


def get_storage(request: Request) -> StorageBackend:
    """Resolve the shared storage backend from app state."""
    backend: StorageBackend | None = getattr(request.app.state, "backend", None)
    if backend is None:
        raise HTTPException(status_code=503, detail="storage backend not initialized")
    return backend


def get_root_dir(request: Request) -> Path:
    """Resolve the library root directory from the loaded config."""
    cfg = getattr(request.app.state, "config", None)
    if cfg is None or not getattr(cfg, "pdl", None):
        raise HTTPException(status_code=503, detail="server config not initialized")
    return Path(cfg.pdl.root_dir)
