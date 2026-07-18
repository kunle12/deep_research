"""Deep Research Agent - public async entrypoint."""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from deep_research.config import AgentTopConfig
from deep_research.llm.client import LLMClient
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.progress import ProgressReporter, ensure_reporter
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
    progress: ProgressReporter | None = None,
) -> Report:
    """Top-level public entrypoint.

    `path_override` takes precedence over both classifier and URL detection.
    `progress` — when provided, the agent calls phase/step as it moves through routing.
    """
    reporter: ProgressReporter = ensure_reporter(progress)

    # LibraryWriter setup (optional, based on config)
    writer: LibraryWriter | NullLibraryWriter | None = None
    run_id: str = ""
    if config.pdl.enabled:
        from deep_research.library.storage import get_backend
        backend = await get_backend(config)
        writer = LibraryWriter(backend, config.pdl.root_dir)
        run_id = uuid.uuid4().hex[:16]
        writer.set_run_id(run_id)

    if not query or not query.strip():
        reporter.phase("error", "empty query")
        reporter.complete()
        return Report(
            markdown="# Error\n\nEmpty query.",
            path="unclear",
            classifier_rationale="Empty query supplied.",
        )

    # Extract URL for url_source override (needs pre-check before async with)
    if path_override == "url_source":
        url = extract_first_url(query) or query.strip()
        remainder = strip_url_from_query(query, url) if url != query.strip() else ""
        if not url.startswith(("http://", "https://")):
            reporter.phase("error", "--url-source without URL")
            reporter.complete()
            return Report(
                markdown=f"# Error\n\n`--url-source` requires a URL. Got: `{query!r}`.",
                path="unclear",
                classifier_rationale="--url-source override with non-URL query.",
            )

    # Single async with block for LLM client + tools — all routing happens inside
    async with LLMClient(config.llm) as client, _build_tools(config) as tools:
        # Step 1 — explicit override (from CLI flag) wins
        if path_override:
            logger.info("path override: %s", path_override)
            reporter.phase("routing", f"--{path_override} override")
            if path_override == "url_source":
                reporter.phase("url_source", "fetching source")
                report = await _dispatch_url_source(
                    url, remainder, client, tools, config, reporter,
                    writer=writer, run_id=run_id,
                )
            else:
                classified = ClassifiedQuery(
                    path=QueryPlan(path_override),
                    rationale=f"explicit --{path_override} override",
                    search_hint=query,
                )
                reporter.phase(path_override, f"--{path_override} override")
                report = await _dispatch_classified(
                    classified, query, client, tools, config, reporter,
                    writer=writer, run_id=run_id,
                )
            reporter.complete()
            await _archive_report(report, writer, run_id)
            return report

        # Step 2 — config force_path (yaml) second-highest priority
        if config.agent.classifier.force_path:
            force = config.agent.classifier.force_path
            logger.info("config force_path: %s", force)
            reporter.phase("routing", f"config.force_path = {force!r}")
            classified = ClassifiedQuery(
                path=QueryPlan(force),
                rationale=f"config.force_path = {force!r}",
                search_hint=query,
            )
            report = await _dispatch_classified(
                classified, query, client, tools, config, reporter,
                writer=writer, run_id=run_id,
            )
            reporter.complete()
            await _archive_report(report, writer, run_id)
            return report

        # Step 3 — URL detection routes to url_source
        url = extract_first_url(query)
        if url and config.url_source.enabled:
            remainder = strip_url_from_query(query, url)
            logger.info("URL detected: %s (remainder: %r)", url, remainder)
            reporter.phase("url_source", f"auto-detected URL: {url[:80]}")
            report = await _dispatch_url_source(
                url, remainder, client, tools, config, reporter,
                writer=writer, run_id=run_id,
            )
            reporter.complete()
            await _archive_report(report, writer, run_id)
            return report

        # Step 4 — classifier (or default to deep if disabled)
        if not config.agent.classifier.enabled:
            logger.info("classifier disabled; defaulting to deep.")
            reporter.phase("deep", "classifier disabled; defaulting to deep")
            classified = ClassifiedQuery(
                path=QueryPlan.deep,
                rationale="classifier disabled by config",
                search_hint=query,
            )
            report = await _dispatch_classified(
                classified, query, client, tools, config, reporter,
                writer=writer, run_id=run_id,
            )
            reporter.complete()
            await _archive_report(report, writer, run_id)
            return report

        reporter.phase("routing", "classifier LLM call")
        from deep_research.paths import classify_query

        classified = await classify_query(query, client, config.llm.text_model)
        logger.info("classifier returned path=%s rationale=%r", classified.path, classified.rationale)
        reporter.phase(
            classified.path.value if hasattr(classified.path, "value") else str(classified.path),
            f"chosen by classifier: {classified.rationale[:80]}",
        )
        report = await _dispatch_classified(
            classified, query, client, tools, config, reporter,
            writer=writer, run_id=run_id,
        )

    reporter.complete()
    await _archive_report(report, writer, run_id)
    return report


async def _dispatch_classified(
    classified: ClassifiedQuery,
    original_query: str,
    client,
    tools: ToolRegistry,
    config: AgentTopConfig,
    reporter: ProgressReporter,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Run the path chosen by the classifier."""
    from deep_research.paths import academic_research, applied_research, deep_research, quick_search

    if classified.path == QueryPlan.quick:
        return await quick_search(classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id)
    if classified.path == QueryPlan.deep:
        return await deep_research(classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id)
    if classified.path == QueryPlan.academic:
        return await academic_research(classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id)
    if classified.path == QueryPlan.applied:
        return await applied_research(classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id)
    if classified.path == QueryPlan.unclear:
        reporter.phase("clarify", "need clarification")
        return Report(
            markdown=(f"# Clarification needed\n\n{chr(10).join(f'- {q}' for q in classified.clarifying_questions)}"),
            path="unclear",
            classifier_rationale=classified.rationale,
            clarifying_questions=list(classified.clarifying_questions),
        )
    return await deep_research(classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id)


async def _dispatch_url_source(
    url: str,
    remainder: str,
    client,
    tools: ToolRegistry,
    config: AgentTopConfig,
    reporter: ProgressReporter,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    from deep_research.paths import url_source

    return await url_source(url, remainder, client, tools, config, reporter, writer=writer, run_id=run_id)


class _ToolsCtx:
    """Async context manager wrapper around build_tool_registry()."""

    def __init__(self, config: AgentTopConfig) -> None:
        self._config = config
        self._tools: ToolRegistry | None = None

    async def __aenter__(self) -> ToolRegistry:
        self._tools = await build_tool_registry(self._config)
        return self._tools

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._tools is not None:
            await self._tools.close()
        self._tools = None


def _build_tools(config: AgentTopConfig) -> _ToolsCtx:
    return _ToolsCtx(config)


async def _archive_report(report: Report, writer: LibraryWriter | None, run_id: str) -> None:
    """Archive report in the personal digital library if writer is configured."""
    if isinstance(writer, LibraryWriter) and run_id:
        try:
            await writer.archive_report(report, run_id)
        except Exception as e:
            logger.warning("archive_report failed: %s: %s", type(e).__name__, e)
        try:
            await writer.storage.close()
        except Exception as e:
            logger.warning("storage close failed: %s: %s", type(e).__name__, e)


__all__ = ["run_research"]
