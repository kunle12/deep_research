"""Tools base — re-exports from llm.tool_loop for backward compat."""

from __future__ import annotations

from deep_research.llm.tool_loop import ToolFunc as Tool
from deep_research.llm.tool_loop import ToolResult

__all__ = ["Tool", "ToolResult"]
