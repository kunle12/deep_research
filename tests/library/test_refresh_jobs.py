"""Conformance tests: refresh jobs."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_start_and_complete_refresh_job(sqlite_backend):
    job_id = await sqlite_backend.start_refresh_job("source_type", "arxiv")
    assert job_id is not None
    assert len(job_id) > 0

    await sqlite_backend.complete_refresh_job(
        job_id=job_id,
        considered=10,
        refreshed=3,
        status="completed",
    )


@pytest.mark.asyncio
async def test_start_refresh_job_twice(sqlite_backend):
    j1 = await sqlite_backend.start_refresh_job("tag", "ml")
    j2 = await sqlite_backend.start_refresh_job("tag", "nlp")
    assert j1 != j2
