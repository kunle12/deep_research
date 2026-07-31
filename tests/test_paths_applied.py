"""Tests for applied path (P12.0)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.paths.applied import applied_research
from deep_research.state import Citation, ClassifiedQuery, QueryPlan, ToolName


@pytest.fixture
def config() -> AgentTopConfig:
    return AgentTopConfig()


@pytest.fixture
def tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.set_concurrency(4)
    return reg


@pytest.mark.asyncio
async def test_applied_no_blog_search(config, tools):
    """When blog_search is not registered, returns a clean error report."""
    classified = ClassifiedQuery(path=QueryPlan.applied, search_hint="test query")
    client = MagicMock()

    report = await applied_research(classified, "test", client, tools, config)

    assert report.path == "applied"
    assert "not available" in report.markdown


@pytest.mark.asyncio
async def test_applied_blog_search_fails(config, tools):
    """When blog_search returns an error, returns a clean error report."""
    classified = ClassifiedQuery(path=QueryPlan.applied, search_hint="test query")

    async def _mock_blog_call(**kwargs):
        return MagicMock(error="API error", citations=[])

    tools._tools["blog_search"] = _mock_blog_call
    tools._schemas.append({"name": "blog_search", "description": "", "parameters": {}})

    client = MagicMock()
    report = await applied_research(classified, "test", client, tools, config)

    assert report.path == "applied"
    assert "failed" in report.markdown


@pytest.mark.asyncio
async def test_applied_happy_path(config, tools):
    """Happy path: blog_search returns citations, fetch_page works, LLM synthesizes."""
    classified = ClassifiedQuery(path=QueryPlan.applied, search_hint="test query")

    # Register blog_search mock
    async def _mock_blog(**kwargs):
        return MagicMock(
            error=None,
            citations=[
                Citation(
                    url="https://openai.com/index/post",
                    title="OpenAI Blog",
                    snippet="Content about AI.",
                    source_type="blog",
                    confidence_score=0.8,
                    discovered_by=ToolName.web_search,
                )
            ],
        )

    tools._tools["blog_search"] = _mock_blog
    tools._schemas.append({"name": "blog_search", "description": "", "parameters": {}})

    # Register fetch_page mock
    async def _mock_fetch(**kwargs):
        return MagicMock(error=None, content="Full blog post content here.")

    tools._tools["fetch_page"] = _mock_fetch
    tools._schemas.append({"name": "fetch_page", "description": "", "parameters": {}})

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="# Applied Report\n\nContent."))]
        )
    )

    report = await applied_research(classified, "test", client, tools, config)

    assert report.path == "applied"
    assert len(report.citations) >= 1


@pytest.mark.asyncio
async def test_applied_fallback_on_llm_error(config, tools):
    """When LLM synthesis fails, falls back to deterministic concatenation."""
    classified = ClassifiedQuery(path=QueryPlan.applied, search_hint="test query")

    async def _mock_blog(**kwargs):
        return MagicMock(
            error=None,
            citations=[
                Citation(
                    url="https://openai.com/index/post",
                    title="OpenAI Blog",
                    snippet="Content.",
                    source_type="blog",
                    confidence_score=0.8,
                    discovered_by=ToolName.web_search,
                )
            ],
        )

    tools._tools["blog_search"] = _mock_blog
    tools._schemas.append({"name": "blog_search", "description": "", "parameters": {}})

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=ValueError("LLM error"))

    report = await applied_research(classified, "test", client, tools, config)

    assert report.path == "applied"
    assert "OpenAI Blog" in report.markdown
