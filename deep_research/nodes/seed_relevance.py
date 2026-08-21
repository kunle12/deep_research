"""Seed relevance pre-gate — batch LLM filter for academic-path seed candidates.

P-relevance (implemented): before any PDF download / full analysis, score the
seed candidates (title + abstract only) against the query in ONE LLM call and
drop clearly off-topic seeds. Prevents keyword-overlap papers from a different
field consuming `max_papers` slots, PDF/vision compute, or library storage.

Non-fatal: any failure keeps all seeds (log warning) — the per-paper relevance
gate in the academic loop remains the backstop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from deep_research.llm.router import LLMClientLike
from deep_research.util import coerce_float

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "seed_relevance.txt"

# Cap so a huge seed set cannot blow the context window.
_MAX_SEEDS = 40
# Per-seed abstract cap (chars) for the prompt table.
_ABSTRACT_CAP = 300


def _build_prompt(query: str, candidates: list[tuple[str, str, str]]) -> str:
    """Build the prompt text from (key, title, abstract) candidate triples."""
    template = _PROMPT_FILE.read_text(encoding="utf-8")
    lines: list[str] = []
    for key, title, abstract in candidates:
        ab = (abstract or "").strip().replace("\n", " ")
        if len(ab) > _ABSTRACT_CAP:
            ab = ab[:_ABSTRACT_CAP] + "…"
        lines.append(f"- [{key}] {title or key}: {ab}")
    return template.replace("{query}", query).replace("{candidates}", "\n".join(lines) or "(none)")


async def _score_seeds(
    query: str,
    seeds: list[Any],
    client: LLMClientLike,
    model: str,
) -> dict[str, float]:
    """One LLM call returning {seed_key: relevance_score}. Seed keys are the
    arxiv id (version-stripped) or the scholar synthetic id."""
    candidates = [
        (seed.arxiv_id, getattr(seed, "title", ""), getattr(seed, "abstract", "")) for seed in seeds
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are a research topic filter. Respond with a SINGLE JSON object "
                "and NOTHING ELSE - no markdown fences, no surrounding text."
            ),
        },
        {"role": "user", "content": _build_prompt(query, candidates)},
    ]
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)
    scores_raw = data.get("scores") if isinstance(data, dict) else None
    if not isinstance(scores_raw, dict):
        return {}
    scores: dict[str, float] = {}
    for key, val in scores_raw.items():
        f = coerce_float(val, -1.0)
        if f >= 0.0:
            scores[str(key)] = max(0.0, min(1.0, f))
    return scores


async def filter_relevant_seeds(
    query: str,
    seeds: list[Any],
    client: LLMClientLike,
    model: str,
    threshold: float,
    *,
    enabled: bool = True,
) -> list[Any]:
    """Drop seeds that are clearly off-topic for *query*.

    Returns the kept seeds (original objects). When disabled, the LLM call
    fails, or the gate is not applicable, all seeds are returned unchanged.
    """
    if not enabled or threshold <= 0 or not seeds or not (query or "").strip():
        return list(seeds)
    if len(seeds) > _MAX_SEEDS:
        logger.warning(
            "seed relevance gate: %d seeds exceed cap %d; gating a capped subset",
            len(seeds),
            _MAX_SEEDS,
        )
    to_score = seeds[:_MAX_SEEDS]
    try:
        scores = await _score_seeds(query, to_score, client, model)
    except Exception as e:
        logger.warning(
            "seed relevance gate failed (%s: %s); keeping all %d seeds",
            type(e).__name__,
            e,
            len(seeds),
        )
        return list(seeds)

    if not scores:
        logger.warning("seed relevance gate returned no scores; keeping all seeds")
        return list(seeds)

    kept: list[Any] = []
    dropped: list[Any] = []
    for seed in to_score:
        score = scores.get(seed.arxiv_id)
        # A seed missing from the response is kept (lenient — we only drop
        # explicitly-low scores, mirroring the per-paper analyzer).
        if score is None or score >= threshold:
            kept.append(seed)
        else:
            dropped.append(seed)
    # Anything beyond the scored cap is kept untouched.
    kept.extend(seeds[_MAX_SEEDS:])

    if dropped:
        logger.info(
            "seed relevance gate: dropped %d off-topic seed(s) (threshold %.2f): %s",
            len(dropped),
            threshold,
            ", ".join(d.arxiv_id for d in dropped),
        )
    return kept


__all__ = ["filter_relevant_seeds"]
