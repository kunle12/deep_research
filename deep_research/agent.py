"""Deep Research Agent - public async entrypoint."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.client import LLMClient
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.glossarize import extract_glossary_from_report
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
        backend = None
        try:
            from deep_research.library.storage import get_backend

            backend = await get_backend(config)
            writer = LibraryWriter(backend, config.pdl.root_dir)
            run_id = uuid.uuid4().hex[:16]
            writer.set_run_id(run_id)
        except Exception as e:
            logger.warning(
                "PDL backend init failed: %s: %s; proceeding without library", type(e).__name__, e
            )
            if backend is not None:
                await backend.close()
            writer = None

    if not query or not query.strip():
        reporter.phase("error", "empty query")
        reporter.complete()
        return Report(
            markdown="# Error\n\nEmpty query.",
            path="unclear",
            classifier_rationale="Empty query supplied.",
        )

    # Extract URL for url_source override (needs pre-check before async with)
    override_url: str | None = None
    override_remainder: str | None = None
    if path_override == "url_source":
        override_url = extract_first_url(query) or query.strip()
        override_remainder = (
            strip_url_from_query(query, override_url) if override_url != query.strip() else ""
        )
        if not override_url.startswith(("http://", "https://")):
            reporter.phase("error", "--url-source without URL")
            reporter.complete()
            return Report(
                markdown=f"# Error\n\n`--url-source` requires a URL. Got: `{query!r}`.",
                path="unclear",
                classifier_rationale="--url-source override with non-URL query.",
            )

    # Single async with block for LLM client + tools — all routing happens inside
    async with LLMClient(config.llm) as client, _build_tools(config) as tools:
        report = await _route_and_dispatch(
            query,
            path_override,
            override_url,
            override_remainder,
            client,
            tools,
            config,
            reporter,
            writer,
            run_id,
        )

        # P10.6: dedicated glossary extraction from report text
        if report.markdown:
            glossary_entries = await extract_glossary_from_report(
                report.markdown,
                client,
                config.llm.text_model,
                writer,
                run_id,
            )
            report.glossary_entries = glossary_entries

    reporter.complete()
    await _archive_report(report, writer, run_id)
    return report


async def _route_and_dispatch(
    query: str,
    path_override: PathOverride | None,
    override_url: str | None,
    override_remainder: str | None,
    client,
    tools: ToolRegistry,
    config: AgentTopConfig,
    reporter: ProgressReporter,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Determine route and dispatch to the correct path.

    Priority: path_override > force_path > URL detection > classifier > deep default.
    """
    # Step 1 — explicit override (from CLI flag) wins
    if path_override:
        logger.info("path override: %s", path_override)
        if path_override == "url_source":
            assert override_url is not None  # validated above
            reporter.phase("routing", "--url_source override")
            return await _dispatch_url_source(
                override_url,
                override_remainder or "",
                client,
                tools,
                config,
                reporter,
                writer=writer,
                run_id=run_id,
            )
        reporter.phase(path_override, f"--{path_override} override")
        classified = ClassifiedQuery(
            path=QueryPlan(path_override),
            rationale=f"explicit --{path_override} override",
            search_hint=query,
        )
        return await _dispatch_classified(
            classified,
            query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )

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
        return await _dispatch_classified(
            classified,
            query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )

    # Step 3 — URL detection routes to url_source
    detected_url = extract_first_url(query)
    if detected_url and config.url_source.enabled:
        remainder = strip_url_from_query(query, detected_url)
        logger.info("URL detected: %s (remainder: %r)", detected_url, remainder)
        reporter.phase("url_source", f"auto-detected URL: {detected_url[:80]}")
        return await _dispatch_url_source(
            detected_url,
            remainder,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )

    # Step 4 — classifier (or default to deep if disabled)
    if not config.agent.classifier.enabled:
        logger.info("classifier disabled; defaulting to deep.")
        reporter.phase("deep", "classifier disabled; defaulting to deep")
        classified = ClassifiedQuery(
            path=QueryPlan.deep,
            rationale="classifier disabled by config",
            search_hint=query,
        )
    else:
        reporter.phase("routing", "classifier LLM call")
        from deep_research.paths import classify_query

        classified = await classify_query(query, client, config.llm.text_model)
        logger.info(
            "classifier returned path=%s rationale=%r", classified.path, classified.rationale
        )
        reporter.phase(
            classified.path.value if hasattr(classified.path, "value") else str(classified.path),
            f"chosen by classifier: {classified.rationale[:80]}",
        )
    return await _dispatch_classified(
        classified,
        query,
        client,
        tools,
        config,
        reporter,
        writer=writer,
        run_id=run_id,
    )


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
        return await quick_search(
            classified,
            original_query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )
    if classified.path == QueryPlan.deep:
        return await deep_research(
            classified,
            original_query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )
    if classified.path == QueryPlan.academic:
        return await academic_research(
            classified,
            original_query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )
    if classified.path == QueryPlan.applied:
        return await applied_research(
            classified,
            original_query,
            client,
            tools,
            config,
            reporter,
            writer=writer,
            run_id=run_id,
        )
    if classified.path == QueryPlan.unclear:
        reporter.phase("clarify", "need clarification")
        return Report(
            markdown=(
                f"# Clarification needed\n\n{chr(10).join(f'- {q}' for q in classified.clarifying_questions)}"
            ),
            path="unclear",
            classifier_rationale=classified.rationale,
            clarifying_questions=list(classified.clarifying_questions),
        )
    return await deep_research(
        classified, original_query, client, tools, config, reporter, writer=writer, run_id=run_id
    )


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

    return await url_source(
        url, remainder, client, tools, config, reporter, writer=writer, run_id=run_id
    )


@asynccontextmanager
async def _build_tools(config: AgentTopConfig) -> AsyncIterator[ToolRegistry]:
    """Async context manager wrapping build_tool_registry()."""
    tools = await build_tool_registry(config)
    try:
        yield tools
    finally:
        await tools.close()


async def _archive_report(report: Report, writer: LibraryWriter | None, run_id: str) -> None:
    """Archive report in the personal digital library if writer is configured."""
    if isinstance(writer, LibraryWriter) and run_id:
        try:
            await writer.archive_report(report, run_id)
        except Exception as e:
            logger.warning("archive_report failed: %s: %s", type(e).__name__, e)
        finally:
            try:
                await writer.storage.close()
            except Exception as e:
                logger.warning("storage close failed: %s: %s", type(e).__name__, e)


__all__ = ["run_research"]
