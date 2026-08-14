"""Tests for the deep-path paper-analysis pipeline (nodes/paper_analysis.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.nodes.paper_analysis import (
    DeepAnalysisResult,
    analyze_paper_deep,
    download_pdf_once,
    extract_text,
    fetch_paper_text_fallback,
    format_deep_analysis_context,
    render_pages,
    run_paper_analysis_pass,
)
from deep_research.state import (
    PaperAnalysis,
    PaperAnalysisRequest,
    ResearchState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**agent_overrides) -> AgentTopConfig:
    cfg = AgentTopConfig()
    for k, v in agent_overrides.items():
        setattr(cfg.agent, k, v)
    return cfg


def _registry(tools: dict[str, object]) -> ToolRegistry:
    reg = ToolRegistry()
    for name, func in tools.items():
        reg.register(name, func, {"type": "function", "name": name})
    return reg


def _fake_analysis(title: str = "Paper") -> PaperAnalysis:
    return PaperAnalysis(
        title=title,
        summary="summary text",
        key_findings=["finding one", "finding two"],
    )


def _fake_router(client: MagicMock, model: str = "text") -> MagicMock:
    """Wrap a fake OpenAI client in a fake LLMRouter exposing `resolve`."""
    router = MagicMock()
    router.resolve.return_value = MagicMock(
        client=client, model=model, max_context_tokens=131072
    )
    return router


# ---------------------------------------------------------------------------
# run_paper_analysis_pass
# ---------------------------------------------------------------------------


class TestRunPaperAnalysisPass:
    @pytest.mark.asyncio
    async def test_filters_caps_and_stores(self) -> None:
        cfg = _cfg(deep_analysis_max_papers=2, deep_analysis_min_priority=0.5)
        state = ResearchState(query="q")
        requests = [
            PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9, rationale="r"),
            PaperAnalysisRequest(arxiv_id="2401.00002", priority_score=0.4, rationale="low"),
            PaperAnalysisRequest(arxiv_id="2401.00003", priority_score=0.8, rationale="r"),
            PaperAnalysisRequest(arxiv_id="2401.00004", priority_score=0.99, rationale="r"),
        ]
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            return_value=DeepAnalysisResult(analysis=_fake_analysis("T")),
        ):
            await run_paper_analysis_pass(
                state,
                requests,
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
            )
        # Top 2 by priority above 0.5: 00004 (0.99), 00001 (0.9)
        assert set(state.deep_analyses) == {"2401.00004", "2401.00001"}
        assert state.deep_analysis_requested == ["2401.00004", "2401.00001"]

    @pytest.mark.asyncio
    async def test_does_not_reselect_requested_papers(self) -> None:
        cfg = _cfg(deep_analysis_max_papers=5)
        state = ResearchState(query="q")
        state.deep_analysis_requested = ["2401.00001"]
        requests = [
            PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.99),
            PaperAnalysisRequest(arxiv_id="2401.00002", priority_score=0.7),
        ]
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            return_value=DeepAnalysisResult(analysis=_fake_analysis("T")),
        ):
            await run_paper_analysis_pass(
                state, requests, query="q", router=_fake_router(MagicMock()), config=cfg, tools=ToolRegistry()
            )
        assert set(state.deep_analyses) == {"2401.00002"}
        assert state.deep_analysis_requested == ["2401.00001", "2401.00002"]

    @pytest.mark.asyncio
    async def test_cap_bounds_the_whole_run(self) -> None:
        """deep_analysis_max_papers must cap the RUN, not each pass call."""
        cfg = _cfg(deep_analysis_max_papers=2, deep_analysis_min_priority=0.0)
        state = ResearchState(query="q")
        fake = DeepAnalysisResult(analysis=_fake_analysis("T"))
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            return_value=fake,
        ):
            # Two separate critic rounds, each proposing 2 fresh papers.
            await run_paper_analysis_pass(
                state,
                [
                    PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9),
                    PaperAnalysisRequest(arxiv_id="2401.00002", priority_score=0.9),
                ],
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
            )
            await run_paper_analysis_pass(
                state,
                [
                    PaperAnalysisRequest(arxiv_id="2401.00003", priority_score=0.9),
                    PaperAnalysisRequest(arxiv_id="2401.00004", priority_score=0.9),
                ],
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
            )
        assert len(state.deep_analyses) == 2

    @pytest.mark.asyncio
    async def test_disabled_by_zero_cap(self) -> None:
        cfg = _cfg(deep_analysis_max_papers=0)
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.99)]
        await run_paper_analysis_pass(
            state, requests, query="q", router=_fake_router(MagicMock()), config=cfg, tools=ToolRegistry()
        )
        assert state.deep_analyses == {}

    @pytest.mark.asyncio
    async def test_failures_are_skipped_not_fatal(self) -> None:
        cfg = _cfg(deep_analysis_max_papers=5)
        state = ResearchState(query="q")
        requests = [
            PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9),
            PaperAnalysisRequest(arxiv_id="2401.00002", priority_score=0.8),
        ]

        async def _boom(*args, **kwargs):
            raise RuntimeError("analyzer down")

        with patch("deep_research.nodes.paper_analysis.analyze_paper_deep", side_effect=_boom):
            await run_paper_analysis_pass(
                state, requests, query="q", router=_fake_router(MagicMock()), config=cfg, tools=ToolRegistry()
            )
        assert state.deep_analyses == {}
        # requested ids are still recorded so we don't retry in this run
        assert state.deep_analysis_requested == ["2401.00001", "2401.00002"]


# ---------------------------------------------------------------------------
# analyze_paper_deep fallback chain
# ---------------------------------------------------------------------------


class TestFormatDeepAnalysisContext:
    def test_renders_analysis_digest(self) -> None:
        out = format_deep_analysis_context({"2401.00001": _fake_analysis("Digest Paper")})
        assert "Deep paper analyses" in out
        assert "arxiv:2401.00001" in out
        assert "finding one" in out

    def test_empty_returns_empty(self) -> None:
        assert format_deep_analysis_context({}) == ""


class TestAnalyzePaperDeep:
    @pytest.mark.asyncio
    async def test_full_pdf_path(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _download(**_) -> ToolResult:
            return ToolResult(content=str(pdf_path))

        async def _extract(**_) -> ToolResult:
            return ToolResult(content="full paper text")

        tools = _registry({"arxiv_download_pdf": _download, "pdf_extract_text": _extract})
        cfg = _cfg()
        cfg.pdf_vision.enabled = False

        called: dict = {}

        async def _fake_analyze(**kwargs):
            called.update(kwargs)
            return _fake_analysis("PDF Paper")

        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_node", side_effect=_fake_analyze
        ):
            result = await analyze_paper_deep(
                "2401.00001", query="q", router=_fake_router(MagicMock()), config=cfg, tools=tools
            )
        assert result is not None
        assert result.text_source == "pdf"
        assert called["text_source"] == "pdf"
        assert called["paper_text"] == "full paper text"

    @pytest.mark.asyncio
    async def test_abstract_fallback_when_download_fails(self) -> None:
        async def _download(**_) -> ToolResult:
            return ToolResult(content="", error="HTTP 503")

        async def _resolve(**_) -> ToolResult:
            return ToolResult(content="Resolved: Paper\nAbstract:\nabstract only")

        tools = _registry({"arxiv_download_pdf": _download, "arxiv_resolve": _resolve})
        cfg = _cfg()

        called: dict = {}

        async def _fake_analyze(**kwargs):
            called.update(kwargs)
            return _fake_analysis("Abstract Paper")

        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_node", side_effect=_fake_analyze
        ):
            result = await analyze_paper_deep(
                "2401.00001", query="q", router=_fake_router(MagicMock()), config=cfg, tools=tools
            )
        assert result is not None
        assert result.text_source == "abstract"
        assert called["text_source"] == "abstract"
        assert called["page_image_data_urls"] is None

    @pytest.mark.asyncio
    async def test_skips_when_no_content_available(self) -> None:
        async def _download(**_) -> ToolResult:
            return ToolResult(content="", error="HTTP 503")

        async def _resolve(**_) -> ToolResult:
            return ToolResult(content="", error="no result")

        tools = _registry({"arxiv_download_pdf": _download, "arxiv_resolve": _resolve})
        cfg = _cfg()
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_node",
            side_effect=AssertionError("should not be called"),
        ):
            result = await analyze_paper_deep(
                "2401.00001", query="q", router=_fake_router(MagicMock()), config=cfg, tools=tools
            )
        assert result is None


# ---------------------------------------------------------------------------
# Library cache reuse
# ---------------------------------------------------------------------------


class TestLibraryCacheReuse:
    @pytest.mark.asyncio
    async def test_reuses_prior_analysis(self, tmp_path) -> None:
        from deep_research.library.storage.rows import AnalysisRow
        from deep_research.library.writer import LibraryWriter

        class _Artifact:
            artifact_id = "art_1"
            title = "Cached Paper"

        class _FakeStorage:
            async def find_artifact_by_arxiv_id(self, arxiv_id):
                return _Artifact()

            async def get_analyses_for_artifact(self, artifact_id):
                return [
                    AnalysisRow(
                        analysis_id="a1",
                        artifact_id="art_1",
                        run_id="old_run",
                        analyzer="analyze_paper",
                        summary="cached summary",
                        key_findings='["cached finding"]',
                        key_references=(
                            '[{"arxiv_id": "2401.00009", "title": "Ref Dict"}, '
                            '"2401.00010", "garbage"]'
                        ),
                    )
                ]

        writer = LibraryWriter(_FakeStorage(), str(tmp_path / "lib"))
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]
        cfg = _cfg()
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            side_effect=AssertionError("should reuse cache, not analyze"),
        ):
            await run_paper_analysis_pass(
                state,
                requests,
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
                writer=writer,
            )
        assert state.deep_analyses["2401.00001"].summary == "cached summary"
        assert state.deep_analyses["2401.00001"].title == "Cached Paper"
        assert state.deep_analyses["2401.00001"].key_findings == ["cached finding"]
        refs = state.deep_analyses["2401.00001"].key_references
        assert [r.arxiv_id for r in refs] == ["2401.00009", "2401.00010"]


# ---------------------------------------------------------------------------
# Timeouts + remaining error branches
# ---------------------------------------------------------------------------


class TestToolTimeouts:
    @pytest.mark.asyncio
    async def test_download_timeout_returns_none(self) -> None:
        import asyncio

        async def _slow(**_: object) -> ToolResult:
            await asyncio.sleep(0.05)
            return ToolResult(content="/tmp/x.pdf")

        tools = _registry({"arxiv_download_pdf": _slow})
        out = await download_pdf_once("2401.00001", tools, timeout_s=0.01)
        assert out is None

    @pytest.mark.asyncio
    async def test_resolve_timeout_returns_empty(self) -> None:
        import asyncio

        async def _slow(**_: object) -> ToolResult:
            await asyncio.sleep(0.05)
            return ToolResult(content="metadata")

        tools = _registry({"arxiv_resolve": _slow})
        out, _ = await fetch_paper_text_fallback("2401.00001", tools, timeout_s=0.01)
        assert out == ""

    @pytest.mark.asyncio
    async def test_extract_timeout_returns_empty(self) -> None:
        import asyncio

        async def _slow(**_: object) -> ToolResult:
            await asyncio.sleep(0.05)
            return ToolResult(content="text")

        tools = _registry({"pdf_extract_text": _slow})
        out = await extract_text("/tmp/x.pdf", tools, timeout_s=0.01)
        assert out == ""

    @pytest.mark.asyncio
    async def test_render_timeout_returns_empty(self) -> None:
        import asyncio

        async def _slow(**_: object) -> ToolResult:
            await asyncio.sleep(0.05)
            return ToolResult(content='{"pages": [], "count": 0}')

        tools = _registry({"pdf_render_pages": _slow})
        out = await render_pages("/tmp/x.pdf", tools, timeout_s=0.01)
        assert out == []


class TestAnalyzePaperDeepTimeouts:
    @pytest.mark.asyncio
    async def test_render_failure_downgrades_to_text_only(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _download(**_: object) -> ToolResult:
            return ToolResult(content=str(pdf_path))

        async def _extract(**_: object) -> ToolResult:
            return ToolResult(content="paper text")

        tools = _registry({"arxiv_download_pdf": _download, "pdf_extract_text": _extract})
        cfg = _cfg()
        cfg.pdf_vision.enabled = True

        async def _render_timeout(**_: object):
            raise TimeoutError()

        called: dict = {}

        async def _fake_analyze(**kwargs):
            called.update(kwargs)
            return _fake_analysis("Text Only")

        with (
            patch(
                "deep_research.nodes.paper_analysis.render_pages",
                side_effect=_render_timeout,
            ),
            patch(
                "deep_research.nodes.paper_analysis.analyze_paper_node",
                side_effect=_fake_analyze,
            ),
        ):
            result = await analyze_paper_deep(
                "2401.00001", query="q", router=_fake_router(MagicMock()), config=cfg, tools=tools
            )
        assert result is not None
        assert result.text_source == "pdf"
        assert called["page_image_data_urls"] is None


class TestPassEdgeBranches:
    @pytest.mark.asyncio
    async def test_skips_when_analysis_returns_none(self) -> None:
        cfg = _cfg()
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]
        with patch("deep_research.nodes.paper_analysis.analyze_paper_deep", return_value=None):
            await run_paper_analysis_pass(
                state, requests, query="q", router=_fake_router(MagicMock()), config=cfg, tools=ToolRegistry()
            )
        assert state.deep_analyses == {}
        assert state.deep_analysis_requested == ["2401.00001"]

    @pytest.mark.asyncio
    async def test_analysis_timeout_is_caught(self) -> None:
        cfg = _cfg(deep_analysis_timeout_s=0.01)
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]

        async def _hang(*_: object, **__: object):
            import asyncio

            await asyncio.sleep(1.0)

        with patch("deep_research.nodes.paper_analysis.analyze_paper_deep", side_effect=_hang):
            await run_paper_analysis_pass(
                state, requests, query="q", router=_fake_router(MagicMock()), config=cfg, tools=ToolRegistry()
            )
        assert state.deep_analyses == {}

    @pytest.mark.asyncio
    async def test_archival_failure_does_not_lose_analysis(self) -> None:
        from deep_research.library.writer import LibraryWriter

        class _FakeStorage:
            async def find_artifact_by_arxiv_id(self, aid):
                return None

        writer = LibraryWriter(_FakeStorage(), "/tmp/paper_analysis_test_lib")
        cfg = _cfg()
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]
        result = DeepAnalysisResult(analysis=_fake_analysis("T"))
        with (
            patch(
                "deep_research.nodes.paper_analysis.analyze_paper_deep",
                return_value=result,
            ),
            patch(
                "deep_research.nodes.paper_analysis._archive_and_record",
                side_effect=RuntimeError("archival down"),
            ),
        ):
            await run_paper_analysis_pass(
                state,
                requests,
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
                writer=writer,
                run_id="run1",
            )
        assert "2401.00001" in state.deep_analyses

    @pytest.mark.asyncio
    async def test_library_cache_error_falls_back_to_analysis(self, tmp_path) -> None:
        from deep_research.library.writer import LibraryWriter

        class _ThrowingStorage:
            async def find_artifact_by_arxiv_id(self, aid):
                raise RuntimeError("db down")

        writer = LibraryWriter(_ThrowingStorage(), str(tmp_path / "lib"))
        cfg = _cfg()
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            return_value=DeepAnalysisResult(analysis=_fake_analysis("T")),
        ) as mock_analyze:
            await run_paper_analysis_pass(
                state,
                requests,
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
                writer=writer,
            )
        mock_analyze.assert_awaited_once()
        assert "2401.00001" in state.deep_analyses


class TestArchiveAndRecord:
    @pytest.mark.asyncio
    async def test_archives_pdf_and_records_analysis(self, tmp_path) -> None:
        from deep_research.library.storage.rows import AnalysisRow, ArtifactRow
        from deep_research.library.writer import LibraryWriter

        class _FakeStorage:
            def __init__(self) -> None:
                self.by_aid: dict[str, object] = {}
                self.analyses: list[AnalysisRow] = []

            async def find_artifact_by_arxiv_id(self, aid):
                return self.by_aid.get(aid)

            async def get_analyses_for_artifact(self, artifact_id):
                return []

            async def upsert_artifact(self, artifact: ArtifactRow) -> str:
                self.by_aid[artifact.arxiv_id or ""] = artifact
                return artifact.artifact_id

            async def insert_analysis(self, row: AnalysisRow) -> str:
                self.analyses.append(row)
                return row.analysis_id

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        storage = _FakeStorage()
        writer = LibraryWriter(storage, str(tmp_path / "lib"))
        cfg = _cfg()
        state = ResearchState(query="q")
        requests = [PaperAnalysisRequest(arxiv_id="2401.00001", priority_score=0.9)]
        result = DeepAnalysisResult(analysis=_fake_analysis("Archived"), pdf_path=str(pdf_path))
        with patch(
            "deep_research.nodes.paper_analysis.analyze_paper_deep",
            return_value=result,
        ):
            await run_paper_analysis_pass(
                state,
                requests,
                query="q",
                router=_fake_router(MagicMock()),
                config=cfg,
                tools=ToolRegistry(),
                writer=writer,
                run_id="run1",
            )
        assert "2401.00001" in state.deep_analyses
        assert storage.by_aid.get("2401.00001") is not None
        assert len(storage.analyses) == 1
        assert storage.analyses[0].run_id == "run1"


__all__ = [
    "TestAnalyzePaperDeep",
    "TestLibraryCacheReuse",
    "TestRunPaperAnalysisPass",
]
