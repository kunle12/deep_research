"""reddit tool — asyncpraw-backed search.

P1 status: NOT IMPLEMENTED (raises NotImplementedError when called).
`reddit.enabled: false` in default config means this tool is NOT registered
in the ToolRegistry — so the LLM never sees it.

To enable later:
1. `uv sync --extra reddit`
2. Set `reddit.enabled: true` in config.yaml
3. Set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` env vars
4. Run as usual.
"""

from __future__ import annotations

import logging
from typing import Any

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.tools.base import NotImplementedError_

logger = logging.getLogger(__name__)

SEARCH_SCHEMA = {
    "type": "function",
    "description": (
        "Search Reddit for posts / comments matching the query. Returns up to "
        "`max_results` post summaries with permalink URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Reddit search query"},
            "subreddit": {
                "type": "string",
                "description": "Optional subreddit name (without /r/)",
                "default": "all",
            },
            "max_results": {
                "type": "integer", "description": "max posts to return", "default": 25,
            },
        },
        "required": ["query"],
    },
}


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    """Register the reddit tool. P1 implementation: raises NotImplementedError."""
    async def _call(query: str, subreddit: str = "all", max_results: int = 25, **_: Any) -> ToolResult:
        raise NotImplementedError_(
            "reddit tool is a stub. Install asyncpraw (`uv sync --extra reddit`) "
            "and set reddit.enabled: true in config.yaml + creds in env."
        )

    reg.register("reddit_search", _call, SEARCH_SCHEMA)


__all__ = ["register"]
