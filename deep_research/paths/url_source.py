"""url_source path — analyze a single URL provided in the user's query.

P2.5: implemented.
Flow:
  1. classify_url(url) -> arxiv | pdf | html
  2. fetch the source via the appropriate tool:
     - arxiv: arxiv_resolve_id -> get metadata
              arxiv_download_pdf -> local path
              pdf_extract_text + (later: pdf_render_pages + VLM)
     - pdf:   download via httpx (FastPDF-downloader helper inside pdf tool)
              pdf_extract_text + vision
     - html:  fetch_page (httpx + trafilatura); browser fallback if low-yield
  3. analyze_source LLM call -> SourceAnalysis (summary, key_claims, follow_ups, gaps)
  4. If user query asks for critique/gaps -> fan into paths.deep with follow-ups
  5. Build the Markdown report from SourceAnalysis (+ mixed-in deep report if follow-up)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.analyze_source import analyze as analyze_source_node
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import Citation, Report
from deep_research.tools.pdf_utils import parse_pdf_path, parse_rendered_pages
from deep_research.tools.url_classifier import (
    UrlType,
    classify_url,
    extract_arxiv_id,
)

logger = logging.getLogger(__name__)


_DEFAULT_TRIGGER_PHRASES = [
    "gaps",
    "what's missing",
    "what is missing",
    "omitted",
    "not mentioned",
    "limitation",
    "limitations",
    "shortcoming",
    "shortcomings",
    "weakness",
    "weaknesses",
    "flaw",
    "flaws",
    "counterexample",
    "counterexamples",
    "refute",
    "refutation",
    "disprove",
    "verify",
    "validate",
    "falsify",
    "check the claims",
    "fact-check",
    "fact check",
    "comparison",
    "compare to",
]


def query_asks_for_follow_up(query: str, custom_phrases: list[str] | None = None) -> bool:
    """Conservative heuristic. True if the query asks for critique / gaps / verification."""
    if not query:
        return False
    q = query.lower()
    phrases = list(_DEFAULT_TRIGGER_PHRASES) + (custom_phrases or [])
    return any(p in q for p in phrases)


async def _fetch_arxiv_source(
    arxiv_id: str,
    tools: ToolRegistry,
    *,
    render_pages: bool = False,
    max_pages: int = 25,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> tuple[str, str, list[Citation], list[str]]:
    """Resolve + download + extract text of an arxiv paper.

    Returns (content_text, paper_title, citations, page_image_data_urls).
    When `render_pages=True` and `pdf_render_pages` is registered, also returns
    VLM-ready JPEG data URLs for the first `max_pages` pages.
    """
    if "arxiv_resolve" not in tools.names():
        return ("(arxiv tool not registered)", "", [], [])
    meta_res = await tools.call("arxiv_resolve", {"arxiv_id": arxiv_id})
    title = ""
    cit: Citation | None = None
    if meta_res.citations:
        cit = meta_res.citations[0]
        title = cit.title

    text = ""
    page_urls: list[str] = []
    pdf_path: str | None = None
    if "arxiv_download_pdf" in tools.names():
        dl = await tools.call("arxiv_download_pdf", {"arxiv_id": arxiv_id})
        if dl.error is None:
            pdf_path = parse_pdf_path(dl.content)
            if pdf_path and "pdf_extract_text" in tools.names():
                extract = await tools.call("pdf_extract_text", {"file_path": pdf_path})
                text = extract.content
            if pdf_path and render_pages and "pdf_render_pages" in tools.names():
                render = await tools.call(
                    "pdf_render_pages", {"file_path": pdf_path, "max_pages": max_pages}
                )
                page_urls = parse_rendered_pages(render)
    if not text:
        text = meta_res.content  # at least show meta as fallback content
    citations = list(meta_res.citations)

    # Archive PDF in library if writer is configured
    if isinstance(writer, LibraryWriter) and pdf_path and run_id:
        await writer.archive_pdf(
            Path(pdf_path),
            arxiv_id=arxiv_id,
            source_url=f"https://arxiv.org/abs/{arxiv_id}",
            title=title,
            source_type="arxiv",
        )

    return (text, title, citations, page_urls)


async def _fetch_pdf_source(
    url: str,
    tools: ToolRegistry,
    *,
    render_pages: bool = False,
    max_pages: int = 25,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
    pdf_cache_dir: str | None = None,
) -> tuple[str, list[Citation], list[str]]:
    """Download a PDF from a direct URL and extract text via the pdf tool.

    Returns (content_text, citations, page_image_data_urls). When
    `render_pages=True` and `pdf_render_pages` is registered, also returns
    VLM-ready JPEG data URLs for the first `max_pages` pages.
    """
    pdf_path = await _download_pdf_to_cache(url, pdf_cache_dir)
    if isinstance(pdf_path, str):
        # Download failed — pdf_path is an error message
        return (pdf_path, [], [])

    text = ""
    page_urls: list[str] = []
    if "pdf_extract_text" in tools.names():
        extract = await tools.call("pdf_extract_text", {"file_path": str(pdf_path)})
        text = extract.content
    if render_pages and "pdf_render_pages" in tools.names():
        render = await tools.call(
            "pdf_render_pages", {"file_path": str(pdf_path), "max_pages": max_pages}
        )
        page_urls = parse_rendered_pages(render)
    if not text:
        text = "(PDF text extraction not available yet — vision path may still work)"

    cit = Citation(
        url=url,
        title=f"PDF from {url[:80]}",
        snippet=text[:200],
        source_type="pdf",
        confidence_score=0.7,
    )

    # Archive PDF in library if writer is configured
    if isinstance(writer, LibraryWriter) and run_id:
        await writer.archive_pdf(
            Path(pdf_path),
            source_url=url,
            title=cit.title,
            source_type="pdf",
        )

    return (text, [cit], page_urls)


async def _download_pdf_to_cache(url: str, cache_dir: str | None = None) -> str | Path:
    """Download a PDF from URL to a tmp cache; return Path or error string.

    Caches by URL digest so repeat calls during the same run don't re-download.
    Uses `cache_dir` if provided, otherwise falls back to ~/.cache/deep_research/pdfs.
    """
    import hashlib
    import os
    from pathlib import Path

    import httpx

    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    default_cache = Path(os.path.expanduser("~/.cache/deep_research/pdfs"))
    tmp_dir = Path(cache_dir).resolve() if cache_dir else default_cache
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_pdf = tmp_dir / f"{digest}.pdf"
    if tmp_pdf.exists() and tmp_pdf.stat().st_size > 1024:
        return tmp_pdf
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            tmp_pdf.write_bytes(resp.content)
    except Exception as e:
        return f"(failed to download PDF: {type(e).__name__}: {e})"
    return tmp_pdf


async def _fetch_html_source(
    url: str,
    tools: ToolRegistry,
    config: AgentTopConfig,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> tuple[str, list[Citation]]:
    """Fetch an HTML page via fetch_page.

    Browser fallback for low-yield JS-heavy pages is handled inside
    `tools/fetch_page.py` itself (P4) so both url_source AND the deep-path
    researcher benefit uniformly. No fallback logic here.
    """
    if "fetch_page" not in tools.names():
        return ("(fetch_page tool not registered)", [])
    res = await tools.call("fetch_page", {"url": url})

    # Archive HTML in library if writer is configured
    if isinstance(writer, LibraryWriter) and res.content and run_id:
        await writer.archive_html(url, res.content)

    return (res.content, list(res.citations))


def _render_analysis_markdown(
    url: str,
    source_type: str,
    analysis,
    query: str | None,
) -> str:
    """Render a SourceAnalysis into a readable Markdown report."""
    parts: list[str] = []
    parts.append("# Source Analysis\n")
    parts.append(f"**Source:** [{url}]({url})\n")
    parts.append(f"**Detected type:** `{source_type}`\n")
    if analysis.title:
        parts.append(f"**Title:** {analysis.title}\n")
    if query:
        parts.append(f"**Query context:** {query}\n")
    parts.append(f"\n## Summary\n\n{analysis.summary}\n")
    if analysis.key_claims:
        parts.append("\n## Key Claims\n")
        for i, c in enumerate(analysis.key_claims, start=1):
            claim = c.get("claim", "") if isinstance(c, dict) else str(c)
            ev = c.get("evidence", "") if isinstance(c, dict) else ""
            loc = c.get("page_or_section", "") if isinstance(c, dict) else ""
            line = f"{i}. **{claim}**"
            if loc:
                line += f"  _({loc})_"
            if ev:
                line += f"\n   Evidence: {ev}"
            parts.append(line + "\n")
    if analysis.methodology:
        parts.append(f"\n## Methodology\n\n{analysis.methodology}\n")
    if analysis.limitations:
        parts.append("\n## Limitations\n")
        for lim in analysis.limitations:
            parts.append(f"- {lim}")
        parts.append("")
    if analysis.relevance_to_query:
        parts.append(f"\n## Relevance to Query\n\n{analysis.relevance_to_query}\n")
    if analysis.gaps:
        parts.append("\n## Identified Gaps\n")
        for g in analysis.gaps:
            parts.append(f"- {g}")
        parts.append("")
    if analysis.follow_ups:
        parts.append("\n## Follow-up Research Suggestions\n")
        for fu in analysis.follow_ups:
            topic = fu.get("topic", "") if isinstance(fu, dict) else str(fu)
            why = fu.get("why", "") if isinstance(fu, dict) else ""
            parts.append(f"- **{topic}**")
            if why:
                parts.append(f"  _{why}_")
        parts.append("")
    return "\n".join(parts)


async def url_source(
    url: str,
    query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Execute the url_source path."""
    reporter: ProgressReporter = ensure_reporter(progress)
    reporter.phase("url.classify", url[:80])
    url_type = await classify_url(url, head_probe_timeout_s=config.url_source.head_probe_timeout_s)
    arxiv_id = extract_arxiv_id(url) if url_type == UrlType.arxiv else None
    wants_follow_up = query_asks_for_follow_up(query, config.url_source.follow_up_trigger_phrases)
    reporter.step(
        "url.classify",
        f"type={url_type.value} arxiv_id={arxiv_id or '-'} followup={wants_follow_up}",
    )

    # Fetch content + initial citation(s)
    citations: list[Citation] = []
    content_text = ""
    page_image_data_urls: list[str] = []
    render_pdf = config.pdf_vision.enabled
    max_pdf_pages = 25

    if url_type == UrlType.arxiv and arxiv_id:
        reporter.phase("url.fetch", f"arxiv:{arxiv_id}")
        content_text, _title, citations, page_image_data_urls = await _fetch_arxiv_source(
            arxiv_id,
            tools,
            render_pages=render_pdf,
            max_pages=max_pdf_pages,
            writer=writer,
            run_id=run_id,
        )
    elif url_type == UrlType.pdf:
        reporter.phase("url.fetch", f"pdf download: {url[:80]}")
        content_text, citations, page_image_data_urls = await _fetch_pdf_source(
            url,
            tools,
            render_pages=render_pdf,
            max_pages=max_pdf_pages,
            writer=writer,
            run_id=run_id,
            pdf_cache_dir=config.arxiv.pdf_cache_dir,
        )
    elif url_type == UrlType.html:
        reporter.phase("url.fetch", f"html: {url[:80]}")
        content_text, citations = await _fetch_html_source(
            url,
            tools,
            config,
            writer=writer,
            run_id=run_id,
        )
    else:
        return Report(
            markdown=f"# Error\n\nUnsupported URL type for `{url}` ({url_type.value}).",
            path="unclear",
            classifier_rationale=f"URL unclassifiable: {url_type.value}",
            created_at=datetime.now(UTC),
            query=query,
        )
    reporter.step(
        "url.fetch", f"{len(content_text)} chars; {len(page_image_data_urls)} vision pages"
    )

    if not content_text or content_text.startswith(("HTTP", "(")):
        # If fetch failed, report it but don't try to analyze
        reporter.phase("url.fetch.failed", content_text[:80])
        md = (
            f"# Source Fetch Failed\n\n"
            f"**URL:** {url}\n\n"
            f"**Detected type:** `{url_type.value}`\n\n"
            f"**Fetch result:**\n\n```\n{content_text[:2000]}\n```\n"
        )
        return Report(
            markdown=md,
            citations=citations,
            path="url_source",
            classifier_rationale=f"URL detected ({url_type.value}); fetch error encountered",
            created_at=datetime.now(UTC),
            query=query,
        )

    # LLM analysis call. If pdf_vision rendered pages, we attach them as
    # image_url content blocks so the VLM can read figures + tables.
    reporter.phase("url.analyze", f"{url_type.value}; vision_pages={len(page_image_data_urls)}")
    analysis = await analyze_source_node(
        url=url,
        source_type=url_type.value,
        content=content_text,
        user_query=query or "",
        client=client,
        model=config.llm.text_model,
        page_image_data_urls=page_image_data_urls or None,
    )

    md = _render_analysis_markdown(url, url_type.value, analysis, query or None)

    # Optional follow-up: spawn deep path with the analysis gaps as planner seeds
    followup_md = ""
    followup_citations: list[Citation] = []
    if wants_follow_up and (analysis.gaps or analysis.follow_ups):
        reporter.phase(
            "url.followup", f"gaps={len(analysis.gaps)}; follow_ups={len(analysis.follow_ups)}"
        )
        followup_md, followup_citations = await _maybe_run_follow_up(
            analysis=analysis,
            original_url=url,
            user_query=query or "",
            client=client,
            tools=tools,
            config=config,
            progress=reporter,
        )
    else:
        reporter.phase("url.done", "no follow-up")

    # Merge follow-up citations into the main citation list
    all_citations = list(citations)
    if followup_citations:
        seen = {c.url for c in all_citations}
        for c in followup_citations:
            if c.url not in seen:
                all_citations.append(c)
                seen.add(c.url)

    return Report(
        markdown=md + "\n\n" + followup_md if followup_md else md,
        citations=all_citations,
        path="url_source_with_followup" if wants_follow_up else "url_source",
        classifier_rationale=f"URL detected; classified as {url_type.value}",
        created_at=datetime.now(UTC),
        query=query,
    )


async def _maybe_run_follow_up(
    analysis,
    original_url: str,
    user_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
) -> tuple[str, list[Citation]]:
    """Run deep-path follow-up research seeded from the analysis's gaps/follow_ups.
    Returns (markdown_section, follow_up_citations).
    """
    from deep_research.state import ClassifiedQuery, QueryPlan

    reporter: ProgressReporter = ensure_reporter(progress)
    sub_qs: list[dict] = []
    # Prefer `gaps`; fall back to follow_ups topics.
    for g in analysis.gaps:
        sub_qs.append({"question": g, "rationale": "gap surfaced by source analysis"})
    for fu in analysis.follow_ups:
        if isinstance(fu, dict):
            t = fu.get("topic", "")
            w = fu.get("why", "")
            if t:
                sub_qs.append({"question": t, "rationale": w})
    if not sub_qs:
        return ("", [])

    # Build a synthetic user query the deep planner can work from
    synthetic_query = (
        f"Following analysis of source {original_url}, "
        f"investigate the following gaps below using web + arxiv sources:\n"
        + "\n".join(f"- {sq['question']}" for sq in sub_qs)
    )

    # Hand off to deep path.
    from deep_research.paths.deep import deep_research as deep_path

    classified = ClassifiedQuery(
        path=QueryPlan.deep,
        rationale=f"Follow-up research spawned by url_source analysis of {original_url}",
        search_hint=user_query or synthetic_query,
    )
    followup_report: Report = await deep_path(
        classified, synthetic_query, client, tools, config, progress=progress
    )

    if not followup_report.markdown:
        return ("", [])

    reporter.phase("url.followup.done", "follow-up research complete")
    return ("## Follow-up Research\n\n" + followup_report.markdown, followup_report.citations)


__all__ = ["query_asks_for_follow_up", "url_source"]
