"""Critic node — reviews state, decides whether to iterate again (deep path).

P3: implemented via `prompts/critic.txt`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from deep_research.llm.router import LLMClientLike
from deep_research.nodes.paper_analysis import format_deep_analysis_context
from deep_research.state import (
    Critique,
    PaperAnalysisRequest,
    ResearchState,
    SubQuestion,
)
from deep_research.util import ARXIV_ID_RE, VALID_TOOL_HINTS, coerce_float, load_prompt_template

logger = logging.getLogger(__name__)

# Bound the candidate table so a huge citation list cannot blow the prompt.
_MAX_CANDIDATES = 40


async def review(state: ResearchState, client: LLMClientLike, model: str) -> Critique:
    """Make the critic LLM call. Returns a Critique (sufficient | gaps[])."""
    # Render the current state for the prompt
    sections_blob = _render_sections_for_prompt(state)
    candidates = _render_paper_candidates(state)
    prompt_template = load_prompt_template("critic")
    prompt = (
        prompt_template.replace("{query}", state.query)
        .replace("{sections}", sections_blob)
        .replace("{candidates}", candidates)
    )
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
        raw_gaps = data.get("gaps") if isinstance(data, dict) else []
        gaps: list[SubQuestion] = []
        for i, g in enumerate(raw_gaps):
            if not isinstance(g, dict):
                logger.warning("critic gap %d is not a dict: %r — skipping", i, type(g).__name__)
                continue
            sid = str(g.get("id") or f"critic_gap_{i + 1}")
            hint = str(g.get("tool_hint") or "general-web")
            if hint not in VALID_TOOL_HINTS:
                hint = "general-web"
            gaps.append(
                SubQuestion(
                    id=sid,
                    question=str(g.get("question") or ""),
                    tool_hint=hint,
                    rationale=str(g.get("rationale") or ""),
                )
            )
        papers = _parse_paper_requests(
            data.get("papers_to_analyze") if isinstance(data, dict) else []
        )
        return Critique(
            sufficient=bool(data.get("sufficient", False)),
            gaps=gaps,
            rationale=str(data.get("rationale") or ""),
            papers_to_analyze=papers,
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

    # Phase 2: deep paper analyses digest — the critic uses these to propose
    # follow-up gaps and nominate key references for the next analysis round.
    digest = format_deep_analysis_context(state.deep_analyses)
    if digest:
        lines.append("\n" + digest + "\n")
    return "\n".join(lines)


def _render_paper_candidates(state: ResearchState) -> str:
    """Render arXiv paper candidates for critic-driven deep PDF analysis.

    Sources: the arxiv citations the researchers actually used (they carry
    the abstract in ``snippet`` plus authors/year when known). Each candidate
    shows how many distinct sub-question drafts reference it — the cheap
    "referenced noticeably" signal. Papers already analyzed or already
    requested this run are excluded.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for c in state.citations.values():
        aid = (c.arxiv_id or "").strip()
        if not ARXIV_ID_RE.match(aid):
            continue
        if aid in state.deep_analyses or aid in state.deep_analysis_requested:
            continue
        entry = by_id.get(aid)
        if entry is None:
            by_id[aid] = {
                "arxiv_id": aid,
                "title": c.title or aid,
                "authors": ", ".join(c.authors[:2]) if c.authors else "",
                "year": c.year or "",
                "abstract": (c.snippet or "").strip()[:300],
                "cited_by": c.cited_by_count,
                "referenced_by_count": 0,
            }
        elif c.cited_by_count and not entry["cited_by"]:
            entry["cited_by"] = c.cited_by_count

    # Phase 2: key references of already-analyzed papers become candidates
    # for the next analysis round (paper-of-paper analysis).
    for _aid, analysis in state.deep_analyses.items():
        for ref in analysis.key_references:
            rid = (ref.arxiv_id or "").strip()
            if not ARXIV_ID_RE.match(rid) or rid in by_id:
                continue
            if rid in state.deep_analyses or rid in state.deep_analysis_requested:
                continue
            by_id[rid] = {
                "arxiv_id": rid,
                "title": ref.title or rid,
                "authors": ", ".join(ref.authors[:2]) if ref.authors else "",
                "year": ref.year or "",
                "abstract": "(key reference of an analyzed paper)",
                "cited_by": None,
                "referenced_by_count": 0,
            }

    if not by_id:
        return "(no arxiv paper candidates)"

    counts = _count_references_by_draft(state, set(by_id))
    for aid, entry in by_id.items():
        entry["referenced_by_count"] = counts.get(aid, 0)

    entries = sorted(
        by_id.values(),
        key=lambda e: (e["referenced_by_count"], e["cited_by"] or 0),
        reverse=True,
    )[:_MAX_CANDIDATES]

    lines: list[str] = []
    for e in entries:
        authors = f" by {e['authors']}" if e["authors"] else ""
        year = f" ({e['year']})" if e["year"] else ""
        cited = f" | cited_by={e['cited_by']}" if e.get("cited_by") else ""
        lines.append(
            f"- arxiv:{e['arxiv_id']} | {e['title']}{authors}{year}"
            f" | referenced_in_drafts={e['referenced_by_count']}{cited}"
        )
        if e["abstract"]:
            lines.append(f"  Abstract: {e['abstract']}")
    return "\n".join(lines)


def _count_references_by_draft(state: ResearchState, arxiv_ids: set[str]) -> dict[str, int]:
    """Count distinct sub-question drafts that mention each arxiv id/URL."""
    counts = {aid: 0 for aid in arxiv_ids}
    drafts = list(state.drafts.values())
    for aid in arxiv_ids:
        url = f"https://arxiv.org/abs/{aid}"
        counts[aid] = sum(1 for d in drafts if url in d or aid in d)
    return counts


def _parse_paper_requests(raw: Any) -> list[PaperAnalysisRequest]:
    """Validate + dedupe the critic's paper-analysis proposals.

    Drops non-arxiv / malformed ids, clamps priority to [0, 1], and keeps
    the highest-priority proposal per arxiv id.
    """
    if not isinstance(raw, list):
        return []
    seen: dict[str, PaperAnalysisRequest] = {}
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            continue
        aid = str(p.get("arxiv_id") or "").strip()
        if not ARXIV_ID_RE.match(aid):
            logger.warning("critic paper proposal %d has invalid arxiv_id %r — skipping", i, aid)
            continue
        reason = str(p.get("reason") or "other")
        if reason not in {
            "abstract_relevance",
            "notable_citations",
            "foundational",
            "other",
        }:
            reason = "other"
        req = PaperAnalysisRequest(
            arxiv_id=aid,
            rationale=str(p.get("rationale") or "")[:300],
            reason=reason,
            priority_score=max(0.0, min(1.0, coerce_float(p.get("priority_score"), 0.5))),
            expected_title=str(p.get("expected_title") or "")[:200],
        )
        cur = seen.get(aid)
        if cur is None or req.priority_score > cur.priority_score:
            seen[aid] = req
    return list(seen.values())


__all__ = ["review"]
