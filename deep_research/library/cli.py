"""Library CLI — Personal Digital Library management commands.

P12(d): implemented. Commands: ls, find, show, tag, stats, prune, delete,
export-bibtex, glossary, refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend
from deep_research.library.writer import remove_artifact_files

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


async def _resolve_report(backend, run_id_prefix: str):
    """Resolve a run_id prefix to a unique report; echo ambiguity/missing."""
    reports = await backend.list_reports(limit=1000)
    matched = [r for r in reports if r.run_id.startswith(run_id_prefix)]
    if len(matched) == 0:
        typer.echo(f"No report found with run_id starting with '{run_id_prefix}'.")
        return None
    if len(matched) > 1:
        typer.echo(f"Multiple reports match prefix '{run_id_prefix}':")
        for r in matched:
            typer.echo(f"  {r.run_id[:16]}  {r.started_at[:19]}  {r.original_query[:60]}")
        typer.echo("Use a longer prefix to select a single report.")
        return None
    return matched[0]


@library_app.command("ls")
def library_ls(
    source_type: str | None = typer.Option(
        None, "--source-type", "-t", help="Filter by source type"
    ),
    limit: int = typer.Option(50, "--limit", "-L", help="Max artifacts to list"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """List artifacts in the library."""

    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        # List all artifacts (no direct list method, so we list reports + use find)
        reports = await backend.list_reports(limit=limit)
        # Batch-fetch tags for all reports' artifact_ids
        art_ids = [r.artifact_id for r in reports if r.artifact_id]
        tags_by_art = await backend.get_tags_for_artifacts(art_ids) if art_ids else {}
        for r in reports:
            started = r.started_at or "(unknown)"
            tags = tags_by_art.get(r.artifact_id, [])
            tag_str = f"  [{', '.join(t.tag for t in tags)}]" if tags else ""
            typer.echo(
                f"{started[:19]}  {r.run_id[:16]}  {r.path_taken:8s}  {r.original_query[:60]}{tag_str}"
            )
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
        try:
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
        finally:
            await backend.close()

    asyncio.run(_run())


@library_app.command("tag")
def library_tag(
    artifact_id: str | None = typer.Argument(None, help="Artifact ID"),
    tag_name: str | None = typer.Argument(None, help="Tag name"),
    remove: bool = typer.Option(False, "--remove", "-r", help="Remove tag instead of adding"),
    list_tags: bool = typer.Option(False, "--list", "-l", help="List tags for an artifact"),
    rename_old: str | None = typer.Option(None, "--rename-old", help="Old tag name to rename"),
    rename_new: str | None = typer.Option(None, "--rename-new", help="New tag name"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Add, remove, list, or rename tags."""

    async def _run():
        _cfg, backend, writer = await _get_backend_and_writer(config_path)

        if remove and artifact_id and tag_name:
            await backend.delete_tag(tag_name, artifact_id)
            typer.echo(f"Removed tag '{tag_name}' from '{artifact_id}'")

        elif list_tags and artifact_id:
            tags = await backend.get_tags_for_artifact(artifact_id)
            if tags:
                for t in tags:
                    typer.echo(f"  {t.tag}  (applied in run: {t.applied_in_run or 'manual'})")
            else:
                typer.echo(f"No tags for artifact '{artifact_id}'.")

        elif rename_old and rename_new:
            await backend.rename_tag(rename_old, rename_new)
            typer.echo(f"Renamed tag '{rename_old}' -> '{rename_new}'")

        elif artifact_id and tag_name:
            await writer.tag(artifact_id, [tag_name], run_id=None)
            typer.echo(f"Tagged '{artifact_id}' with '{tag_name}'")

        else:
            typer.echo("Usage: deep-research-library tag <artifact_id> <tag_name>")
            typer.echo("       deep-research-library tag --remove/-r <artifact_id> <tag_name>")
            typer.echo("       deep-research-library tag --list/-l <artifact_id>")
            typer.echo("       deep-research-library tag --rename-old <old> --rename-new <new>")

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


@library_app.command("delete")
def library_delete(
    run_id: str = typer.Argument(..., help="Run ID (or prefix) of the report to delete"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Delete a single report and its files from the library."""

    async def _run():
        cfg, backend, _writer = await _get_backend_and_writer(config_path)
        # Find matching report(s)
        reports = await backend.list_reports(limit=1000)
        matched = [r for r in reports if r.run_id.startswith(run_id)]
        if len(matched) == 0:
            typer.echo(f"No report found with run_id starting with '{run_id}'.")
            await backend.close()
            return
        if len(matched) > 1:
            typer.echo(f"Multiple reports match prefix '{run_id}':")
            for r in matched:
                typer.echo(f"  {r.run_id[:16]}  {r.started_at[:19]}  {r.original_query[:60]}")
            typer.echo("Use a longer prefix to select a single report.")
            await backend.close()
            return

        r = matched[0]
        typer.echo(f"Deleting report {r.run_id[:16]} ({r.original_query[:60]})...")

        # Delete files on disk
        date_str = r.completed_at or r.started_at
        root = Path(cfg.pdl.root_dir)
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                reports_dir = root / "reports"
                ymd_dir = reports_dir / f"{dt.year}" / f"{dt.month:02d}" / f"{dt.day:02d}"
                md_path = ymd_dir / f"{r.run_id}.md"
                pdf_path = ymd_dir / f"{r.run_id}.pdf"
                if md_path.exists():
                    md_path.unlink()
                    typer.echo(f"  Removed: {md_path}")
                if pdf_path.exists():
                    pdf_path.unlink()
                    typer.echo(f"  Removed: {pdf_path}")
            except Exception as e:
                typer.echo(f"  Warning: could not remove files: {e}")

        # Delete DB records
        await backend.delete_report(r.run_id)
        typer.echo("  Deleted from database.")

        # Clean up the report's own output artifact when no other report still
        # references it (delete_report deliberately keeps artifacts).
        report_artifact_id = r.artifact_id
        if report_artifact_id:
            try:
                reports = await backend.list_reports(limit=100000)
                if not any(x.artifact_id == report_artifact_id for x in reports):
                    art = await backend.get_artifact(report_artifact_id)
                    if art is not None:
                        removed = remove_artifact_files(root, art)
                        for p in removed:
                            typer.echo(f"  Removed artifact file: {p}")
                        await backend.delete_artifact(report_artifact_id)
                        typer.echo("  Deleted report artifact from database.")
            except Exception as e:
                typer.echo(f"  Warning: report artifact cleanup failed: {e}")

        await backend.close()

    asyncio.run(_run())


@library_app.command("rm-artifact")
def library_rm_artifact(
    artifact_id: str | None = typer.Argument(None, help="Artifact ID to delete"),
    arxiv_id: str | None = typer.Option(
        None, "--arxiv", help="Delete the artifact for this arXiv ID"
    ),
    url: str | None = typer.Option(None, "--url", help="Delete the artifact archived for this URL"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Delete a single artifact (PDF/HTML/image + its analyses) from the library.

    Use this to remove a misclassified document that the agent archived but that
    turned out to be off-topic. Refuses when the artifact is a report's own
    output — delete that report instead. Re-run `deep-research-library ls` to
    confirm the artifact is gone.
    """

    async def _run():
        cfg, backend, _writer = await _get_backend_and_writer(config_path)
        if artifact_id:
            art = await backend.get_artifact(artifact_id)
        elif arxiv_id:
            from deep_research.util import strip_arxiv_version

            art = await backend.find_artifact_by_arxiv_id(strip_arxiv_version(arxiv_id))
        elif url:
            art = await backend.find_artifact_by_url(url)
        else:
            typer.echo("Provide an <artifact_id>, --arxiv <id>, or --url <url>.")
            await backend.close()
            raise typer.Exit(code=1)
        if art is None:
            typer.echo("Artifact not found.")
            await backend.close()
            raise typer.Exit(code=1)

        # A report's own output artifact must not be deleted out from under it.
        reports = await backend.list_reports(limit=100000)
        owners = [r for r in reports if r.artifact_id == art.artifact_id]
        if owners:
            typer.echo(
                "Cannot delete: this artifact is the archived output of report(s) "
                + ", ".join(r.run_id[:12] for r in owners)
                + ". Delete those report(s) instead."
            )
            await backend.close()
            raise typer.Exit(code=1)

        typer.echo(
            f"Deleting artifact {art.artifact_id[:16]} ({art.title or art.source_url or art.kind})..."
        )
        root = Path(cfg.pdl.root_dir)
        removed = remove_artifact_files(root, art)
        for p in removed:
            typer.echo(f"  Removed: {p}")
        await backend.delete_artifact(art.artifact_id)
        typer.echo("  Deleted from database.")
        await backend.close()

    asyncio.run(_run())


@library_app.command("rename")
def library_rename(
    run_id: str = typer.Argument(..., help="Run ID (or prefix) of the report to rename"),
    new_name: str = typer.Argument(..., help="New name for the research"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Rename a research report (updates its display name/title)."""

    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        r = await _resolve_report(backend, run_id)
        if r is None:
            await backend.close()
            raise typer.Exit(code=1)
        await backend.rename_report(r.run_id, new_name)
        typer.echo(f"Renamed '{r.original_query[:60]}' -> '{new_name}'")
        await backend.close()

    asyncio.run(_run())


@library_app.command("merge")
def library_merge(
    run_ids: list[str] = typer.Argument(..., help="Two or more run ID prefixes to merge"),
    name: str | None = typer.Option(None, "--name", help="New name for the merged research"),
    delete_sources: bool = typer.Option(
        False, "--delete-sources", help="Delete the source reports after merging"
    ),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Merge two or more research reports into a single unified report."""

    async def _run():
        cfg, backend, writer = await _get_backend_and_writer(config_path)
        if len(run_ids) < 2:
            typer.echo("merge requires at least two run IDs.")
            await backend.close()
            raise typer.Exit(code=1)

        resolved = []
        for prefix in run_ids:
            r = await _resolve_report(backend, prefix)
            if r is None:
                await backend.close()
                raise typer.Exit(code=1)
            resolved.append(r.run_id)

        typer.echo(
            f"Merging {len(resolved)} report(s): "
            + ", ".join(f"{rid[:16]}" for rid in resolved)
            + "..."
        )
        from deep_research.library.merge import merge_reports
        from deep_research.llm.router import LLMRouter

        async with LLMRouter(cfg.llm) as router:
            new_run_id = await merge_reports(
                backend,
                writer,
                resolved,
                router,
                name=name,
                delete_sources=delete_sources,
            )
        typer.echo(f"Merged report created: {new_run_id}")
        await backend.close()

    asyncio.run(_run())


@library_app.command("add-source")
def library_add_source(
    run_id: str = typer.Argument(..., help="Run ID (or prefix) of the research"),
    url: str = typer.Argument(..., help="URL of the paper/doc/blog to attach"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Attach even when the source is off-topic for the research"
    ),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Fetch, fully analyze, and attach a new source to an existing research."""

    async def _run():
        cfg, backend, writer = await _get_backend_and_writer(config_path)
        r = await _resolve_report(backend, run_id)
        if r is None:
            await backend.close()
            raise typer.Exit(code=1)
        if not url.startswith(("http://", "https://")):
            typer.echo(f"URL must start with http(s):// (got: {url!r})")
            await backend.close()
            raise typer.Exit(code=1)

        from deep_research.library.attach import attach_source
        from deep_research.llm.router import LLMRouter

        typer.echo(f"Attaching {url} to '{r.original_query[:60]}'...")
        async with LLMRouter(cfg.llm) as router:
            result = await attach_source(
                url,
                r.run_id,
                backend,
                writer,
                cfg,
                router,
                force=force,
            )
        if result.get("status") == "skipped":
            typer.echo(f"Skipped: {result.get('reason')}")
        else:
            typer.echo(
                f"Attached. artifact_id={result.get('artifact_id', '-')} "
                f"analysis_id={result.get('analysis_id', '-')}"
            )
        await backend.close()

    asyncio.run(_run())


@library_app.command("prune")
def library_prune(
    older_than_days: int = typer.Option(
        90, "--older-than", help="Delete reports older than N days"
    ),
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
    artifact_id: str | None = typer.Option(
        None, "--artifact-id", help="Refresh a specific artifact"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be refreshed without fetching"
    ),
    re_analyze: bool = typer.Option(
        False, "--re-analyze", help="Force re-analysis even if content unchanged"
    ),
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
            scope_kind,
            scope_value,
            dry_run=dry_run,
        )
        typer.echo(
            f"Refresh job: considered={result['considered']}, "
            f"refreshed={result['refreshed']}, "
            f"errored={result['errored']}"
        )
        await backend.close()

    asyncio.run(_run())


@library_app.command("glossary")
def library_glossary(
    filter_tag: str | None = typer.Option(None, "--filter-tag", help="Filter by domain tag"),
    find: str | None = typer.Option(None, "--find", help="FTS search for a term"),
    term: str | None = typer.Option(None, "--term", help="Detail view of one term"),
    output: str | None = typer.Option(None, "--out", "-o", help="Output file path"),
    limit: int = typer.Option(50, "--limit", "-L", help="Max results for --find"),
    config_path: str = typer.Option("config.yaml", "--config", "-c", help="Config path"),
) -> None:
    """Browse the glossary. Use --find for full-text search (FTS5), --term for a single term, --filter-tag to filter by domain tag, --out to export as JSON."""

    async def _run():
        _cfg, backend, _writer = await _get_backend_and_writer(config_path)
        if find:
            entries = await backend.glossary_search(find, limit)
        else:
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

    if output:
        data = [
            {
                "term": e.term,
                "kind": e.kind,
                "short_def": e.short_def,
                "acronym_expansion": e.acronym_expansion,
                "confidence": e.confidence,
            }
            for e in entries
        ]
        Path(output).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        typer.echo(f"Wrote {len(entries)} entries to {output}")
    else:
        for e in entries:
            expansion = f"  ·  {e.acronym_expansion}" if e.acronym_expansion else ""
            typer.echo(f"{e.term}{expansion}")
            typer.echo(f"  ({e.kind}) {e.short_def or ''}")
