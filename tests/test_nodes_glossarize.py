"""Tests for glossarize node (P10.6)."""

from __future__ import annotations

import json

from deep_research.nodes.glossarize import (
    _canonicalize,
    parse_glossary_from_response,
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
    response = json.dumps(
        {
            "answer": "some text",
            "glossary": [
                {
                    "term": "RLHF",
                    "kind": "acronym",
                    "short_def": "RL from human feedback",
                    "acronym_expansion": "Reinforcement Learning from Human Feedback",
                    "confidence": 0.9,
                },
                {
                    "term": "PPO",
                    "kind": "acronym",
                    "short_def": "Proximal Policy Optimization",
                    "confidence": 0.8,
                },
            ],
        }
    )
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
    response = json.dumps(
        {"glossary": [{"term": "test", "kind": "invalid_kind", "short_def": "test"}]}
    )
    entries = parse_glossary_from_response(response, "run1")
    assert len(entries) == 1
    assert entries[0].kind == "concept"  # defaults to concept
