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
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from deep_research.config import AgentTopConfig, LLMRole
from deep_research.library.render_archive import archive_html_source
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.router import LLMClientLike, LLMRouter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.analyze_paper import (
    analyze as analyze_paper_node,
)
from deep_research.nodes.analyze_paper import (
    extract_key_reference_arxiv_ids,
)
from deep_research.nodes.paper_analysis import (
    download_pdf_once,
    extract_text,
    fetch_paper_text_fallback,
    render_pages,
)
from deep_research.nodes.recall import format_recall_context
from deep_research.nodes.recall import recall as recall_run
from deep_research.nodes.seed_relevance import filter_relevant_seeds
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import (
    Citation,
    CitationGraph,
    ClassifiedQuery,
    PaperNode,
    Report,
)
from deep_research.tools.arxiv import _strip_version

logger = logging.getLogger(__name__)


# Request boilerplate that carries no topical signal but dilutes keyword-based
# search backends (arxiv/scholar do relevance matching on the raw query string).
# Matched as whole phrases, case-insensitively, longest-first.
_FILLER_PHRASES: list[str] = sorted(
    [
        "i need",
        "i want",
        "i would like",
        "i'd like",
        "please",
        "can you",
        "could you",
        "give me",
        "show me",
        "find me",
        "provide me",
        "generate",
        "a comprehensive",
        "comprehensive",
        "a detailed",
        "detailed",
        "an in-depth",
        "in-depth",
        "thorough",
        "extensive",
        "deep dive into",
        "literature survey and reviews of",
        "literature review of",
        "literature survey of",
        "literature review on",
        "literature survey on",
        "a literature review",
        "a literature survey",
        "and reviews of",
        "survey of",
        "surveys of",
        "review of",
        "reviews of",
        "review on",
        "survey on",
        "overview of",
        "summary of",
        "synthesis of",
        "the state of the art",
        "state of the art",
        "the state of art",
        "state of art",
        "state-of-the-art",
        "about",
        "regarding",
        "concerning",
        "on the topic of",
        "in the field of",
        "from the beginning",
        "from scratch",
        "to learn",
        "i want to learn",
        "up to date",
        "to date",
        "as of now",
        "as of today",
        "today is",
        "currently",
        "recent",
        "latest",
        "today",
    ],
    key=len,
    reverse=True,
)

# Date / time-range phrases: "up to July 2026", bare years, and full numeric
# dates (29/09/2026, 29-09-26). The numeric branch requires THREE components so
# it does not eat version numbers or ranges like "3.5", "0.7", "10-20".
_DATE_RX = re.compile(
    r"\b(?:up to|until|as of|before|after|since|from|to)\s+"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s*\d{4}\b"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b",
    re.IGNORECASE,
)


def _clean_search_query(query: str) -> str:
    """Strip request boilerplate and date ranges from a natural-language query
    so keyword search backends match on topical terms.

    Conservative: if cleaning leaves fewer than two words, fall back to the
    original query (dates removed) rather than risk over-stripping signal.
    """
    text = _DATE_RX.sub(" ", query)
    for phrase in _FILLER_PHRASES:  # longest-first
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)
    # Keep dots and hyphens so technical tokens survive (GPT-3.5, v2.0,
    # state-of-the-art); other punctuation becomes a separator.
    text = re.sub(r"[^\w\s\-.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Drop sentence punctuation stranded at the edges (e.g. a trailing period
    # left behind after a date range was removed).
    text = re.sub(r"^[.,;:]+|[.,;:]+$", "", text).strip()
    if len(text.split()) < 2:
        fallback = re.sub(r"\s+", " ", _DATE_RX.sub(" ", query)).strip()
        return fallback or query
    return text


async def academic_research(
    classified: ClassifiedQuery,
    original_query: str,
    router: LLMRouter,
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
    excluded: set[str] = set()  # ids gated out as off-topic (kept out of report)
    processed_count: int = 0  # atomic counter for max_papers cap
    seeds_citations: list[Citation] = []

    # ---- SEED: arxiv_search ------------------------------------------------
    reporter.phase("academic.seed", f"gathering seeds (n ≤ {cfg.seed_count})")
    seeds = await _gather_seeds(classified, original_query, tools, config, seeds_citations)
    reporter.step("academic.seed", f"{len(seeds)} seed papers")

    # P-relevance: pre-gate seed candidates (title + abstract) before any PDF
    # download / full analysis. Stops off-topic keyword-overlap papers from
    # consuming max_papers slots, PDF/vision compute, or library storage.
    if cfg.seed_relevance_gate and seeds:
        reporter.phase("academic.seed.gate", f"filtering {len(seeds)} seeds")
        gate_llm = router.resolve(LLMRole.ANALYSIS)
        kept = await filter_relevant_seeds(
            original_query,
            seeds,
            gate_llm.client,
            gate_llm.model,
            cfg.seed_relevance_threshold,
        )
        kept_ids = {s.arxiv_id for s in kept}
        dropped = [s for s in seeds if s.arxiv_id not in kept_ids]
        if dropped:
            # Scholar-only seeds carry a synthetic `scholar:<hash>` id and their
            # citations have NO arxiv_id, so filter those by source URL; arxiv
            # seeds by (stripped) arxiv id. Otherwise a dropped off-topic paper
            # would still leak into the bibliography via seeds_citations.
            drop_bases = {_strip_version(s.arxiv_id) for s in dropped}
            drop_urls = {s.url for s in dropped if (s.arxiv_id or "").startswith("scholar:")}
            seeds = kept
            seeds_citations[:] = [
                c
                for c in seeds_citations
                if not (
                    (c.arxiv_id and _strip_version(c.arxiv_id) in drop_bases)
                    or (c.url in drop_urls)
                )
            ]
            reporter.step(
                "academic.seed.gate",
                f"dropped {len(dropped)} off-topic seed(s)",
            )

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

    # P10.0b: fetch + archive blog posts as library artifacts so academic-mode
    # web/blog sources are preserved, not just cited. Mirrors the applied-path
    # behavior (top-N posts, best-effort, never breaks the run). Gated on
    # `pdl.archive_fetched_html` and bounded by `pdl.archive_timeout_s`.
    if (
        blog_citations
        and isinstance(writer, LibraryWriter)
        and config.pdl.archive_fetched_html
        and run_id
    ):
        reporter.step("academic.seed", f"archiving up to {min(len(blog_citations), 3)} blog posts")

        async def _fetch_and_archive(c: Citation) -> None:
            if "fetch_page" not in tools.names():
                return
            res = await tools.call("fetch_page", {"url": c.url})
            if res.error is None and res.content:
                try:
                    await asyncio.wait_for(
                        archive_html_source(
                            c.url, res.content, tools=tools, config=config, writer=writer
                        ),
                        timeout=config.pdl.archive_timeout_s,
                    )
                except TimeoutError:
                    logger.debug("academic blog archive timed out for %s", c.url)
                except Exception:
                    logger.debug("academic blog archive failed for %s", c.url)

        await asyncio.gather(*[_fetch_and_archive(c) for c in blog_citations[:3]])

    # Enqueue seed nodes at depth 0 (parent_arxiv_id=None, rationale="")
    queue_white: deque[tuple[PaperNode, int, str | None]] = deque((node, 0, None) for node in seeds)

    # ---- per-paper concurrency ---------------------------------------------
    sem = asyncio.Semaphore(cfg.concurrency)
    claim_lock = asyncio.Lock()

    async def _analyze_and_recurse(node: PaperNode, depth: int, parent: str | None) -> None:
        nonlocal processed_count
        async with sem:
            base = _strip_version(node.arxiv_id)
            if base in processed:
                logger.debug("arxiv_id %s already processed; skipping", base)
                return

            # Claim slot atomically under lock to prevent TOCTOU race.
            # Re-check `base in processed` here too: the pre-check above runs
            # before the lock, so two tasks for the same paper (e.g. the same
            # reference enqueued by two parents in one batch) can both pass
            # it; without this re-check both would claim and double-analyze.
            async with claim_lock:
                if base in processed:
                    logger.debug("arxiv_id %s already claimed; skipping", base)
                    return
                if processed_count >= cfg.max_papers:
                    logger.info("max_papers=%d reached; skipping enqueued %s", cfg.max_papers, base)
                    return
                processed.add(base)
                processed_count += 1

            # node already added to graph by _gather_seeds OR by the enqueuer
            graph.add_node(node)

            reporter.step("academic.analyze", f"depth={depth} arxiv={base} parent={parent or '-'}")

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
                # Download PDF once, then extract text and optionally render
                # pages from the same local file.  Running extraction and
                # rendering concurrently means a slow render cannot delay
                # the text-analysis path.
                pdf_path = await download_pdf_once(node.arxiv_id, tools)
                if pdf_path is None:
                    # Fall back to arxiv_resolve metadata
                    paper_text, _ = await fetch_paper_text_fallback(node.arxiv_id, tools)
                    page_urls: list[str] = []
                    text_source: Literal["pdf", "abstract", "html"] = "abstract"
                else:
                    # Run text extraction and page rendering concurrently
                    text_task = asyncio.create_task(extract_text(pdf_path, tools))
                    render_task = (
                        asyncio.create_task(render_pages(pdf_path, tools, max_pages=10))
                        if config.pdf_vision.enabled and "pdf_render_pages" in tools.names()
                        else None
                    )
                    # Always await-or-cancel render_task so a text-extraction
                    # failure or an outer per-task timeout can never orphan the
                    # heavy page-rendering coroutine in the background.
                    try:
                        paper_text = await text_task
                        page_urls: list[str] = []
                        if render_task is not None:
                            try:
                                page_urls = await asyncio.wait_for(render_task, timeout=300.0)
                            except TimeoutError:
                                logger.debug("page rendering timed out; falling back to text-only")
                                page_urls = []
                    finally:
                        if render_task is not None and not render_task.done():
                            render_task.cancel()
                    text_source = "pdf"

            # Skip LLM analysis if paper_text is empty (e.g., download failed)
            if not paper_text.strip():
                logger.warning("arxiv=%s has no extractable text; skipping analysis", base)
                graph.analyses[base] = None
                analyses[base] = None
                return

            resolved = router.resolve(LLMRole.ANALYSIS, has_images=bool(page_urls))
            text_resolved = router.resolve(LLMRole.ANALYSIS)
            analysis = await analyze_paper_node(
                arxiv_id=node.arxiv_id,
                paper_text=paper_text,
                query=original_query,
                client=resolved.client,
                model=resolved.model,
                page_image_data_urls=page_urls or None,
                text_source=text_source,
                max_context_tokens=resolved.max_context_tokens,
                # The final synthesis call carries no images, so it may run on
                # the (cheaper/faster) text route even when pages were rendered.
                synthesis_client=text_resolved.client,
                synthesis_model=text_resolved.model,
                synthesis_max_context_tokens=text_resolved.max_context_tokens,
            )

            # Relevance gate: an off-topic paper (e.g. one that merely shares a
            # keyword like "adversarial" while living in another field) must not
            # pollute the synthesis digest nor seed recursive reference mining.
            # Mirrors the "no extractable text" skip path below.
            if analysis.relevance_score < cfg.key_reference_threshold:
                logger.info(
                    "arxiv=%s off-topic (relevance %.2f < %.2f); excluding from report",
                    base,
                    analysis.relevance_score,
                    cfg.key_reference_threshold,
                )
                excluded.add(base)
                graph.analyses[base] = None
                analyses[base] = None
                return

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
                    await writer.record_analysis(
                        artifact.artifact_id, analysis, run_id, "analyze_paper"
                    )
                    for ref in analysis.key_references:
                        if ref.arxiv_id:
                            await writer.record_citation_edge(
                                artifact.artifact_id,
                                ref.arxiv_id,
                                weight=0.5,
                                run_id=run_id,
                                rationale=f"key reference in {base}",
                            )
            logger.info(
                "analyzed arxiv=%s depth=%d title=%r refs=%d",
                base,
                depth,
                (analysis.title or "")[:60],
                len(analysis.key_references),
            )
            reporter.step(
                "academic.analyzed",
                f"{base} refs={len(analysis.key_references)} depth={depth}",
            )

            # Optionally enqueue children
            if depth < cfg.max_depth and processed_count < cfg.max_papers:
                child_ids = extract_key_reference_arxiv_ids(analysis)[
                    : cfg.max_key_references_to_recurse
                ]
                # Enqueue newly-discovered child arxiv_ids (visible in next batch)
                new_kids: list[str] = []
                existing_ids = {n.arxiv_id for n in graph.nodes.values()}  # cache O(1) lookups
                for child_id in child_ids:
                    child_base = _strip_version(child_id)
                    if child_base in processed or child_base in existing_ids:
                        continue
                    child_node = PaperNode(
                        arxiv_id=child_base,
                        title="",  # unknown until resolved — analyze_paper will populate
                        depth=depth + 1,
                        parent_arxiv_id=base,
                        rationale=f"referenced by {base}",
                    )
                    graph.add_node(child_node)
                    graph.add_edge(base, child_base)
                    queue_white.append((child_node, depth + 1, base))
                    new_kids.append(child_base)
                    existing_ids.add(child_base)
                if new_kids:
                    reporter.step("academic.enqueue", f"+{len(new_kids)} kids (depth {depth + 1})")

    # ---- LOOP --------------------------------------------------------------
    iterations = 0
    while queue_white and processed_count < cfg.max_papers:
        batch_size = min(cfg.concurrency, len(queue_white), cfg.max_papers - processed_count)
        batch: list[tuple[PaperNode, int, str | None]] = []
        for _ in range(batch_size):
            if queue_white:
                batch.append(queue_white.popleft())
        if not batch:
            break
        reporter.phase(
            "academic.batch",
            f"batch {iterations + 1}: {len(batch)} paper(s); "
            f"processed={len(processed)}/{cfg.max_papers}",
        )
        raw_coros = [_analyze_and_recurse(node, depth, parent) for (node, depth, parent) in batch]
        tasks = [asyncio.create_task(c) for c in raw_coros]
        # Each batch task gets its own wall-clock budget via wait_for; inner
        # tool sub-calls have a per-call timeout in ToolRegistry.call
        # (asyncio.wait_for), so a hung tool surfaces as a clean error result
        # instead of dead weight. The hard per-batch-member timeout is the
        # safety net for anything that still slips through.
        per_task_timeout_s = config.agent.researcher_timeout_s
        timed_tasks = [asyncio.wait_for(t, timeout=per_task_timeout_s) for t in tasks]
        gather_results = await asyncio.gather(*timed_tasks, return_exceptions=True)
        failed = 0
        for (node, _depth, _parent), r in zip(batch, gather_results):
            if isinstance(r, Exception):
                rtype = "timeout" if isinstance(r, TimeoutError) else type(r).__name__
                base = _strip_version(node.arxiv_id)
                logger.warning("academic task for %s raised (%s): %s", base, rtype, r)
                # Release the claimed budget slot on failure so a transient
                # error (bad PDF, flaky download, LLM failure, timeout) does
                # not permanently waste a max_papers slot. Only un-claim when
                # the analysis did NOT complete (analyses[base] unset), so a
                # paper that analyzed but failed later (e.g. archival) keeps
                # its slot. The paper stays in the graph and, if un-claimed,
                # renders as an unanalyzed low-confidence citation instead of
                # being silently dropped.
                if base in processed and base not in analyses:
                    processed.discard(base)
                    processed_count -= 1
                failed += 1
        if failed:
            logger.info("academic batch %d: %d paper(s) failed", iterations + 1, failed)
        iterations += 1
        # Don't grow past max_papers even if children were enqueued during the batch
        if processed_count >= cfg.max_papers:
            logger.info("max_papers cap reached after iteration %d", iterations)
            break

    # ---- SYNTHESIZE --------------------------------------------------------
    reporter.phase("academic.synthesize", f"{len(analyses)} analyses")
    writer_llm = router.resolve(LLMRole.WRITER)
    final_md = await _synthesize_markdown(
        original_query,
        analyses,
        writer_llm.client,
        writer_llm.model,
        blog_citations,
        writer,
        run_id,
        tools,
        config,
    )

    # Collect citations from PaperNodes (use what we resolved; metadata is
    # sparse for un-resolved child refs but the URL is still valid).
    citations: list[Citation] = []
    for aid, node in graph.nodes.items():
        if aid in excluded:
            continue  # off-topic paper gated out — keep it out of the bibliography
        a = analyses.get(aid)
        node_url = node.url or (
            f"https://arxiv.org/abs/{aid}" if not aid.startswith("scholar:") else aid
        )
        source_type: Literal["arxiv", "scholar"] = (
            "scholar" if aid.startswith("scholar:") else "arxiv"
        )
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
    raw_query = classified.search_hint or original_query
    if not raw_query.strip():
        return []
    # Strip request boilerplate / dates so arxiv & scholar match on topic terms.
    search_query = _clean_search_query(raw_query)
    if search_query != raw_query:
        logger.info("academic seed query cleaned: %r -> %r", raw_query, search_query)

    backends = cfg.seed_backends  # e.g. ["arxiv"] or ["arxiv", "scholar"]
    has_arxiv = "arxiv_search" in tools.names()
    has_scholar = "scholar_search" in tools.names() and config.scholar.enabled

    nodes: list[PaperNode] = []
    seeds_citations.clear()

    import hashlib

    async def _run_scholar(
        q: str, n: int, t: ToolRegistry, c: AgentTopConfig
    ) -> tuple[list[PaperNode], list[Citation]]:
        """Run scholar search and return (nodes, citations)."""
        results = await t.call("scholar_search", {"query": q, "max_results": n})
        if results.error is not None:
            logger.warning("scholar_search failed: %s", results.error)
            return ([], [])
        out_nodes: list[PaperNode] = []
        out_cits: list[Citation] = []
        for cit in results.citations:
            if cit.arxiv_id:
                out_cits.append(cit)
                out_nodes.append(
                    PaperNode(
                        arxiv_id=_strip_version(cit.arxiv_id),
                        title=cit.title or cit.arxiv_id,
                        authors=list(cit.authors),
                        abstract=cit.snippet or "",
                        depth=0,
                        url=cit.url,
                        doi=cit.doi,
                        pdf_url=cit.pdf_url,
                        venue=cit.venue,
                        year=cit.year,
                        rationale="scholar search hit (arxiv overlap)",
                    )
                )
            else:
                synthetic = "scholar:" + hashlib.sha256(cit.url.encode()).hexdigest()[:12]
                out_nodes.append(
                    PaperNode(
                        arxiv_id=synthetic,
                        title=cit.title or synthetic,
                        authors=list(cit.authors),
                        abstract=cit.snippet or "",
                        depth=0,
                        url=cit.url,
                        doi=cit.doi,
                        pdf_url=cit.pdf_url,
                        venue=cit.venue,
                        year=cit.year,
                        rationale="scholar search hit",
                    )
                )
                out_cits.append(cit)
        return (out_nodes, out_cits)

    # Run arxiv first (always) so scholar guardrail can check its result
    arxiv_nodes: list[PaperNode] = []
    arxiv_cits: list[Citation] = []
    if "arxiv" in backends and has_arxiv:
        results = await tools.call(
            "arxiv_search", {"query": search_query, "max_results": seed_count}
        )
        if results.error is None:
            for c in results.citations:
                if c.arxiv_id:
                    arxiv_nodes.append(
                        PaperNode(
                            arxiv_id=_strip_version(c.arxiv_id),
                            title=c.title or c.arxiv_id,
                            authors=list(c.authors),
                            abstract=c.snippet or "",
                            depth=0,
                            rationale="arxiv search hit",
                        )
                    )
                    arxiv_cits.append(c)
        else:
            logger.warning("arxiv_search failed: %s", results.error)

    scholar_nodes: list[PaperNode] = []
    scholar_cits: list[Citation] = []
    if "scholar" in backends and has_scholar:
        # Cost guardrail: skip Scholar when arxiv seeds already >= threshold
        if config.scholar.skip_if_arxiv_hits_ge is not None:
            if len(arxiv_cits) >= config.scholar.skip_if_arxiv_hits_ge:
                logger.info(
                    "scholar skip: arxiv seeds=%d >= skip_if_arxiv_hits_ge=%d",
                    len(arxiv_cits),
                    config.scholar.skip_if_arxiv_hits_ge,
                )
                # skip scholar
                pass
            else:
                scholar_nodes, scholar_cits = await _run_scholar(
                    search_query, seed_count, tools, config
                )
        else:
            scholar_nodes, scholar_cits = await _run_scholar(
                search_query, seed_count, tools, config
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

    logger.info(
        "_gather_seeds: %d arxiv + %d scholar → %d deduped seeds",
        len(arxiv_nodes),
        len(scholar_nodes),
        len(nodes),
    )
    return nodes


# ---------------------------------------------------------------------------
# Writer-style synthesis across all analyses
# ---------------------------------------------------------------------------


async def _synthesize_markdown(
    original_query: str,
    analyses: dict[str, Any],
    client: LLMClientLike,
    model: str,
    blog_citations: list | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
    tools: ToolRegistry | None = None,
    config: AgentTopConfig | None = None,
) -> str:
    """Run a single LLM synthesis call over all paper analyses.

    Falls back to a deterministic markdown rendering when the LLM is unreachable.
    """
    if not analyses:
        # No arxiv papers were gathered/analyzed. Academic mode is not limited
        # to arxiv: fall back to a report synthesized from blog/web content when
        # any was found, otherwise surface the boilerplate so the run is honest
        # about the empty arxiv result.
        if blog_citations:
            return await _synthesize_blog_only(
                original_query, blog_citations, client, model, tools, config
            )
        return (
            "# Academic Research Report\n\n"
            "No arxiv papers were successfully analyzed. Re-check the arxiv tool "
            "registration and your network/POPPLER setup.\n"
        )

    # Build a condensed digest of each analysis for the prompt.
    # Skip entries where analysis is None (paper had no extractable text).
    digest_lines: list[str] = []
    paper_no = 0
    for aid, a in analyses.items():
        if a is None:
            continue
        paper_no += 1
        digest_lines.append(
            f"### Paper {paper_no}: arxiv:{aid} — {a.title} (relevance {a.relevance_score:.2f})\n"
            f"Summary: {a.summary}\n"
            f"Key findings: {'; '.join(a.key_findings) if a.key_findings else 'N/A'}\n"
            f"Methodology: {a.methodology or 'N/A'}\n"
            f"Limitations: {'; '.join(a.limitations) if a.limitations else 'N/A'}\n"
        )
    digest = "\n\n".join(digest_lines)
    if not digest.strip():
        # Every arxiv paper lacked extractable text. Same blog/web fallback as
        # the empty-analyses case so the report is still generated from the
        # web/blog sources that were found.
        if blog_citations:
            return await _synthesize_blog_only(
                original_query, blog_citations, client, model, tools, config
            )
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
        "Begin with a `# ` title you compose yourself: a formal, descriptive "
        "headline reformulated from the research query — do NOT copy the query "
        "verbatim (it is often informal or phrased as a question). Immediately "
        "below the title, on its own line, preserve the original query as: "
        '_Original query: "<the exact query>"_. '
        "Stay strictly on the query's topic: use only the analyses that are "
        "genuinely relevant (higher `relevance` score), and DO NOT mention, "
        "summarize, or cite any paper that is off-topic or only shares a "
        "superficial keyword with the query — even if it appears in the digest. "
        "Cite each paper inline using the bare-URL form "
        "([arxiv:ID](https://arxiv.org/abs/ID))."
    )

    # P13: inject prior context from library recall
    prior_section = ""
    if isinstance(writer, LibraryWriter):
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
        resp = await client.chat.completions.create(model=model, messages=messages, temperature=0.0)
        md = (resp.choices[0].message.content or "").strip() or _fallback_synthesis(
            original_query, analyses
        )

        return md
    except Exception as e:
        logger.warning(
            "academic synthesis LLM call failed: %s: %s; using fallback", type(e).__name__, e
        )
        return _fallback_synthesis(original_query, analyses)


async def _synthesize_blog_only(
    original_query: str,
    blog_citations: list[Citation],
    client: LLMClientLike,
    model: str,
    tools: ToolRegistry | None = None,
    config: AgentTopConfig | None = None,
) -> str:
    """Synthesize an academic-mode report from blog/web citations when no
    arxiv papers were analyzable. Mirrors the applied path's blog-first
    synthesis so web/blog content still yields a report even when arxiv came
    up empty.
    """
    # Fetch page content for up to 3 posts so the writer has real material
    # rather than only search snippets.
    fetched_texts: dict[str, str] = {}
    if tools is not None and "fetch_page" in tools.names():
        for c in blog_citations[:3]:
            res = await tools.call("fetch_page", {"url": c.url})
            if res.error is None and res.content:
                fetched_texts[c.url] = res.content[:8000]

    blog_digest = "\n\n".join(
        f"### {c.title}\nURL: {c.url}\n\n{fetched_texts.get(c.url, c.snippet or '')}"
        for c in blog_citations
    )

    system_msg = (
        "You are an academic synthesis writer. No peer-reviewed arxiv papers "
        "could be analyzed for this query, so you are synthesizing from "
        "technical blog and web content instead. Write a 2-4 section markdown "
        "report answering the user's research query. Begin with a `# ` title "
        "you compose yourself: a formal, descriptive headline reformulated "
        "from the research query — do NOT copy the query verbatim (it is often "
        "informal or phrased as a question). Immediately below the title, on "
        "its own line, preserve the original query as: "
        '_Original query: "<the exact query>"_. '
        "Stay strictly on the query's topic and cite each blog post inline "
        "with a standard markdown link like [post](https://example.com/post)."
    )

    messages = [
        {"role": "system", "content": system_msg},
        {
            "role": "user",
            "content": (
                f"# Research query\n{original_query}\n\n"
                f"# Blog/web content digest\n"
                f"{blog_digest or '(no content fetched; using search snippets only)'}"
            ),
        },
    ]
    try:
        resp = await client.chat.completions.create(model=model, messages=messages, temperature=0.0)
        md = (resp.choices[0].message.content or "").strip()
        if md:
            return md
    except Exception as e:
        logger.warning("academic blog fallback synthesis failed: %s: %s", type(e).__name__, e)
    return _fallback_blog_synthesis(original_query, blog_citations)


def _fallback_blog_synthesis(original_query: str, blog_citations: list[Citation]) -> str:
    """Deterministic markdown fallback when the LLM call is unavailable."""
    lines: list[str] = [
        "# Academic Research Report\n",
        f"**Query:** {original_query}\n",
        f"**Web/blog sources found:** {len(blog_citations)}\n",
        "\n*No peer-reviewed arxiv papers could be analyzed; this report is "
        "synthesized from blog and web content.*\n",
    ]
    for i, c in enumerate(blog_citations, start=1):
        lines.append(f"\n## {i}. {c.title}\n")
        lines.append(f"URL: {c.url}\n")
        if c.snippet:
            lines.append(f"> {c.snippet}\n")
    return "\n".join(lines)


def _fallback_synthesis(original_query: str, analyses: dict[str, Any]) -> str:
    """Deterministic markdown synthesis when the LLM call is unavailable."""
    lines: list[str] = [
        "# Academic Research Report\n",
        f"**Query:** {original_query}\n",
        f"**Papers analyzed:** {sum(1 for a in analyses.values() if a is not None)}\n",
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
