"""Tiny coverage tests for trivial modules (import-time statements)."""

from __future__ import annotations


def test_base_tools_module_imports() -> None:
    """tools/base.py is a backward-compat re-export module."""
    from deep_research.tools.base import Tool, ToolResult

    assert Tool is not None
    assert ToolResult is not None


def test_main_module_imports() -> None:
    """`python -m deep_research` entrypoint must import cleanly."""
    import deep_research.__main__

    assert deep_research.__main__.__name__ == "deep_research.__main__"
