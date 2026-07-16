"""browser tool — Playwright MCP async client.

P1 status: MINIMAL STUB. P8 will implement real MCP stdio client spawn
(npx -y @playwright/mcp@latest) with navigate / click / snapshot tools.

Tool schemas (subject to change when MCP is wired in P8):
- browser_navigate(url) -> page snapshot summary
- browser_click(selector) -> new snapshot
- browser_snapshot() -> accessibility tree summary
"""

from __future__ import annotations

import logging
from typing import Any

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)

NAVIGATE_SCHEMA = {
    "type": "function",
    "description": (
        "Open a URL in a headless browser (Playwright MCP). Useful for "
        "JS-heavy pages where fetch_page returns little content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to navigate to"},
        },
        "required": ["url"],
    },
}

SNAPSHOT_SCHEMA = {
    "type": "function",
    "description": "Return an accessibility-tree snapshot of the current page.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    async def _navigate(url: str, **_: Any) -> ToolResult:
        logger.info("browser_navigate STUB url=%r", url)
        cit = Citation(
            url=url,
            title=f"[STUB] browser nav {url[:64]}",
            snippet="P1 stub. P8 will launch Playwright MCP.",
            source_type="html",
            confidence_score=0.5,
            discovered_by=ToolName.browser,
        )
        return ToolResult(content=f"P1 STUB. browser_navigate({url!r})", citations=[cit])

    async def _snapshot(**_: Any) -> ToolResult:
        logger.info("browser_snapshot STUB")
        return ToolResult(content="P1 STUB. browser_snapshot()")

    reg.register("browser_navigate", _navigate, NAVIGATE_SCHEMA)
    reg.register("browser_snapshot", _snapshot, SNAPSHOT_SCHEMA)


__all__ = ["register"]
