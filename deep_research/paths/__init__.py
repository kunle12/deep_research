"""Paths layer - one runner per routing target."""

from deep_research.paths.academic import academic_research
from deep_research.paths.applied import applied_research
from deep_research.paths.classifier import classify_query
from deep_research.paths.deep import deep_research
from deep_research.paths.quick import quick_search
from deep_research.paths.url_source import query_asks_for_follow_up, url_source

__all__ = [
    "academic_research",
    "applied_research",
    "classify_query",
    "deep_research",
    "query_asks_for_follow_up",
    "quick_search",
    "url_source",
]
