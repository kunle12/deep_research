from __future__ import annotations

import logging
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.state import ResearchState

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "writer.txt"


async def write(
    state: ResearchState,
    client: AsyncOpenAI,
    model: str,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> str:
    """Call the writer LLM and return Markdown. Glossary extraction is handled separately in agent.py."""
    sections_blob = _render_sections_for_prompt(state)
    citations_blob = _render_citations_for_prompt(state)
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    prompt = (
        prompt_template.replace("{query}", state.query)
        .replace("{sections}", sections_blob)
        .replace("{citations}", citations_blob)
    )
    try:
        system_msg = (
            "You are the final research report writer. Output a single Markdown "
            "document (the report). Do not wrap in code fences. "
            "Cite sources inline using bare URLs like [https://example.com/source]. "
            "Do NOT include a Bibliography section — that is appended separately."
        )

        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        md = resp.choices[0].message.content or ""
        # Defensive cleanup: strip markdown fences
        if md.startswith("```"):
            lines = md.splitlines()
            if len(lines) >= 2:
                md = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        return md
    except Exception as e:
        logger.warning("writer LLM call failed: %s: %s", type(e).__name__, e)
        return _concatenate_drafts(state)


def _render_sections_for_prompt(state: ResearchState) -> str:
    lines: list[str] = []
    for sq in state.plan.sub_questions:
        draft = state.drafts.get(sq.id)
        if not draft:
            continue
        lines.append(f"## {sq.question}")
        lines.append(draft)
        lines.append("")
    if not lines:
        lines.append("(no drafts available)")
    return "\n".join(lines)


def _render_citations_for_prompt(state: ResearchState) -> str:
    lines: list[str] = []
    for c in state.citations.values():
        lines.append(
            f"- URL: {c.url}\n  Title: {c.title or '(no title)'}\n  Snippet: {c.snippet[:200]}"
        )
    if not lines:
        lines.append("(no citations available)")
    return "\n".join(lines)


def _concatenate_drafts(state: ResearchState) -> str:
    """Fallback if the writer fails."""
    parts = [f"# Report\n\nQuery: {state.query}\n"]
    for sq in state.plan.sub_questions:
        draft = state.drafts.get(sq.id)
        if draft:
            parts.append(f"## {sq.question}\n{draft}\n")
    return "\n".join(parts)


__all__ = ["write"]
