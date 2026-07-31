"""Pytest setup — fixtures shared across tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Load .env.local EXPLICITLY only when a test requests network via
# `requires_tavily` / `requires_llm_endpoint` fixtures, so unit tests stay
# deterministic.
_ENV_LOCAL_PATH = Path(__file__).resolve().parents[1] / ".env.local"
_ENV_DOTLOCAL_LOADED = False


def _load_env_dotlocal_once() -> None:
    global _ENV_DOTLOCAL_LOADED
    if _ENV_DOTLOCAL_LOADED:
        return
    _ENV_DOTLOCAL_LOADED = True
    if not _ENV_LOCAL_PATH.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_LOCAL_PATH, override=False)
    except ImportError:
        pass


def _has_tavily_key() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY", "").strip())


def _has_llm_endpoint() -> bool:
    return bool(os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL"))


# Skips for tests that require network access
@pytest.fixture
def requires_tavily():
    _load_env_dotlocal_once()
    if not _has_tavily_key():
        pytest.skip("TAVILY_API_KEY unset; skipping real-Tavily test")


@pytest.fixture
def requires_llm_endpoint():
    _load_env_dotlocal_once()
    if not _has_llm_endpoint():
        pytest.skip("LLM endpoint env unset; skipping real-LLM test")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_markdown_query() -> str:
    return "Survey recent advances in RLHF for large language models"


@pytest.fixture
def sample_quick_query() -> str:
    return "What is the capital of France?"


@pytest.fixture
def sample_arxiv_url() -> str:
    return "https://arxiv.org/abs/2401.12345"


@pytest.fixture
def sample_blog_url() -> str:
    return "https://blog.example.com/post"


@pytest.fixture
def sample_pdf_url() -> str:
    return "https://example.com/paper.pdf"
