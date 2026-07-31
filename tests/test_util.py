"""Tests for shared coercion/parsing helpers in deep_research.util."""

from __future__ import annotations

import pytest

from deep_research.util import coerce_float, strip_arxiv_version


class TestCoerceFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.9, 0.9),
            ("0.9", 0.9),
            (1, 1.0),
            (0, 0.0),
            (True, 1.0),
            (False, 0.0),
        ],
    )
    def test_accepts_numeric_inputs(self, value, expected) -> None:
        assert coerce_float(value, 0.5) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "high",
            "not-a-number",
            [],
            {},
        ],
    )
    def test_falls_back_to_default(self, value) -> None:
        assert coerce_float(value, 0.6) == 0.6


class TestStripArxivVersion:
    def test_no_version(self) -> None:
        assert strip_arxiv_version("2401.12345") == "2401.12345"

    def test_versioned(self) -> None:
        assert strip_arxiv_version("2401.12345v3") == "2401.12345"

    def test_old_style_id(self) -> None:
        assert strip_arxiv_version("cs.LG/0702001v10") == "cs.LG/0702001"
