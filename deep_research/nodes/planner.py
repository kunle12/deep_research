"""Planner node — decomposes user query into research sub-questions.

P3: implemented via `prompts/planner.txt` LLM call with structured JSON output.
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from deep_research.state import ResearchPlan, SubQuestion
from deep_research.util import VALID_TOOL_HINTS, load_prompt_template

logger = logging.getLogger(__name__)


async def plan(
    query: str,
    client: AsyncOpenAI,
    model: str,
    breadth: int = 6,
) -> ResearchPlan:
    """Make one LLM call to generate a research plan."""
    prompt_template = load_prompt_template("planner")
    # Validate tool_hint vocabulary client-side so we don't surprise downstream.
    valid_hints = VALID_TOOL_HINTS

    prompt = prompt_template.replace("{max_subquestions}", str(breadth)).replace("{query}", query)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a research planner. Respond with a SINGLE JSON object and "
                        "NOTHING ELSE - no markdown fences. Schema: "
                        '{"sub_questions": [{"id": "sq1", "question": "...", '
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
        raw_subs = data.get("sub_questions") if isinstance(data, dict) else []
        sub_qs: list[SubQuestion] = []
        for i, sq in enumerate(raw_subs[:breadth]):
            sid = str(sq.get("id") or f"sq{i + 1}")
            hint = str(sq.get("tool_hint") or "general-web")
            if hint not in valid_hints:
                hint = "general-web"
            sub_qs.append(
                SubQuestion(
                    id=sid,
                    question=str(sq.get("question") or ""),
                    tool_hint=hint,
                    rationale=str(sq.get("rationale") or ""),
                )
            )
        if not sub_qs:
            # Fallback: produce a single sub-question = the original query
            sub_qs.append(
                SubQuestion(
                    id="sq1",
                    question=query,
                    tool_hint="general-web",
                    rationale="planner produced no sub-questions; using original query",
                )
            )
        return ResearchPlan(sub_questions=sub_qs, breadth=len(sub_qs), max_depth=0)
    except Exception as e:
        logger.warning("planner LLM call failed: %s: %s", type(e).__name__, e)
        return ResearchPlan(
            sub_questions=[
                SubQuestion(
                    id="sq1",
                    question=query,
                    tool_hint="general-web",
                    rationale=f"planner failed ({type(e).__name__}); using original query",
                )
            ],
            breadth=1,
            max_depth=0,
        )


__all__ = ["plan"]
