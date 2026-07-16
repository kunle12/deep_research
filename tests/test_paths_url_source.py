"""Paths-level tests for the url_source follow-up heuristic."""

from __future__ import annotations

from deep_research.paths.url_source import query_asks_for_follow_up


class TestFollowUpHeuristic:
    def test_no_query(self) -> None:
        assert query_asks_for_follow_up("") is False
        assert query_asks_for_follow_up(None) is False  # type: ignore[arg-type]

    def test_neutral_query(self) -> None:
        assert query_asks_for_follow_up("summarize this paper") is False
        assert query_asks_for_follow_up("what does it say?") is False

    def test_gaps_keyword(self) -> None:
        assert query_asks_for_follow_up("what are the gaps in this paper?") is True

    def test_limitations_keyword(self) -> None:
        assert query_asks_for_follow_up("identify the limitations") is True

    def test_counterexamples_keyword(self) -> None:
        assert query_asks_for_follow_up("find counterexamples to its main claim") is True

    def test_verify(self) -> None:
        assert query_asks_for_follow_up("verify the claims in this study") is True

    def test_compare(self) -> None:
        assert query_asks_for_follow_up("compare to current literature") is True

    def test_custom_phrases(self) -> None:
        assert query_asks_for_follow_up("is this trustworthy", ["is this trustworthy"]) is True
        # Without custom phrase, default returns False
        assert query_asks_for_follow_up("is this trustworthy") is False

    def test_case_insensitive(self) -> None:
        assert query_asks_for_follow_up("What Are The Gaps") is True
        assert query_asks_for_follow_up("FIND THE WEAKNESSES") is True
