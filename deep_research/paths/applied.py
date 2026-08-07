"""applied path — blog-first research (P12.0).

This path seeds research from technical blogs rather than arxiv papers,
producing a report focused on practical / implementation details.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import (
    Citation,
    ClassifiedQuery,
    Report,
)

logger = logging.getLogger(__name__)


async def applied_research(
    classified: ClassifiedQuery,
    original_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Execute the blog-first applied research path.

    Seeds from blog_search, fetches top blog posts, and synthesizes a
    markdown report focused on practical / implementation details.
    """
    reporter: ProgressReporter = ensure_reporter(progress)

    # Step 1: blog search
    reporter.phase("applied.seed", "searching technical blogs")
    citations: list[Citation] = []
    if "blog_search" not in tools.names():
        return Report(
            markdown="# Applied Research\n\nblog_search tool not available.",
            path="applied",
            citations=[],
        )

    search_query = classified.search_hint or original_query
    blog_result = await tools.call("blog_search", {"query": search_query, "max_results": 5})
    if blog_result.error is not None:
        return Report(
            markdown=f"# Applied Research\n\nBlog search failed: {blog_result.error}",
            path="applied",
            citations=[],
        )

    citations = list(blog_result.citations)
    reporter.step("applied.seed", f"{len(citations)} blog posts found")

    # Step 2: fetch blog content
    reporter.phase("applied.fetch", f"fetching {min(len(citations), 3)} blog posts")
    fetched_texts: dict[str, str] = {}
    for i, c in enumerate(citations[:3]):
        if "fetch_page" in tools.names():
            result = await tools.call("fetch_page", {"url": c.url})
            if result.error is None:
                # Key by URL so a failed middle fetch can never misattribute
                # another post's text to the wrong title (see digest below).
                fetched_texts[c.url] = result.content[:8000]
                reporter.step("applied.fetch", f"post {i + 1}: {c.title[:60]}")
                # Archive HTML in library if writer is configured
                if isinstance(writer, LibraryWriter) and run_id:
                    await writer.archive_html(c.url, result.content)

    # Step 3: synthesize report
    reporter.phase("applied.synthesize", "writing report")
    final_md = await _synthesize_applied(
        original_query, citations, fetched_texts, client, config.llm.text_model
    )

    reporter.phase("applied.done", f"{len(citations)} blog posts")
    return Report(
        markdown=final_md,
        citations=citations,
        path="applied",
        created_at=datetime.now(UTC),
        query=original_query,
    )


async def _synthesize_applied(
    query: str,
    citations: list[Citation],
    fetched_texts: list[str],
    client: AsyncOpenAI,
    model: str,
) -> str:
    """Synthesize blog posts into an applied research report."""
    blog_digest = (
        "\n\n".join(
            f"### {c.title}\nURL: {c.url}\n\n{fetched_texts.get(c.url, c.snippet)}"
            for c in citations
        )
        or "No blog content available."
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are an applied research writer. Given blog posts from technical "
                "sources, write a 1-3 section markdown report answering the user's "
                "research query. Focus on practical implementation details, code "
                "examples (when available), and key takeaways. Cite each blog post "
                "inline with an autolink like <https://example.com/post>."
            ),
        },
        {
            "role": "user",
            "content": f"# Research query\n{query}\n\n# Blog posts digest\n{blog_digest}",
        },
    ]

    try:
        resp = await client.chat.completions.create(model=model, messages=messages, temperature=0.0)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("applied synthesis failed: %s: %s; using fallback", type(e).__name__, e)
        return _fallback_synthesis(query, citations)


def _fallback_synthesis(query: str, citations: list[Citation]) -> str:
    """Deterministic fallback when LLM is unreachable."""
    lines = [
        "# Applied Research Report\n",
        f"**Query:** {query}\n",
        f"**Blog posts found:** {len(citations)}\n",
    ]
    for i, c in enumerate(citations, start=1):
        lines.append(f"\n## {i}. {c.title}\n")
        lines.append(f"URL: {c.url}\n")
        if c.snippet:
            lines.append(f"> {c.snippet}\n")
    return "\n".join(lines)


__all__ = ["applied_research"]
