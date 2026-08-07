"""Tiny coverage tests for trivial modules (import-time statements)."""

from __future__ import annotations


def test_main_module_imports() -> None:
    """`python -m deep_research` entrypoint must import cleanly."""
    import deep_research.__main__

    assert deep_research.__main__.__name__ == "deep_research.__main__"
