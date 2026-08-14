"""Deep-path paper analysis — critic-selected full PDF analysis.

Phase 1 (implemented): the critic selects a small set of arXiv papers for
full PDF analysis; this module runs the same pipeline as the academic path
(download -> extract text -> optionally render pages -> `analyze_paper`),
stores the results on `ResearchState.deep_analyses`, archives them in the
personal library, and the writer weaves them into the final report.

Phase 2 (planned): analyses feed back into the critic (gap/key-reference
chasing) and into later researchers via `prior_context`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deep_research.config import AgentTopConfig, LLMRole
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.router import LLMRouter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.analyze_paper import analyze as analyze_paper_node
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import (
    Citation,
    PaperAnalysis,
    PaperAnalysisRequest,
    PaperNode,
    ResearchState,
    ToolName,
)
from deep_research.tools.pdf_utils import parse_pdf_path, parse_rendered_pages
from deep_research.util import ARXIV_ID_RE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared PDF helpers (factored out of paths/academic.py so the deep path
# reuses one implementation instead of two diverging copies)
# ---------------------------------------------------------------------------


async def download_pdf_once(
    arxiv_id: str,
    tools: ToolRegistry,
    timeout_s: float = 180.0,
) -> str | None:
    """Download an arXiv PDF once. Returns a local path or None on failure."""
    if "arxiv_download_pdf" not in tools.names():
        return None
    try:
        async with asyncio.timeout(timeout_s):
            dl = await tools.call("arxiv_download_pdf", {"arxiv_id": arxiv_id})
    except TimeoutError:
        logger.warning("arxiv_download_pdf timed out for %s after %.0fs", arxiv_id, timeout_s)
        return None
    if dl.error is not None:
        logger.info("arxiv_download_pdf failed for %s: %s", arxiv_id, dl.error)
        return None
    pdf_path = parse_pdf_path(dl.content)
    if pdf_path is None:
        logger.warning(
            "arxiv_download_pdf returned unexpected content for %s: %r",
            arxiv_id,
            (dl.content or "")[:100],
        )
        return None
    return pdf_path


async def fetch_paper_text_fallback(
    arxiv_id: str,
    tools: ToolRegistry,
    timeout_s: float = 180.0,
) -> tuple[str, str | None]:
    """Fall back to arxiv_resolve metadata when PDF download is unavailable."""
    if "arxiv_resolve" not in tools.names():
        return ("", None)
    try:
        async with asyncio.timeout(timeout_s):
            resolved = await tools.call("arxiv_resolve", {"arxiv_id": arxiv_id})
        return (resolved.content or "", None)
    except TimeoutError:
        logger.warning("arxiv_resolve timed out for %s (%.0fs)", arxiv_id, timeout_s)
        return ("", None)


async def extract_text(
    pdf_path: str,
    tools: ToolRegistry,
    timeout_s: float = 180.0,
) -> str:
    """Extract text from a local PDF file. Returns the extracted text or ''."""
    if "pdf_extract_text" not in tools.names():
        return ""
    try:
        async with asyncio.timeout(timeout_s):
            extracted = await tools.call("pdf_extract_text", {"file_path": pdf_path})
        return extracted.content or ""
    except TimeoutError:
        logger.warning("pdf_extract_text timed out (path=%s); returning empty", pdf_path)
        return ""


async def render_pages(
    pdf_path: str,
    tools: ToolRegistry,
    max_pages: int = 10,
    timeout_s: float = 300.0,
) -> list[str]:
    """Render pages from a local PDF file. Returns [] on any failure."""
    if "pdf_render_pages" not in tools.names():
        return []
    try:
        async with asyncio.timeout(timeout_s):
            render = await tools.call(
                "pdf_render_pages", {"file_path": pdf_path, "max_pages": max_pages}
            )
        return parse_rendered_pages(render)
    except TimeoutError:
        logger.warning(
            "pdf_render_pages timed out (path=%s); downgrading to text-only",
            pdf_path,
        )
        return []


# ---------------------------------------------------------------------------
# Deep analysis orchestration
# ---------------------------------------------------------------------------


@dataclass
class DeepAnalysisResult:
    """Outcome of analyzing one paper for the deep path."""

    analysis: PaperAnalysis
    pdf_path: str | None = None
    text_source: Literal["pdf", "abstract"] = "pdf"


async def analyze_paper_deep(
    arxiv_id: str,
    *,
    query: str,
    router: LLMRouter,
    config: AgentTopConfig,
    tools: ToolRegistry,
) -> DeepAnalysisResult | None:
    """Analyze one arXiv paper for the deep path.

    Fallback chain: full PDF (text + optional vision rendering) -> abstract-
    only via arxiv_resolve -> None (skip). Never raises on LLM failures —
    `analyze_paper` degrades gracefully and this function falls back.
    """
    pdf_path = await download_pdf_once(arxiv_id, tools)
    if pdf_path is not None:
        # Run text extraction and page rendering concurrently so a slow
        # render cannot delay the text-analysis path (mirrors academic.py).
        text_task = asyncio.create_task(extract_text(pdf_path, tools))
        render_task = (
            asyncio.create_task(render_pages(pdf_path, tools, max_pages=10))
            if config.pdf_vision.enabled and "pdf_render_pages" in tools.names()
            else None
        )

        paper_text = await text_task
        page_urls: list[str] = []
        if render_task is not None:
            try:
                page_urls = await asyncio.wait_for(render_task, timeout=300.0)
            except TimeoutError:
                logger.debug(
                    "page rendering timed out for %s; falling back to text-only",
                    arxiv_id,
                )
                page_urls = []

        if paper_text.strip():
            resolved = router.resolve(LLMRole.ANALYSIS, has_images=bool(page_urls))
            text_resolved = router.resolve(LLMRole.ANALYSIS)
            analysis = await analyze_paper_node(
                arxiv_id=arxiv_id,
                paper_text=paper_text,
                query=query,
                client=resolved.client,
                model=resolved.model,
                page_image_data_urls=page_urls or None,
                text_source="pdf",
                max_context_tokens=resolved.max_context_tokens,
                # Final synthesis is text-only — allow it on the text route even
                # when pages were rendered.
                synthesis_client=text_resolved.client,
                synthesis_model=text_resolved.model,
                synthesis_max_context_tokens=text_resolved.max_context_tokens,
            )
            return DeepAnalysisResult(analysis=analysis, pdf_path=pdf_path, text_source="pdf")

    # Abstract-only fallback (download failed or no extractable text).
    paper_text, _ = await fetch_paper_text_fallback(arxiv_id, tools)
    if paper_text.strip():
        resolved = router.resolve(LLMRole.ANALYSIS)
        analysis = await analyze_paper_node(
            arxiv_id=arxiv_id,
            paper_text=paper_text,
            query=query,
            client=resolved.client,
            model=resolved.model,
            page_image_data_urls=None,
            text_source="abstract",
            max_context_tokens=resolved.max_context_tokens,
        )
        return DeepAnalysisResult(analysis=analysis, pdf_path=None, text_source="abstract")
    return None


async def run_paper_analysis_pass(
    state: ResearchState,
    requests: list[PaperAnalysisRequest],
    *,
    query: str,
    router: LLMRouter,
    config: AgentTopConfig,
    tools: ToolRegistry,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    reporter: ProgressReporter | None = None,
    run_id: str = "",
) -> None:
    """Run full PDF analysis for critic-selected papers, storing results on *state*.

    Bounded by ``agent.deep_analysis_max_papers`` (0 disables the feature),
    filtered by ``agent.deep_analysis_min_priority``, deduped against
    ``state.deep_analysis_requested``, and run concurrently under
    ``agent.deep_analysis_concurrency``. Failures are logged and skipped —
    they never fail the deep run.
    """
    rep = ensure_reporter(reporter)
    cap = config.agent.deep_analysis_max_papers
    if cap <= 0 or not requests:
        return
    min_priority = config.agent.deep_analysis_min_priority

    already = {aid.strip().lower() for aid in state.deep_analysis_requested}
    # The cap bounds the whole RUN, not a single pass invocation: subtract
    # papers already requested in earlier critic rounds so repeated passes
    # cannot exceed deep_analysis_max_papers in total.
    remaining_budget = max(0, cap - len(already))
    if remaining_budget <= 0:
        return
    selected: list[PaperAnalysisRequest] = []
    seen: set[str] = set()
    for r in sorted(requests, key=lambda r: r.priority_score, reverse=True):
        aid = (r.arxiv_id or "").strip().lower()
        if not aid or aid in already or aid in seen:
            continue
        if r.priority_score < min_priority:
            continue
        seen.add(aid)
        selected.append(r)
        if len(selected) >= remaining_budget:
            break

    if not selected:
        return
    # Mark all selected ids up-front so later critic iterations cannot
    # re-select the same papers (even if an analysis later fails).
    state.deep_analysis_requested.extend(r.arxiv_id for r in selected)

    rep.phase("deep.analysis", f"analyzing {len(selected)} paper(s)")
    sem = asyncio.Semaphore(max(1, config.agent.deep_analysis_concurrency))

    async def _one(r: PaperAnalysisRequest) -> None:
        aid = (r.arxiv_id or "").strip()
        async with sem:
            try:
                if config.agent.deep_analysis_use_library_cache:
                    cached = await _cached_analysis(aid, writer)
                    if cached is not None:
                        state.deep_analyses[aid] = cached
                        logger.info("deep analysis: reused library analysis for %s", aid)
                        rep.step("deep.analysis.cache", aid)
                        return

                result = await asyncio.wait_for(
                    analyze_paper_deep(
                        aid,
                        query=query,
                        router=router,
                        config=config,
                        tools=tools,
                    ),
                    timeout=config.agent.deep_analysis_timeout_s,
                )
                if result is None:
                    logger.warning("deep analysis skipped for %s (no extractable content)", aid)
                    rep.step("deep.analysis.skip", aid)
                    return

                state.deep_analyses[aid] = result.analysis
                rep.step("deep.analysis.ok", aid)

                # Ensure the paper is present as a citation so the report can
                # cite it; existing (richer) researcher citations win on dedup.
                state.absorb_citations(
                    [
                        Citation(
                            url=f"https://arxiv.org/abs/{aid}",
                            title=result.analysis.title or aid,
                            snippet=(result.analysis.summary or "")[:200],
                            source_type="arxiv",
                            arxiv_id=aid,
                            confidence_score=0.9,
                            discovered_by=ToolName.arxiv,
                        )
                    ]
                )

                if isinstance(writer, LibraryWriter):
                    try:
                        await _archive_and_record(aid, result, writer, run_id)
                    except Exception:
                        logger.warning("deep analysis archival failed for %s", aid, exc_info=True)
            except TimeoutError:
                logger.warning(
                    "deep analysis timed out for %s after %.0fs",
                    aid,
                    config.agent.deep_analysis_timeout_s,
                )
                rep.step("deep.analysis.fail", aid)
            except Exception as e:
                logger.warning(
                    "deep analysis failed for %s: %s: %s",
                    aid,
                    type(e).__name__,
                    e,
                )
                rep.step("deep.analysis.fail", aid)

    await asyncio.gather(*[_one(r) for r in selected])


# ---------------------------------------------------------------------------
# Library cache + archival
# ---------------------------------------------------------------------------


async def _cached_analysis(
    arxiv_id: str,
    writer: LibraryWriter | NullLibraryWriter | None,
) -> PaperAnalysis | None:
    """Reuse a prior ``analyze_paper`` analysis from the personal library."""
    if not isinstance(writer, LibraryWriter):
        return None
    try:
        artifact = await writer.storage.find_artifact_by_arxiv_id(arxiv_id)
        if artifact is None:
            return None
        rows = await writer.storage.get_analyses_for_artifact(artifact.artifact_id)
    except Exception:
        logger.debug("library lookup failed for %s", arxiv_id, exc_info=True)
        return None
    paper_rows = [r for r in rows if getattr(r, "analyzer", "") == "analyze_paper"]
    if not paper_rows:
        return None
    return _analysis_from_row(artifact, paper_rows[-1])


def _analysis_from_row(artifact: Any, row: Any) -> PaperAnalysis:
    """Reconstruct a PaperAnalysis from a stored analysis row + artifact."""

    def _as_list(raw: str | None) -> list:
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    key_references: list[PaperNode] = []
    for r in _as_list(getattr(row, "key_references", None)):
        if isinstance(r, dict) and r.get("arxiv_id"):
            try:
                key_references.append(PaperNode.model_validate(r))
            except Exception:
                continue
        elif isinstance(r, str) and ARXIV_ID_RE.match(r):
            key_references.append(
                PaperNode(
                    arxiv_id=r,
                    title=r,
                    url=f"https://arxiv.org/abs/{r}",
                )
            )

    return PaperAnalysis(
        title=artifact.title or getattr(row, "summary", "") or "",
        summary=getattr(row, "summary", "") or "",
        key_findings=_as_list(getattr(row, "key_findings", None)),
        methodology=getattr(row, "methodology", "") or "",
        limitations=_as_list(getattr(row, "limitations", None)),
        relevance_to_query=getattr(row, "relevance_to_query", "") or "",
        key_references=key_references,
    )


async def _archive_and_record(
    arxiv_id: str,
    result: DeepAnalysisResult,
    writer: LibraryWriter,
    run_id: str,
) -> None:
    """Archive the analyzed PDF + analysis in the personal library."""
    artifact = await writer.storage.find_artifact_by_arxiv_id(arxiv_id)
    if artifact is None and result.pdf_path:
        await writer.archive_pdf(
            Path(result.pdf_path),
            arxiv_id=arxiv_id,
            source_url=f"https://arxiv.org/abs/{arxiv_id}",
            title=result.analysis.title or None,
            source_type="arxiv",
        )
        artifact = await writer.storage.find_artifact_by_arxiv_id(arxiv_id)
    if artifact is not None:
        await writer.record_analysis(artifact.artifact_id, result.analysis, run_id, "analyze_paper")


def format_deep_analysis_context(
    analyses: dict[str, PaperAnalysis],
    max_chars: int = 6000,
) -> str:
    """Render analyzed papers as a compact digest (Phase 2 feedback).

    Injected into the critic's state view and later researchers' prior
    context so follow-up work is informed by the deep analyses.
    """
    if not analyses:
        return ""
    lines = ["# Deep paper analyses", ""]
    for aid, a in analyses.items():
        lines.append(f"- {a.title or aid} (arxiv:{aid})")
        if a.summary:
            lines.append(f"  Summary: {a.summary[:300]}")
        if a.key_findings:
            findings = "; ".join(str(f)[:120] for f in a.key_findings[:3])
            lines.append(f"  Key findings: {findings[:500]}")
        if a.key_references:
            refs = ", ".join(r.arxiv_id for r in a.key_references[:5])
            lines.append(f"  Key references: {refs}")
    return "\n".join(lines)[:max_chars]


__all__ = [
    "DeepAnalysisResult",
    "analyze_paper_deep",
    "download_pdf_once",
    "extract_text",
    "fetch_paper_text_fallback",
    "format_deep_analysis_context",
    "render_pages",
    "run_paper_analysis_pass",
]
