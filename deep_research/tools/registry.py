"""ToolRegistry factory — wires all enabled tools from config.

P1 implementation: registers stubs for every defined tool. Each phase (P2+)
fills in the actual behaviors. Tools that are disabled in config are skipped
so the LLM never sees them in the schema list.
"""

from __future__ import annotations

import logging

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry

logger = logging.getLogger(__name__)


async def build_tool_registry(config: AgentTopConfig) -> ToolRegistry:
    """Construct a ToolRegistry populated per the agent config.

    Tools that are disabled by config (e.g., `reddit.enabled: false`) are
    *not* registered, so they are invisible to the LLM.

    Stubs register with TODO behavior so we know the wiring is correct.
    """
    reg = ToolRegistry()
    reg.set_concurrency(config.agent.max_concurrent_tools)
    if config.agent.tool_timeout_s and config.agent.tool_timeout_s > 0:
        reg.set_tool_timeout(config.agent.tool_timeout_s)

    # Order matters only for log readability. Each tool registers its own schema.

    if config.search.primary or config.search.fallback_chain:
        from deep_research.tools import web_search

        await web_search.register(reg, config)

    if config.fetch_page.enabled:
        from deep_research.tools import fetch_page

        await fetch_page.register(reg, config)

    if config.arxiv.enabled:
        from deep_research.tools import arxiv as arxiv_tool

        await arxiv_tool.register(reg, config)

    # pdf_extract_text is always registered (works without poppler); only
    # pdf_render_pages (vision path) is gated on pdf_vision.enabled, inside
    # pdf_tool.register itself.
    from deep_research.tools import pdf as pdf_tool

    await pdf_tool.register(reg, config)

    if config.browser.enabled:
        from deep_research.tools import browser

        await browser.register(reg, config)

    if config.reddit.enabled:
        from deep_research.tools import reddit

        await reddit.register(reg, config)

    if config.scholar.enabled:
        from deep_research.tools import scholar

        await scholar.register(reg, config)

    if config.blog_search.enabled:
        from deep_research.tools import blog_search

        await blog_search.register(reg, config)

    logger.info("registered tools: %s", reg.names())
    return reg


__all__ = ["build_tool_registry"]
