"""Unit tests for `deep_research.checkpoint`.

Covers:
  - save_checkpoint: writes JSON to the expected path
  - load_checkpoint: loads saved state, returns (state, metadata)
  - load_checkpoint: returns None when no file exists
  - load_checkpoint: returns None on corrupt JSON
  - discard_checkpoint: removes the file
  - Round-trip: save → load produces identical ResearchState
  - Metadata extras survive the round-trip
"""

from __future__ import annotations

import json

import pytest

from deep_research.checkpoint import (
    _checkpoint_path,
    discard_checkpoint,
    find_checkpoint_for_query,
    load_checkpoint,
    save_checkpoint,
)
from deep_research.state import Citation, ResearchPlan, ResearchState, SubQuestion

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_state() -> ResearchState:
    state = ResearchState(query="test query")
    state.plan = ResearchPlan(
        sub_questions=[
            SubQuestion(id="sq1", question="Q1?", tool_hint="general-web", rationale="r1"),
        ],
        breadth=1,
        max_depth=0,
    )
    state.iteration = 2
    state.absorb_section(
        "sq1",
        [
            Citation(
                url="https://example.com", title="Test", snippet="snippet", confidence_score=0.8
            )
        ],
        "some draft text",
    )
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_save_checkpoint_writes_file(sample_state: ResearchState) -> None:
    run_id = "test_save_001"
    save_checkpoint(sample_state, run_id)
    path = _checkpoint_path(run_id)
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["run_id"] == run_id
    assert raw["state"]["query"] == "test query"
    assert raw["state"]["iteration"] == 2
    discard_checkpoint(run_id)


def test_load_checkpoint_returns_state(sample_state: ResearchState) -> None:
    run_id = "test_load_001"
    save_checkpoint(sample_state, run_id)
    loaded, extra = load_checkpoint(run_id)
    assert loaded is not None
    assert loaded.query == sample_state.query
    assert loaded.iteration == sample_state.iteration
    assert len(loaded.plan.sub_questions) == 1
    assert "sq1" in loaded.drafts
    assert "sq1" in loaded.sections
    assert extra["run_id"] == run_id
    discard_checkpoint(run_id)


def test_load_checkpoint_no_file() -> None:
    result = load_checkpoint("nonexistent_run_id")
    assert result is None


def test_load_checkpoint_corrupt_json() -> None:
    run_id = "test_corrupt_001"
    path = _checkpoint_path(run_id)
    path.write_text("not valid json")
    result = load_checkpoint(run_id)
    assert result is None
    discard_checkpoint(run_id)


def test_discard_checkpoint_removes_file(sample_state: ResearchState) -> None:
    run_id = "test_discard_001"
    save_checkpoint(sample_state, run_id)
    assert _checkpoint_path(run_id).exists()
    discard_checkpoint(run_id)
    assert not _checkpoint_path(run_id).exists()


def test_discard_checkpoint_missing_file_does_not_raise() -> None:
    discard_checkpoint("nonexistent_run_id")  # should not crash


def test_round_trip_preserves_all_fields(sample_state: ResearchState) -> None:
    run_id = "test_roundtrip_001"
    save_checkpoint(sample_state, run_id)
    loaded, _ = load_checkpoint(run_id)
    assert loaded is not None
    assert loaded.query == sample_state.query
    assert loaded.iteration == sample_state.iteration
    assert len(loaded.plan.sub_questions) == len(sample_state.plan.sub_questions)
    for sq in sample_state.plan.sub_questions:
        assert sq.id in loaded.drafts
        assert sq.id in loaded.sections
    for sq_id, cites in sample_state.sections.items():
        assert sq_id in loaded.sections
        assert len(loaded.sections[sq_id]) == len(cites)
    discard_checkpoint(run_id)


def test_extra_metadata_survives(sample_state: ResearchState) -> None:
    run_id = "test_extra_001"
    save_checkpoint(sample_state, run_id, custom_key="custom_value", nested={"a": 1})
    loaded, extra = load_checkpoint(run_id)
    assert loaded is not None
    assert extra["custom_key"] == "custom_value"
    assert extra["nested"] == {"a": 1}
    discard_checkpoint(run_id)


def test_save_and_load_empty_state() -> None:
    state = ResearchState(query="empty test")
    run_id = "test_empty_001"
    save_checkpoint(state, run_id)
    loaded, _ = load_checkpoint(run_id)
    assert loaded is not None
    assert loaded.query == "empty test"
    assert loaded.iteration == 0
    assert len(loaded.plan.sub_questions) == 0
    assert len(loaded.drafts) == 0
    assert len(loaded.sections) == 0
    discard_checkpoint(run_id)


def test_find_checkpoint_for_query_no_match() -> None:
    """Returns None when no checkpoint matches the query."""
    q = "nonexistent query"
    run_id = "test_find_none_001"
    save_checkpoint(ResearchState(query="other query"), run_id)
    result = find_checkpoint_for_query(q)
    assert result is None
    discard_checkpoint(run_id)


def test_find_checkpoint_for_query_single_match() -> None:
    """Returns the matching checkpoint when one exists."""
    q = "matching query"
    run_id = "test_find_one_001"
    state = ResearchState(query=q, iteration=3)
    save_checkpoint(state, run_id)
    result = find_checkpoint_for_query(q)
    assert result is not None
    loaded, meta = result
    assert loaded.query == q
    assert loaded.iteration == 3
    assert meta["run_id"] == run_id
    discard_checkpoint(run_id)


def test_find_checkpoint_for_query_prefers_latest() -> None:
    """When multiple checkpoints match the same query, picks the most recent by mtime."""
    q = "latest query"
    from time import sleep

    run_id_old = "test_find_latest_old"
    run_id_new = "test_find_latest_new"
    save_checkpoint(ResearchState(query=q, iteration=1), run_id_old)
    sleep(0.1)
    save_checkpoint(ResearchState(query=q, iteration=5), run_id_new)
    result = find_checkpoint_for_query(q)
    assert result is not None
    loaded, _ = result
    # Should pick the one with higher iteration (written later)
    assert loaded.iteration == 5
    discard_checkpoint(run_id_old)
    discard_checkpoint(run_id_new)


def test_find_checkpoint_for_query_skips_corrupt_json() -> None:
    """Skips unparseable checkpoint files without crashing."""
    q = "robust query"
    run_id = "test_find_corrupt_001"
    # Write a valid checkpoint
    save_checkpoint(ResearchState(query=q, iteration=2), run_id)
    # Write a corrupt file in the same dir
    import pathlib

    corrupt = pathlib.Path("./.cache/research_checkpoints/corrupt_test.json")
    corrupt.write_text("not json")
    result = find_checkpoint_for_query(q)
    assert result is not None
    loaded, _ = result
    assert loaded.query == q
    assert loaded.iteration == 2
    corrupt.unlink()
    discard_checkpoint(run_id)


def test_find_checkpoint_for_query_skips_missing_run_id() -> None:
    """Skips checkpoint files that lack a run_id metadata key."""
    q = "missing run_id query"
    import json
    import pathlib

    # Manually write a checkpoint that has state matching q but no run_id
    state = ResearchState(query=q, iteration=1)
    payload = {"state": state.model_dump(mode="json")}  # no run_id key
    p = pathlib.Path("./.cache/research_checkpoints/test_no_run_id.json")
    p.write_text(json.dumps(payload))
    result = find_checkpoint_for_query(q)
    assert result is None
    p.unlink()
