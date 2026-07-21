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

from deep_research.llm.tool_loop import ToolRegistry, run_with_tools
from deep_research.state import Citation, SubQuestion

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "researcher.txt"

# Hard cap on free-text length each tool result is truncated to before showing the LLM,
# to keep context manageable across many sub-agents.
_MAX_RESULT_CHARS = 6000


async def research(
    sub_q: SubQuestion,
    client: AsyncOpenAI,
    model: str,
    tools: ToolRegistry,
    max_turns: int = 8,
    prior_context: str = "",
) -> tuple[str, list[Citation]]:
    """Run the researcher loop for one sub-question. Returns (markdown_answer, citations).

    `prior_context`: optional markdown section from library recall injected as
    additional context so the researcher knows what we already know.
    """
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    prompt = prompt_template.replace("{question}", sub_q.question)

    # Surface the planner's tool_hint to the researcher so it prefers the
    # right tool family (e.g. `arxiv_search` for arxiv-flagged sub-questions).
    hint_blurb = _hint_blurb(sub_q.tool_hint, tools.names())

    # Build user message: hint_blurb + prompt + optional prior_context
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

    final_messages, citations = await run_with_tools(
        client=client,
        messages=messages,
        tools=tools,
        model=model,
        max_turns=max_turns,
    )

    # Pull the last assistant message content (which should be the answer JSON)
    answer_md, extra_citations = _parse_final_assistant(final_messages)
    citations.extend(extra_citations)
    return (answer_md, citations)


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
