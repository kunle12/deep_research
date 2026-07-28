"""Critic node — reviews state, decides whether to iterate again (deep path).

P3: implemented via `prompts/critic.txt`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from deep_research.state import Critique, ResearchState, SubQuestion

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "critic.txt"

_VALID_TOOL_HINTS = {"general-web", "arxiv", "reddit", "browser-required"}


async def review(state: ResearchState, client: AsyncOpenAI, model: str) -> Critique:
    """Make the critic LLM call. Returns a Critique (sufficient | gaps[])."""
    # Render the current state for the prompt
    sections_blob = _render_sections_for_prompt(state)
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    prompt = prompt_template.replace("{query}", state.query).replace("{sections}", sections_blob)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a research critic. Respond with a SINGLE JSON object and "
                        "NOTHING ELSE - no markdown fences. Schema: "
                        '{"sufficient": bool, "rationale": str, '
                        '"gaps": [{"id": "gap1", "question": "...", '
                        '"tool_hint": "general-web", "rationale": "..."}]}'
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw)
        raw_gaps = data.get("gaps") or []
        gaps: list[SubQuestion] = []
        for i, g in enumerate(raw_gaps):
            if not isinstance(g, dict):
                logger.warning("critic gap %d is not a dict: %r — skipping", i, type(g).__name__)
                continue
            sid = str(g.get("id") or f"critic_gap_{i + 1}")
            hint = str(g.get("tool_hint") or "general-web")
            if hint not in _VALID_TOOL_HINTS:
                hint = "general-web"
            gaps.append(
                SubQuestion(
                    id=sid,
                    question=str(g.get("question") or ""),
                    tool_hint=hint,
                    rationale=str(g.get("rationale") or ""),
                )
            )
        return Critique(
            sufficient=bool(data.get("sufficient", False)),
            gaps=gaps,
            rationale=str(data.get("rationale") or ""),
        )
    except Exception as e:
        logger.warning("critic LLM call failed: %s: %s", type(e).__name__, e)
        # Conservative fallback: if any drafts exist, treat as sufficient.
        # If no drafts, add a synthetic gap so the loop doesn't silently stop.
        if state.drafts:
            return Critique(
                sufficient=True,
                gaps=[],
                rationale=f"critic LLM call failed ({type(e).__name__}); treating as sufficient",
            )
        return Critique(
            sufficient=False,
            gaps=[
                SubQuestion(
                    id="critic_fallback_gap",
                    question=f"Re-analyze the research; critic LLM failed ({type(e).__name__})",
                    tool_hint="general-web",
                    rationale="critic failure forced a synthetic gap",
                )
            ],
            rationale=f"critic LLM call failed ({type(e).__name__}); synthetic gap added",
        )


def _render_sections_for_prompt(state: ResearchState) -> str:
    lines: list[str] = []
    for sq in state.plan.sub_questions:
        draft = state.drafts.get(sq.id)
        cites = state.sections.get(sq.id, [])
        lines.append(f"### Sub-question: {sq.question}")
        lines.append(f"_tool_hint: {sq.tool_hint}; rationale: {sq.rationale}_")
        if draft:
            lines.append("\nDraft answer:\n")
            lines.append(draft[:2000])
        else:
            lines.append("\n(no draft produced)")
        if cites:
            lines.append(f"\nCitations ({len(cites)}):")
            for c in cites:
                lines.append(f"- [{c.title or c.url}]({c.url})")
        lines.append("")
    return "\n".join(lines)


__all__ = ["review"]
