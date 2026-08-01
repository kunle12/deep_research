"""Tests for the opt-in cited-arXiv-PDF archiving pass in agent.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research.agent import _archive_cited_arxiv_pdfs
from deep_research.config import AgentTopConfig
from deep_research.library.storage.rows import ArtifactRow
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.library.writer import LibraryWriter
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, Report
from deep_research.util import strip_arxiv_version


def _report(citations: list[Citation]) -> Report:
    return Report(markdown="# t", citations=citations, path="deep", query="q")


def _fake_downloader(tmp: Path, hits: dict[str, bytes]):
    async def _download(**kwargs):
        base = strip_arxiv_version(str(kwargs["arxiv_id"]))
        data = hits.get(base)
        if data is None:
            return ToolResult(content="", error=f"not found: {base}")
        path = tmp / f"{base}.pdf"
        path.write_bytes(data)
        return ToolResult(content=str(path))

    return _download


def _tools(tmp: Path, hits: dict[str, bytes]) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "arxiv_download_pdf",
        _fake_downloader(tmp, hits),
        {"description": "", "parameters": {}},
    )
    return reg


@pytest.fixture
async def backend_and_writer(tmp_path: Path):
    backend = SqliteStorageBackend(str(tmp_path / "index.db"))
    await backend.connect()
    await backend.ensure_schema()
    writer = LibraryWriter(backend, str(tmp_path / "library"))
    yield backend, writer
    await backend.close()


@pytest.mark.asyncio
async def test_archives_cited_pdfs_and_dedupes(backend_and_writer, tmp_path: Path):
    backend, writer = backend_and_writer
    hits = {f"2401.{i:05d}": f"%PDF-{i}".encode() for i in (10001, 10002, 10003)}
    reg = _tools(tmp_path, hits)
    report = _report(
        [
            Citation(url="https://arxiv.org/abs/2401.10001", arxiv_id="2401.10001", title="A"),
            Citation(url="https://arxiv.org/abs/2401.10002v2", arxiv_id="2401.10002v2", title="B"),
            Citation(url="https://arxiv.org/abs/2401.10001", arxiv_id="2401.10001", title="A dup"),
            Citation(url="https://scholar.example/x", arxiv_id="scholar:abc", title="S"),
        ]
    )
    n = await _archive_cited_arxiv_pdfs(report, reg, writer, AgentTopConfig(), run_id="r1")
    assert n == 2  # 10001 + version-normalized 10002; dup + scholar skipped
    assert await backend.find_artifact_by_arxiv_id("2401.10001") is not None
    assert await backend.find_artifact_by_arxiv_id("2401.10002") is not None
    assert await backend.find_artifact_by_arxiv_id("2401.10003") is None


@pytest.mark.asyncio
async def test_skips_already_archived(backend_and_writer, tmp_path: Path):
    backend, writer = backend_and_writer
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="existing",
            kind="pdf",
            source_type="arxiv",
            arxiv_id="2401.10001",
            bytes_path="artifacts/pdf/existing.pdf",
            bytes_size=1,
        )
    )
    reg = _tools(tmp_path, {"2401.10001": b"%PDF-existing"})
    report = _report([Citation(url="x", arxiv_id="2401.10001", title="A")])
    n = await _archive_cited_arxiv_pdfs(report, reg, writer, AgentTopConfig(), run_id="r1")
    assert n == 0
    art = await backend.find_artifact_by_arxiv_id("2401.10001")
    assert art is not None and art.artifact_id == "existing"


@pytest.mark.asyncio
async def test_missing_pdfs_are_skipped_gracefully(backend_and_writer, tmp_path: Path):
    backend, writer = backend_and_writer
    reg = _tools(tmp_path, {"2401.10001": b"%PDF-ok"})
    report = _report(
        [
            Citation(url="x", arxiv_id="2401.10001", title="A"),
            Citation(url="y", arxiv_id="2401.99999", title="Missing"),
        ]
    )
    n = await _archive_cited_arxiv_pdfs(report, reg, writer, AgentTopConfig(), run_id="r1")
    assert n == 1
    assert await backend.find_artifact_by_arxiv_id("2401.99999") is None


@pytest.mark.asyncio
async def test_disabled_when_downloads_off(backend_and_writer, tmp_path: Path):
    backend, writer = backend_and_writer
    cfg = AgentTopConfig()
    cfg.arxiv.download_pdfs = False
    reg = _tools(tmp_path, {"2401.10001": b"%PDF-x"})
    report = _report([Citation(url="x", arxiv_id="2401.10001", title="A")])
    n = await _archive_cited_arxiv_pdfs(report, reg, writer, cfg, run_id="r1")
    assert n == 0
    assert await backend.find_artifact_by_arxiv_id("2401.10001") is None


@pytest.mark.asyncio
async def test_default_flag_is_off():
    assert AgentTopConfig().pdl.archive_cited_arxiv_pdfs is False
