"""quick path - single web_search + fetch top-k + synthesize via LLM call.

P2: implemented. Flow:
  1. call web_search(query, max_results=5)
  2. fetch_page() the top-k (default 3) result URLs in parallel
  3. submit the snippet + gathered page text to a single chat-completion LLM
     call using the `quick_summary.txt` prompt
  4. parse JSON the LLM returns into answer + citations
  5. return a Report
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.progress import NullReporter, ProgressReporter
from deep_research.state import Citation, ClassifiedQuery, Report

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "quick_summary.txt"

MAX_PAGES_TO_FETCH = 3


async def quick_search(
    classified: ClassifiedQuery,
    original_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
) -> Report:
    """Execute the quick path: 1 search + summarize via LLM synthesis."""
    reporter: ProgressReporter = progress if progress is not None else NullReporter()
    reporter.phase("quick.search", "querying web_search")
    if "web_search" in tools.names():
        search_result = await tools.call(
            "web_search", {"query": classified.search_hint, "max_results": 5}
        )
    else:
        search_result = ToolResult(content="no web_search tool registered", citations=[])

    citations = list(search_result.citations)

    top_urls = [c.url for c in citations[:MAX_PAGES_TO_FETCH] if c.url]
    pages_text: list[str] = []
    if top_urls and "fetch_page" in tools.names():
        reporter.phase("quick.fetch", f"fetching top {len(top_urls)} pages")
        fetches = [tools.call("fetch_page", {"url": u}) for u in top_urls]
        fetched = await asyncio.gather(*fetches, return_exceptions=True)
        for u, fr in zip(top_urls, fetched):
            if isinstance(fr, Exception):
                logger.warning("fetch_page failed for %s: %s", u, fr)
                reporter.step("fetch.fail", f"{u}: {type(fr).__name__}")
                continue
            reporter.step("fetch.ok", u[:80])
            pages_text.append(f"=== Source: {u} ===\n{fr.content[:4000]}\n")
            for c in fr.citations:
                if c.url != u and c.url not in {x.url for x in citations}:
                    citations.append(c)

    reporter.phase("quick.synthesize", f"{len(citations)} citations + {len(pages_text)} pages")
    rendered_results = _render_for_llm(original_query, search_result, pages_text)
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    prompt_text = (
        prompt_template
        .replace("{query}", original_query)
        .replace("{results}", rendered_results)
    )

    answer_text, llm_citations = await _synthesize(client, config, prompt_text)
    citations = _merge_citations(citations, llm_citations)

    reporter.phase("quick.done", f"{len(citations)} citations")
    return Report(
        markdown=answer_text,
        citations=citations,
        path="quick",
        classifier_rationale=classified.rationale,
    )


def _render_for_llm(
    original_query: str,
    search_result: ToolResult,
    pages_text: list[str],
) -> str:
    """Build a text block for the LLM showing the search hits + fetched pages."""
    lines: list[str] = []
    lines.append(f"# Search query: {original_query}\n")
    lines.append("## Web search results:")
    if not search_result.citations:
        lines.append(f"(error: {search_result.error or 'no results'})")
        lines.append(search_result.content)
    else:
        for i, c in enumerate(search_result.citations, start=1):
            lines.append(
                f"{i}. {c.title or c.url}\n   URL: {c.url}\n   Snippet: {c.snippet[:300]}"
            )
    if pages_text:
        lines.append("")
        lines.append("## Fetched page contents (truncated to 4000 chars each):")
        lines.append("")
        lines.append("\n\n".join(pages_text))
    else:
        lines.append("\n(no fetched pages)")
    return "\n".join(lines)


async def _synthesize(
    client: AsyncOpenAI,
    config: AgentTopConfig,
    prompt_text: str,
) -> tuple[str, list[Citation]]:
    """Call the LLM with the quick_summary prompt."""
    citations: list[Citation] = []
    try:
        resp = await client.chat.completions.create(
            model=config.llm.text_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a quick research synthesizer. "
                        "Respond with a SINGLE JSON object and NOTHING ELSE - no markdown fences, "
                        'no surrounding text. Schema: '
                        '{"answer": "<markdown string>", '
                        '"citations": [{"url":"...","title":"...","snippet":"...","confidence_score":0.8}]}'
                    ),
                },
                {"role": "user", "content": prompt_text},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        try:
            data = json.loads(raw)
            answer_md = str(data.get("answer", raw))
            for c in data.get("citations", []) or []:
                url = c.get("url")
                if not url:
                    continue
                citations.append(
                    Citation(
                        url=url,
                        title=str(c.get("title", "") or ""),
                        snippet=str(c.get("snippet", "") or ""),
                        confidence_score=float(c.get("confidence_score") or 0.6),
                        source_type="web",
                    )
                )
        except json.JSONDecodeError:
            answer_md = raw
    except Exception as e:
        logger.warning("LLM synthesis failed: %s: %s", type(e).__name__, e)
        answer_md = (
            f"# Quick Answer\n\n"
            f"Could not synthesize via LLM ({type(e).__name__}: {e}). "
            f"Raw search results:\n\n"
            f"{prompt_text}"
        )

    return answer_md, citations


def _merge_citations(base: list[Citation], additions: list[Citation]) -> list[Citation]:
    """Dedup by URL; additions with higher confidence override base."""
    out: dict[str, Citation] = {c.url: c for c in base}
    for c in additions:
        existing = out.get(c.url)
        if existing is None or existing.confidence_score < c.confidence_score:
            out[c.url] = c
    return list(out.values())


__all__ = ["quick_search"]
