"""Deep Research Agent - async-first multi-path research agent.

Public API re-exports:

    from deep_research import run_research, AgentTopConfig, Report, Citation
"""

from deep_research.agent import run_research
from deep_research.config import AgentTopConfig, BlogSearchConfig, PDLConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.state import (
    AcademicState,
    Citation,
    CitationGraph,
    ClassifiedQuery,
    Critique,
    PaperAnalysis,
    PaperNode,
    QueryPlan,
    Report,
    ResearchPlan,
    ResearchState,
    SourceAnalysis,
    SubQuestion,
    ToolName,
)

__version__ = "0.1.0"

__all__ = [
    "AcademicState",
    "AgentTopConfig",
    "BlogSearchConfig",
    "Citation",
    "CitationGraph",
    "ClassifiedQuery",
    "Critique",

    "LibraryWriter",
    "NullLibraryWriter",
    "PDLConfig",
    "PaperAnalysis",
    "PaperNode",
    "QueryPlan",
    "Report",
    "ResearchPlan",
    "ResearchState",
    "SourceAnalysis",
    "SubQuestion",
    "ToolName",
    "__version__",
    "run_research",
]
