"""Merge two or more archived research reports into a single unified report.

Used by the library CLI (`deep-research-library merge`) and the web UI
(`POST /api/reports/{run_id}/merge`). Reuses the existing LibraryWriter
archival pipeline so the merged report gets the same PDF/markdown artifacts,
tags, and citation handling as any other research run.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.citations import normalize_url
from deep_research.library.storage.base import StorageBackend
from deep_research.library.writer import LibraryWriter, remove_report_files
from deep_research.state import Citation, Report

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "merge_reports.txt"
# Per-source markdown cap so two large reports fit comfortably in a 131k
# context window even with the prompt + citations appended.
_MAX_SOURCE_CHARS = 50000


def _auto_name(reports) -> str:
    """Auto-combined name when the user doesn't supply one."""
    parts = [r.original_query.strip()[:40] for r in reports if r.original_query.strip()]
    if not parts:
        return "Merged research"
    return "Merged: " + " + ".join(parts)


def _render_reports_blob(reports) -> str:
    """Render source reports for the merge LLM prompt (capped per source)."""
    sections: list[str] = []
    for i, r in enumerate(reports, start=1):
        md = (r.markdown or "").strip()
        if not md:
            md = "(empty report)"
        sections.append(
            f"=== REPORT {i} (run {r.run_id}, query: {r.original_query}) ===\n{md[:_MAX_SOURCE_CHARS]}"
        )
    return "\n\n".join(sections)


async def _synthesize_merged_markdown(
    reports,
    merged_name: str,
    llm: AsyncOpenAI,
    model: str,
) -> str:
    """One LLM call produces the unified markdown. Falls back to a
    deterministic stitch on any failure so merge never hard-fails."""
    try:
        prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
        prompt = prompt_template.replace("{merged_name}", merged_name).replace(
            "{reports}", _render_reports_blob(reports)
        )
        system = (
            "You are a research report merging assistant. Produce a single "
            "coherent Markdown research report. Do not wrap in code fences. "
            "Do NOT include a Bibliography section."
        )
        resp = await llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        md = resp.choices[0].message.content or ""
        # Defensive cleanup: strip markdown fences
        if md.startswith("```"):
            lines = md.splitlines()
            if len(lines) >= 2:
                md = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        if md.strip():
            return md
    except Exception as e:
        logger.warning("merge LLM call failed (%s: %s); using stitch fallback", type(e).__name__, e)
    return _stitch_fallback(reports, merged_name)


def _stitch_fallback(reports, merged_name: str) -> str:
    """Deterministic fallback: merged intro + each source under a sub-header."""
    parts = [f"# {merged_name}\n"]
    parts.append(f"_Merged from {len(reports)} research reports._\n")
    for i, r in enumerate(reports, start=1):
        parts.append(f"## {i}. {r.original_query.strip() or '(untitled)'}\n")
        parts.append((r.markdown or "").strip())
        parts.append("")
    return "\n".join(parts)


def _merge_citations(reports) -> list[Citation]:
    """Union of all citations, deduplicated by normalized URL."""
    seen: dict[str, Citation] = {}
    for r in reports:
        if not r.citations_json:
            continue
        try:
            raw_list = json.loads(r.citations_json)
        except json.JSONDecodeError as e:
            logger.warning("skipping unparsable citations for %s: %s", r.run_id, e)
            continue
        for raw in raw_list:
            try:
                c = Citation(**raw)
            except Exception as e:
                logger.warning("skipping bad citation row for %s: %s", r.run_id, e)
                continue
            key = normalize_url(c.url or "")
            if key and key not in seen:
                seen[key] = c
    return list(seen.values())


async def merge_reports(
    storage: StorageBackend,
    writer: LibraryWriter,
    run_ids: list[str],
    llm: AsyncOpenAI,
    model: str,
    *,
    name: str | None = None,
    delete_sources: bool = False,
) -> str:
    """Merge N reports into a new unified report.

    Returns the new run_id. With `delete_sources=True`, the source reports are
    reassigned to the merged run and then deleted (their analyses/citation
    edges/glossary references survive via reassignment). Otherwise sources are
    kept and tagged ``merged``.
    """
    distinct = list(dict.fromkeys(r for r in run_ids if r))  # preserve order, drop dupes
    if len(distinct) < 2:
        raise ValueError("merge requires at least two distinct run_ids")
    if len(distinct) > 20:
        raise ValueError("cannot merge more than 20 reports at once")

    reports = []
    for rid in distinct:
        r = await storage.get_report(rid)
        if r is None:
            raise ValueError(f"report not found: {rid}")
        reports.append(r)

    merged_name = (name or "").strip() or _auto_name(reports)
    merged_markdown = await _synthesize_merged_markdown(reports, merged_name, llm, model)

    # Provenance note (visible in the rendered report + on disk).
    provenance = ", ".join(f"`{r.run_id}` ({r.original_query})" for r in reports)
    merged_markdown = (
        f"> Merged from {len(reports)} research reports: {provenance}.\n\n" + merged_markdown
    )

    new_run_id = uuid.uuid4().hex[:16]
    report = Report(
        markdown=merged_markdown,
        citations=_merge_citations(reports),
        path="merged",
        classifier_rationale=f"Merged from runs: {', '.join(distinct)}",
        created_at=datetime.now(UTC),
        query=merged_name,
    )
    new_artifact_id = await writer.archive_report(report, new_run_id)

    # Copy the union of source tags onto the merged artifact.
    src_art_ids = [r.artifact_id for r in reports if r.artifact_id]
    if new_artifact_id and src_art_ids:
        tags_map = await storage.get_tags_for_artifacts(src_art_ids)
        union: set[str] = set()
        for aid in src_art_ids:
            union.update(t.tag for t in tags_map.get(aid, []))
        if union:
            await writer.tag(new_artifact_id, sorted(union), run_id=new_run_id)

    if delete_sources:
        await _delete_sources(storage, writer, reports, new_run_id)
    else:
        # Keep originals but mark them so the merge is discoverable.
        for r in reports:
            if r.artifact_id:
                try:
                    await writer.tag(r.artifact_id, ["merged"], run_id=None)
                except Exception as e:
                    logger.warning("tagging source %s failed: %s", r.run_id, e)

    return new_run_id


async def _delete_sources(
    storage: StorageBackend,
    writer: LibraryWriter,
    reports,
    new_run_id: str,
) -> None:
    """Reassign run-scoped rows to the merged report, then delete each source."""
    for r in reports:
        await storage.reassign_run(r.run_id, new_run_id)
    for r in reports:
        await storage.delete_report(r.run_id)
        remove_report_files(writer.root_dir, r.run_id, r.completed_at or r.started_at)
        # Best-effort cleanup of the now-unreferenced source report artifact.
        if r.artifact_id:
            try:
                await storage.delete_artifact(r.artifact_id)
            except Exception as e:
                logger.warning(
                    "could not remove orphan artifact %s for %s: %s",
                    r.artifact_id,
                    r.run_id,
                    e,
                )


__all__ = ["merge_reports"]
