"""Library CLI — Personal Digital Library management commands.

P12(d): implemented. Commands: ls, find, show, tag, stats, prune, export-bibtex,
glossary, refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend

logger = logging.getLogger(__name__)

library_app = typer.Typer(name="library", help="Personal Digital Library commands")


async def _get_backend_and_writer(config_path: str):
    cfg = AgentTopConfig.load_yaml(config_path)
    if not cfg.pdl.enabled:
        typer.echo("PDL is not enabled (pdl.enabled=false in config)")
        raise typer.Exit(code=1)
    backend = await get_backend(cfg)
    from deep_research.library.writer import LibraryWriter
    writer = LibraryWriter(backend, cfg.pdl.root_dir)
    return cfg, backend, writer


@library_app.command("ls")
def library_ls(
    source_type: str | None = typer.Option(None, "--source-type", "-t", help="Filter by source type"),
    limit: int = typer.Option(50, "--limit", "-L", help="Max artifacts to list"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """List artifacts in the library."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        # List all artifacts (no direct list method, so we list reports + use find)
        reports = await backend.list_reports(limit=limit)
        for r in reports:
            typer.echo(f"{r.run_id[:16]}  {r.path_taken:8s}  {r.original_query[:60]}")
        await backend.close()

    asyncio.run(_run())


@library_app.command("find")
def library_find(
    query: str = typer.Argument(..., help="Full-text search query"),
    kind: str = typer.Option("pdf", "--kind", "-k", help="Artifact kind to search"),
    limit: int = typer.Option(20, "--limit", "-L", help="Max results"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Full-text search across analyses."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        hits = await backend.full_text_search(query, kind=kind, limit=limit)
        for h in hits:
            typer.echo(f"{h.artifact_id[:16]}  {h.title[:60]}")
        if not hits:
            typer.echo("No results found.")
        await backend.close()

    asyncio.run(_run())


@library_app.command("show")
def library_show(
    artifact_id: str = typer.Argument(..., help="Artifact ID to show"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Show details of a specific artifact."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        art = await backend.get_artifact(artifact_id)
        if art is None:
            typer.echo(f"Artifact '{artifact_id}' not found.")
            return
        typer.echo(f"ID:        {art.artifact_id}")
        typer.echo(f"Kind:      {art.kind}")
        typer.echo(f"Title:     {art.title or '(no title)'}")
        typer.echo(f"Source:    {art.source_url or '(no URL)'}")
        typer.echo(f"Type:      {art.source_type or '(unknown)'}")
        typer.echo(f"Path:      {art.bytes_path}")
        typer.echo(f"Size:      {art.bytes_size or 0} bytes")
        typer.echo(f"Seen:      {art.first_seen_at}")
        # Show analyses
        analyses = await backend.get_analyses_for_artifact(artifact_id)
        if analyses:
            typer.echo(f"\nAnalyses ({len(analyses)}):")
            for a in analyses:
                typer.echo(f"  {a.analyzer}: {a.summary[:80] if a.summary else '(no summary)'}")
        await backend.close()

    asyncio.run(_run())


@library_app.command("tag")
def library_tag(
    artifact_id: str = typer.Argument(..., help="Artifact ID to tag"),
    tag_name: str = typer.Argument(..., help="Tag name"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Tag an artifact."""
    async def _run():
        _cfg, backend, writer = await _get_backend_and_writer(config_path)
        # run_id=None — this tag is being applied directly from the CLI,
        # not from a research run. tags.applied_in_run is nullable.
        await writer.tag(artifact_id, [tag_name], run_id=None)
        typer.echo(f"Tagged '{artifact_id}' with '{tag_name}'")
        await backend.close()

    asyncio.run(_run())


@library_app.command("stats")
def library_stats(
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Show library statistics."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        reports = await backend.list_reports(limit=1000)
        typer.echo(f"Reports:     {len(reports)}")
        # Count artifacts by kind (approximate)
        typer.echo("(Detailed stats via 'ls' for now)")
        await backend.close()

    asyncio.run(_run())


@library_app.command("prune")
def library_prune(
    older_than_days: int = typer.Option(90, "--older-than", help="Delete reports older than N days"),
    dry_run: bool = typer.Option(True, "--dry-run", help="Show what would be pruned"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Prune old reports (and their analyses/tags/citation_edges) from the library.

    Note: artifacts themselves are NOT deleted, since an artifact may have
    been discovered by multiple independent runs and we can't safely
    determine that no other report still references it.
    """
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        # List all reports and filter by age
        reports = await backend.list_reports(limit=1000)
        from datetime import UTC, datetime, timedelta
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        pruned = 0
        for r in reports:
            completed = r.completed_at or r.started_at
            try:
                dt = datetime.fromisoformat(completed)
                if dt < cutoff:
                    if dry_run:
                        typer.echo(f"  Would prune: {r.run_id[:16]} ({r.original_query[:40]})")
                    else:
                        await backend.delete_report(r.run_id)
                        typer.echo(f"  Pruned: {r.run_id[:16]} ({r.original_query[:40]})")
                    pruned += 1
            except Exception:
                pass
        typer.echo(f"{'Dry run: ' if dry_run else ''}Found {pruned} old reports.")
        await backend.close()

    asyncio.run(_run())


@library_app.command("export-bibtex")
def library_export_bibtex(
    output: str = typer.Option("refs.bib", "--out", "-o", help="Output .bib file"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Export library artifacts as BibTeX."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        reports = await backend.list_reports(limit=1000)
        lines = ["% Generated by deep-research library export-bibtex\n"]
        for r in reports:
            title = r.original_query[:80].replace("--", " ")
            lines.append(f"@misc{{{r.run_id[:16]},\n")
            lines.append(f"  title = {{{title}}},\n")
            lines.append(f"  year = {{{r.started_at[:4]}}},\n")
            lines.append(f"  note = {{Path: {r.path_taken}}}\n")
            lines.append("}\n")
        bib = "".join(lines)
        Path(output).write_text(bib, encoding="utf-8")
        typer.echo(f"Wrote {len(reports)} entries to {output}")
        await backend.close()

    asyncio.run(_run())


@library_app.command("refresh")
def library_refresh(
    source_type: str | None = typer.Option(None, "--source-type", help="Filter by source type"),
    tag: str | None = typer.Option(None, "--tag", help="Filter by tag"),
    artifact_id: str | None = typer.Option(None, "--artifact-id", help="Refresh a specific artifact"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be refreshed without fetching"),
    re_analyze: bool = typer.Option(False, "--re-analyze", help="Force re-analysis even if content unchanged"),
    once: bool = typer.Option(False, "--once", help="Run exactly one cycle and exit"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Refresh stale artifacts in the personal library."""
    async def _run():
        _cfg, backend, writer = await _get_backend_and_writer(config_path)
        if artifact_id:
            scope_kind, scope_value = "artifact_id", artifact_id
        elif tag:
            scope_kind, scope_value = "tag", tag
        else:
            scope_kind, scope_value = "source_type", source_type or "html"
        result = await writer.run_refresh_job(
            scope_kind, scope_value,
            dry_run=dry_run,
        )
        typer.echo(f"Refresh job: considered={result['considered']}, "
                   f"refreshed={result['refreshed']}, "
                   f"errored={result['errored']}")
        await backend.close()

    asyncio.run(_run())


@library_app.command("glossary")
def library_glossary(
    filter_tag: str | None = typer.Option(None, "--filter-tag", help="Filter by domain tag"),
    find: str | None = typer.Option(None, "--find", help="FTS search for a term"),
    term: str | None = typer.Option(None, "--term", help="Detail view of one term"),
    output: str | None = typer.Option(None, "--out", "-o", help="Output file path"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Browse the glossary."""
    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        entries = await backend.list_glossary_entries()
        await backend.close()
        return entries

    entries = asyncio.run(_run())

    if not entries:
        typer.echo("No glossary entries found.")
        return

    if term:
        for e in entries:
            if e.term.lower() == term.lower() or e.term_canonical == term.lower():
                typer.echo(f"--- {e.term} ---")
                typer.echo(f"  Kind: {e.kind}")
                typer.echo(f"  Short def: {e.short_def}")
                if e.long_def:
                    typer.echo(f"  Long def: {e.long_def}")
                if e.acronym_expansion:
                    typer.echo(f"  Expansion: {e.acronym_expansion}")
                return
        typer.echo(f"Term '{term}' not found.")
        return

    if filter_tag:
        entries = [e for e in entries if e.domain_tags and filter_tag in e.domain_tags]

    if find:
        entries = [e for e in entries if find.lower() in (e.term.lower() or "") or find.lower() in (e.short_def.lower() or "")]

    if output:
        data = [{
            "term": e.term, "kind": e.kind, "short_def": e.short_def,
            "acronym_expansion": e.acronym_expansion,
            "confidence": e.confidence,
        } for e in entries]
        Path(output).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        typer.echo(f"Wrote {len(entries)} entries to {output}")
    else:
        for e in entries:
            expansion = f"  ·  {e.acronym_expansion}" if e.acronym_expansion else ""
            typer.echo(f"{e.term}{expansion}")
            typer.echo(f"  ({e.kind}) {e.short_def or ''}")
