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
    Citation,
    Critique,
    PaperAnalysisRequest,
    ResearchState,
    SubQuestion,
)
from deep_research.util import (
    ARXIV_ID_RE,
    VALID_TOOL_HINTS,
    coerce_float,
    load_prompt_template,
    utc_today_str,
)

logger = logging.getLogger(__name__)

# Bound the candidate table so a huge citation list cannot blow the prompt.
_MAX_CANDIDATES = 40

# Critic prompt budgets. Every sub-question heading and its citation list is
# always kept; only draft bodies are clipped when the plan is very large. The
# old fixed 2k-per-draft cap hid most of the evidence from the critic.
_MAX_SECTIONS_CHARS = 24000
_MAX_DRAFT_CHARS = 4000
_MIN_DRAFT_CHARS = 300
_TRUNCATION_NOTE = "\n\n[... truncated to fit the critic context budget]"


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + _TRUNCATION_NOTE


def _sections_budget(max_context_tokens: int | None) -> int:
    if not max_context_tokens or max_context_tokens <= 0:
        return _MAX_SECTIONS_CHARS
    estimated = int(max_context_tokens * 4 * 0.35)
    return max(4000, min(_MAX_SECTIONS_CHARS, estimated))


async def review(
    state: ResearchState,
    client: LLMClientLike,
    model: str,
    max_context_tokens: int | None = None,
    iteration: int | None = None,
) -> Critique:
    """Make the critic LLM call. Returns a Critique (sufficient | gaps[])."""
    # Render the current state for the prompt
    sections_blob = _render_sections_for_prompt(
        state, max_chars=_sections_budget(max_context_tokens)
    )
    candidates = _render_paper_candidates(state)
    prompt_template = load_prompt_template("critic")
    prompt = (
        prompt_template.replace("{query}", state.query)
        .replace("{sections}", sections_blob)
        .replace("{candidates}", candidates)
        .replace("{today}", utc_today_str())
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
                        '"tool_hint": "general-web", "rationale": "..."}], '
                        '"papers_to_analyze": [{"arxiv_id": "2401.12345", '
                        '"rationale": "...", "reason": "abstract_relevance", '
                        '"priority_score": 0.9, "expected_title": "..."}]}'
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
        prefix = f"critic_gap_{iteration + 1}_" if iteration is not None else "critic_gap_"
        for i, g in enumerate(raw_gaps):
            if not isinstance(g, dict):
                logger.warning("critic gap %d is not a dict: %r — skipping", i, type(g).__name__)
                continue
            # Deterministic, iteration-scoped ids. LLM-supplied ids can collide
            # with an existing sub-question id (making the gap appear covered)
            # or with a gap from a previous critic round.
            sid = f"{prefix}{i + 1}"
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
        fallback_id = (
            f"critic_fallback_gap_{iteration + 1}" if iteration is not None else "critic_fallback_gap"
        )
        return Critique(
            sufficient=False,
            gaps=[
                SubQuestion(
                    id=fallback_id,
                    question=f"Re-analyze the research; critic LLM failed ({type(e).__name__})",
                    tool_hint="general-web",
                    rationale="critic failure forced a synthetic gap",
                )
            ],
            rationale=f"critic LLM call failed ({type(e).__name__}); synthetic gap added",
        )


def _render_sections_for_prompt(
    state: ResearchState,
    max_chars: int = _MAX_SECTIONS_CHARS,
) -> str:
    """Render every sub-question for the critic with a shared draft budget.

    Headings, tool hints, and citation lists are always preserved. Only draft
    bodies are clipped, and the clip is applied uniformly across drafts so one
    huge draft cannot consume the whole prompt.
    """
    blocks: list[tuple[SubQuestion, str, list[Citation]]] = []
    for sq in state.plan.sub_questions:
        draft = state.drafts.get(sq.id) or ""
        cites = state.sections.get(sq.id, [])
        blocks.append((sq, draft, cites))

    digest = format_deep_analysis_context(state.deep_analyses)
    digest_text = ("\n" + digest + "\n") if digest else ""

    # Reserve headings + citation lines + digest, then split the remainder
    # evenly across draft bodies.
    fixed_chars = len(digest_text)
    for sq, _draft, cites in blocks:
        fixed_chars += len(f"### Sub-question: {sq.question}") + 80
        fixed_chars += len(f"_tool_hint: {sq.tool_hint}; rationale: {sq.rationale}_") + 1
        for c in cites:
            fixed_chars += len(f"- [{c.title or c.url}]({c.url})") + 1
    available = max_chars - fixed_chars - 200
    per_draft = _MAX_DRAFT_CHARS
    if blocks:
        per_draft = max(_MIN_DRAFT_CHARS, min(_MAX_DRAFT_CHARS, available // len(blocks)))

    lines: list[str] = []
    for sq, draft, cites in blocks:
        lines.append(f"### Sub-question: {sq.question}")
        lines.append(f"_tool_hint: {sq.tool_hint}; rationale: {sq.rationale}_")
        if draft:
            lines.append("\nDraft answer:\n")
            lines.append(_clip(draft, per_draft))
        else:
            lines.append("\n(no draft produced)")
        if cites:
            lines.append(f"\nCitations ({len(cites)}):")
            for c in cites:
                lines.append(f"- [{c.title or c.url}]({c.url})")
        lines.append("")

    if digest_text:
        lines.append(digest_text)

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
