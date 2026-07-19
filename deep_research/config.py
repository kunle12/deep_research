"""AgentConfig — the strict pydantic schema for config.yaml.

Resolved at startup; never mutated. All env-var bindings happen here.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, returning default if unset or empty."""
    v = os.environ.get(name, "")
    return v if v else default


class LLMConfig(BaseModel):
    """LLM endpoint config. Each field may be overridden by an env var of the
    same name prefixed with `DEEP_RESEARCH_LLM_` (e.g.
    `DEEP_RESEARCH_LLM_BASE_URL`), or by `OPENAI_BASE_URL` / `OPENAI_API_KEY`
    for compatibility with the standard OpenAI SDK convention."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8000/v1"
    api_key: str = "llama.cpp"
    text_model: str = "qwen3.5-122b"
    vision_model: str = "qwen3.5-122b"
    max_context_tokens: int = 131072
    timeout_s: int = 240
    # Optional: separate api key for vision calls (some endpoints require this)
    vision_api_key: str | None = None

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> LLMConfig:
        # Precedence (low to high): default, yaml, DEEP_RESEARCH_LLM_*, OPENAI_*
        if v := os.environ.get("DEEP_RESEARCH_LLM_BASE_URL"):
            self.base_url = v
        if v := os.environ.get("OPENAI_BASE_URL"):
            self.base_url = v
        if v := os.environ.get("DEEP_RESEARCH_LLM_API_KEY"):
            self.api_key = v
        if v := os.environ.get("OPENAI_API_KEY"):
            self.api_key = v
        if v := os.environ.get("DEEP_RESEARCH_LLM_TEXT_MODEL"):
            self.text_model = v
        if v := os.environ.get("DEEP_RESEARCH_LLM_VISION_MODEL"):
            self.vision_model = v
        return self


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # If set, every query routes here regardless of classifier output.
    # One of: null | "quick" | "deep" | "academic" | "url_source"
    force_path: Literal["quick", "deep", "academic", "url_source"] | None = None
    min_query_length_for_deep: int = 30  # short queries default to quick


class AgentConfig(BaseModel):
    """Top-level agent execution knobs."""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = 3
    max_subquestions: int = 6
    max_concurrent_tools: int = 8
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)


class TavilyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_env: str = "TAVILY_API_KEY"
    search_depth: Literal["basic", "advanced"] = "basic"
    max_results: int = 10
    rate_limit_retries: int = 2  # retries on 429 before falling back
    max_calls_per_session: int | None = None  # None = unlimited; set to switch to SearXNG proactively


class SearXNGConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "http://localhost:8080/search"
    fetch_format: str = "json"
    max_results: int = 10


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: Literal["tavily", "searxng"] = "tavily"
    fallback_chain: list[Literal["tavily", "searxng"]] = Field(
        default_factory=lambda: ["searxng"]
    )
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    searxng: SearXNGConfig = Field(default_factory=SearXNGConfig)

    def resolve_tavily_key(self) -> str | None:
        return _env(self.tavily.api_key_env)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mcp_command: str = "npx"
    mcp_args: list[str] = Field(default_factory=lambda: ["-y", "@playwright/mcp@latest", "--headless"])
    transport: Literal["stdio", "http"] = "stdio"
    mcp_url: str | None = None  # only when transport == "http"


class ArxivConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_results_per_query: int = 15
    download_pdfs: bool = True
    pdf_cache_dir: str = "./.cache/arxiv_pdfs"
    concurrency: int = 2  # global semaphore around arxiv calls (3s rate limit)
    request_delay_s: float = 3.0


class RedditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    client_id_env: str = "REDDIT_CLIENT_ID"
    client_secret_env: str = "REDDIT_CLIENT_SECRET"
    user_agent: str = "deep_research_bot/0.1"
    max_results_per_query: int = 25


class ScholarSerperConfig(BaseModel):
    """Serper API (https://serper.dev) — paid Scholar backend, primary."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = "SERPER_API_KEY"
    endpoint: str = "https://google.serper.dev/scholar"
    timeout_s: int = 30
    max_calls_per_session: int | None = None  # None = unlimited; set to switch to SearXNG proactively


class ScholarSearXNGConfig(BaseModel):
    """SearXNG fallback: requires the `scholar` engine enabled on the instance."""

    model_config = ConfigDict(extra="forbid")

    url: str = "http://localhost:8080/search"
    timeout_s: int = 30


class ScholarConfig(BaseModel):
    """Google Scholar discovery backend for the academic path.

    Disabled by default. To enable: set `scholar.enabled: true` and either
    `export SERPER_API_KEY=...` (primary) or point `scholar.searxng.url` at a
    SearXNG instance with the `scholar` engine enabled (fallback), and add
    `"scholar"` to `academic.seed_backends`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    primary: Literal["serper", "searxng"] = "serper"
    fallback_chain: list[Literal["serper", "searxng"]] = Field(
        default_factory=lambda: ["searxng"]
    )
    serper: ScholarSerperConfig = Field(default_factory=ScholarSerperConfig)
    searxng: ScholarSearXNGConfig = Field(default_factory=ScholarSearXNGConfig)

    max_results_per_query: int = 10
    concurrency: int = 2  # global rate-limit semaphore
    request_delay_s: float = 1.0  # spacing between calls (Serper ~1 rps)
    # When True, parse the free-PDF side link from each hit and store on Citation.pdf_url
    include_pdf_links: bool = True
    # Optional year window — Serper `tbs=qdr:yYYYY..yYYYY`; SearXNG `time_range` heuristic
    year_from: int | None = None
    year_to: int | None = None
    # Cost guardrail: skip Scholar when arxiv seeds already returned >= this many hits.
    # None = always call Scholar regardless of arxiv hit count (default).
    skip_if_arxiv_hits_ge: int | None = None

    def resolve_serper_key(self) -> str | None:
        return _env(self.serper.api_key_env)


class PdfVisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    renderer: Literal["pdf2image"] = "pdf2image"
    poppler_path: str | None = None  # None = system PATH
    render_dpi: int = 150
    max_dim: int = 1024  # longest side in px after downscale
    jpeg_quality: int = 80
    batch_size: int = 4  # pages per VLM call
    text_extract_first: bool = True  # also run text extraction alongside vision


class FetchPageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cache_dir: str = "./.cache/pages"
    cache_ttl_hours: int = 168
    user_agent: str = "DeepResearchBot/0.1 (+https://example.com)"
    request_timeout_s: int = 30
    # If trafilatura extraction returns fewer chars than this, auto-try browser MCP
    min_content_chars_for_browser_fallback: int = 500


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diskcache_dir: str = "./.cache/misc"


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["markdown", "json"] = "markdown"
    include_citations_bibliography: bool = True
    citation_style: Literal["inline_bare_url", "footnote"] = "inline_bare_url"


class AcademicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: Literal["flat", "recursive"] = "recursive"
    max_depth: int = 2
    max_papers: int = 15
    concurrency: int = 3  # parallel paper processing
    key_reference_threshold: float = 0.7  # 0..1 — LLM-scored gating
    always_extract_text: bool = True
    seed_count: int = 5
    output_citation_graph: bool = True
    citation_graph_formats: list[Literal["bibtex", "json"]] = Field(
        default_factory=lambda: ["bibtex", "json"]
    )
    # Per-paper cap on how many of its references get recursed into
    max_key_references_to_recurse: int = 5
    # Which discovery backends engage in the academic path's seed phase.
    # Default ["arxiv"] preserves backward-compat; add "scholar" to also draw
    # seeds from Google Scholar (requires `scholar.enabled: true`).
    seed_backends: list[Literal["arxiv", "scholar"]] = Field(
        default_factory=lambda: ["arxiv"]
    )


class BlogSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    primary: Literal["tavily", "direct", "both"] = "tavily"
    use_domains_fallback: bool = True
    known_domains: list[str] | None = None  # None = use defaults in blog_search.py
    search_limit: int = 8
    concurrency: int = 8
    domain_fallback_per_domain_limit: int = 3
    domain_fallback_min_spacing_ms: int = 500
    cross_ref_arxiv: bool = True
    last_known_good_date: str = ""


class UrlSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    allowed_url_types: list[Literal["arxiv", "pdf", "html"]] = Field(
        default_factory=lambda: ["arxiv", "pdf", "html"]
    )
    fetch_pdf_size_limit_mb: int = 50
    fetch_html_size_limit_mb: int = 10
    head_probe_timeout_s: int = 8
    follow_up_trigger_phrases: list[str] = Field(default_factory=list)
    auto_follow_up: bool = False
    # If fetch_page returns fewer content chars than this for an HTML url, try browser
    min_content_chars_for_browser_fallback: int = 500


class PDLStorageSQLiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wal_mode: bool = True
    busy_timeout_ms: int = 5000


class PDLStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite: PDLStorageSQLiteConfig = Field(default_factory=PDLStorageSQLiteConfig)
    postgres_dsn_env: str = "DEEP_RESEARCH_PG_DSN"


class PDLRefreshConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    stale_after_days_by_source_type: dict[str, int] = Field(
        default_factory=lambda: {
            "arxiv": 365,
            "blog": 30,
            "html": 14,
            "research_report": 0,
            "scholar": 365,
        }
    )
    refresh_concurrency: int = 4
    re_analyze_on_change: bool = True
    notify_on_change: list[str] = Field(default_factory=lambda: ["log"])


class PDLConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    root_dir: str = ".deep_research_library"
    storage: PDLStorageConfig = Field(default_factory=PDLStorageConfig)
    refresh: PDLRefreshConfig = Field(default_factory=PDLRefreshConfig)


class AgentTopConfig(BaseModel):
    """Top-level config.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    scholar: ScholarConfig = Field(default_factory=ScholarConfig)
    pdf_vision: PdfVisionConfig = Field(default_factory=PdfVisionConfig)
    fetch_page: FetchPageConfig = Field(default_factory=FetchPageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    academic: AcademicConfig = Field(default_factory=AcademicConfig)
    url_source: UrlSourceConfig = Field(default_factory=UrlSourceConfig)
    blog_search: BlogSearchConfig = Field(default_factory=BlogSearchConfig)
    pdl: PDLConfig = Field(default_factory=PDLConfig)

    @model_validator(mode="after")
    def _validate_paths(self) -> AgentTopConfig:
        for p in [
            self.arxiv.pdf_cache_dir,
            self.fetch_page.cache_dir,
            self.cache.diskcache_dir,
        ]:
            with contextlib.suppress(OSError):
                Path(p).mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def load_yaml(cls, path: str | Path) -> AgentTopConfig:
        p = Path(path)
        if not p.exists():
            # Fall back to all defaults
            return cls()
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)


# Convenience re-export
AgentConfigType = AgentTopConfig

__all__ = [
    "AcademicConfig",
    "AgentConfig",
    "AgentConfigType",
    "AgentTopConfig",
    "ArxivConfig",
    "BlogSearchConfig",
    "BrowserConfig",
    "CacheConfig",
    "ClassifierConfig",
    "FetchPageConfig",
    "LLMConfig",
    "OutputConfig",
    "PDLConfig",
    "PdfVisionConfig",
    "RedditConfig",
    "ScholarConfig",
    "ScholarSearXNGConfig",
    "ScholarSerperConfig",
    "SearXNGConfig",
    "SearchConfig",
    "TavilyConfig",
    "UrlSourceConfig",
]
