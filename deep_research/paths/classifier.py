"""Classifier — single LLM call to route query to one of the four paths.

Returns a `ClassifiedQuery` (pydantic model) that the agent dispatches on.
"""

from __future__ import annotations

import asyncio
import json
import logging

from deep_research.llm.router import LLMClientLike
from deep_research.state import ClassifiedQuery, QueryPlan
from deep_research.util import load_prompt_template

logger = logging.getLogger(__name__)


async def classify_query(
    query: str,
    client: LLMClientLike,
    model: str,
) -> ClassifiedQuery:
    """Make a single LLM call to classify `query`.

    Falls back to `deep` if the LLM response can't be parsed (log warning).
    """
    # Blocking read off the event loop (only happens once, on cold start).
    prompt_template = await asyncio.to_thread(load_prompt_template, "classifier")
    prompt = prompt_template.replace("{query}", query)

    messages = [
        {
            "role": "system",
            "content": "You are a research routing classifier. Return valid JSON only.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        path_str = str(data.get("path", "deep")).lower()
        if path_str not in {"quick", "deep", "academic", "applied", "unclear", "url_source"}:
            path_str = "deep"
        return ClassifiedQuery(
            path=QueryPlan(path_str),
            rationale=str(data.get("rationale", "")),
            search_hint=str(data.get("search_hint", "") or query),
            breadth_hint=int(data.get("breadth_hint") or 0),
            depth_hint=int(data.get("depth_hint") or 0),
            arxiv_first=bool(data.get("arxiv_first", False)),
            clarifying_questions=list(data.get("clarifying_questions", []) or []),
        )
    except Exception as e:
        logger.warning("classifier fallback to deep: %s: %s", type(e).__name__, e)
        return ClassifiedQuery(
            path=QueryPlan.deep,
            rationale=f"Classifier LLM call failed ({type(e).__name__}); defaulting to deep.",
            search_hint=query,
        )


__all__ = ["classify_query"]
