"""Config loading + pydantic strict-model tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research.config import AgentTopConfig, LLMConfig, UrlSourceConfig
from deep_research.paths.url_source import query_asks_for_follow_up


def test_default_config_loads() -> None:
    cfg = AgentTopConfig()
    assert cfg.llm.base_url.startswith("http")
    assert cfg.agent.max_iterations == 3
    assert cfg.academic.max_depth == 2
    assert cfg.pdf_vision.max_dim == 1024


def test_yaml_loading_partial(tmp_path: Path) -> None:
    p = tmp_path / "test.yaml"
    p.write_text(
        "llm:\n  base_url: 'http://example.test/v1'\nagent:\n  max_iterations: 5\n",
        encoding="utf-8",
    )
    cfg = AgentTopConfig.load_yaml(p)
    assert cfg.llm.base_url == "http://example.test/v1"
    assert cfg.agent.max_iterations == 5
    assert cfg.academic.max_papers == 15


def test_yaml_missing_file_uses_defaults() -> None:
    cfg = AgentTopConfig.load_yaml("/nonexistent/path.yaml")
    assert cfg.llm.text_model == "qwen3.5-122b"


def test_extra_fields_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMConfig.model_validate({"unsupported_key": "x"})


def test_url_source_extra_phrase_extension() -> None:
    cfg = UrlSourceConfig(follow_up_trigger_phrases=["is this authoritative"])
    assert cfg.follow_up_trigger_phrases == ["is this authoritative"]
    assert query_asks_for_follow_up("is this authoritative", cfg.follow_up_trigger_phrases) is True


def test_force_path_enum_values() -> None:
    from pydantic import ValidationError

    cfg = AgentTopConfig()
    assert cfg.agent.classifier.force_path is None
    with pytest.raises(ValidationError):
        AgentTopConfig.model_validate({"agent": {"classifier": {"force_path": "bogus_path"}}})
