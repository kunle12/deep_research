"""Attach a newly found source URL to an existing research report.

Used by the library CLI (`deep-research-library add-source`) and the web UI
("Add source" on a report detail page). Reuses the exact url_source fetch +
analyze pipeline so the new paper/document/blog gets a *full* analysis, then:

1. records the analysis against the target run (FTS-searchable results),
2. appends a rendered analysis section to the report markdown (inserted before
   the Bibliography when present),
3. merges the source's citation into the report,
4. re-archives the report in place (regenerated PDF, original metadata carried
   over, tags migrated to the new report artifact, stale files removed).

On any fetch/analysis failure the target research is left untouched.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from openai import AsyncOpenAI

from deep_research.citations import normalize_url
from deep_research.config import AgentTopConfig
from deep_research.library.storage.base import StorageBackend
from deep_research.library.writer import LibraryWriter, remove_report_files
from deep_research.nodes.analyze_source import analyze as analyze_source_node
from deep_research.paths.url_source import fetch_source
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import BLOCKED_PREFIX, Citation, Report
from deep_research.tools import build_tool_registry

logger = logging.getLogger(__name__)


def _analysis_to_dict(analysis) -> dict:
    """Map a SourceAnalysis to the fields record_analysis persists.

    record_analysis reads `key_findings` (list of strings) and JSON-serializes
    it; `limitations`/`gaps`/`follow_ups` are stored as raw TEXT, so list
    fields are serialized here to survive the DB binding.
    """
    key_findings: list[str] = []
    for c in analysis.key_claims:
        if isinstance(c, dict):
            claim = c.get("claim", "") or ""
            ev = c.get("evidence", "") or ""
            key_findings.append(f"{claim} — {ev}".strip(" —") if ev else claim)
        elif c:
            key_findings.append(str(c))
    return {
        "summary": analysis.summary,
        "key_findings": key_findings,
        "methodology": analysis.methodology,
        "limitations": json.dumps(analysis.limitations or [], ensure_ascii=False),
        "gaps": json.dumps(analysis.gaps or [], ensure_ascii=False),
        "follow_ups": json.dumps(analysis.follow_ups or [], ensure_ascii=False),
        "relevance_to_query": analysis.relevance_to_query,
    }


def _insert_section(markdown: str, section: str) -> str:
    """Insert *section* before the Bibliography heading when present, else append.

    Report views strip the Bibliography on display; appending after it would
    hide the new source section, so insert before it when it exists.
    """
    match = re.search(r"(?m)^#+ (Bibliography|References)\s*$", markdown)
    if match:
        return markdown[: match.start()] + section + "\n\n" + markdown[match.start() :]
    return markdown.rstrip() + "\n\n" + section + "\n"


def _render_added_source_section(url: str, source_type: str, analysis, query: str) -> str:
    """Build the "Added source" markdown section appended to the report.

    Reuses url_source's analysis renderer, demoted one heading level so it
    nests under an `## Added source` header in the existing report.
    """
    from deep_research.paths.url_source import _render_analysis_markdown

    body = _render_analysis_markdown(
        url, source_type, analysis, query or None
    )  # Drop the leading "# Source Analysis" heading (we supply our own).
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    inner = "\n".join(lines).strip()
    title = (analysis.title or "").strip() or url
    return f"## Added source: {title}\n\n{inner}"


def _merge_citations(original: str | None, fetched: list[Citation]) -> list[Citation]:
    """Original citations + the new source citation, deduped by URL."""
    seen: dict[str, Citation] = {}
    if original:
        try:
            raw_list = json.loads(original)
        except json.JSONDecodeError as e:
            logger.warning("skipping unparsable citations on attach: %s", e)
            raw_list = []
        for raw in raw_list:
            try:
                c = Citation(**raw)
            except Exception as e:
                # Skip the bad row rather than dropping every original citation.
                logger.warning("skipping bad citation row on attach: %s", e)
                continue
            key = normalize_url(c.url or "")
            if key:
                seen[key] = c
    for c in fetched:
        key = normalize_url(c.url or "")
        if key and key not in seen:
            seen[key] = c
    return list(seen.values())


@asynccontextmanager
async def _build_tools(config: AgentTopConfig) -> AsyncIterator:
    """Async context manager wrapping the (coroutine) registry builder."""
    tools = await build_tool_registry(config)
    try:
        yield tools
    finally:
        await tools.close()


async def attach_source(
    url: str,
    run_id: str,
    storage: StorageBackend,
    writer: LibraryWriter,
    config: AgentTopConfig,
    llm: AsyncOpenAI,
    model: str,
    progress: ProgressReporter | None = None,
) -> dict:
    """Fetch + fully analyze *url* and attach it to the research *run_id*.

    Returns a dict describing what happened:
      {"status": "attached", "artifact_id": ..., "analysis_id": ...}
      {"status": "skipped", "reason": ...}   # URL already attached
    Raises ValueError when the target report is missing or the URL cannot be
    fetched/analyzed (the research is left untouched in that case).
    """
    reporter: ProgressReporter = ensure_reporter(progress)
    report = await storage.get_report(run_id)
    if report is None:
        raise ValueError(f"research report not found: {run_id}")

    async with _build_tools(config) as tools:
        reporter.phase("attach.fetch", f"{url[:80]}")
        fetched = await fetch_source(url, tools, config, writer=writer, run_id=run_id)
        reporter.step(
            "attach.fetch",
            f"type={fetched.url_type.value} chars={len(fetched.content_text)} "
            f"vision_pages={len(fetched.page_image_data_urls)}",
        )

        reason = fetched.fetch_error or fetched.content_text or ""
        if (
            fetched.fetch_error
            or not fetched.content_text
            or fetched.content_text.startswith((BLOCKED_PREFIX, "HTTP", "("))
        ):
            raise ValueError(f"could not fetch {url}: {reason[:300]}")

        # Duplicate guard: same URL already analyzed for this run.
        if fetched.artifact_id:
            existing = await storage.get_analyses_for_artifact(fetched.artifact_id)
            if any(a.run_id == run_id and a.analyzer == "analyze_source" for a in existing):
                return {
                    "status": "skipped",
                    "reason": f"{url} is already attached to this research",
                }

        reporter.phase(
            "attach.analyze",
            f"{fetched.url_type.value}; vision_pages={len(fetched.page_image_data_urls)}",
        )
        analysis = await analyze_source_node(
            url=url,
            source_type=fetched.url_type.value,
            content=fetched.content_text,
            user_query=report.original_query or "",
            client=llm,
            model=config.llm.vision_model if fetched.page_image_data_urls else model,
            page_image_data_urls=fetched.page_image_data_urls or None,
        )

    # 1. Record the analysis against the target run.
    analysis_id = ""
    if fetched.artifact_id:
        try:
            analysis_id = await writer.record_analysis(
                fetched.artifact_id,
                _analysis_to_dict(analysis),
                run_id,
                "analyze_source",
            )
        except Exception as e:
            logger.warning("record_analysis failed on attach: %s", e)
            analysis_id = ""

    # 2. Build the section + updated markdown + citations.
    section = _render_added_source_section(
        url, fetched.url_type.value, analysis, report.original_query or ""
    )
    new_markdown = _insert_section(report.markdown or "", section)
    merged_citations = _merge_citations(report.citations_json, fetched.citations)

    # 3. Re-archive in place, carrying over original metadata (the upsert
    #    overwrites path_taken/iterations/classifier_rationale/config_snapshot/
    #    original_query — started_at is insert-only so it survives).
    old_artifact_id = report.artifact_id
    old_date = report.completed_at or report.started_at
    # Remove the OLD on-disk report files BEFORE re-archiving: archive_report
    # writes the new {run_id}.md/.pdf under TODAY's date dir, and when the
    # report was completed today, old_date == today, so a post-archive removal
    # would delete the brand-new files (leaving the artifact pointing at a
    # missing file). Removing first always leaves the new files intact.
    remove_report_files(writer.root_dir, run_id, old_date)
    config_snapshot = None
    if report.config_snapshot:
        try:
            config_snapshot = json.loads(report.config_snapshot)
        except json.JSONDecodeError:
            config_snapshot = None
    new_artifact_id = await writer.archive_report(
        Report(
            markdown=new_markdown,
            citations=merged_citations,
            path=report.path_taken,
            classifier_rationale=report.classifier_rationale or "",
            iterations=report.iterations or 0,
            created_at=(
                datetime.fromisoformat(report.started_at)
                if report.started_at
                else datetime.now(UTC)
            ),
            query=report.original_query,
        ),
        run_id,
        config_snapshot=config_snapshot,
    )

    # 4. Migrate tags from the old report artifact to the new one (tags hang
    #    off reports.artifact_id; re-archive created a new content sha), then
    #    drop the orphaned old artifact.
    if new_artifact_id and old_artifact_id and new_artifact_id != old_artifact_id:
        try:
            old_tags = await storage.get_tags_for_artifact(old_artifact_id)
            if old_tags:
                await writer.tag(new_artifact_id, [t.tag for t in old_tags], run_id=run_id)
            await storage.delete_artifact(old_artifact_id)
        except Exception as e:
            logger.warning("tag migration / old artifact cleanup failed: %s", e)

    # 5. (Old on-disk files were removed before re-archive; nothing to do.)

    reporter.phase("attach.done", f"attached {url[:80]}")
    return {
        "status": "attached",
        "artifact_id": fetched.artifact_id or "",
        "analysis_id": analysis_id,
    }


__all__ = ["attach_source"]
