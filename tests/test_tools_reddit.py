"""Tests for reddit tool (P11)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.tools.reddit import register


@pytest.fixture
def config() -> AgentTopConfig:
    cfg = AgentTopConfig()
    cfg.reddit.enabled = True
    cfg.reddit.client_id_env = "REDDIT_CLIENT_ID"
    cfg.reddit.client_secret_env = "REDDIT_CLIENT_SECRET"
    return cfg


@pytest.fixture
def reg() -> ToolRegistry:
    return ToolRegistry()


class TestRedditCredentials:
    """Credentials and configuration handling."""

    async def test_missing_credentials_returns_error(self, config, reg):
        """When env vars are unset, returns a clean error (no crash)."""
        with patch("deep_research.tools.reddit.os.environ.get", return_value=""):
            await register(reg, config)
            result = await reg.call("reddit_search", {"query": "AI safety"})

        assert result.error is not None
        assert "credentials" in result.error.lower() or "unavailable" in result.error.lower()

    async def test_missing_asyncpraw_returns_error(self, config, reg):
        """When asyncpraw is not installed, returns a clean error."""
        with (
            patch("deep_research.tools.reddit.os.environ.get", side_effect=["fake_id", "fake_secret"]),
            patch("deep_research.tools.reddit._HAS_ASYNCPRAW", False),
        ):
            await register(reg, config)
            result = await reg.call("reddit_search", {"query": "test"})

        assert result.error is not None
        assert "asyncpraw" in result.error or "unavailable" in result.error


class TestRedditHappyPath:
    """Happy path — mocked asyncpraw returns results."""

    async def test_returns_citations(self, config, reg):
        """When asyncpraw works, returns Citation objects with source_type='reddit'."""
        mock_submission = MagicMock()
        mock_submission.title = "Test Reddit Post"
        mock_submission.url = "https://example.com/post"
        mock_submission.permalink = "/r/test/comments/123/"
        mock_submission.score = 100
        mock_submission.selftext = "This is a test post content."

        mock_subreddit = MagicMock()
        # search must return an async iterable, not a list
        async def _mock_search(*args, **kwargs):
            yield mock_submission
        mock_subreddit.search = _mock_search

        mock_reddit = MagicMock()
        mock_reddit.subreddit = AsyncMock(return_value=mock_subreddit)
        mock_reddit.__aenter__ = AsyncMock(return_value=mock_reddit)
        mock_reddit.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("deep_research.tools.reddit.os.environ.get", side_effect=["fake_id", "fake_secret"]),
            patch("deep_research.tools.reddit.asyncpraw") as mock_ap_mod,
        ):
            mock_ap_mod.Reddit = MagicMock(return_value=mock_reddit)
            await register(reg, config)
            result = await reg.call("reddit_search", {"query": "test", "max_results": 5})

        assert result.error is None
        assert len(result.citations) >= 1
        for c in result.citations:
            assert c.source_type == "reddit"
            assert c.discovered_by.value == "reddit"

    async def test_empty_results_returns_no_error(self, config, reg):
        """When Reddit returns no results, returns empty ToolResult (no crash)."""
        mock_subreddit = MagicMock()

        async def _mock_empty(*args, **kwargs):
            # Return an empty async generator
            if False:
                yield None
        mock_subreddit.search = _mock_empty

        mock_reddit = MagicMock()
        mock_reddit.subreddit = AsyncMock(return_value=mock_subreddit)
        mock_reddit.__aenter__ = AsyncMock(return_value=mock_reddit)
        mock_reddit.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("deep_research.tools.reddit.os.environ.get", side_effect=["fake_id", "fake_secret"]),
            patch("deep_research.tools.reddit.asyncpraw") as mock_ap_mod,
        ):
            mock_ap_mod.Reddit = MagicMock(return_value=mock_reddit)
            await register(reg, config)
            result = await reg.call("reddit_search", {"query": "xyznonexistent", "max_results": 5})

        assert result.error is None
        assert len(result.citations) == 0

    async def test_api_error_returns_graceful_error(self, config, reg):
        """When asyncpraw API call fails, returns a clean ToolResult.error (no crash)."""
        mock_subreddit = MagicMock()

        async def _mock_raise(*args, **kwargs):
            raise ValueError("API rate limit exceeded")
            yield  # never reached
        mock_subreddit.search = _mock_raise

        mock_reddit = MagicMock()
        mock_reddit.subreddit = AsyncMock(return_value=mock_subreddit)
        mock_reddit.__aenter__ = AsyncMock(return_value=mock_reddit)
        mock_reddit.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("deep_research.tools.reddit.os.environ.get", side_effect=["fake_id", "fake_secret"]),
            patch("deep_research.tools.reddit.asyncpraw") as mock_ap_mod,
        ):
            mock_ap_mod.Reddit = MagicMock(return_value=mock_reddit)
            await register(reg, config)
            result = await reg.call("reddit_search", {"query": "test", "max_results": 5})

        assert result.error is not None
        assert "API" in result.error or "rate limit" in result.error
