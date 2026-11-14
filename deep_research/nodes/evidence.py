"""Evidence extraction node — structured claim/quote table before the writer.

This is the same dedicated, single-purpose JSON pattern that makes glossary
extraction work well: dense synthesized input, an explicit schema, and a
separate post-pass instead of stuffing everything into the writer prompt.
"""

from __future__ import annotations

import json
import logging

from deep_research.llm.router import LLMClientLike
from deep_research.nodes.writer import (
    _render_citations_for_prompt,
    _render_sections_for_prompt,
    _sections_budget,
)
from deep_research.state import EvidenceItem, ResearchState
from deep_research.util import coerce_float, load_prompt_template

logger = logging.getLogger(__name__)

_VALID_STANCES = {"supports", "contradicts", "unclear"}


def _parse_evidence(
    raw: str,
    *,
    max_items: int,
) -> tuple[list[EvidenceItem], list[str]]:
    """Parse the LLM JSON into EvidenceItem rows + contradiction strings."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    items: list[EvidenceItem] = []
    seen_claims: set[str] = set()
    raw_items = data.get("evidence") or []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            if not claim:
                continue
            key = claim.lower()
            if key in seen_claims:
                continue
            seen_claims.add(key)
            stance = str(item.get("stance") or "supports").strip().lower()
            if stance not in _VALID_STANCES:
                stance = "unclear"
            items.append(
                EvidenceItem(
                    claim=claim[:500],
                    evidence=str(item.get("evidence") or "")[:500],
                    source_url=str(item.get("source_url") or "")[:500],
                    stance=stance,
                    confidence=max(0.0, min(1.0, coerce_float(item.get("confidence"), 0.5))),
                    sub_question_id=str(item.get("sub_question_id") or "")[:80],
                )
            )
            if max_items > 0 and len(items) >= max_items:
                break

    contradictions: list[str] = []
    raw_conflicts = data.get("contradictions") or []
    if isinstance(raw_conflicts, list):
        for conflict in raw_conflicts:
            text = str(conflict or "").strip()
            if text:
                contradictions.append(text[:500])

    return items, contradictions


async def extract_evidence(
    state: ResearchState,
    client: LLMClientLike,
    model: str,
    *,
    max_context_tokens: int | None = None,
    max_items: int = 60,
) -> tuple[list[EvidenceItem], list[str]]:
    """Run one dedicated evidence-extraction call.

    Non-fatal: any parse/LLM failure returns empty lists so a finished report
    is never discarded because of an enrichment pass.
    """
    if not state.drafts:
        return [], []

    prompt_template = load_prompt_template("evidence_extract")
    sections = _render_sections_for_prompt(
        state, max_chars=_sections_budget(max_context_tokens)
    )
    citations = _render_citations_for_prompt(state)
    prompt = (
        prompt_template.replace("{query}", state.query)
        .replace("{sections}", sections)
        .replace("{citations}", citations)
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an evidence extraction assistant. Respond with a "
                        "SINGLE JSON object and NOTHING ELSE — no markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("evidence extraction LLM call failed: %s: %s", type(e).__name__, e)
        return [], []

    items, contradictions = _parse_evidence(raw, max_items=max_items)
    logger.info(
        "evidence extraction: %d claim(s), %d contradiction(s)",
        len(items),
        len(contradictions),
    )
    return items, contradictions


__all__ = ["extract_evidence"]
