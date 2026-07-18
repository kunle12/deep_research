"""Tests for glossarize node (P10.6)."""

from __future__ import annotations

import json

from deep_research.library.storage.rows import GlossaryEntry
from deep_research.nodes.glossarize import (
    _canonicalize,
    merge_glossary_entries,
    parse_glossary_from_response,
    render_glossary_md,
)


def test_canonicalize():
    assert _canonicalize("RLHF") == "rlhf"
    assert _canonicalize("RL-HF!") == "rlhf"
    assert _canonicalize("  PPO  ") == "ppo"
    assert _canonicalize("") == ""
    assert _canonicalize("hello world") == "hello world"


def test_parse_glossary_from_response_empty():
    assert parse_glossary_from_response("", "run1") == []
    assert parse_glossary_from_response("{}", "run1") == []
    assert parse_glossary_from_response('{"answer":"test"}', "run1") == []


def test_parse_glossary_from_response_valid():
    response = json.dumps({
        "answer": "some text",
        "glossary": [
            {"term": "RLHF", "kind": "acronym", "short_def": "RL from human feedback",
             "acronym_expansion": "Reinforcement Learning from Human Feedback",
             "confidence": 0.9},
            {"term": "PPO", "kind": "acronym", "short_def": "Proximal Policy Optimization",
             "confidence": 0.8},
        ]
    })
    entries = parse_glossary_from_response(response, "run1", "art1")
    assert len(entries) == 2
    assert entries[0].term == "RLHF"
    assert entries[0].term_canonical == "rlhf"
    assert entries[0].kind == "acronym"
    assert entries[0].acronym_expansion == "Reinforcement Learning from Human Feedback"
    assert entries[0].confidence == 0.9
    assert entries[0].first_seen_run_id == "run1"
    assert entries[0].first_seen_artifact_id == "art1"


def test_parse_glossary_from_response_invalid_json():
    entries = parse_glossary_from_response("not json", "run1")
    assert entries == []


def test_parse_glossary_from_response_no_glossary_field():
    entries = parse_glossary_from_response('{"answer":"test"}', "run1")
    assert entries == []


def test_parse_glossary_from_response_invalid_kind():
    response = json.dumps({
        "glossary": [{"term": "test", "kind": "invalid_kind", "short_def": "test"}]
    })
    entries = parse_glossary_from_response(response, "run1")
    assert len(entries) == 1
    assert entries[0].kind == "concept"  # defaults to concept


def test_merge_glossary_entries_empty():
    assert merge_glossary_entries([], []) == []
    new = [GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym", last_updated="now")]
    assert len(merge_glossary_entries([], new)) == 1


def test_merge_glossary_entries_dedup():
    existing = [
        GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                       short_def="old", confidence=0.5, last_updated="old")
    ]
    new = [
        GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                       short_def="newer", long_def="longer definition",
                       confidence=0.9, last_updated="new")
    ]
    merged = merge_glossary_entries(existing, new)
    assert len(merged) == 1
    assert merged[0].confidence == 0.9  # Higher confidence wins
    assert merged[0].long_def == "longer definition"  # Longer definition wins


def test_merge_glossary_entries_conflicting_acronym():
    existing = [
        GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                       acronym_expansion="Reinforcement Learning from Human Feedback",
                       last_updated="old")
    ]
    new = [
        GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                       acronym_expansion="Reinforcement Learning from Human Feedback v2",
                       last_updated="new")
    ]
    merged = merge_glossary_entries(existing, new)
    assert len(merged) == 1
    # Existing expansion should be kept
    assert merged[0].acronym_expansion == "Reinforcement Learning from Human Feedback"


def test_render_glossary_md_empty():
    md = render_glossary_md([])
    assert "No entries yet" in md


def test_render_glossary_md_with_entries():
    entries = [
        GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                       short_def="test", acronym_expansion="Reinforcement Learning",
                       confidence=0.9, last_updated="now"),
    ]
    md = render_glossary_md(entries)
    assert "RLHF" in md
    assert "Reinforcement Learning" in md
    assert "acronym" in md
