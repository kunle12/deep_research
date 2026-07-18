"""Tests for storage backend factory."""

from __future__ import annotations

import os

import pytest

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend


@pytest.mark.asyncio
async def test_get_sqlite_backend():
    """SQLite backend can be created from config."""
    cfg = AgentTopConfig()
    cfg.pdl.root_dir = f"/tmp/test_pdl_{os.urandom(4).hex()}"
    cfg.pdl.storage.backend = "sqlite"
    backend = await get_backend(cfg)
    assert backend is not None
    # Schema initialized; no version-based checks needed
    await backend.close()


@pytest.mark.asyncio
async def test_get_unknown_backend():
    """Unknown backend raises ValueError."""
    cfg = AgentTopConfig()
    cfg.pdl.storage.backend = "unknown"  # type: ignore
    with pytest.raises(ValueError, match="Unknown storage backend"):
        await get_backend(cfg)
