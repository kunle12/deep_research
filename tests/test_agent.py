"""End-to-end agent routing tests.

P1 has no real LLM; these tests exercise the routing logic by disabling the
classifier and using explicit path overrides, which avoids touching the LLM
client entirely.
"""

from __future__ import annotations

import pytest

from deep_research import run_research
from deep_research.config import AgentTopConfig


@pytest.fixture
def no_llm_config() -> AgentTopConfig:
    """Config with classifier disabled; default fall-through is `deep`."""
    cfg = AgentTopConfig()
    cfg.agent.classifier.enabled = False
    return cfg


class TestRunResearchRouting:
    @pytest.mark.asyncio
    async def test_empty_query_returns_error(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research("", no_llm_config)
        assert report.path == "unclear"
        assert "Empty query" in report.markdown

    @pytest.mark.asyncio
    async def test_path_override_quick(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "any question", no_llm_config, path_override="quick"
        )
        assert report.path == "quick"
        assert "Quick Answer" in report.markdown

    @pytest.mark.asyncio
    async def test_path_override_deep(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "any question", no_llm_config, path_override="deep"
        )
        assert report.path == "deep"

    @pytest.mark.asyncio
    async def test_path_override_academic(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "any question", no_llm_config, path_override="academic"
        )
        assert report.path == "academic"

    @pytest.mark.asyncio
    async def test_path_override_url_source_with_url(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "https://arxiv.org/abs/2401.12345 summarize",
            no_llm_config,
            path_override="url_source",
        )
        assert report.path in {"url_source", "url_source_with_followup"}
        assert "arxiv.org/abs/2401.12345" in report.markdown

    @pytest.mark.asyncio
    async def test_path_override_url_source_without_url_errors(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "no url here", no_llm_config, path_override="url_source"
        )
        assert report.path == "unclear"
        assert "requires a URL" in report.markdown

    @pytest.mark.asyncio
    async def test_url_auto_detection_arxiv(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "https://arxiv.org/abs/2401.12345 what are the main findings?",
            no_llm_config,
        )
        assert "arxiv" in report.classifier_rationale.lower()

    @pytest.mark.asyncio
    async def test_url_auto_detection_pdf(self, no_llm_config: AgentTopConfig) -> None:
        report = await run_research(
            "https://example.com/paper.pdf what does it prove?",
            no_llm_config,
        )
        assert "pdf" in report.classifier_rationale.lower()

    @pytest.mark.asyncio
    async def test_url_auto_detection_html_blog(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "https://blog.example.com/post summarize this",
            no_llm_config,
        )
        assert "html" in report.classifier_rationale.lower()


class TestReportShapes:
    @pytest.mark.asyncio
    async def test_quick_report_returns_citations_list(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        report = await run_research(
            "test", no_llm_config, path_override="quick"
        )
        # The quick path always returns a Report with citations (may be empty
        # if no Tavily key in env).
        assert isinstance(report.citations, list)
        # When TAVILY_API_KEY is set in the env, we expect at least 1 citation.
        import os
        if os.environ.get("TAVILY_API_KEY"):
            assert len(report.citations) >= 1

    @pytest.mark.asyncio
    async def test_report_renders_without_error(
        self, no_llm_config: AgentTopConfig
    ) -> None:
        from deep_research.report import render_report_markdown

        report = await run_research(
            "test", no_llm_config, path_override="quick"
        )
        rendered = render_report_markdown(report, no_llm_config.output)
        # Always produces a non-empty string
        assert rendered
        assert "Quick Answer" in rendered or "quick" in rendered.lower() or "Could not" in rendered
