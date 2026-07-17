"""Deep Research Agent - public async entrypoint.

Usage:

    from deep_research import run_research, AgentTopConfig

    config = AgentTopConfig.load_yaml("config.yaml")
    report = await run_research("your query here", config)
    print(report.markdown)

Routing logic:
  1. If `config.agent.classifier.force_path` is set, use that path (skip URL detection too).
  2. Else, if a URL is detected in the query text, route to paths.url_source.
  3. Else, if classifier.enabled, call classifier LLM and dispatch on its output.
  4. Else (classifier disabled, no force path, no URL), default to paths.deep.

CLI flags (when invoked via `python -m deep_research`):
  - --quick / --deep / --academic / --url-source override the classifier/URL detection entirely.
  - These map to `force_path` in `agent.classifier` config OR are passed via `path_override`.
"""

from __future__ import annotations

import logging
from typing import Literal

from deep_research.config import AgentTopConfig
from deep_research.llm.client import LLMClient
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.state import (
    ClassifiedQuery,
    QueryPlan,
    Report,
)
from deep_research.tools import build_tool_registry
from deep_research.tools.url_detector import extract_first_url, strip_url_from_query

logger = logging.getLogger(__name__)


PathOverride = Literal["quick", "deep", "academic", "url_source"]


async def run_research(
    query: str,
    config: AgentTopConfig,
    *,
    path_override: PathOverride | None = None,
) -> Report:
    """Top-level public entrypoint.

    `path_override` (optional) takes precedence over both classifier and URL
    detection. CLI commands should pass `--quick/--deep/--academic/--url-source`
    flag value here.
    """
    if not query or not query.strip():
        return Report(
            markdown="# Error\n\nEmpty query.",
            path="unclear",
            classifier_rationale="Empty query supplied.",
        )

    # Step 1 — explicit override (from CLI flag) wins
    if path_override:
        logger.info("path override: %s", path_override)
        if path_override == "url_source":
            url = extract_first_url(query) or query.strip()
            remainder = strip_url_from_query(query, url) if url != query.strip() else ""
            if not _looks_like_url(url):
                # User forced --url-source but no URL given; emit friendly error.
                return Report(
                    markdown=f"# Error\n\n`--url-source` requires a URL. Got: `{query!r}`.",
                    path="unclear",
                    classifier_rationale="--url-source override with non-URL query.",
                )
            async with LLMClient(config.llm) as client, _build_tools(config) as tools:
                return await _dispatch_url_source(url, remainder, client, tools, config)
        # Other overrides (quick / deep / academic) go through normal dispatch
        # with a fabricated ClassifiedQuery
        classified = ClassifiedQuery(
            path=QueryPlan(path_override),
            rationale=f"explicit --{path_override} override",
            search_hint=query,
        )
        async with LLMClient(config.llm) as client, _build_tools(config) as tools:
            return await _dispatch_classified(classified, query, client, tools, config)

    # Step 2 — config force_path (yaml) second-highest priority
    if config.agent.classifier.force_path:
        force = config.agent.classifier.force_path
        logger.info("config force_path: %s", force)
        classified = ClassifiedQuery(
            path=QueryPlan(force),
            rationale=f"config.force_path = {force!r}",
            search_hint=query,
        )
        async with LLMClient(config.llm) as client, _build_tools(config) as tools:
            return await _dispatch_classified(classified, query, client, tools, config)

    # Step 3 — URL detection routes to url_source
    url = extract_first_url(query)
    if url and config.url_source.enabled:
        remainder = strip_url_from_query(query, url)
        logger.info("URL detected: %s (remainder: %r)", url, remainder)
        async with LLMClient(config.llm) as client, _build_tools(config) as tools:
            return await _dispatch_url_source(url, remainder, client, tools, config)

    # Step 4 — classifier (or default to deep if disabled)
    if not config.agent.classifier.enabled:
        logger.info("classifier disabled; defaulting to deep.")
        classified = ClassifiedQuery(
            path=QueryPlan.deep,
            rationale="classifier disabled by config",
            search_hint=query,
        )
        async with LLMClient(config.llm) as client, _build_tools(config) as tools:
            return await _dispatch_classified(classified, query, client, tools, config)

    async with LLMClient(config.llm) as client, _build_tools(config) as tools:
        from deep_research.paths import classify_query

        classified = await classify_query(query, client, config.llm.text_model)
        logger.info("classifier returned path=%s rationale=%r", classified.path, classified.rationale)
        return await _dispatch_classified(classified, query, client, tools, config)


async def _dispatch_classified(
    classified: ClassifiedQuery,
    original_query: str,
    client,  # openai.AsyncOpenAI
    tools: ToolRegistry,
    config: AgentTopConfig,
) -> Report:
    """Run the path chosen by the classifier."""
    from deep_research.paths import academic_research, deep_research, quick_search

    if classified.path == QueryPlan.quick:
        return await quick_search(classified, original_query, client, tools, config)
    if classified.path == QueryPlan.deep:
        return await deep_research(classified, original_query, client, tools, config)
    if classified.path == QueryPlan.academic:
        return await academic_research(classified, original_query, client, tools, config)
    if classified.path == QueryPlan.unclear:
        return Report(
            markdown=(f"# Clarification needed\n\n{chr(10).join(f'- {q}' for q in classified.clarifying_questions)}"),
            path="unclear",
            classifier_rationale=classified.rationale,
            clarifying_questions=list(classified.clarifying_questions),
        )
    # Defensive fallback
    return await deep_research(classified, original_query, client, tools, config)


async def _dispatch_url_source(
    url: str,
    remainder: str,
    client,
    tools: ToolRegistry,
    config: AgentTopConfig,
) -> Report:
    from deep_research.paths import url_source

    return await url_source(url, remainder, client, tools, config)


class _ToolsCtx:
    """Async context manager wrapper around build_tool_registry() so we can `async with` it."""

    def __init__(self, config: AgentTopConfig) -> None:
        self._config = config
        self._tools: ToolRegistry | None = None

    async def __aenter__(self) -> ToolRegistry:
        self._tools = await build_tool_registry(self._config)
        return self._tools

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # P8: explicit teardown for the browser MCP subprocess (closed via the
        # hook the browser tool registers on the registry at register() time).
        # We swallow teardown errors so we don't mask the original exception
        # that triggered __aexit__.
        if self._tools is not None:
            close_hook = getattr(self._tools, "_browser_close", None)
            if close_hook is not None:
                try:
                    await close_hook()
                except Exception as e:
                    logger.debug("browser MCP teardown raised: %s: %s", type(e).__name__, e)
        self._tools = None


def _build_tools(config: AgentTopConfig) -> _ToolsCtx:
    return _ToolsCtx(config)


def _looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


__all__ = ["run_research"]
