"""JSON renderer - emit Report (incl. citation graph) as JSON for microservice use."""

from __future__ import annotations

from deep_research.state import Report


def render_report_json(report: Report) -> str:
    return report.model_dump_json(indent=2, exclude_none=True)


def parse_report_json(s: str) -> Report:
    return Report.model_validate_json(s)


__all__ = ["parse_report_json", "render_report_json"]
