"""Deep Research Agent - async-first multi-path research agent.

Public API re-exports:

    from deep_research import run_research, AgentTopConfig, Report, Citation
"""

import sys
import sysconfig

# Ensure the virtual environment's site-packages take precedence over
# system dist-packages
_venv_paths = sysconfig.get_paths(scheme="venv")
_venv_site = _venv_paths.get("purelib")
if _venv_site and _venv_site in sys.path:
    sys.path.insert(0, sys.path.pop(sys.path.index(_venv_site)))

from deep_research.agent import run_research  # noqa: E402  (imports after sys.path fixup)
from deep_research.config import AgentTopConfig, BlogSearchConfig, PDLConfig  # noqa: E402
from deep_research.library.writer import LibraryWriter, NullLibraryWriter  # noqa: E402
from deep_research.state import (  # noqa: E402
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

__all__ = [
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
    "run_research",
]
