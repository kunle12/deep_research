"""Deep Research Agent - public async entrypoint."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from deep_research.config import AgentTopConfig, LLMRole
from deep_research.library.citation_archive import archive_cited_pdf
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.router import LLMRouter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.auto_tag import auto_tag_report
from deep_research.nodes.glossarize import extract_glossary_from_report
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import (
    Citation,
    ClassifiedQuery,
    QueryPlan,
    Report,
)
from deep_research.tools import build_tool_registry
from deep_research.tools.url_detector import extract_first_url, strip_url_from_query
from deep_research.util import strip_arxiv_version

logger = logging.getLogger(__name__)

PathOverride = Literal["quick", "deep", "academic", "url_source"]


async def run_research(
    query: str,
    config: AgentTopConfig,
    *,
    path_override: PathOverride | None = None,
    progress: ProgressReporter | None = None,
    run_id: str = "",
) -> Report:
    """Top-level public entrypoint.

    `path_override` takes precedence over both classifier and URL detection.
    `progress` — when provided, the agent calls phase/step as it moves through routing.
    """
    reporter: ProgressReporter = ensure_reporter(progress)

    # LibraryWriter setup (optional, based on config)
    writer: LibraryWriter | NullLibraryWriter | None = None
    if config.pdl.enabled:
        backend = None
        try:
            from deep_research.library.storage import get_backend

            backend = await get_backend(config)
            writer = LibraryWriter(backend, config.pdl.root_dir)
            if not run_id:
                # Auto-detect: if a checkpoint exists for this query, use its run_id
                from deep_research.checkpoint import find_checkpoint_for_query

                found = find_checkpoint_for_query(query)
                if found is not None:
                    _, meta = found
                    run_id = meta.get("run_id", "")
                    logger.info("auto-resuming from checkpoint run_id=%s", run_id)
                else:
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
        if writer and isinstance(writer, LibraryWriter):
            await writer.storage.close()
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
            if writer and isinstance(writer, LibraryWriter):
                await writer.storage.close()
            return Report(
                markdown=f"# Error\n\n`--url-source` requires a URL. Got: `{query!r}`.",
                path="unclear",
                classifier_rationale="--url-source override with non-URL query.",
            )

    try:
        # Create the `reports` row up-front so run-scoped rows (analyses,
        # citation_edges, tags, glossary) that reference `reports(run_id)`
        # satisfy their FK before research starts. `archive_report`
        # overwrites the placeholder fields at the end.
        if writer and isinstance(writer, LibraryWriter) and run_id:
            try:
                await writer.begin_report(run_id, query)
            except Exception as e:
                logger.warning("begin_report failed: %s: %s", type(e).__name__, e)

        # Single async with block for LLM router + tools — all routing happens inside
        async with LLMRouter(config.llm) as router, _build_tools(config) as tools:
            report = await _route_and_dispatch(
                query,
                path_override,
                override_url,
                override_remainder,
                router,
                tools,
                config,
                reporter,
                writer,
                run_id,
            )

            # Optional post-run pass: archive PDFs for citations that carry an
            # arXiv id (deep-path researchers cite papers without downloading
            # them, so the web UI would otherwise fall back to the upstream
            # link). Opt-in via pdl.archive_cited_arxiv_pdfs.
            if (
                config.pdl.enabled
                and config.pdl.archive_cited_arxiv_pdfs
                and isinstance(writer, LibraryWriter)
                and run_id
            ):
                try:
                    await _archive_cited_arxiv_pdfs(report, tools, writer, config, run_id)
                except Exception:
                    logger.warning("cited-arxiv-pdf archiving failed", exc_info=True)

            # P10.6: dedicated glossary extraction from report text
            if report.markdown:
                try:
                    post = router.resolve(LLMRole.POST)
                    glossary_entries = await extract_glossary_from_report(
                        report.markdown,
                        post.client,
                        post.model,
                        writer,
                        run_id,
                    )
                    report.glossary_entries = glossary_entries
                except Exception as e:
                    # Post-processing must never discard a finished report.
                    logger.warning(
                        "glossary extraction failed: %s: %s; continuing without glossary",
                        type(e).__name__,
                        e,
                    )

            artifact_id = await _archive_report(report, writer, run_id)
            if artifact_id is None and writer and isinstance(writer, LibraryWriter) and run_id:
                # archive_report failed — don't leave a placeholder report row
                # (or run-scoped rows) that would misrepresent a broken run.
                try:
                    await writer.delete_report(run_id)
                except Exception as e:
                    logger.warning("failed-archive cleanup error: %s: %s", type(e).__name__, e)

            # P10.7: auto-tag the report artifact with topic tags
            if artifact_id:
                try:
                    post = router.resolve(LLMRole.POST)
                    await auto_tag_report(
                        query,
                        report.markdown,
                        artifact_id,
                        post.client,
                        post.model,
                        writer,
                        run_id,
                    )
                except Exception as e:
                    logger.warning(
                        "auto-tagging failed: %s: %s; continuing without tags",
                        type(e).__name__,
                        e,
                    )

            reporter.complete()
    except BaseException:
        # Failed run — drop the placeholder report row (and any run-scoped
        # rows) so a broken/incomplete run leaves no trace, matching the
        # pre-fix behavior where no reports row existed on failure.
        if writer and isinstance(writer, LibraryWriter) and run_id:
            try:
                await writer.delete_report(run_id)
            except Exception as e:
                logger.warning("failed-run cleanup error: %s: %s", type(e).__name__, e)
        raise

    finally:
        if writer and isinstance(writer, LibraryWriter):
            await writer.storage.close()

    return report


async def _route_and_dispatch(
    query: str,
    path_override: PathOverride | None,
    override_url: str | None,
    override_remainder: str,
    router: LLMRouter,
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
            if override_url is None:
                return Report(
                    markdown="# Error\n\n`--url-source` requires a URL.",
                    path="unclear",
                    classifier_rationale="--url-source override without URL.",
                )
            reporter.phase("routing", "--url_source override")
            return await _dispatch_url_source(
                override_url,
                override_remainder or "",
                router,
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
            router,
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
            router,
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
            router,
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

        cls = router.resolve(LLMRole.CLASSIFIER)
        classified = await classify_query(query, cls.client, cls.model)
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
        router,
        tools,
        config,
        reporter,
        writer=writer,
        run_id=run_id,
    )


async def _dispatch_classified(
    classified: ClassifiedQuery,
    original_query: str,
    router: LLMRouter,
    tools: ToolRegistry,
    config: AgentTopConfig,
    reporter: ProgressReporter,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Run the path chosen by the classifier."""
    from deep_research.paths import academic_research, applied_research, deep_research, quick_search

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

    _DISPATCH = {
        QueryPlan.quick: quick_search,
        QueryPlan.deep: deep_research,
        QueryPlan.academic: academic_research,
        QueryPlan.applied: applied_research,
    }
    handler = _DISPATCH.get(classified.path, deep_research)
    return await handler(
        classified,
        original_query,
        router,
        tools,
        config,
        reporter,
        writer=writer,
        run_id=run_id,
    )


async def _dispatch_url_source(
    url: str,
    remainder: str,
    router: LLMRouter,
    tools: ToolRegistry,
    config: AgentTopConfig,
    reporter: ProgressReporter,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    from deep_research.paths import url_source

    return await url_source(
        url, remainder, router, tools, config, reporter, writer=writer, run_id=run_id
    )


async def _archive_cited_arxiv_pdfs(
    report: Report,
    tools: ToolRegistry,
    writer: LibraryWriter,
    config: AgentTopConfig,
    run_id: str,
) -> int:
    """Download + archive PDFs for citations that carry an arxiv_id.

    Enabled via ``pdl.archive_cited_arxiv_pdfs``. Papers already archived are
    skipped (by arxiv_id), and per-paper failures are logged and ignored so a
    single unreachable PDF never fails the run.
    """
    if (
        not report.citations
        or not config.arxiv.enabled
        or not config.arxiv.download_pdfs
        or "arxiv_download_pdf" not in tools.names()
    ):
        return 0

    sem = asyncio.Semaphore(max(1, config.arxiv.concurrency))
    seen: set[str] = set()
    archived = 0

    async def _archive_one(citation: Citation) -> None:
        nonlocal archived
        aid = (citation.arxiv_id or "").strip()
        base = strip_arxiv_version(aid)
        if not aid or aid.startswith("scholar:") or base in seen:
            return
        seen.add(base)
        async with sem:
            if await archive_cited_pdf(aid, title=citation.title, tools=tools, writer=writer):
                archived += 1

    await asyncio.gather(*[_archive_one(c) for c in report.citations])
    if archived:
        logger.info("run %s: archived %d cited arXiv PDF(s)", run_id, archived)
    return archived


@asynccontextmanager
async def _build_tools(config: AgentTopConfig) -> AsyncIterator[ToolRegistry]:
    """Async context manager wrapping build_tool_registry()."""
    tools = await build_tool_registry(config)
    try:
        yield tools
    finally:
        await tools.close()


async def _archive_report(report: Report, writer: LibraryWriter | None, run_id: str) -> str | None:
    """Archive report in the personal digital library if writer is configured.

    Returns the artifact_id of the archived report, or None if archiving failed
    or was skipped. Does NOT close storage — caller is responsible for that.
    """
    if isinstance(writer, LibraryWriter) and run_id:
        try:
            return await writer.archive_report(report, run_id)
        except Exception as e:
            logger.warning("archive_report failed: %s: %s", type(e).__name__, e)
    return None


__all__ = ["run_research"]
