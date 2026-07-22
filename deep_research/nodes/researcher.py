"""Researcher node — answers one sub-question using the async tool-calling loop.

P3: implemented. The researcher uses the `run_with_tools` loop from
`llm/tool_loop.py` so the LLM can request tools in parallel and absorb
their returned citations into state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.llm.tool_loop import ScopedToolRegistry, ToolRegistry, ToolResult, run_with_tools
from deep_research.state import Citation, SubQuestion

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "researcher.txt"

# Hard cap on free-text length each tool result is truncated to before showing the LLM,
# to keep context manageable across many sub-agents.
_MAX_RESULT_CHARS = 6000

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


async def research(
    sub_q: SubQuestion,
    client: AsyncOpenAI,
    model: str,
    tools: ToolRegistry,
    max_turns: int = 8,
    prior_context: str = "",
    max_refinement_per_researcher: int = 3,
    max_refinement_depth: int = 2,
) -> tuple[str, list[Citation], list[SubQuestion]]:
    """Run the researcher loop for one sub-question.

    Returns (markdown_answer, citations, refinements).

    `prior_context`: optional markdown section from library recall injected as
    additional context so the researcher knows what we already know.
    """
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    prompt = prompt_template.replace("{question}", sub_q.question)

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

    scoped = ScopedToolRegistry(tools)
    refine_calls_collector: list[dict] = []

    async def _refine_handler(**kwargs) -> ToolResult:
        if kwargs.get("action") == "revise_strategy":
            strategy = kwargs.get("rationale", "")
            return ToolResult(content=(
                f"Strategy update noted: {strategy}. "
                "You should adjust your remaining tool calls accordingly."
            ))
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
    )

    answer_md, extra_citations = _parse_final_assistant(final_messages)
    citations.extend(extra_citations)

    refinements: list[SubQuestion] = []
    ref_depth = sub_q.refinement_depth + 1
    for call in refine_calls_collector:
        if ref_depth > max_refinement_depth:
            logger.info("refinement skipped: depth %d exceeds max %d", ref_depth, max_refinement_depth)
            break
        action = call.get("action")
        if action == "drill_deeper" and call.get("question"):
            refinements.append(SubQuestion(
                id=f"{sub_q.id}.refine{len(refinements) + 1}",
                question=call["question"],
                tool_hint=call.get("tool_hint", "general-web"),
                refinement_depth=ref_depth,
                rationale=call.get("rationale", ""),
            ))
        elif action == "chase_reference" and call.get("reference_url"):
            refinements.append(SubQuestion(
                id=f"{sub_q.id}.ref{len(refinements) + 1}",
                question=call.get("question", f"Follow reference: {call['reference_url']}"),
                tool_hint=call.get("tool_hint", "general-web"),
                refinement_depth=ref_depth,
                rationale=call.get("rationale", ""),
                parent_arxiv_id=call["reference_url"] if "arxiv" in call["reference_url"] else None,
            ))

    return (answer_md, citations, refinements)


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
    """Pull the last assistant message and try to parse it as JSON answer+citations."""
    last_assistant = None
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            last_assistant = m["content"]
            break
    if not last_assistant:
        return ("(no answer synthesized)", [])
    try:
        data = json.loads(last_assistant)
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
                    confidence_score=float(c.get("confidence_score") or 0.6),
                    source_type="web",
                )
            )
        return (answer, cites)
    except json.JSONDecodeError:
        # Not JSON — return as-is markdown
        return (last_assistant, [])


__all__ = ["research"]
