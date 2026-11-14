from __future__ import annotations

import logging

from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.router import LLMClientLike
from deep_research.state import ResearchState
from deep_research.util import load_prompt_template, utc_today_str

logger = logging.getLogger(__name__)

# Character budgets for the writer prompt. The old hard truncation at 12k
# could silently drop entire sub-question drafts. We instead reserve room for
# every heading and distribute the remaining budget across drafts, so a long
# research plan always reaches the writer with all its coverage intact.
_MAX_SECTIONS_CHARS = 24000
_MAX_DRAFT_CHARS = 3000
_MAX_ANALYSIS_CHARS = 1500
_MIN_DRAFT_CHARS = 200
_TRUNCATION_NOTE = "\n\n[... truncated to fit the writer context budget]"


def _clip(text: str, limit: int) -> str:
    """Clip *text* to *limit* chars, adding a visible truncation marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + _TRUNCATION_NOTE


def _sections_budget(max_context_tokens: int | None) -> int:
    """Choose a sections char budget from the model context window.

    Keeps the historical 24k cap for large contexts and shrinks proportionally
    for small contexts. This is deliberately conservative: the writer still
    needs room for the prompt, citation table, and generated report.
    """
    if not max_context_tokens or max_context_tokens <= 0:
        return _MAX_SECTIONS_CHARS
    # ~4 chars/token; reserve ~35% of the window for the section digest.
    estimated = int(max_context_tokens * 4 * 0.35)
    return max(4000, min(_MAX_SECTIONS_CHARS, estimated))


async def write(
    state: ResearchState,
    client: LLMClientLike,
    model: str,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
    max_context_tokens: int | None = None,
) -> str:
    """Call the writer LLM and return Markdown. Glossary extraction is handled separately in agent.py."""
    sections_blob = _render_sections_for_prompt(
        state, max_chars=_sections_budget(max_context_tokens)
    )
    citations_blob = _render_citations_for_prompt(state)
    prompt_template = load_prompt_template("writer")
    prompt = (
        prompt_template.replace("{query}", state.query)
        .replace("{sections}", sections_blob)
        .replace("{citations}", citations_blob)
        .replace("{today}", utc_today_str())
    )
    try:
        system_msg = (
            "You are the final research report writer. Output a single Markdown "
            "document (the report). Do not wrap in code fences. "
            "Cite sources inline using standard markdown links like [source](https://example.com/source). "
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


def _render_sections_for_prompt(
    state: ResearchState,
    max_chars: int = _MAX_SECTIONS_CHARS,
) -> str:
    """Render deep analyses + every drafted sub-question for the writer.

    Deep analyses are rendered first and individually capped so they can never
    be dropped. Drafts are then allocated a share of the remaining budget; all
    headings are always preserved, and only individual draft bodies are clipped
    when the plan is very large. This replaces the old whole-blob truncation
    that silently dropped later sub-questions.
    """
    analysis_blocks: list[str] = []
    if state.deep_analyses:
        for aid, a in state.deep_analyses.items():
            lines: list[str] = [f"### {a.title or aid} (arxiv:{aid})"]
            if a.summary:
                lines.append(f"Summary: {a.summary[:600]}")
            if a.key_findings:
                lines.append("Key findings:")
                for f in a.key_findings[:5]:
                    lines.append(f"- {str(f)[:200]}")
            if a.relevance_to_query:
                lines.append(f"Relevance: {a.relevance_to_query[:300]}")
            if a.limitations:
                lines.append(
                    "Limitations: " + "; ".join(str(x)[:100] for x in a.limitations[:3])[:300]
                )
            analysis_blocks.append(_clip("\n".join(lines), _MAX_ANALYSIS_CHARS))

    analysis_text = ""
    if analysis_blocks:
        analysis_text = "## Deep paper analyses\n\n" + "\n\n".join(analysis_blocks) + "\n\n"

    evidence_text = ""
    if state.evidence:
        evidence_lines: list[str] = ["## Evidence table", ""]
        for e in state.evidence[:40]:
            stance = f" [{e.stance}]" if e.stance != "supports" else ""
            evidence_lines.append(f"- Claim: {e.claim}{stance}")
            if e.evidence:
                evidence_lines.append(f"  Quote: {e.evidence[:300]}")
            if e.source_url:
                evidence_lines.append(f"  Source: <{e.source_url}>")
        if state.contradictions:
            evidence_lines.append("")
            evidence_lines.append("### Contradictions / conflicts")
            for conflict in state.contradictions[:10]:
                evidence_lines.append(f"- {conflict[:300]}")
        evidence_text = "\n".join(evidence_lines) + "\n\n"

    prefix_text = analysis_text + evidence_text

    drafts: list[tuple[str, str]] = []
    for sq in state.plan.sub_questions:
        draft = state.drafts.get(sq.id)
        if not draft:
            continue
        drafts.append((f"## {sq.question}\n", draft))

    if not prefix_text and not drafts:
        return "(no drafts available)"

    heading_chars = sum(len(h) for h, _ in drafts)
    # Reserve a little slack for separators and the truncation note.
    available = max_chars - len(prefix_text) - heading_chars - 200
    per_draft = _MAX_DRAFT_CHARS
    if drafts:
        per_draft = max(_MIN_DRAFT_CHARS, min(_MAX_DRAFT_CHARS, available // len(drafts)))

    parts: list[str] = []
    if prefix_text:
        parts.append(prefix_text.rstrip())
    for heading, draft in drafts:
        parts.append(heading + _clip(draft, per_draft))

    return "\n\n".join(parts)


def _render_citations_for_prompt(state: ResearchState) -> str:
    lines: list[str] = []
    used = 0
    for c in state.citations.values():
        line = (
            f"- URL: {c.url}\n  Title: {c.title or '(no title)'}\n  Snippet: {c.snippet[:200]}"
        )
        if used + len(line) > 16000:
            lines.append("[... remaining citations omitted for context budget]")
            break
        lines.append(line)
        used += len(line) + 1
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
