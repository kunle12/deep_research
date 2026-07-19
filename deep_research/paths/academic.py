"""academic path - bounded recursive citation graph traversal.

P7: implemented. The recursive-mining loop:

  1. SEED: `arxiv_search(query)` returns top-K candidate papers.
     Each becomes a depth-0 `PaperNode` and is enqueued.
  2. LOOP while queue and len(processed) < max_papers:
       - Pick a node (BFS or FIFO order), dedup via arxiv_id.
       - resolve + download_pdf + extract text (+ optionally render pages
         for VLM analysis).
       - `analyze_paper` LLM call → `PaperAnalysis`.
       - Add to `CitationGraph`; record analysis keyed by arxiv_id.
       - If `len(processed) < max_depth`: walk key_references, cap to
         `max_key_references_to_recurse` per paper, enqueue children at
         depth+1 with parent_arxiv_id pointing at us.
  3. SYNTHESIZE: writer-style LLM call over all analyses produces the
     final markdown report body. The bibliography + citation-graph
     sections are appended by the report renderer (markdown.py).

The crawler obeys:
  - `arxiv.concurrency` semaphore around arxiv_search/resolve (3s rate limit)
  - `academic.concurrency` semaphore around per-paper analysis work
  - `academic.max_papers` hard cap on total papers processed
  - `academic.max_depth` hard cap on recursion depth (depth-0 = seed, depth-1
    = direct references, depth-2 = references of references)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.analyze_paper import (
    analyze as analyze_paper_node,
)
from deep_research.nodes.analyze_paper import (
    extract_key_reference_arxiv_ids,
)
from deep_research.nodes.recall import format_recall_context
from deep_research.nodes.recall import recall as recall_run
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import (
    Citation,
    CitationGraph,
    ClassifiedQuery,
    PaperNode,
    Report,
)
from deep_research.tools.arxiv import _strip_version
from deep_research.tools.pdf_utils import parse_pdf_path, parse_rendered_pages

logger = logging.getLogger(__name__)


async def academic_research(
    classified: ClassifiedQuery,
    original_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Execute the academic recursive-mining path.

    Returns a Report populated with:
      - markdown body: synthesis over all analyses (LLM-written)
      - citation_graph: nodes + edges built during recursion
      - citations: deduped Citation objects corresponding to graph nodes,
        sorted by descending confidence. The report renderer will append
        both the markdown bibliography AND the citation-graph structure.
    """
    cfg = config.academic
    reporter: ProgressReporter = ensure_reporter(progress)
    graph = CitationGraph()
    analyses: dict[str, Any] = {}  # arxiv_id -> PaperAnalysis
    processed: set[str] = set()  # version-stripped ids we've already analyzed
    seeds_citations: list[Citation] = []

    # ---- SEED: arxiv_search ------------------------------------------------
    reporter.phase("academic.seed", f"gathering seeds (n ≤ {cfg.seed_count})")
    seeds = await _gather_seeds(classified, original_query, tools, config, seeds_citations)
    reporter.step("academic.seed", f"{len(seeds)} seed papers")

    # P10.0 optional: run parallel blog fetch and merge into citations
    blog_citations: list[Citation] = []
    if config.blog_search.enabled and "blog_search" in tools.names():
        reporter.step("academic.seed", "fetching blog context")
        try:
            blog_result = await tools.call(
                "blog_search", {"query": original_query, "max_results": 5}
            )
            if blog_result.error is None:
                blog_citations = list(blog_result.citations)
                reporter.step("academic.seed", f"{len(blog_citations)} blog citations")
        except Exception:
            logger.info("blog_search in academic path failed; skipping")

    # Enqueue seed nodes at depth 0 (parent_arxiv_id=None, rationale="")
    queue_white: list[tuple[PaperNode, int, str | None]] = [
        (node, 0, None) for node in seeds
    ]
    # Use a deque via list for FIFO; pop(0) is O(n) but n<=15 so fine.

    # ---- per-paper concurrency ---------------------------------------------
    sem = asyncio.Semaphore(cfg.concurrency)

    async def _analyze_and_recurse(node: PaperNode, depth: int, parent: str | None) -> None:
        async with sem:
            nonlocal queue_white, processed
            base = _strip_version(node.arxiv_id)
            if base in processed:
                logger.debug("arxiv_id %s already processed; skipping", base)
                return

            # Claim slot BEFORE analysis to avoid TOCTOU race on max_papers.
            # Under semaphore, the check+add is atomic (GIL protects dict ops).
            processed.add(base)
            if len(processed) > cfg.max_papers:
                logger.info("max_papers=%d reached; skipping enqueued %s", cfg.max_papers, base)
                return

            # node already added to graph by _gather_seeds OR by the enqueuer
            graph.add_node(node)

            reporter.step(
                "academic.analyze", f"depth={depth} arxiv={base} parent={parent or '-'}"
            )

            # Detect scholar-only synthetic nodes — abstract-only path
            is_scholar_only = node.arxiv_id.startswith("scholar:")

            if is_scholar_only:
                # If scholar hit has a free PDF side-link, fetch it like a PDF
                if node.pdf_url and "fetch_page" in tools.names():
                    fetch_result = await tools.call("fetch_page", {"url": node.pdf_url})
                    if fetch_result.error is None:
                        paper_text = fetch_result.content or node.abstract or ""
                        pdf_path: str | None = None
                        page_urls: list[str] = []
                        text_source: Literal["pdf", "abstract", "html"] = "html"
                    else:
                        paper_text = node.abstract or ""
                        pdf_path = None
                        page_urls = []
                        text_source = "abstract"
                else:
                    paper_text = node.abstract or ""
                    pdf_path = None
                    page_urls = []
                    text_source = "abstract"
            else:
                paper_text, pdf_path = await _fetch_paper_text(node.arxiv_id, tools)
                page_urls = []
                if config.pdf_vision.enabled and "pdf_render_pages" in tools.names():
                    page_urls = await _render_paper_pages(node.arxiv_id, tools, max_pages=10)
                text_source = "pdf"

            # Skip LLM analysis if paper_text is empty (e.g., download failed)
            if not paper_text.strip():
                logger.warning("arxiv=%s has no extractable text; skipping analysis", base)
                graph.analyses[base] = None
                analyses[base] = None
                return

            analysis = await analyze_paper_node(
                arxiv_id=node.arxiv_id,
                paper_text=paper_text,
                query=original_query,
                client=client,
                model=config.llm.text_model,
                page_image_data_urls=page_urls or None,
                text_source=text_source,
            )
            graph.analyses[base] = analysis
            analyses[base] = analysis

            # P10.5a: archive PDF + record analysis + citation edges in library
            if isinstance(writer, LibraryWriter) and run_id:
                if pdf_path and not is_scholar_only:
                    await writer.archive_pdf(
                        Path(pdf_path),
                        arxiv_id=base,
                        source_url=f"https://arxiv.org/abs/{node.arxiv_id}",
                        title=node.title or analysis.title or None,
                        source_type="arxiv",
                    )
                # Find artifact — skip for scholar synthetic IDs (no upstream to refresh).
                artifact = None
                if not is_scholar_only:
                    try:
                        artifact = await writer.storage.find_artifact_by_arxiv_id(base)
                    except Exception:
                        artifact = None
                if artifact:
                    await writer.record_analysis(artifact.artifact_id, analysis, run_id, "analyze_paper")
                    for ref in analysis.key_references:
                        if ref.arxiv_id:
                            await writer.record_citation_edge(
                                artifact.artifact_id, ref.arxiv_id,
                                weight=0.5, run_id=run_id,
                                rationale=f"key reference in {base}",
                            )
            logger.info(
                "analyzed arxiv=%s depth=%d title=%r refs=%d",
                base, depth, (analysis.title or "")[:60], len(analysis.key_references),
            )
            reporter.step(
                "academic.analyzed",
                f"{base} refs={len(analysis.key_references)} depth={depth}",
            )

            # Optionally enqueue children
            if depth < cfg.max_depth and len(processed) < cfg.max_papers:
                child_ids = extract_key_reference_arxiv_ids(
                    analysis, threshold=cfg.key_reference_threshold
                )[: cfg.max_key_references_to_recurse]
                # Enqueue newly-discovered child arxiv_ids (visible in next batch)
                new_kids: list[str] = []
                for child_id in child_ids:
                    child_base = _strip_version(child_id)
                    if child_base in processed or child_base in {n.arxiv_id for n in graph.nodes.values()}:
                        continue
                    child_node = PaperNode(
                        arxiv_id=child_id,
                        title="",  # unknown until resolved — analyze_paper will populate
                        depth=depth + 1,
                        parent_arxiv_id=base,
                        rationale=f"referenced by {base}",
                    )
                    graph.add_node(child_node)
                    graph.add_edge(base, child_id)
                    queue_white.append((child_node, depth + 1, base))
                    new_kids.append(child_base)
                if new_kids:
                    reporter.step("academic.enqueue", f"+{len(new_kids)} kids (depth {depth + 1})")

    # ---- LOOP --------------------------------------------------------------
    iterations = 0
    while queue_white and len(processed) < cfg.max_papers:
        batch_size = min(cfg.concurrency, len(queue_white), cfg.max_papers - len(processed))
        batch: list[tuple[PaperNode, int, str | None]] = []
        for _ in range(batch_size):
            if queue_white:
                batch.append(queue_white.pop(0))
        if not batch:
            break
        reporter.phase(
            "academic.batch",
            f"batch {iterations + 1}: {len(batch)} paper(s); "
            f"processed={len(processed)}/{cfg.max_papers}",
        )
        await asyncio.gather(
            *[_analyze_and_recurse(node, depth, parent) for (node, depth, parent) in batch],
            return_exceptions=True,
        )
        iterations += 1
        # Don't grow past max_papers even if children were enqueued during the batch
        if len(processed) >= cfg.max_papers:
            logger.info("max_papers cap reached after iteration %d", iterations)
            break

    # ---- SYNTHESIZE --------------------------------------------------------
    reporter.phase("academic.synthesize", f"{len(analyses)} analyses")
    final_md = await _synthesize_markdown(
        original_query, analyses, client, config.llm.text_model, blog_citations, writer, run_id
    )

    # Collect citations from PaperNodes (use what we resolved; metadata is
    # sparse for un-resolved child refs but the URL is still valid).
    citations: list[Citation] = []
    for aid, node in graph.nodes.items():
        a = analyses.get(aid)
        node_url = node.url or (f"https://arxiv.org/abs/{aid}" if not aid.startswith("scholar:") else aid)
        source_type: Literal["arxiv", "scholar"] = "scholar" if aid.startswith("scholar:") else "arxiv"
        if a is not None:
            citations.append(
                Citation(
                    url=node_url,
                    title=a.title or node.title,
                    snippet=(a.summary or "")[:300],
                    source_type=source_type,
                    arxiv_id=aid,
                    authors=node.authors,
                    confidence_score=0.8,
                    discovered_by=None,
                )
            )
        else:
            citations.append(
                Citation(
                    url=node_url,
                    title=node.title,
                    snippet=node.rationale,
                    source_type=source_type,
                    arxiv_id=aid,
                    authors=node.authors,
                    confidence_score=0.5,
                )
            )
    # Dedup by url + keep highest confidence (avoid double seeds)
    seen: dict[str, Citation] = {}
    for c in citations + seeds_citations + blog_citations:
        existing = seen.get(c.url)
        if existing is None or existing.confidence_score < c.confidence_score:
            seen[c.url] = c
    citations = sorted(seen.values(), key=lambda c: c.confidence_score, reverse=True)

    reporter.phase("academic.done", f"{len(analyses)} papers; {len(citations)} citations")
    from datetime import UTC, datetime
    return Report(
        markdown=final_md,
        citations=citations,
        path="academic",
        citation_graph=graph,
        classifier_rationale=classified.rationale,
        iterations=len(processed),
        created_at=datetime.now(UTC),
        query=original_query,
    )


# ---------------------------------------------------------------------------
# Seed gathering: pull top-N arxiv results as initial nodes
# ---------------------------------------------------------------------------


async def _gather_seeds(
    classified: ClassifiedQuery,
    original_query: str,
    tools: ToolRegistry,
    config: AgentTopConfig,
    seeds_citations: list[Citation],  # mutated, fills bibliography-style citations
) -> list[PaperNode]:
    """Run arxiv_search and optionally scholar_search in parallel.

    When `cfg.seed_backends` includes `"scholar"` and the scholar tool is
    registered, both backends fire concurrently. Scholar hits that carry an
    arxiv_id (detected via DOI or URL) are deduped against arxiv seeds.
    Scholar-only hits get a synthetic id `scholar:<url-hash>`.
    """
    cfg = config.academic
    seed_count = cfg.seed_count
    search_query = classified.search_hint or original_query
    if not search_query.strip():
        return []

    backends = cfg.seed_backends  # e.g. ["arxiv"] or ["arxiv", "scholar"]
    has_arxiv = "arxiv_search" in tools.names()
    has_scholar = "scholar_search" in tools.names() and config.scholar.enabled

    nodes: list[PaperNode] = []
    seeds_citations.clear()

    # Dispatch parallel seed gathering
    async def _arxiv():
        if "arxiv" not in backends or not has_arxiv:
            return ([], [])
        results = await tools.call(
            "arxiv_search", {"query": search_query, "max_results": seed_count}
        )
        if results.error is not None:
            logger.warning("arxiv_search failed: %s", results.error)
            return ([], [])
        out_nodes: list[PaperNode] = []
        out_cits: list[Citation] = []
        for c in results.citations:
            if not c.arxiv_id:
                continue
            out_nodes.append(PaperNode(
                arxiv_id=c.arxiv_id,
                title=c.title or c.arxiv_id,
                authors=list(c.authors),
                abstract=c.snippet or "",
                depth=0,
                rationale="arxiv search hit",
            ))
            out_cits.append(c)
        return (out_nodes, out_cits)

    import hashlib

    async def _scholar():
        if "scholar" not in backends or not has_scholar:
            return ([], [])
        # Cost guardrail: skip Scholar when arxiv seeds already >= threshold
        if config.scholar.skip_if_arxiv_hits_ge is not None:
            arxiv_count = len(arxiv_cits) if arxiv_cits else 0
            if arxiv_count >= config.scholar.skip_if_arxiv_hits_ge:
                logger.info(
                    "scholar skip: arxiv seeds=%d >= skip_if_arxiv_hits_ge=%d",
                    arxiv_count, config.scholar.skip_if_arxiv_hits_ge,
                )
                return ([], [])
        results = await tools.call(
            "scholar_search", {"query": search_query, "max_results": seed_count}
        )
        if results.error is not None:
            logger.warning("scholar_search failed: %s", results.error)
            return ([], [])
        out_nodes: list[PaperNode] = []
        out_cits: list[Citation] = []
        for c in results.citations:
            if c.arxiv_id:
                # Will be deduped later against arxiv nodes — still track
                # the citation so it appears in the bibliography.
                out_cits.append(c)
                out_nodes.append(PaperNode(
                    arxiv_id=c.arxiv_id,
                    title=c.title or c.arxiv_id,
                    authors=list(c.authors),
                    abstract=c.snippet or "",
                    depth=0,
                    url=c.url,
                    doi=c.doi,
                    pdf_url=c.pdf_url,
                    venue=c.venue,
                    year=c.year,
                    rationale="scholar search hit (arxiv overlap)",
                ))
            else:
                # Scholar-only hit — synthetic id
                synthetic = "scholar:" + hashlib.sha256(c.url.encode()).hexdigest()[:12]
                out_nodes.append(PaperNode(
                    arxiv_id=synthetic,
                    title=c.title or synthetic,
                    authors=list(c.authors),
                    abstract=c.snippet or "",
                    depth=0,
                    url=c.url,
                    doi=c.doi,
                    pdf_url=c.pdf_url,
                    venue=c.venue,
                    year=c.year,
                    rationale="scholar search hit",
                ))
                out_cits.append(c)
        return (out_nodes, out_cits)

    # Run backends: parallel when cost guardrail is off, sequential when on
    # (sequential needed because _scholar reads arxiv_cits for guardrail check)
    if config.scholar.skip_if_arxiv_hits_ge is not None:
        arxiv_nodes, arxiv_cits = await _arxiv()
        scholar_nodes, scholar_cits = await _scholar()
    else:
        (arxiv_nodes, arxiv_cits), (scholar_nodes, scholar_cits) = await asyncio.gather(
            _arxiv(), _scholar()
        )
    nodes = list(arxiv_nodes)
    seeds_citations.extend(arxiv_cits)

    # Dedup scholar nodes that overlap arxiv by version-stripped arxiv_id
    arxiv_ids = {_strip_version(n.arxiv_id) for n in arxiv_nodes}
    for sn in scholar_nodes:
        base = _strip_version(sn.arxiv_id)
        if base not in arxiv_ids:
            nodes.append(sn)
            arxiv_ids.add(base)  # prevent further duplicates within scholar batch
    # Dedup scholar citations against existing seeds by URL
    seen_urls = {sc.url for sc in seeds_citations}
    for c in scholar_cits:
        if c.url not in seen_urls:
            seeds_citations.append(c)
            seen_urls.add(c.url)

    logger.info("_gather_seeds: %d arxiv + %d scholar → %d deduped seeds",
                len(arxiv_nodes), len(scholar_nodes), len(nodes))
    return nodes


# ---------------------------------------------------------------------------
# Per-paper fetch: resolve metadata + download + extract text
# ---------------------------------------------------------------------------


async def _fetch_paper_text(arxiv_id: str, tools: ToolRegistry) -> tuple[str, str | None]:
    """Download + extract text. Returns (text, pdf_path_or_None) on success,
    or (metadata-content string, None) on failure."""
    if "arxiv_download_pdf" not in tools.names() or "pdf_extract_text" not in tools.names():
        # Fall back to arxiv_resolve metadata if that's all we have
        if "arxiv_resolve" in tools.names():
            resolved = await tools.call("arxiv_resolve", {"arxiv_id": arxiv_id})
            return (resolved.content or "", None)
        return ("", None)
    dl = await tools.call("arxiv_download_pdf", {"arxiv_id": arxiv_id})
    if dl.error is not None:
        logger.info("arxiv_download_pdf failed for %s: %s; trying metadata", arxiv_id, dl.error)
        if "arxiv_resolve" in tools.names():
            resolved = await tools.call("arxiv_resolve", {"arxiv_id": arxiv_id})
            return (resolved.content or "", None)
        return ("", None)
    pdf_path = parse_pdf_path(dl.content)
    if pdf_path is None:
        logger.warning("arxiv_download_pdf returned unexpected content for %s: %r", arxiv_id, (dl.content or "")[:100])
        return (dl.content or "", None)
    extracted = await tools.call("pdf_extract_text", {"file_path": pdf_path})
    return (extracted.content or "", pdf_path)


async def _render_paper_pages(arxiv_id: str, tools: ToolRegistry, max_pages: int = 10) -> list[str]:
    """Download + render. Returns [] on any failure (the analysis falls back
    to text-only mode)."""
    if "arxiv_download_pdf" not in tools.names() or "pdf_render_pages" not in tools.names():
        return []
    dl = await tools.call("arxiv_download_pdf", {"arxiv_id": arxiv_id})
    if dl.error is not None:
        return []
    pdf_path = parse_pdf_path(dl.content)
    if pdf_path is None:
        return []
    render = await tools.call(
        "pdf_render_pages", {"file_path": pdf_path, "max_pages": max_pages}
    )
    return parse_rendered_pages(render)


# ---------------------------------------------------------------------------
# Writer-style synthesis across all analyses
# ---------------------------------------------------------------------------


async def _synthesize_markdown(
    original_query: str,
    analyses: dict[str, Any],
    client: AsyncOpenAI,
    model: str,
    blog_citations: list | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> str:
    """Run a single LLM synthesis call over all paper analyses.

    Falls back to a deterministic markdown rendering when the LLM is unreachable.
    """
    if not analyses:
        return (
            "# Academic Research Report\n\n"
            "No arxiv papers were successfully analyzed. Re-check the arxiv tool "
            "registration and your network/POPPLER setup.\n"
        )

    # Build a condensed digest of each analysis for the prompt.
    # Skip entries where analysis is None (paper had no extractable text).
    digest_lines: list[str] = []
    for i, (aid, a) in enumerate(analyses.items(), start=1):
        if a is None:
            continue
        digest_lines.append(
            f"### Paper {i}: arxiv:{aid} — {a.title}\n"
            f"Summary: {a.summary}\n"
            f"Key findings: {'; '.join(a.key_findings) if a.key_findings else 'N/A'}\n"
            f"Methodology: {a.methodology or 'N/A'}\n"
            f"Limitations: {'; '.join(a.limitations) if a.limitations else 'N/A'}\n"
        )
    digest = "\n\n".join(digest_lines)
    if not digest.strip():
        return (
            "# Academic Research Report\n\n"
            "No arxiv papers had extractable text. Re-check the arxiv tool "
            "registration and your network/POPPLER setup.\n"
        )

    # P10.0: inject blog context when available
    blog_section = ""
    if blog_citations:
        blog_lines: list[str] = []
        for c in blog_citations:
            blog_lines.append(f"- [{c.title}]({c.url}): {c.snippet[:200]}")
        blog_section = f"\n\n# Blog context\n{chr(10).join(blog_lines)}"

    system_msg = (
        "You are an academic synthesis writer. Given a set of analyses of "
        "discovered academic papers (with recursively-mined citations), write "
        "a 2-4 section markdown report answering the user's research query. "
        "Cite each paper inline using the bare-URL form "
        "([arxiv:ID](https://arxiv.org/abs/ID))."
    )
    # P10.6 glossary augmentation
    glossary_prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "glossary_extract.txt"
    if glossary_prompt_path.exists():
        glossary_text = glossary_prompt_path.read_text().strip()
        if glossary_text:
            system_msg += "\n\n" + glossary_text

    # P13: inject prior context from library recall
    prior_section = ""
    if writer is not None:
        try:
            prior_entries = await recall_run(original_query, writer.storage, max_results=3)
            if prior_entries:
                prior_section = "\n\n" + format_recall_context(prior_entries)
        except Exception:
            pass

    messages = [
        {"role": "system", "content": system_msg},
        {
            "role": "user",
            "content": f"# Research query\n{original_query}\n\n# Paper analyses digest\n{digest}{blog_section}{prior_section}",
        },
    ]
    try:
        resp = await client.chat.completions.create(
            model=model, messages=messages, temperature=0.0
        )
        md = (resp.choices[0].message.content or "").strip() or _fallback_synthesis(original_query, analyses)

        # P10.6 glossary extraction
        from deep_research.nodes.glossarize import extract_and_save_glossary
        await extract_and_save_glossary(md, run_id, writer)

        return md
    except Exception as e:
        logger.warning("academic synthesis LLM call failed: %s: %s; using fallback", type(e).__name__, e)
        return _fallback_synthesis(original_query, analyses)


def _fallback_synthesis(original_query: str, analyses: dict[str, Any]) -> str:
    """Deterministic markdown synthesis when the LLM call is unavailable."""
    lines: list[str] = [
        "# Academic Research Report\n",
        f"**Query:** {original_query}\n",
        f"**Papers analyzed:** {len(analyses)}\n",
    ]
    for aid, a in analyses.items():
        if a is None:
            lines.append(f"\n## {aid} (no extractable text)\n")
            continue
        lines.append(f"\n## {a.title or aid}\n")
        lines.append(f"_(arxiv:[{aid}](https://arxiv.org/abs/{aid}))_\n")
        if a.summary:
            lines.append(a.summary + "\n")
        if a.key_findings:
            lines.append("\n**Key findings:**\n")
            for f in a.key_findings:
                lines.append(f"- {f}")
        if a.methodology:
            lines.append(f"\n**Methodology:** {a.methodology}\n")
        if a.limitations:
            lines.append("**Limitations:**\n")
            for lim in a.limitations:
                lines.append(f"- {lim}")
    return "\n".join(lines)


__all__ = ["academic_research"]
