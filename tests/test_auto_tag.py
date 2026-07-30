"""Tests for auto_tag node (P10.7)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.library.writer import NullLibraryWriter


@pytest.mark.asyncio
async def test_auto_tag_skips_without_writer():
    """auto_tag_report returns empty when writer is None."""
    from deep_research.nodes.auto_tag import auto_tag_report

    result = await auto_tag_report("query", "report text", "art1", None, "model", None, "run1")
    assert result == []


@pytest.mark.asyncio
async def test_auto_tag_skips_without_run_id():
    """auto_tag_report returns empty when run_id is empty."""
    from deep_research.nodes.auto_tag import auto_tag_report

    writer = NullLibraryWriter()
    result = await auto_tag_report("query", "report text", "art1", None, "model", writer, "")
    assert result == []


@pytest.mark.asyncio
async def test_auto_tag_skips_without_artifact_id():
    """auto_tag_report returns empty when artifact_id is empty."""
    from deep_research.nodes.auto_tag import auto_tag_report

    writer = NullLibraryWriter()
    result = await auto_tag_report("query", "report text", "", None, "model", writer, "run1")
    assert result == []


@pytest.mark.asyncio
async def test_auto_tag_calls_llm_and_tags():
    """auto_tag_report makes LLM call and persists tags."""
    import tempfile
    from pathlib import Path

    from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
    from deep_research.library.writer import LibraryWriter
    from deep_research.nodes.auto_tag import auto_tag_report

    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    db_path = str(root / "index.db")
    backend = SqliteStorageBackend(db_path=db_path)
    await backend.connect()

    # Create an artifact to tag
    from datetime import UTC, datetime

    from deep_research.library.storage.rows import ArtifactRow, ReportRow

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://example.com",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at=now,
        last_touched_at=now,
    )
    await backend.upsert_artifact(art)
    # Insert a report row so the FK constraint on applied_in_run is satisfied
    report = ReportRow(run_id="test_run", started_at=now, markdown="# Report")
    await backend.insert_report(report)

    writer = LibraryWriter(backend, str(root))

    # Mock LLM client
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps({"tags": ["quantization", "llm_inference"]})
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    result = await auto_tag_report(
        "test query about LLM quantization",
        "# Report\n\nContent about quantization techniques.",
        "art1",
        mock_client,
        "test-model",
        writer,
        "test_run",
    )

    assert result == ["quantization", "llm_inference"]

    # Verify tags were persisted
    tags = await backend.get_tags_for_artifact("art1")
    assert len(tags) == 2
    assert tags[0].applied_in_run == "test_run"
    await backend.close()


@pytest.mark.asyncio
async def test_auto_tag_handles_llm_error():
    """auto_tag_report returns empty on LLM failure."""
    import tempfile
    from pathlib import Path

    from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
    from deep_research.library.writer import LibraryWriter
    from deep_research.nodes.auto_tag import auto_tag_report

    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    db_path = str(root / "index.db")
    backend = SqliteStorageBackend(db_path=db_path)
    await backend.connect()

    from datetime import UTC, datetime

    from deep_research.library.storage.rows import ArtifactRow

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="art2",
        kind="pdf",
        source_url="https://example.com",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art2.pdf",
        bytes_size=1024,
        first_seen_at=now,
        last_touched_at=now,
    )
    await backend.upsert_artifact(art)

    writer = LibraryWriter(backend, str(root))

    # Mock LLM client that raises
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))

    result = await auto_tag_report(
        "test query",
        "# Report",
        "art2",
        mock_client,
        "test-model",
        writer,
        "test_run",
    )
    assert result == []
    await backend.close()


@pytest.mark.asyncio
async def test_auto_tag_handles_bad_json():
    """auto_tag_report returns empty on invalid JSON from LLM."""
    import tempfile
    from pathlib import Path

    from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
    from deep_research.library.writer import LibraryWriter
    from deep_research.nodes.auto_tag import auto_tag_report

    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    db_path = str(root / "index.db")
    backend = SqliteStorageBackend(db_path=db_path)
    await backend.connect()

    from datetime import UTC, datetime

    from deep_research.library.storage.rows import ArtifactRow

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="art3",
        kind="pdf",
        source_url="https://example.com",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art3.pdf",
        bytes_size=1024,
        first_seen_at=now,
        last_touched_at=now,
    )
    await backend.upsert_artifact(art)

    writer = LibraryWriter(backend, str(root))

    # Mock LLM client that returns bad JSON
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "not json at all"
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    result = await auto_tag_report(
        "test query",
        "# Report",
        "art3",
        mock_client,
        "test-model",
        writer,
        "test_run",
    )
    assert result == []
    await backend.close()
