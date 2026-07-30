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


class ResearchState(BaseModel):
    """Mutable state threaded through the deep-research loop.

    This is the only state object carried between planner / researcher /
    critic / writer for the `deep` path. `academic` path uses
    `AcademicState`; `quick` and `url_source` paths use local state.
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

    def is_covered(self, sub_q: SubQuestion) -> bool:
        """A sub-question is 'covered' if it has >=1 cited source AND a draft."""
        return bool(self.sections.get(sub_q.id)) and sub_q.id in self.drafts

    def absorb_citations(self, new_citations: list[Citation]) -> None:
        for c in new_citations:
            # dedup by url, keep highest confidence
            existing = self.citations.get(c.url)
            if existing is None or existing.confidence_score < c.confidence_score:
                self.citations[c.url] = c

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

    def flush_refinements(self, max_total: int | None = None) -> list[SubQuestion]:
        """Move pending refinements into the plan and return them."""
        flushed = list(self.pending_refinements)
        if max_total is not None and len(flushed) > max_total:
            logger.info("flush_refinements: capping %d → %d", len(flushed), max_total)
            flushed = flushed[:max_total]
        self.plan.sub_questions.extend(flushed)
        self.pending_refinements.clear()
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


class AcademicState(BaseModel):
    """Mutable state for the academic-recursion path.

    NOTE: Currently unused — the academic path uses local variables.
    Kept for potential future checkpoint support.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    graph: CitationGraph = Field(default_factory=CitationGraph)
    seed_papers: list[PaperNode] = Field(default_factory=list)
    queue: list[PaperNode] = Field(default_factory=list)
    processed_count: int = 0


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
    "AcademicState",
    "Citation",
    "CitationGraph",
    "ClassifiedQuery",
    "Critique",
    "PaperAnalysis",
    "PaperNode",
    "QueryPlan",
    "Report",
    "ResearchPlan",
    "ResearchState",
    "SourceAnalysis",
    "SubQuestion",
    "ToolName",
]
