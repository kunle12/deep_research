"""reddit tool — asyncpraw-backed search.

P11: implemented — real asyncpraw integration.
Requires `uv sync --extra reddit`, `reddit.enabled: true`, and Reddit API credentials.
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    import asyncpraw

    _HAS_ASYNCPRAW = True
except ImportError:
    _HAS_ASYNCPRAW = False

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)

SEARCH_SCHEMA = {
    "type": "function",
    "description": (
        "Search Reddit for posts / comments matching the query. Returns up to "
        "`max_results` post summaries with permalink URL, score, and snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Reddit search query"},
            "subreddit": {
                "type": "string",
                "description": "Optional subreddit name (without /r/). Default 'all'.",
                "default": "all",
            },
            "max_results": {
                "type": "integer",
                "description": "Max posts to return (default 25).",
                "default": 25,
            },
        },
        "required": ["query"],
    },
}


def _build_reddit(config: AgentTopConfig) -> Any:
    """Build or retrieve a cached asyncpraw.Reddit instance."""
    if not _HAS_ASYNCPRAW:
        raise ImportError("asyncpraw is not installed. Run `uv sync --extra reddit` to install it.")
    client_id = os.environ.get(config.reddit.client_id_env, "")
    client_secret = os.environ.get(config.reddit.client_secret_env, "")
    if not client_id or not client_secret:
        raise ValueError(
            f"Reddit credentials not set. Set {config.reddit.client_id_env} "
            f"and {config.reddit.client_secret_env} env vars."
        )
    return asyncpraw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=config.reddit.user_agent,
    )


async def _search_subreddit(
    reddit: Any,
    query: str,
    subreddit_name: str,
    max_results: int,
) -> list[Citation]:
    """Search a subreddit (or all) and return normalized Citation objects."""
    subreddit = await reddit.subreddit(subreddit_name)
    citations: list[Citation] = []
    async for submission in subreddit.search(
        query,
        limit=max_results,
        sort="relevance",
        time_filter="all",
    ):
        title = submission.title or ""
        url = submission.url or f"https://reddit.com{submission.permalink}"
        selftext = (getattr(submission, "selftext", "") or "")[:500]
        citations.append(
            Citation(
                url=url,
                title=title,
                snippet=selftext[:300] or f"{submission.score} points — r/{subreddit_name}",
                source_type="reddit",
                confidence_score=(
                    max(0.0, min(submission.score / 1000.0, 0.9)) if submission.score else 0.5
                ),
                discovered_by=ToolName.reddit,
            )
        )
    return citations


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    """Register the reddit tool. P11: real asyncpraw-backed search."""

    async def _call(
        query: str,
        subreddit: str = "all",
        max_results: int = 25,
        **_: Any,
    ) -> ToolResult:
        try:
            reddit = _build_reddit(config)
        except (ImportError, ValueError) as e:
            return ToolResult(
                content="",
                error=f"Reddit unavailable: {e}",
            )

        try:
            async with reddit as r:
                citations = await _search_subreddit(r, query, subreddit, max_results)
        except Exception as e:
            logger.warning("reddit search failed: %s: %s", type(e).__name__, e)
            return ToolResult(
                content="",
                error=f"{type(e).__name__}: {e}",
            )

        if not citations:
            return ToolResult(
                content="No Reddit results found.",
                error=None,
            )

        # Format for LLM
        lines: list[str] = []
        for i, c in enumerate(citations, start=1):
            title = c.title or "(no title)"
            snippet = c.snippet.strip()[:200]
            lines.append(
                f"{i}. {title}\n   URL: {c.url}\n   Score: {c.confidence_score:.2f}\n   {snippet}"
            )
        content = "\n\n".join(lines)

        return ToolResult(content=content, citations=citations)

    reg.register("reddit_search", _call, SEARCH_SCHEMA)


__all__ = ["register"]
