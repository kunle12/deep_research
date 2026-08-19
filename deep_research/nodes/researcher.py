"""Researcher node — answers one sub-question using the async tool-calling loop.

P3: implemented. The researcher uses the `run_with_tools` loop from
`llm/tool_loop.py` so the LLM can request tools in parallel and absorb
their returned citations into state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from deep_research.citations import extract_urls_from_markdown, normalize_url
from deep_research.llm.router import LLMClientLike
from deep_research.llm.tool_loop import ScopedToolRegistry, ToolRegistry, ToolResult, run_with_tools
from deep_research.state import BLOCKED_PREFIX, BlockedSource, Citation, SubQuestion
from deep_research.util import coerce_float, load_prompt_template, utc_today_str

logger = logging.getLogger(__name__)

# Tools whose results embed base64 image content. A researcher may run on a
# text-only secondary endpoint (LLM routing), so these must never be offered to
# the tool loop — an image blob pulled into the message history would flow
# straight into a possibly vision-less model. The deep/academic paper-analysis
# paths invoke them directly and don't go through the researcher.
_IMAGE_PRODUCING_TOOLS = frozenset({"pdf_render_pages", "browser_take_screenshot"})


REFINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "refine",
        "description": (
            "Call this during research to dynamically refine the research plan. "
            "Use cases:\n"
            "1. You found an important reference that should be followed — set action='chase_reference'.\n"
            "2. You discovered an interesting subtopic that needs its own investigation — "
            "set action='drill_deeper'.\n"
            "3. You realize the sub-question needs a different search strategy — "
            "set action='revise_strategy'.\n\n"
            "This does NOT replace answering the current sub-question. "
            "It merely queues refinement for the next iteration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["chase_reference", "drill_deeper", "revise_strategy"],
                    "description": "What kind of refinement to perform",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "For drill_deeper: the new sub-question to investigate. "
                        "For chase_reference: a question about the reference's content. "
                        "For revise_strategy: not used."
                    ),
                },
                "reference_url": {
                    "type": "string",
                    "description": (
                        "For chase_reference: the URL or arxiv ID of the reference to follow. "
                        "For drill_deeper: optional URL hint for where to start."
                    ),
                },
                "tool_hint": {
                    "type": "string",
                    "enum": ["general-web", "arxiv", "reddit", "browser-required"],
                    "description": "Optional tool hint for the new sub-question.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this refinement is needed (for the report's provenance).",
                },
            },
            "required": ["action", "rationale"],
        },
    },
}


class _BlockedTrackingScopedRegistry(ScopedToolRegistry):
    """ScopedToolRegistry that records ``BLOCKED:...`` tool errors.

    Intercepts fetch/browser calls so skipped sources are captured
    programmatically — not just at the LLM's discretion — and can be surfaced
    as an "Unavailable Sources" section in the final report.
    """

    def __init__(self, parent: ToolRegistry, collector: list[BlockedSource]) -> None:
        super().__init__(parent)
        self._collector = collector

    def names(self) -> list[str]:
        return [n for n in super().names() if n not in _IMAGE_PRODUCING_TOOLS]

    def schemas(self) -> list[dict]:
        return [s for s in super().schemas() if s["function"]["name"] not in _IMAGE_PRODUCING_TOOLS]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await super().call(name, arguments)
        if result.error and result.error.startswith(BLOCKED_PREFIX):
            url = arguments.get("url", "") if isinstance(arguments, dict) else ""
            if url:
                self._collector.append(BlockedSource(url=url, reason=result.error))
        return result


async def research(
    sub_q: SubQuestion,
    client: LLMClientLike,
    model: str,
    tools: ToolRegistry,
    max_turns: int = 8,
    prior_context: str = "",
    max_refinement_per_researcher: int = 3,
    max_refinement_depth: int = 2,
    max_context_tokens: int = 131072,
    max_citations_per_researcher: int = 10,
) -> tuple[str, list[Citation], list[SubQuestion], list[BlockedSource]]:
    """Run the researcher loop for one sub-question.

    Returns (markdown_answer, citations, refinements, blocked_sources).

    `prior_context`: optional markdown section from library recall injected as
    additional context so the researcher knows what we already know.
    """
    prompt_template = load_prompt_template("researcher")
    prompt = prompt_template.replace("{question}", sub_q.question).replace(
        "{today}", utc_today_str()
    )

    hint_blurb = _hint_blurb(sub_q.tool_hint, tools.names())

    user_content = (hint_blurb + prompt) if hint_blurb else prompt
    if prior_context:
        user_content += "\n\n" + prior_context

    messages = [
        {
            "role": "system",
            "content": (
                "You are a research sub-agent. Use only the tools provided. "
                "You have a hard turn budget — be economical. "
                "BATCH independent tool calls into a single response (e.g. fetch "
                "multiple URLs together, not one per turn). Stop calling tools as "
                "soon as you have enough evidence. "
                "After all tool calls conclude, end with a single JSON object "
                "(no markdown fences, no surrounding text) with schema "
                '{"answer": "<markdown synthesis>", '
                '"citations": [{"url": "...", "title": "...", "snippet": "...", '
                '"confidence_score": 0.8}]}.'
            ),
        },
        {"role": "user", "content": user_content},
    ]

    blocked_sources: list[BlockedSource] = []
    scoped = _BlockedTrackingScopedRegistry(tools, blocked_sources)
    refine_calls_collector: list[dict] = []

    async def _refine_handler(**kwargs) -> ToolResult:
        if kwargs.get("action") == "revise_strategy":
            strategy = kwargs.get("rationale", "")
            return ToolResult(
                content=(
                    f"Strategy update noted: {strategy}. "
                    "You should adjust your remaining tool calls accordingly."
                )
            )
        if len(refine_calls_collector) >= max_refinement_per_researcher:
            return ToolResult(content="Refinement budget exhausted for this sub-question.")
        refine_calls_collector.append(dict(kwargs))
        return ToolResult(content="Refinement queued. Continue your research.")

    scoped.register("refine", _refine_handler, REFINE_SCHEMA["function"])

    final_messages, citations = await run_with_tools(
        client=client,
        messages=messages,
        tools=scoped,
        model=model,
        max_turns=max_turns,
        max_context_tokens=max_context_tokens,
    )

    answer_md, extra_citations = _parse_final_assistant(final_messages)

    if not answer_md or answer_md.strip() == "(no answer synthesized)":
        # The model never produced a final answer (e.g. the turn budget
        # expired mid-tool-call). Treat the whole researcher as failed so the
        # deep loop retries / marks the sub-question stuck instead of shipping
        # a garbage draft.
        raise RuntimeError("researcher produced no final answer")

    citations = _gate_citations(
        answer_md,
        citations,
        extra_citations,
        max_citations_per_researcher=max_citations_per_researcher,
    )

    refinements: list[SubQuestion] = []
    ref_depth = sub_q.refinement_depth + 1
    for call in refine_calls_collector:
        if ref_depth > max_refinement_depth:
            logger.info(
                "refinement skipped: depth %d exceeds max %d", ref_depth, max_refinement_depth
            )
            break
        action = call.get("action")
        if action == "drill_deeper" and call.get("question"):
            refinements.append(
                SubQuestion(
                    id=f"{sub_q.id}.refine{len(refinements) + 1}",
                    question=call["question"],
                    tool_hint=call.get("tool_hint", "general-web"),
                    refinement_depth=ref_depth,
                    rationale=call.get("rationale", ""),
                )
            )
        elif action == "chase_reference" and call.get("reference_url"):
            refinements.append(
                SubQuestion(
                    id=f"{sub_q.id}.ref{len(refinements) + 1}",
                    question=call.get("question", f"Follow reference: {call['reference_url']}"),
                    tool_hint=call.get("tool_hint", "general-web"),
                    refinement_depth=ref_depth,
                    rationale=call.get("rationale", ""),
                    parent_arxiv_id=call["reference_url"]
                    if "arxiv" in call["reference_url"]
                    else None,
                )
            )

    return (answer_md, citations, refinements, blocked_sources)


def _gate_citations(
    answer_md: str,
    tool_citations: list[Citation],
    json_citations: list[Citation],
    *,
    max_citations_per_researcher: int,
) -> list[Citation]:
    """Keep only citations the researcher actually used.

    Three sources feed this function:
    - *tool_citations*: every citation attached to tool results (search hits,
      fetched pages). These are *candidates* the model saw, not sources it
      used — most get dropped here.
    - *json_citations*: the URLs the model explicitly listed in its final
      answer's ``citations`` array. These are kept by definition.
    - URLs referenced anywhere in *answer_md* (autolinks or bare URLs) are
      kept too; if no citation object exists for one, a minimal Citation is
      synthesized so the bibliography can still point at it.

    The result is capped at *max_citations_per_researcher* (prose-referenced
    URLs first, then by confidence) so a chatty model cannot flood the final
    report's bibliography.
    """
    referenced = extract_urls_from_markdown(answer_md)
    used = set(referenced) | {normalize_url(c.url) for c in json_citations}

    best: dict[str, Citation] = {}
    # Tool citations carry richer metadata (source_type, discovered_by,
    # arxiv_id, authors...), so prefer them when the URL is used.
    for c in tool_citations:
        norm = normalize_url(c.url)
        if norm in used and norm not in best:
            best[norm] = c
    for c in json_citations:
        norm = normalize_url(c.url)
        if norm in used and norm not in best:
            best[norm] = c
    # Referenced URLs with no matching citation object — synthesize one.
    for norm, original in referenced.items():
        if norm not in best:
            best[norm] = Citation(
                url=original,
                title=original,
                source_type="web",
                confidence_score=0.5,
            )

    out = list(best.values())
    if max_citations_per_researcher > 0 and len(out) > max_citations_per_researcher:
        out.sort(
            key=lambda c: (
                normalize_url(c.url) not in referenced,
                -c.confidence_score,
            )
        )
        out = out[:max_citations_per_researcher]
    return out


def _hint_blurb(tool_hint: str, available: list[str]) -> str:
    """Render a short instruction nudging the researcher toward tool_hint.

    Returns "" when the hint is generic or the matching tool isn't registered.
    """
    if not tool_hint or tool_hint == "general-web":
        return ""
    if tool_hint == "arxiv" and "arxiv_search" in available:
        return (
            "Hint: this sub-question is best answered using arxiv_search as the first "
            "tool call, then optionally fetch_page on the most-relevant returned URL. "
            "Only fall back to general web_search if arxiv returns nothing.\n\n"
        )
    if tool_hint == "reddit" and "reddit_search" in available:
        return (
            "Hint: this sub-question benefits from Reddit discussion data via the "
            "`reddit` tool, when relevant. Supplement with web_search if needed.\n\n"
        )
    if tool_hint == "browser-required" and "browser_navigate" in available:
        return (
            "Hint: this sub-question requires JS-rendering to extract content; "
            "use browser_navigate rather than fetch_page for the primary sources.\n\n"
        )
    return ""


def _parse_final_assistant(messages: list[dict]) -> tuple[str, list[Citation]]:
    """Pull the FINAL assistant answer and try to parse it as JSON answer+citations.

    Only the last message counts as the answer. If the tool loop was cut off
    by the turn budget, the last message is a tool-call message or a tool
    result with no synthesized answer; callers must treat that as a failed
    researcher instead of recycling intermediate chatter like "Let me search
    for that." as the sub-question's answer.
    """
    if not messages:
        return ("(no answer synthesized)", [])
    last = messages[-1]
    if last.get("role") != "assistant" or not last.get("content") or last.get("tool_calls"):
        return ("(no answer synthesized)", [])
    last_assistant = last["content"]
    try:
        data = json.loads(last_assistant)
        if not isinstance(data, dict):
            return (last_assistant, [])
        answer = str(data.get("answer", last_assistant))
        cites: list[Citation] = []
        for c in data.get("citations", []) or []:
            if not isinstance(c, dict):
                continue
            url = c.get("url")
            if not url or not url.startswith(("http://", "https://", "ftp://")):
                continue
            cites.append(
                Citation(
                    url=url,
                    title=str(c.get("title", "") or ""),
                    snippet=str(c.get("snippet", "") or ""),
                    confidence_score=coerce_float(c.get("confidence_score"), 0.6),
                    source_type="web",
                )
            )
        return (answer, cites)
    except json.JSONDecodeError:
        # Not JSON — return as-is markdown
        return (last_assistant, [])


__all__ = ["research"]
