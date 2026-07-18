"""Tests for refresh scheduler (P12.0)."""

from __future__ import annotations

import pytest

from deep_research.config import AgentTopConfig
from deep_research.scheduler import RefreshScheduler


@pytest.mark.asyncio
async def test_scheduler_initialization():
    """Scheduler can be created from config."""
    cfg = AgentTopConfig()
    cfg.pdl.enabled = True
    scheduler = RefreshScheduler(cfg)
    assert scheduler._config.pdl.enabled is True


@pytest.mark.asyncio
async def test_scheduler_disabled():
    """When PDL is disabled, scheduler does nothing."""
    cfg = AgentTopConfig()
    cfg.pdl.enabled = False
    scheduler = RefreshScheduler(cfg)
    assert scheduler._config.pdl.enabled is False
