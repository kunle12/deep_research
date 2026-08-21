"""State and core data models for the deep research agent.

All schemas are pydantic models with `extra="forbid"` so config drift
is caught at load time rather than mid-run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deep_research.library.storage.rows import GlossaryEntry

logger = logging.getLogger(__name__)


# Machine-readable prefix for ToolResult errors that mean "source is
# unavailable / blocked". Tools emit `BLOCKED:<reason>[:<detail>] (<status>)`
# (e.g. `BLOCKED:bot_detection:cloudflare (403)`, `BLOCKED:rate_limited (429)`,
# `BLOCKED:not_found (404)`, `BLOCKED:http_error (500)`). Callers use
# `startswith(BLOCKED_PREFIX)` to branch: no retries, no circumvention,
# record the source as unavailable.
BLOCKED_PREFIX = "BLOCKED:"


class ToolName(str, Enum):
    web_search = "web_search"
    fetch_page = "fetch_page"
    browser = "browser"
    arxiv = "arxiv"
    pdf = "pdf"
    reddit = "reddit"
    scholar = "scholar"


class Citation(BaseModel):
    """A single source we're citing in the final report."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    snippet: str = ""
    source_type: Literal["web", "arxiv", "reddit", "html", "pdf", "blog", "scholar"] = "web"
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    # Optional provenance: which tool surfaced this citation?
    discovered_by: ToolName | None = None
    # Optional: for arxiv, the paper metadata block
    arxiv_id: str | None = None
    authors: list[str] = Field(default_factory=list)
    # Optional: free-PDF side link surfaced by Google Scholar (may differ from `url`)
    pdf_url: str | None = None
    # Optional: DOI when known (e.g., from Scholar or Crossref)
    doi: str | None = None
    # Optional: publication year (Scholar / Crossref)
    year: int | None = None
    # Optional: venue / publication name (e.g., "Nature Materials", "ICML 2024")
    venue: str | None = None
    # Optional: Google Scholar citation count
    cited_by_count: int | None = None


class BlockedSource(BaseModel):
    """A source that could not be retrieved (bot detection, rate limit, fetch error).

    Recorded so skipped sources are visible in the final report instead of
    disappearing silently. `reason` is the raw `BLOCKED:...` error emitted by
    the fetch/browser tools.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    reason: str = ""
    blocked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Which sub-question was researching this source (deep path only).
    sub_question: str | None = None


class SubQuestion(BaseModel):
    """One node of the planner's research plan."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    # Hint for which tools to prioritize for this sub-question.
    # e.g., "arxiv", "reddit", "general-web", "browser-required"
    tool_hint: str = "general-web"
    depth: int = 0  # recursion depth in academic mode
    rationale: str = ""  # why this sub-question matters
    # For academic-mode sub-questions spawned by recursive ref mining:
    parent_arxiv_id: str | None = None
    refinement_depth: int = 0


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub_questions: list[SubQuestion] = Field(default_factory=list)
    breadth: int = 0  # number of sub-questions at depth 0
    max_depth: int = 0


class Critique(BaseModel):
    """Output of the critic node — decides whether to iterate again."""

    model_config = ConfigDict(extra="forbid")

    sufficient: bool
    gaps: list[SubQuestion] = Field(default_factory=list)
    rationale: str = ""
    # arXiv papers the critic wants full PDF analysis for (deep path).
    papers_to_analyze: list[PaperAnalysisRequest] = Field(default_factory=list)


class PaperAnalysisRequest(BaseModel):
    """A critic-selected arXiv paper proposed for full PDF deep analysis."""

    model_config = ConfigDict(extra="forbid")

    arxiv_id: str
    rationale: str = ""
    reason: Literal["abstract_relevance", "notable_citations", "foundational", "other"] = "other"
    priority_score: float = Field(default=0.5, ge=0.0, le=1.0)
    expected_title: str = ""


class ResearchState(BaseModel):
    """Mutable state threaded through the deep-research loop.

    This is the only state object carried between planner / researcher /
    critic / writer for the `deep` path. `academic` path uses local
    variables; `quick` and `url_source` paths use local state.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    plan: ResearchPlan = Field(default_factory=ResearchPlan)
    # Per sub-question accumulated citations + summaries
    sections: dict[str, list[Citation]] = Field(default_factory=dict)
    # Per sub-question draft text (writer's intermediate output)
    drafts: dict[str, str] = Field(default_factory=dict)
    # Global citation dedup table
    citations: dict[str, Citation] = Field(default_factory=dict)
    iteration: int = 0
    pending_refinements: list[SubQuestion] = Field(default_factory=list)
    # Critic-selected full PDF analyses (arxiv_id -> PaperAnalysis). The
    # writer weaves these into the final report.
    deep_analyses: dict[str, PaperAnalysis] = Field(default_factory=dict)
    # arxiv ids already selected for deep analysis this run (prevents
    # re-selecting the same paper across critic iterations).
    deep_analysis_requested: list[str] = Field(default_factory=list)
    # Sources skipped during research (bot detection / fetch errors). Surfaced
    # as an "Unavailable Sources" section in the final report.
    blocked_sources: list[BlockedSource] = Field(default_factory=list)

    def is_covered(self, sub_q: SubQuestion) -> bool:
        """A sub-question is 'covered' if it has a draft (with or without citations)."""
        return sub_q.id in self.drafts

    @staticmethod
    def _dedup_citations(
        existing: dict[str, Citation], new_citations: list[Citation]
    ) -> dict[str, Citation]:
        """Dedup by url, keep highest confidence."""
        for c in new_citations:
            cur = existing.get(c.url)
            if cur is None or cur.confidence_score < c.confidence_score:
                existing[c.url] = c
        return existing

    def absorb_citations(self, new_citations: list[Citation]) -> None:
        self._dedup_citations(self.citations, new_citations)

    def absorb_section(self, sq_id: str, citations: list[Citation], draft: str) -> None:
        self.sections[sq_id] = citations
        self.drafts[sq_id] = draft
        self.absorb_citations(citations)

    def absorb_refinements(self, refinements: list[SubQuestion]) -> None:
        """Deduplicate and absorb refinements emitted by a researcher."""
        existing_qs = {sq.question.strip().lower() for sq in self.plan.sub_questions}
        existing_qs.update(sq.question.strip().lower() for sq in self.pending_refinements)
        for r in refinements:
            key = r.question.strip().lower()
            if r.question and key not in existing_qs:
                self.pending_refinements.append(r)
                existing_qs.add(key)

    def absorb_blocked_sources(
        self, sources: list[BlockedSource], sq_id: str | None = None
    ) -> None:
        """Record skipped sources, dedup by URL, annotating the sub-question."""
        existing = {s.url for s in self.blocked_sources}
        for s in sources:
            if not s.url or s.url in existing:
                continue
            if sq_id and not s.sub_question:
                s.sub_question = sq_id
            self.blocked_sources.append(s)
            existing.add(s.url)

    def flush_refinements(self, max_total: int | None = None) -> list[SubQuestion]:
        """Move pending refinements into the plan and return them.

        When *max_total* caps how many move into the plan this round, the
        overflow stays pending so the next iteration can flush them too —
        it is never silently dropped.
        """
        if max_total is not None and len(self.pending_refinements) > max_total:
            logger.info(
                "flush_refinements: capping %d → %d; %d held for next round",
                len(self.pending_refinements),
                max_total,
                len(self.pending_refinements) - max_total,
            )
            flushed = list(self.pending_refinements[:max_total])
            self.pending_refinements = self.pending_refinements[max_total:]
        else:
            flushed = list(self.pending_refinements)
            self.pending_refinements.clear()
        self.plan.sub_questions.extend(flushed)
        return flushed


class PaperNode(BaseModel):
    """A node in the academic citation graph."""

    model_config = ConfigDict(extra="forbid")

    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    depth: int = 0
    parent_arxiv_id: str | None = None
    rationale: str = ""  # why this paper was enqueued
    # For scholar-only nodes: the URL and DOI from the Scholar hit
    url: str = ""
    doi: str | None = None
    pdf_url: str | None = None
    venue: str | None = None
    year: int | None = None


class PaperAnalysis(BaseModel):
    """LLM output of `analyze_paper` (academic mode)."""

    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    relevance_to_query: str = ""
    # LLM-scored 0..1: how directly the paper is about the query's topic.
    # Default 1.0 so papers that predate the field (or error fallbacks that
    # omit it) are kept; the academic gate excludes only explicitly-low scores.
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0)
    methodology: str = ""
    limitations: list[str] = Field(default_factory=list)
    is_key_reference: bool = False
    key_references: list[PaperNode] = Field(default_factory=list)
    extraction_text: str = ""
    figure_descriptions: list[str] = Field(default_factory=list)


class CitationGraph(BaseModel):
    """The directed graph of paper → cited papers, used in academic mode."""

    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, PaperNode] = Field(default_factory=dict)  # arxiv_id → node
    analyses: dict[str, PaperAnalysis] = Field(default_factory=dict)
    edges: dict[str, list[str]] = Field(default_factory=dict)  # parent_id → [child_id]

    def has(self, arxiv_id: str) -> bool:
        return arxiv_id in self.nodes

    def add_node(self, node: PaperNode) -> None:
        if node.arxiv_id not in self.nodes:
            self.nodes[node.arxiv_id] = node
            self.edges.setdefault(node.arxiv_id, [])

    def add_edge(self, parent_id: str, child_id: str) -> None:
        if child_id not in self.edges.get(parent_id, []):
            self.edges.setdefault(parent_id, []).append(child_id)


class SourceAnalysis(BaseModel):
    """LLM output of `analyze_source` (url_source mode)."""

    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    key_claims: list[dict] = Field(default_factory=list)
    # each item: {"claim": str, "evidence": str, "page_or_section": str}
    methodology: str | None = None  # arxiv only
    limitations: list[str] | None = None
    relevance_to_query: str | None = None
    # LLM-scored 0..1: how directly this source is ABOUT the user's query's
    # topic. Defaults to 1.0 when there is no explicit user query (an explicit
    # URL is assumed on-topic unless the query says otherwise); gates `attach`.
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    follow_ups: list[dict] = Field(default_factory=list)
    # each: {"topic": str, "why": str}
    gaps: list[str] = Field(default_factory=list)


class Report(BaseModel):
    """Final structured output of any `run_research` invocation."""

    model_config = ConfigDict(extra="forbid")

    markdown: str = ""
    citations: list[Citation] = Field(default_factory=list)
    # Path that produced this report: "quick" | "deep" | "academic" | "url_source"
    # | "url_source_with_followup" | "unclear"
    path: str = ""
    # For academic mode: the citation graph (may be empty otherwise)
    citation_graph: CitationGraph | None = None
    # For classifier's "unclear" path: questions to ask the user
    clarifying_questions: list[str] = Field(default_factory=list)
    # Classifier rationale (always populated when classifier runs)
    classifier_rationale: str = ""
    # For URL-source mode follow-up: the follow-up sub-questions used
    follow_up_sub_questions: list[SubQuestion] = Field(default_factory=list)
    # Iterations completed (for deep / academic)
    iterations: int = 0
    # Glossary entries extracted from this report
    glossary_entries: list[GlossaryEntry] = Field(default_factory=list)
    # Sources that could not be retrieved (bot detection / fetch errors);
    # rendered as an "Unavailable Sources" section.
    blocked_sources: list[BlockedSource] = Field(default_factory=list)
    # For library archival
    created_at: datetime | None = None
    query: str = ""


class QueryPlan(str, Enum):
    """Classifier routing decision."""

    quick = "quick"
    deep = "deep"
    academic = "academic"
    url_source = "url_source"
    applied = "applied"
    unclear = "unclear"


class ClassifiedQuery(BaseModel):
    """Classifier output."""

    model_config = ConfigDict(extra="forbid")

    path: QueryPlan
    rationale: str = ""
    # When path == "quick": the search hint to send to web search
    search_hint: str = ""
    # When path == "deep": suggested breadth/depth
    breadth_hint: int = 0
    depth_hint: int = 0
    # When path == "academic": should arxiv be tried first
    arxiv_first: bool = False
    # When path == "unclear": clarifying questions for the user
    clarifying_questions: list[str] = Field(default_factory=list)


__all__ = [
    "BLOCKED_PREFIX",
    "BlockedSource",
    "Citation",
    "CitationGraph",
    "ClassifiedQuery",
    "Critique",
    "PaperAnalysis",
    "PaperAnalysisRequest",
    "PaperNode",
    "QueryPlan",
    "Report",
    "ResearchPlan",
    "ResearchState",
    "SourceAnalysis",
    "SubQuestion",
    "ToolName",
]
