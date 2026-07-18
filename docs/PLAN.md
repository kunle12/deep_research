# Deep Research Agent — Master Plan

> **Status**: v8 — final, proceeded to implementation (P1–P13 done). P12.5 (Web UI) optional/deferred.  
> **Last update**: All phases P1–P12 implemented.  
> **Resume guidance**: If a session is interrupted, read this file first; it contains every locked-in decision. Then read `docs/IMPLEMENTATION_LOG.md` for progress state.

---

## Project goal

A standalone Python agent that performs web/deep/academic research with the assistance of an LLM. The agent:

- Auto-routes user queries to one of: `quick`, `deep`, `academic`, `url_source`.
- Optionally digs recursively into paper citation chains (academic mode, bounded).
- Accepts a URL + optional query (URL-source mode) for direct analysis of a single article / arxiv paper / PDF / blog.
- Uses a multimodal LLM (Qwen3.5-122B-A10B) for vision-based PDF understanding of figures.
- Uses Playwright MCP for dynamic JS pages when needed.
- Searches Tavily (primary) + SearXNG (fallback) + direct page scrape.
- Fetches arXiv papers + downloads PDFs + reads them (text + vision).
- Searches Reddit via PRAW (stubbed in v1).
- Produces Markdown reports with inline citations and bibliography; for academic mode, also emits a BibTeX citation graph.
- Ships a CLI now, but the core `await run_research(query, config) -> Report` is the public microservice entrypoint.

---

## Locked-in decisions (cumulative)

1. **LLM**: open OpenAI-compatible endpoint (user-provided, Qwen3.5-122B-A10B multimodal MoE assumed). Just consume via `AsyncOpenAI(base_url, api_key)`.
2. **Orchestration**: raw `asyncio` — no LangGraph, no LangChain, no AutoGen.
3. **Architecture**: modular — CLI is a thin shell; `await run_research(query, config)` is the importable public entrypoint.
4. **PDF stack**: full MIT — `pypdf` + `pdfplumber` + `pdf2image` (subprocess to poppler binary). PIL for image resize. NO PyMuPDF (AGPL).
5. **PDF vision**: every page downscaled to `max_dim=1024` JPEG quality 80, batch_size=4, sent through Qwen3.5-VLM. No LLM-arbitrated pruning.
6. **Reddit**: stubbed — interface present, raises `NotImplementedError` at runtime, lazy `asyncpraw` import.
7. **Twitter/X**: out of scope.
8. **Web search**: Tavily (primary) → SearXNG (fallback) → direct fetch via `httpx + trafilatura`. Multi-backend chain.
9. **arXiv**: full pipeline in early phases.
10. **Phases**: P1–P9 built in order; P10/P11 deferred.
11. **README**: written early in P1.
12. **Auto-routing classifier**: always runs unless `--quick/--deep/--academic/--url-source` flag override.
13. **Recursive reference mining (academic mode)**: bounded by `max_depth=2`, `max_papers=15`. LLM-only reference scoring (reuses `analyze_paper` call, zero extra cost).
14. **URL-source mode**: auto-detect first URL in query via regex; remaining text treated as the optional query. Single-source scope only. Follow-up to `deep` path **only on explicit request** (gated by conservative phrase list, configurable via yaml).
15. **Package manager**: `uv` (Rust-based, fast, modern).

---

## Dependency list (all MIT-compat)

| Dep | License | Purpose |
|---|---|---|
| `httpx` | BSD-3 | web fetch, arxiv PDF dl, SearXNG client |
| `openai` | Apache-2.0 | LLM client + vision image_url + tool-calling loop |
| `mcp` | MIT | Playwright MCP client (pin `>=1.27,<2`) |
| `trafilatura` | Apache-2.0 | article body extraction |
| `pypdf` | BSD-3 | PDF text (fast) |
| `pdfplumber` | MIT | PDF text (accuracy fallback for tables) |
| `pdf2image` | MIT | PDF → PNG via poppler subprocess |
| `pillow` | MIT-CMU | image downscale + JPEG |
| `arxiv` | MIT | arXiv API client |
| `tavily` | Apache-2.0 | Tavily search client |
| `pydantic` | MIT | Strict schema validation |
| `typer` | MIT | CLI (CLI layer only) |
| `rich` | MIT | CLI progress |
| `pyyaml` | MIT | config.yaml loading |

**System binary** (declared in README): `poppler-utils` (`brew install poppler` / `apt install poppler-utils`), invoked via `subprocess` by `pdf2image`. Does not affect package license.

---

## Project structure (target)

```
deep_research/
├── pyproject.toml                # uv-managed
├── config.example.yaml
├── README.md
├── docs/
│   ├── PLAN.md                   # this file
│   └── IMPLEMENTATION_LOG.md     # progress tracker
├── deep_research/
│   ├── __init__.py               # exports: run_research, AgentTopConfig, Report
│   ├── __main__.py               # python -m deep_research → cli.app()
│   ├── agent.py                  # run_research(query, config) → Report
│   ├── config.py                 # AgentTopConfig (pydantic, Strict)
│   ├── state.py                  # ResearchState + AcademicState + CitationGraph
│   ├── citations.py              # Citation model; dedup; bibliography + .bib generator
│   ├── progress.py               # ProgressReporter Protocol + NullReporter (P9)
│   │
│   ├── llm/
│   │   ├── client.py             # AsyncOpenAI factory
│   │   ├── vision.py             # resize_for_vlm() → JPEG bytes
│   │   └── tool_loop.py          # async tool-calling loop w/ parallel dispatch
│   │
│   ├── paths/                    # one runner per routing target
│   │   ├── __init__.py
│   │   ├── classifier.py         # classify_query() → quick|deep|academic|unclear
│   │   ├── quick.py              # 1 search + summarize, ~5-15s
│   │   ├── deep.py               # planner → researcher → critic → writer loop
│   │   ├── academic.py           # recursive citation graph traversal
│   │   ├── url_source.py         # URL detection + analyze_source runner
│   │   └── applied.py            # (P12) blog-first research path
│   │
│   ├── nodes/
│   │   ├── planner.py            # used by paths.deep
│   │   ├── researcher.py         # used by paths.deep
│   │   ├── critic.py             # used by paths.deep
│   │   ├── writer.py             # used by paths.deep + academic (synthesis)
│   │   ├── analyze_paper.py      # LLM call: summary + key_references (academic)
│   │   ├── analyze_source.py     # LLM call: summary + claims + follow_ups (url_source)
│   │   └── glossarize.py         # (P10.6) rule-based cross-run glossary merger
│   │
│   ├── tools/
│   │   ├── base.py               # Tool Protocol + ToolResult types
│   │   ├── registry.py           # build_tool_registry async factory (one reg per run)
│   │   ├── web_search.py         # Tavily → SearXNG fallback chain
│   │   ├── fetch_page.py         # httpx + trafilatura + diskcache (TTL)
│   │   ├── browser.py            # Playwright MCP client (async context manager)
│   │   ├── arxiv.py              # arxiv lib + httpx PDF download + resolve_id
│   │   ├── pdf.py                # pypdf + pdfplumber + pdf2image + vision embed builder
│   │   ├── reddit.py             # STUB: NotImplementedError; lazy asyncpraw import
│   │   ├── url_detector.py       # extract first URL from query text
│   │   ├── url_classifier.py     # classify: arxiv | pdf | html (sync + async HEAD-probe)
│   │   └── blog_search.py        # (P10.0) Tavily site: primary + direct-domain fallback
│   │
│   ├── report/
│   │   ├── markdown.py           # render Report (incl. academic graph bibliography)
│   │   ├── bibtex.py             # emit citation graph → .bib
│   │   ├── json_export.py       # emit Report (incl. graph) → .json
│   │   └── citations_json.py    # emit citations[] → .json (used by --cite flag)
│   │
│   ├── cli/
│   │   ├── app.py                # typer app
│   │   └── progress.py           # RichProgressReporter (live panel, auto-disables off-TTY)
│   └── prompts/
│       ├── classifier.txt
│       ├── planner.txt
│       ├── researcher.txt
│       ├── critic.txt
│       ├── writer.txt
│       ├── quick_summary.txt
│       ├── analyze_paper.txt
│       ├── analyze_source.txt
│       └── glossary_extract.txt   # (P10.6) paragraph appended to synthesis prompts
│
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── sample_page.html
    │   └── sample_paper.pdf
    ├── test_agent.py
    ├── test_classifier.py
    ├── test_paths_quick.py
    ├── test_paths_deep.py
    ├── test_paths_academic.py
    ├── test_paths_url_source.py
    ├── test_paths_url_source_analyze.py
    ├── test_nodes_planner.py
    ├── test_nodes_researcher.py
    ├── test_nodes_critic.py
    ├── test_nodes_writer.py
    ├── test_nodes_analyze_paper.py
    ├── test_progress.py
    ├── test_cli_app.py
    ├── test_tools_arxiv.py
    ├── test_tools_pdf.py
    ├── test_tools_web_search.py
    ├── test_tools_fetch_page.py
    ├── test_tools_fetch_page_p4.py
    ├── test_tools_browser.py
    ├── test_url_tools.py
    ├── test_citations.py
    └── test_config.py
```

---

## Orchestration flow

```
            ┌───────────────────────────────────────┐
            │     agent.run_research(query, cfg)   │  PUBLIC API
            └────────────────┬─────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ extract_url()   │  regex on query text
                    └────────┬────────┘
                             │
          ┌──────────────────┴────────────────────┐
          │ URL found?                            │
          ▼                                       ▼
   YES ─────────► paths.url_source                NO ─────────► paths.classifier
   (single source)                                (one LLM call, JSON)
          │                                              │
          │                                  ┌───────────┴────────────┐
          │                                  ▼            ▼           ▼
          │                              paths.quick  paths.deep  paths.academic
          │
          ▼
   1. classify URL: arxiv | pdf | html
   2. fetch via appropriate tool (arxiv.resolve_id, fetch_page, browser)
   3. extract content (pdf: text+vision; html: trafilatura)
   4. analyze_source LLM call (single)
   5. optional: if query asks for critique/gaps and no user override
                 fan into paths.deep with extracted "sub-questions"
   6. return Report (analyze mode) or chained Report (follow-up mode)
```

---

## Phase plan (canonical — single source of truth)

| Phase | Scope | Deliverable | Status |
|---|---|---|---|
| **P1** | Scaffold + README + config + state + path stubs returning placeholder Reports. Mock-LLM dry run of all routing paths. | All paths reachable end-to-end with fakes. | done |
| **P2** | paths.quick + tools/web_search (Tavily) + tools/fetch_page + citations. | Real quick results. | done |
| **P2.5** | paths.url_source ANALYZE mode (no follow-up). | URL analysis for arxiv + blogs. | done |
| **P3** | paths.deep full loop. | Deep report. | done |
| **P4** | Web search multi-backend + browser fallback in fetch_page. | Robust fetching. | done |
| **P5** | tools/arxiv full pipeline. Wired into deep path. | arxiv surfaced in deep. | done |
| **P6** | tools/pdf (pypdf+pdfplumber+pdf2image+vision). Used by url_source + deep + academic. | PDF vision understanding. | done |
| **P7** | paths.academic recursive mining + paths.url_source FOLLOW-UP mode. | Academic recursion + URL follow-up research. | done |
| **P8** | tools/browser via Playwright MCP. Surfaced to all paths. | Dynamic page fetch. | done |
| **P9** | CLI polish: flags, rich progress, finalized README, `--quiet` flag, `ProgressReporter` Protocol. | Production-ready CLI + library-friendly progress hooks. | done |
| **P10.0** | `blog_search` tool: Tavily `site:` primary + direct-domain fallback. Optional academic-path blog-seeding shim. (No `applied` path — that lands in P12.0.) | Blogs surfaced; no persistence. | done |
| **P10.5a** | Personal Digital Library v1 core — `.deep_research_library/` artifact store (PDFs only; no JPEGs persisted) + `StorageBackend` Protocol + SQLite default backend with ordered idempotent migrations (`0001_initial`, `0002_add_glossary`, `0003_add_refresh_foundation`) + `LibraryWriter` middleware (archive_pdf / archive_html / archive_report / record_analysis / record_citation_edge / tag / upsert_glossary_entries) routing through the Protocol + weasyprint markdown→PDF (xhtml2pdf fallback) + Playwright print-to-PDF for blogs. `pdl.enabled: true` by default. | Every arxiv PDF + every report archived; library accumulates. Storage-agnostic. | done |
| **P10.5b** | Refresh foundation — `artifact_versions` + `refresh_jobs` tables + the three `artifacts` refresh columns + `LibraryWriter.{refresh_needed, probe_upstream, run_refresh_job}` public methods + `library refresh` CLI one-shot command. Decoupled from P10.5a so library writes aren't blocked on the scheduler work landing. Schema migrations for these tables ship in P10.5a's `0003_add_refresh_foundation.sql` so the columns exist from day one; P10.5b ships the logic that touches them. | Manual `library refresh` works from day one. | done |
| **P10.6** | Glossary generation — per-run LLM-call enrichment + rule-based cross-run dedup. `glossary.md` regenerated atomically each run. SQLite `glossary` table with FTS5. | Curated key concepts + acronyms accumulate across runs. | done |
| **P11** | Wire `asyncpraw`. | Reddit access. | done |
| **P12.0** | (a) **Postgres `StorageBackend` + asyncpg + conformance test suite** parameterized over both SQLite + Postgres. (b) **Long-running refresh scheduler** (`apscheduler` + `croniter`) wrapping `LibraryWriter.run_refresh_job` on a configurable cron; webhook + email notifications; daemonized `deep_research.scheduler` entrypoint. (c) **`applied` path** (blog-first research) — `paths/applied.py` lands here, not in P10.0. (d) **Library CLI completes**: `ls`, `find`, `show`, `tag`, `stats`, `prune`, `export-bibtex`, `glossary --refresh`. (e) **FastAPI microservice** + Dockerfile + poppler setup. | Personal library UX; long-running service that auto-refreshes the library; deployable Postgres-backed microservice. | done |
| **P12.5** | Web UI for browsing the library. | Visual library browser. | optional / deferred |
| **P13** | Library-first recall — prior-knowledge injection before web search. Uses existing FTS5 index over prior analyses. Every path checks the library before hitting the web; delta-only fetching. | `nodes/recall.py` + integration into deep + academic + quick paths. | done |

> **Rule**: any new phase sub-rows must be added to THIS table. The detailed P10.0 / P10.5a / P10.5b / P10.6 sections later in this document are *expositions* of the rows above — they do not introduce new phases. If the table and the prose disagree, the table wins.

---

## Configuration schema (high-level; see `deep_research/config.py` for pydantic definition)

```yaml
llm:
  base_url: "http://localhost:8000/v1"
  api_key: "llama.cpp"
  text_model: "qwen3.5-122b"
  vision_model: "qwen3.5-122b"
  max_context_tokens: 131072
  timeout_s: 240

agent:
  max_iterations: 3
  max_subquestions: 6
  max_concurrent_tools: 8
  classifier:
    enabled: true
    force_path: null  # null | "quick" | "deep" | "academic" | "url_source"
    min_query_length_for_deep: 30

search:
  primary: "tavily"
  fallback_chain: ["searxng"]
  tavily:
    api_key_env: "TAVILY_API_KEY"
    search_depth: "basic"
  searxng:
    url: "http://localhost:8080/search"
    fetch_format: "json"

browser:
  enabled: true
  mcp_command: "npx"
  mcp_args: ["-y", "@playwright/mcp@latest"]
  transport: "stdio"

arxiv:
  enabled: true
  max_results_per_query: 15
  download_pdfs: true
  pdf_cache_dir: "./.cache/arxiv_pdfs"

reddit:
  enabled: false

pdf_vision:
  enabled: true
  renderer: "pdf2image"
  poppler_path: null
  render_dpi: 150
  max_dim: 1024
  jpeg_quality: 80
  batch_size: 4
  text_extract_first: true

fetch_page:
  enabled: true
  cache_dir: "./.cache/pages"
  cache_ttl_hours: 168
  user_agent: "DeepResearchBot/0.1"

cache:
  diskcache_dir: "./.cache/misc"

output:
  format: "markdown"
  include_citations_bibliography: true
  citation_style: "inline_bare_url"

academic:
  enabled: true
  mode: "recursive"
  max_depth: 2
  max_papers: 15
  concurrency: 3
  key_reference_threshold: 0.7
  always_extract_text: true
  seed_count: 5
  output_citation_graph: true
  citation_graph_formats: ["bibtex", "json"]

url_source:
  enabled: true
  allowed_url_types: ["arxiv", "pdf", "html"]
  fetch_pdf_size_limit_mb: 50
  fetch_html_size_limit_mb: 10
  head_probe_timeout_s: 8
  follow_up_trigger_phrases: []
  auto_follow_up: false
```

---

## CLI surface

```bash
# Auto-routing (default)
python -m deep_research "What is the capital of France?"
python -m deep_research "Survey recent advances in RLHF"
python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"
python -m deep_research "https://blog.example.com/post — what are the gaps?"

# Force paths
python -m deep_research --quick "..."
python -m deep_research --deep "..." --max-iterations 5
python -m deep_research --academic "..." --max-depth 2 --max-papers 20 --dump-graph refs.bib
python -m deep_research --url-source "https://example.com/foo" "verify its claims"

# Output controls
python -m deep_research "..." --out report.md --cite citations.json --format markdown
```

---

## Acceptance criteria (full, all must pass)

- [ ] `python -m deep_research "What is the capital of France?"` auto-classifies `quick`, returns in <15s, with 2-3 citations.
- [ ] `python -m deep_research "Survey recent RLHF advances"` auto-classifies `deep`/`academic`, produces multi-section Markdown.
- [ ] `python -m deep_research "..." --academic --max-depth 2 --max-papers 10 --dump-graph refs.bib` produces Markdown + `refs.bib`.
- [ ] `python -m deep_research "..." --quick` bypasses classifier.
- [ ] `python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"` produces a 1-2 page Markdown analysis w/ summary, key claims, methodology, limitations; no auto follow-up research.
- [ ] `python -m deep_research "https://arxiv.org/abs/2401.12345 — what are the gaps?"` produces analysis + a separate "follow-up research" section.
- [ ] `python -m deep_research "https://blog.example.com/post — key claims?"` works for plain HTML; falls back to Playwright MCP if trafilatura returns low content.
- [ ] `python -m deep_research --url-source "https://example.com/paper.pdf"` auto-detects PDF and uses vision path (falls back to text-only if `pdf_vision.enabled=false` — no crash).
- [ ] `--url-source` overrides regex detection.
- [ ] When no URL detected in query, `--url-source` flag raises a clean error.
- [ ] `reddit.enabled: false` allows runs without `asyncpraw` installed.
- [ ] `agent.classifier.enabled: false` routes everything to `deep`.
- [ ] `await run_research(query, config)` works from a fresh Python process (microservice-ready).
- [ ] Academic recursive: capped at `max_papers=3` completes in <5 min when seeded from 1 paper.
- [ ] Citation graph traversal dedups via version-stripped `arxiv_id` (e.g. `2401.12345v3` and `2401.12345` are the same paper).
- [ ] CLI `--quiet` flag suppresses the live progress panel; auto-disables when stdout is not a TTY; `ProgressReporter` Protocol is always plumbed through (even when silent).
- [ ] All tests green: `pytest -q`.

---

## Risk register (canonical — single list across all phases)

Risks for already-shipped work (P1–P9) first, then P10.x and later. New risks added by a phase use the next available number; never renumber.

1. Vision cost at academic depth=2: up to ~375 page renders → ~100 VLM calls. Multi-minute runs accepted.
2. Classifier false-routes: returns `rationale` in report; users can pass flags to override.
3. Recursive mining loops on famous papers: `max_key_references_to_recurse: 5` cap — only top-5 LLM-scored refs enqueued per paper.
4. arXiv 3s rate limit: handled by `arxiv` lib + `asyncio.Semaphore(2)` around arxiv tool.
5. SearXNG public instances increasingly AI-restricted: README recommends self-hosting via Docker.
6. poppler missing at runtime: catch `PDFInfoNotInstalledError` at startup, emit clear error pointing to README.
7. MCP SDK v2 churn: pin `mcp>=1.27,<2`. (The DNS risk here is *v2* churn — minor v1.x changes are tolerable.)
8. Node.js LTS absent: `npx -y @playwright/mcp@latest` will fail; detect & emit clear error in P8.
9. Paywalled / login-walled URLs in url_source mode: extractor's content-length heuristic flag auth-required; emit Report saying so.
10. Very large PDFs: `fetch_pdf_size_limit_mb` cap, clean error if exceeded.
11. JS-only blogs return empty HTML: auto-fallback to `tools.browser` when `trafilatura` extraction is <N chars (configurable).
12. Follow-up trigger heuristic too eager: documented trigger phrase list (visible in README); conservative default.
13. Blog content is less stable than arxiv — links rot, pages restructure. Mitigated by P10.5a's PDF archival of every blog fetched. (`blog_search`)
14. Blog-to-arxiv false positives: a blog may mention an arxiv paper tangentially. The LLM call in `analyze_blog` (P12) should filter relevance. (`blog_search`)
15. Known-domain list is manually curated — stale entries produce dead links. `last_known_good_date` yaml field warns if older than 90d on first use. (`blog_search`)
16. Direct-domain fetch may be IP-rate-limited by aggressive hosts (429/Retry-After). Surface as `ToolResult.error`, continue to next domain. Mitigated by `domain_fallback_min_spacing_ms: 500ms` default. (`blog_search`)
17. JS-heavy domains (openai.com, deepmind.google) yield garbage under trafilatura. Mitigated by `fetch_page`'s built-in Playwright MCP low-yield fallback (P4). (`blog_search`)
18. `weasyprint` system deps (`pango`, `cairo`) absent at runtime → auto-fallback to `xhtml2pdf`; logged as `WARNING`. (`P10.5a`)
19. SQLite `index.db` exceeds 1 GB → log a one-time `INFO` reminder; user runs `library prune` (P12). (`P10.5a`)
20. Two concurrent `run_research()` calls writing to the same `index.db` → SQLite single-writer lock; mitigate via `aiosqlite`'s single-connection-per-writer queue + WAL mode + `busy_timeout_ms: 5000`. Add explicit retry-on-busy with exponential backoff up to 3 attempts so transactions exceeding 5s don't surface as user-facing errors. Document that parallel runs serialize writes. (`P10.5a`)
21. Playwright MCP unavailable for blog→PDF rendering → fall back to `archive_html` raw HTML bytes path; artifact kind flips `pdf` → `html`. Log a `WARNING`. (`P10.5a`)
22. Content-addressable dedup collisions (SHA-256 collision) — astronomically unlikely; ignore. (`P10.5a`)
23. Both `weasyprint` AND `xhtml2pdf` fail at runtime → degrade to a markdown-only artifact (`kind="report"`, `bytes_path` points at the `.md` file) so the `reports` row can still be inserted; emit a `WARNING`; do not raise. (`P10.5a`)
24. Refresh job re-fetches a URL whose upstream permanently 404s → log `WARNING`; set `artifacts.refresh_after_at` to NULL (mark as no-refresh) so future refresh runs skip it. Old artifact bytes remain in the library as historical record; mark `last_refreshed_at` with the 404 timestamp. (`P10.5b`)
25. Storage-backend abstraction drift: P12's Postgres `StorageBackend` impl might fall behind the SQLite impl if a `LibraryWriter` method's SQL is added to one but not the other. Mitigated by the **conformance test suite** in P12 — SQLite always runs in CI; Postgres runs when `DEEP_RESEARCH_TEST_PG_DSN` is set. (`P12.0`)
26. `StorageBackend.Protocol` is `runtime_checkable` only — Python's `Protocol` can't enforce return-type safety at runtime. Mitigated by mypy + the explicit `Row` dataclasses + a contract test (`tests/library/test_protocol_conformance.py`) that introspects method signatures at collection time and fails if a backend is missing a method. (`P10.5a`)
27. LLM emits malformed `glossary` JSON → fallback parser drops the array silently; existing run not affected. (`P10.6`)
28. LLM emission rates too few or too many glossary entries (we asked 0-10) → no remediation; user can `library glossary --refresh` later for an LLM reconcile pass. (`P10.6`)
29. Cross-run rule-based glossary dedup misses semantically-same definitions with different wording → flagged for P12's LLM reconcile pass (opt-in CLI command). (`P10.6`)
30. Glossary `UNIQUE(term_canonical)` constraint requires `acronym_expansion` to share its row with the acronym's row (not stored as a separate row). Mitigated by the schema fix below (single row per canonical term; `acronym_expansion` is a nullable column on the same row). (`P10.6`)

---

## Follow-up trigger phrases (URL-source follow-up mode)

Conservative default list (case-insensitive substring match):

```
gaps, what's missing, what is missing, omitted, not mentioned,
limitation, limitations, shortcoming, shortcomings, weakness, weaknesses, flaw, flaws,
counterexample, counterexamples, refute, refutation, disprove,
verify, validate, falsify, check the claims, fact-check, fact check,
comparison of, compare to, alternative, alternatives, competing,
what else, what other
```

User can extend via `config.url_source.follow_up_trigger_phrases: []`.

---

## Resume guidance

If implementation gets disrupted mid-session, do the following in the new session:

1. Read this file (`docs/PLAN.md`) to refresh full plan.
2. Read `docs/IMPLEMENTATION_LOG.md` to see what's done.
3. Check tracked files (`git ls-files`) to verify project structure.
4. Run `cd /Users/xun/dev/deep_research && uv sync` to verify deps still resolve.
5. Run `uv run python -m deep_research --help` to confirm CLI still works.
6. Continue from the next pending item in `IMPLEMENTATION_LOG.md`.

Update `IMPLEMENTATION_LOG.md` as you complete each module/phase.

---
## P10.0 — Blog search tool (Tavily primary + direct-domain fallback)

### Motivation

The academic path today only seeds from `arxiv_search`, missing valuable technical blogs (OpenAI, Anthropic, DeepMind, Meta AI, Microsoft Research, NVIDIA, Google Research, Distill, Stripe, etc.). Blogs provide accessible explanations, working code, and often precede arxiv by weeks. For applied / implementation-focused queries they're often more useful than the formal paper.

### Tool shape

One tool, `blog_search`, registered when `blog_search.enabled: true`.

**Signature**: `blog_search(query: str, max_results: int = 8, domains: list[str] | None = None) -> ToolResult`

Returns `ToolResult.content` = JSON `{"hits": [...], "backend_used": "tavily|direct"}` and `ToolResult.citations` = list of `Citation` objects with `source_type="blog"`, deduped by URL.

**Primary: Tavily `site:` queries** (network-cheap, robust to domain restructure)
- Builds one Tavily query of the form `<user_query> (site:openai.com/index OR site:anthropic.com/research OR site:deepmind.google OR ...)`.
- Reuses the existing Tavily client already wired into `web_search.py` (P2).
- Returns up to `blog_search.search_limit` hits.
- This is the "happy path" — no direct fetch required.

**Fallback: direct-domain fetching** when Tavily is unconfigured OR returns empty/stale
- Iterate `blog_search.known_domains` (limited by `search_limit`), `httpx.get(domain_url)` + `trafilatura` to extract post links + titles.
- For each domain pick the **top-K posts by match score** between the domain's recent-posts page and the user query (cheap Jaccard on title tokens, threshold ≥ 0.15).
- Reuse `tools/fetch_page.py` `fetch_page()` for both the page fetch AND any browser-fallback low-yield handling — keeps the JS-heavy domains (openai.com, deepmind.google) covered for free (P4 architecture).
- Per-domain spacing delay (`blog_search.domain_fallback_min_spacing_ms = 500`) so we don't hammer one host.
- Concurrency governed by `blog_search.concurrency` semaphore (default 8).

**User-visible behavior**
- If `blog_search.enabled=true` but Tavily is unconfigured AND known-domain fallback yields 0 posts → tool returns an empty `ToolResult` with `content` noting why (Tavily unconfigured + 0 blog hits from direct fetch). Caller degrades gracefully (path falls back to no-blog run, no crash).
- If `blog_search.primary == "tavily"` and Tavily succeeds, the direct-domain path is **not** invoked (saves bandwidth).
- If `blog_search.primary == "both"`, both backends run in parallel and results are merged by confidence score.
- If `blog_search.primary == "direct"`, only the direct-domain fallback runs (useful for offline / privacy-sensitive setups).

### Config additions

```yaml
blog_search:
  enabled: true
  primary: "tavily"                       # "tavily" | "direct" | "both"
  use_domains_fallback: true              # engage direct-domain fetch when Tavily unconfigured OR empty
  known_domains:
    - "openai.com/index"
    - "anthropic.com/research"
    - "deepmind.google"
    - "ai.meta.com/blog"
    - "research.microsoft.com"
    - "developers.googleblog.com"
    - "stripe.com/blog"
    - "distill.pub"
    - "neclab.org"
    - "paperswithcode.com"
    - "github.blog"
  search_limit: 8                          # max blog posts returned per call
  concurrency: 8                           # HTTP-only, cheaper than PDF
  domain_fallback_per_domain_limit: 3      # cap posts per domain in direct mode
  domain_fallback_min_spacing_ms: 500      # per-host spacing delay
  cross_ref_arxiv: true                    # auto-detect arxiv IDs in blog body
  last_known_good_date: "<release_date>"      # surface stale-domain warning if older than 90d; replace with date when P10.0 ships
```

### Path integration

- **`paths/applied.py`** (P12 — not in P10): a new `applied` path that's blog-first. Deferred from P10.0; P10.0 only ships the tool.
- **`paths/academic.py`** (P10.0 optional integration): after `_gather_seeds`, run a parallel blog fetch (`blog_search` + `fetch_page` for top-K results). Blog citations are added to `Report.citations` ONLY — **not** to `CitationGraph.nodes` (which stays arxiv-only). The synthesis prompt in `_synthesize_markdown` gets an additional "Blog context" section injected.
- **`paths/url_source.py`** (auto-extended): if user provides a blog URL, `fetch_page` already handles it; `analyze_source` LLM call already HTML-aware. No P10.0 change needed.

### Why this backend ordering (decision record)

1. Tavily `site:` saves us from being a generic blog crawler — their index already de-duplicates and ranks.
2. Direct-domain fallback is the right "graceful degradation" path that lets the tool work offline-of-Tavily without us re-implementing a search engine.
3. Reusing `fetch_page` (with its built-in Playwright MCP fallback for low-yield) gives us JS-domain coverage for free — no new code path for those domains.

### Acceptance criteria

- [ ] `blog_search` returns `Citation` objects with `source_type="blog"`, deduped by URL
- [ ] When `blog_search.primary == "tavily"` AND Tavily API key set, direct-domain path is NOT invoked (network-cheap happy path)
- [ ] When Tavily is unconfigured AND `blog_search.use_domains_fallback: true`, direct-domain fallback still returns ≥1 citation from known domains (synthetic test)
- [ ] Direct-domain fallback respects `domain_fallback_min_spacing_ms` between hits to the same host (asserted via mock-time test)
- [ ] Direct-domain fallback surfaces 429/Retry-After as a friendly `ToolResult.error` and continues to the next domain
- [ ] When both Tavily and direct fallback return zero results, tool returns empty `ToolResult` (no exception) — callers degrade gracefully
- [ ] `tests/test_tools_blog_search.py` covers Tavily happy path, direct-domain happy path, Tavily-fails-to-direct-fallback, mixed `primary: "both"` merge, 429-handling, empty-results-graceful-degradation, network-error-returns-empty

### Risk register additions

See the canonical **Risk register** near the top of this file. P10.0 contributes risks #13–#17 (blog-search-specific).

### Project structure additions (P10.0)

```
deep_research/
└── tools/
    └── blog_search.py       # NEW — Tavily primary + direct-domain fallback
```

### Existing files modified

| File | Change |
|---|---|
| `tools/registry.py` | Register `blog_search` when `config.blog_search.enabled` |
| `config.py` | Add `BlogSearchConfig` pydantic schema (all knobs above) |
| `config.example.yaml` | Add `blog_search:` section with documented defaults |

P10.0 is intentionally minimal — it ships the tool. Academic-path integration is a 5-line shim (parallel `blog_search` call merged into `citations`). The full `applied` path + `analyze_blog` node + `classifier` admin route belong to P12.

---
## P10.5a — Personal Digital Library v1 (core storage + archival)

### Motivation

A standalone, opt-out archive of every research artifact, producing a personally-owned knowledge base that accumulates across runs. Every arxiv PDF fetched, every report synthesized, every blog post discovered — bytes-on-disk + structured metadata — so re-researching a topic benefits from prior runs, not from re-fetching URLs that rot. Operates on the principle that **the artifact is the primary source**, not the URL: the same paper fetched via arxiv-by-id vs arxiv-by-URL is one row, not two.

P10.5a ships the **pluggable storage backend** — `StorageBackend` Protocol; SQLite is the default and only backend in P10.5a. The Protocol + Row dataclasses are designed so a Postgres backend can land in P12 without any change to `LibraryWriter` or the existing seam-point call sites.

P10.5a also ships the **schema foundation** for refresh (`artifact_versions` + `refresh_jobs` tables + `artifacts.{refresh_after_at, last_refreshed_at, upstream_unchanged_since}` columns via migration `0003_add_refresh_foundation.sql`) so the columns exist from day one — but the logic that *uses* them (`probe_upstream`, `run_refresh_job`) ships in P10.5b. This decoupling means library writes aren't blocked on the scheduler work landing.

### Conceptual model — three tiers

```
              ┌──────────────────────────────────────────────┐
              │  Personal Digital Library (PDL)              │
              ├──────────────────────────────────────────────┤
              │  1. ARTIFACT STORE  (immutable bin blobs)    │  PDFs only; NO JPEGs persisted (per decision)
              │  2. METADATA DB     (structured records)      │  summary, key_findings, citations, tags, glossary, refresh_jobs
              │  3. INDEX           (fast full-text search)   │  SQLite FTS5 (Postgres tsvector in P12)
              └──────────────────────────────────────────────┘
                       ▲                ▲                ▲
                       │                │                │
              ┌────────┴────────┐ ┌────┴─────┐ ┌─────────┴─────────┐
              │ SourceArchive    │ │ Reports  │ │ Citation Graph    │
              │ (raw bytes for   │ │ (run-    │ │ (academic-mode    │
              │  citing back)    │ │  level-  │ │  recursive-mined  │
              │                  │ │  output) │ │  artifacts)       │
              └─────────────────┘ └──────────┘ └───────────────────┘
```

### Key invariants

- **Every artifact has a content-addressable ID** (SHA-256[:16] of bytes). Identical PDF from arxiv-by-URL vs arxiv-by-arxiv_id → one row, stored once.
- **Every derived record links back to its source artifact** via foreign key (`artifact_id`). Reports cite back to artifacts, not URLs.
- **URL → artifact** is best-effort; rot is real, we keep the bytes. When upstream content changes, we archive the new version under a new artifact_id and record a parent→child row in `artifact_versions`. The old version stays in the library as historical record.
- **Reports are themselves archived** — each `run_research()` call commits its final markdown + JSON + weasyprint-PDF dump, so a corpus of "things researched" builds over time.
- **`pdl.enabled: true` by default** (everybody gets a library from run #1). Disable in yaml for headless microservice deployments.
- **Schema is a single file per backend**, applied on first connect. No migration versioning — the database is created fresh from the consolidated `0001_initial.sql`.
- **Storage backend is swappable** via a single yaml knob (`pdl.storage.backend: "sqlite" | "postgres"`). `LibraryWriter` is backend-agnostic — all SQL lives behind the `StorageBackend` Protocol.

### Directory layout

```
.deep_research_library/                    # configurable via yaml: pdl.root_dir
├── artifacts/
│   ├── pdf/
│   │   ├── 2401.12345.pdf                # by arxiv_id (legacy alias when known)
│   │   └── blog/
│   │       ├── 9f8e7d6c-openai.pdf        # blog→PDF; filename = sha256[:16] + domain-slug
│   │       └── ...
│   └── html/                             # only populated when Playwright unavailable
│       ├── 9f8e7d6c/                     # content-addressable
│       │   ├── page.html                 # original raw HTML
│       │   └── meta.json                 # opengraph / extracted metadata
│       └── ...
│
├── reports/                              # produced reports (level-1 outputs)
│   └── 2026/
│       └── 07/
│           └── 17/
│               ├── 20260717T1423RLHF-recursive.pdf   # markdown → weasyprint PDF (or xhtml2pdf fallback)
│               ├── 20260717T1423RLHF-recursive.md
│               └── 20260717T1423RLHF-recursive.json   # full structured dump
│
├── glossary.md                           # regenerated atomically each run (P10.6)
├── index.db                              # single SQLite file (mvcc-safe, WAL mode)
└── config.yaml                           # per-library path overrides
```

`artifacts/html/` is populated **only** when `browser.enabled: false` AND the blog-render path can't run Playwright print-to-PDF. When Playwright is available, the html branch produces `*.pdf` files under `artifacts/pdf/blog/` instead — keeps storage layout simple.

### Artifact policies (confirmed with user)

- **Rendered JPEGs**: NOT persisted. `pdf_render_pages` returns base64 data URLs to the LLM, then discarded.
- **HTML storage as PDF** when Playwright available — JS-rendered posts captured once, readable in any viewer forever.
- **Eviction**: no automatic eviction. Grow forever. User can `library prune --older-than 90d` (P12). One-time `INFO` reminder if `index.db` exceeds 1 GB.
- **Upstream changes**: NEVER overwrite.changed upstream → archive as a new artifact_id + insert `artifact_versions` row. Old bytes remain for historical reference.

### SQLite schema (consolidated)

Single `.db` file (`pdl.root_dir/index.db`). All tables created at once from a single `0001_initial.sql` on first connect.

```sql
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 1. Canonical artifacts (everything archived)
CREATE TABLE artifacts (
    artifact_id      TEXT PRIMARY KEY,                 -- sha256[:16] hex
    kind             TEXT NOT NULL,                    -- "pdf" | "html" | "report"
    source_url       TEXT,
    source_type      TEXT,                             -- "arxiv" | "blog" | "html" | "research_report"
    title            TEXT,
    authors          TEXT,                             -- JSON array
    discovered_by    TEXT,                             -- ToolName.value
    arxiv_id         TEXT,                             -- denormalized for arxiv; null otherwise
    parents          TEXT,                             -- JSON array of artifact_ids (citation-of-...)
    bytes_path       TEXT NOT NULL,                    -- for kind=pdf: path to a single file; for kind=html: path to a directory containing page.html + meta.json. Relative to pdl.root_dir.
    bytes_size       INTEGER,
    first_seen_at     TEXT NOT NULL,
    last_touched_at   TEXT NOT NULL,
    raw_metadata     TEXT,                             -- JSON blob
    -- P10.5a refresh foundation (columns added by migration v3; logic in P10.5b)
    refresh_after_at        TEXT,                       -- ISO 8601; computed from refresh_policy at insert
    last_refreshed_at       TEXT,                       -- when LibraryWriter.probe_upstream last ran
    upstream_unchanged_since TEXT,                     -- last SHA-equal probe
    CHECK (kind IN ('pdf', 'html', 'report'))
);

-- 2. Run-level records (the user-facing "what I researched")
CREATE TABLE reports (
    run_id           TEXT PRIMARY KEY,                 -- UUID v4 (timestamp-derived)
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    original_query   TEXT NOT NULL,
    path_taken       TEXT NOT NULL,
    classifier_rationale TEXT,
    iterations       INTEGER,
    config_snapshot  TEXT,                             -- JSON blob
    markdown         TEXT NOT NULL,
    artifact_id      TEXT,                              -- FK -> pdf version of this report
    citations_json   TEXT,
    classifier_json  TEXT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);

-- 3. Per-source analyses (key findings, methodology etc.)
CREATE TABLE analyses (
    analysis_id      TEXT PRIMARY KEY,
    artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id),
    run_id           TEXT NOT NULL REFERENCES reports(run_id),
    analyzer         TEXT NOT NULL,                    -- "analyze_paper" | "analyze_source" | "analyze_blog"
    summary          TEXT,
    key_findings     TEXT,                             -- JSON array
    methodology      TEXT,
    limitations      TEXT,
    gaps             TEXT,
    follow_ups       TEXT,
    key_references   TEXT,                             -- JSON array of {arxiv_id, title, is_key_ref}
    relevance_to_query TEXT,
    analyzed_at      TEXT NOT NULL
);

-- 4. Citation graph (academic mode)
CREATE TABLE citation_edges (
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    target_artifact_id TEXT REFERENCES artifacts(artifact_id),
    target_arxiv_id    TEXT,
    rationale          TEXT,
    weight             REAL DEFAULT 0.5,
    discovered_in_run  TEXT REFERENCES reports(run_id),
    PRIMARY KEY (source_artifact_id, target_arxiv_id)
);

-- 5. Tags
CREATE TABLE tags (
    tag              TEXT NOT NULL,
    artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id),
    applied_in_run   TEXT REFERENCES reports(run_id),
    PRIMARY KEY (tag, artifact_id)
);

-- 6. Glossary (P10.6 — schema shipped in P10.5a migration v2)
CREATE TABLE glossary (
    term_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term         TEXT NOT NULL,
    term_canonical TEXT NOT NULL,
    kind         TEXT NOT NULL,                        -- concept|acronym|method|metric|dataset|model|tool
    short_def    TEXT,
    long_def     TEXT,
    acronym_expansion TEXT,
    related_terms TEXT,                               -- JSON array
    domain_tags  TEXT,                                -- JSON array
    confidence   REAL,
    first_seen_run_id TEXT REFERENCES reports(run_id),
    first_seen_artifact_id TEXT REFERENCES artifacts(artifact_id),
    last_updated TEXT NOT NULL,
    UNIQUE(term_canonical)                    -- acronym_expansion lives on the same row as its term (not a separate row)
    CHECK (kind IN ('concept','acronym','method','metric','dataset','model','tool'))
);

-- 7 + 8. Refresh foundation (schema shipped in P10.5a migration v3; long-running scheduler deferred to P12)
CREATE TABLE artifact_versions (
    artifact_id_old TEXT NOT NULL REFERENCES artifacts(artifact_id),
    artifact_id_new TEXT NOT NULL REFERENCES artifacts(artifact_id),
    reason          TEXT NOT NULL,                    -- "content_changed" | "url_moved"
    discovered_at   TEXT NOT NULL,
    discovered_in_run TEXT REFERENCES reports(run_id),
    PRIMARY KEY (artifact_id_old, artifact_id_new)
);

CREATE TABLE refresh_jobs (
    job_id         TEXT PRIMARY KEY,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    scope_kind     TEXT NOT NULL,                     -- "source_type" | "tag" | "artifact_id"
    scope_value    TEXT NOT NULL,
    artifacts_considered INTEGER,
    artifacts_refreshed   INTEGER,
    status         TEXT NOT NULL,                     -- "running" | "completed" | "failed" | "partial"
    error          TEXT
);

-- FTS5 virtual tables (SQLite-specific; Postgres equivalent in P12)
CREATE VIRTUAL TABLE search_index USING fts5(
    artifact_id UNINDEXED,
    title,
    authors,
    summary,
    extracted_text,
    content='analyses',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE glossary_fts USING fts5(
    term, short_def, long_def, related_terms,
    content='glossary', content_rowid='term_id',
    tokenize='porter unicode61'
);
```

### Pluggable storage backend — `StorageBackend` Protocol (P10.5a)

The `LibraryWriter` calls **only** the Protocol, never sqlite3 directly. Postgres backend lands in P12 by satisfying the same Protocol — no changes to `LibraryWriter` or any existing seam-point call site.

```
deep_research/
└── library/
    └── storage/
        ├── __init__.py                # get_backend(config) factory
        ├── base.py                    # StorageBackend Protocol
        ├── rows.py                    # typed Row dataclasses (shared between backends)
        ├── sqlite_backend.py          # P10.5a default; uses aiosqlite + stdlib sqlite3
        ├── migrations/
        │   ├── sqlite/
        │   ├── 0001_initial.sql        # schema_meta, artifacts, reports, analyses,
        │   │                            #   citation_edges, tags, FTS indices (P10.5a)
        │   ├── 0002_add_glossary.sql   # glossary + glossary_fts (P10.5a — schema ready for P10.6)
        │   └── 0003_add_refresh_foundation.sql  # artifact_versions + refresh_jobs +
        │                                          #   artifacts.{refresh_after_at,
        │                                          #   last_refreshed_at, upstream_unchanged_since}
        └── postgres/                  # empty in P10.5a; P12.0 fills these:
            └── README.md               # "Postgres migrations land in P12.0"
```

```python
# deep_research/library/storage/base.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class StorageBackend(Protocol):
    """Pluggable library backend. SQLite + Postgres both comply."""

    # -- Lifecycle --
    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    # -- Schema management --
    async def current_schema_version(self) -> int: ...
    async def apply_migration(self, version: int) -> None: ...
    async def ensure_schema(self) -> None:
        """Idempotent: apply any pending migrations."""

    # -- Artifact ops --
    async def upsert_artifact(self, artifact: ArtifactRow) -> str: ...
    async def get_artifact(self, artifact_id: str) -> ArtifactRow | None: ...
    async def find_artifact_by_url(self, url: str) -> ArtifactRow | None: ...
    async def find_artifact_by_arxiv_id(self, arxiv_id: str) -> ArtifactRow | None: ...
    async def artifacts_needing_refresh(
        self, scope_kind: str, scope_value: str, limit: int
    ) -> list[ArtifactRow]: ...

    # -- Report ops --
    async def insert_report(self, report: ReportRow) -> None: ...
    async def get_report(self, run_id: str) -> ReportRow | None: ...
    async def list_reports(self, limit: int) -> list[ReportRow]: ...

    # -- Analysis, citation_edges, tags, glossary, refresh_jobs --
    # Each gets a typed Row in rows.py + async CRUD methods. Implementation lives
    # in the backend (sqlite_backend.py in P10.5a; postgres_backend.py in P12).

    # -- FTS (backend-encapsulated) --
    async def full_text_search(
        self, query: str, *, kind: str, limit: int
    ) -> list[SearchHit]: ...
    async def glossary_search(self, query: str, limit: int) -> list[GlossaryEntry]: ...

    # -- Refresh foundation (P10.5a; the scheduler that calls these lands in P12) --
    async def insert_artifact_version(
        self, old_id: str, new_id: str, reason: str, run_id: str
    ) -> None: ...
    async def start_refresh_job(
        self, scope_kind: str, scope_value: str
    ) -> str: ...
    async def complete_refresh_job(
        self, job_id: str, considered: int, refreshed: int,
        status: str, error: str | None = None
    ) -> None: ...
```

Tight Row dataclasses (`ArtifactRow`, `ReportRow`, `AnalysisRow`, `CitationEdgeRow`, `TagRow`, `GlossaryEntry`, `RefreshJobRow`, `ArtifactVersionRow`, `SearchHit`) live in `library/storage/rows.py` — shared between both backends so the in-memory shape is identical regardless of which backend is configured.

### `LibraryWriter` middleware — the only integration surface (P10.5a)

`LibraryWriter.__init__` takes a `storage: StorageBackend`. All artifact/file ops go through it; all SQL goes through `storage.*`. The writer itself is backend-agnostic.

```python
# deep_research/library/writer.py (NEW)
class LibraryWriter:
    """Persists artifacts + metadata at well-defined seam points."""

    def __init__(self, storage: StorageBackend, root_dir: Path,
                 refresh_policy: RefreshPolicy) -> None: ...

    # -- Artifact archival --
    async def archive_pdf(self, path: Path, *, arxiv_id: str | None,
                         source_url: str | None, title: str | None = None) -> str:
        """Hash-and-file path under artifacts/pdf/, upsert artifact row.
        Returns artifact_id."""

    async def archive_html(self, url: str, html: str, pdf_bytes: bytes) -> str: ...
    async def archive_report(self, report: Report, run_id: str,
                            config_snapshot: dict) -> str: ...

    # -- Derived records --
    async def record_analysis(self, artifact_id: str, analysis, run_id: str,
                            analyzer: str) -> str: ...
    async def record_citation_edge(self, source_aid: str, target_arxiv_id: str,
                                 weight: float, run_id: str,
                                 rationale: str) -> None: ...
    async def tag(self, artifact_id: str, tags: list[str], run_id: str) -> None: ...
    async def upsert_glossary_entries(self, entries: list[GlossaryEntry],
                                     run_id: str) -> int: ...

    # -- Refresh foundation (P10.5a ships these; the scheduler that uses them is P12) --
    def refresh_needed(self, artifact_id: str) -> bool:
        """Compute staleness from refresh_policy.stale_after_days_by_source_type
        against artifacts.last_touched_at. Pure computation; no I/O."""

    async def probe_upstream(self, artifact_id: str) -> tuple[bool, str | None]:
        """Re-fetch the source_url, compute SHA, compare.
        - If unchanged -> bump last_touched_at + upstream_unchanged_since. Return (True, None).
        - If changed -> archive new bytes under a new artifact_id, insert
          artifact_versions row, return (False, new_artifact_id).
        - If fetch errors -> log WARNING, return (False, None). Don't raise."""

    async def run_refresh_job(
        self, scope_kind: str, scope_value: str, *,
        re_analyze: bool = False, dry_run: bool = False
    ) -> dict:
        """Top-level refresh invocation. Used by:
          - the `library refresh` CLI command (P10.5b, manual)
          - the long-running scheduler (P12, cron-triggered)
        Both call this same method. Returns a stats dict:
          {considered, refreshed, unchanged, errored, new_versions}
        Inserts a refresh_jobs row at start + updates it on completion."""
```

### Seam points (only these places need a LibraryWriter call)

| Existing module | Seam point | Library call |
|---|---|---|
| `tools/arxiv.py` `arxiv_download_pdf` | right after disk write returns local path | `archive_pdf(path, arxiv_id=…, source_url=…)` |
| `tools/fetch_page.py` (P10.5a addition) | after trafilatura/Playwright extracts the blog HTML | `archive_html(url, html, pdf_bytes=playwright_print(url))` |
| `paths/url_source.py` `url_source()` | after `_fetch_arxiv_source` / `_fetch_pdf_source` / `_fetch_html_source` | ensure artifact exists; analyses link via artifact_id |
| `paths/academic.py` `_analyze_and_recurse` | after `analyze_paper_node` returns | `record_analysis` |
| `paths/academic.py` after each batch | for batch's discovered children | `record_citation_edge` (one per child) |
| `agent.py run_research` | at completion (after `return Report(...)`) | `archive_report(report, run_id, config_snapshot)` |
| `nodes/writer.py` + `paths/academic.py:_synthesize_markdown` + `paths/quick.py:_synthesize` | augmentation of synthesizing LLM prompt (P10.6) — no new seam for P10.5a | `upsert_glossary_entries` (called by writer code path post-synthesis) |
| `paths/applied.py` (P12) and `paths/quick.py` | per fetched blog/page | `archive_html(url, html, pdf=…)` — aggregation target for blog posts |

`LibraryWriter` is constructed once per `run_research()` call (when `pdl.enabled`), threaded through the same path-module routing as the `ProgressReporter` from P9. When `pdl.enabled: false`, the writer is a No-op `NullLibraryWriter` (decorative null object — zero behavior change).

### weasyprint integration + fallback (P10.5a)

Dependency additions (pyproject.toml):

| New dep | License | Purpose |
|---|---|---|
| `weasyprint` | BSD-3-Clause | markdown → PDF for report archival |
| `aiosqlite` | MIT | async SQLite access (stdlib sqlite3 blocks the event loop) |
| `markdown` | BSD-3-Clause | markdown → HTML intermediate before weasyprint renders to PDF |
| `xhtml2pdf` (P10.5a `pdf-fallback` extra) | Apache-2.0 | pure-Python fallback when weasyprint's native deps missing |

Native deps for `weasyprint` (documented in README prerequisites table): `pango`, `cairo`.

**Auto-fallback**: if `weasyprint` import fails OR `pango`/`cairo` missing at runtime → fall back to `xhtml2pdf` (lower visual quality, no system deps). Both paths produce valid PDFs; the filename is the same; user sees a `WARNING` log line the first time.

**Degraded path when BOTH fail**: if `weasyprint` AND `xhtml2pdf` both error (e.g. system deps missing for both), fall back to a markdown-only artifact (`kind="report"`, `bytes_path` pointing at the `.md` file). Emit a `WARNING`. The `reports` row is still inserted; the run does not crash.

```python
try:
    from weasyprint import HTML
    HTML(string=html).write_pdf(pdf_path)
except (ImportError, OSError) as e:
    logger.warning("weasyprint unavailable (%s); falling back to xhtml2pdf", e)
    from xhtml2pdf import pisa
    try:
        with open(pdf_path, "wb") as fh:
            pisa.CreatePDF(html, dest=fh)
    except Exception as e2:
        logger.warning("xhtml2pdf also failed (%s); falling back to markdown-only artifact", e2)
        # report.md already exists on disk; set bytes_path to it
        artifact_kind = "report"
        bytes_path = report_md_path
```

### Refresh foundation — columns/tables in P10.5a, logic + CLI in P10.5b

| Column/table (P10.5a — schema ships now) | Logic + CLI (P10.5b — ships next) | P12 ships later |
|---|---|
| `artifacts.refresh_after_at`, `artifacts.last_refreshed_at`, `artifacts.upstream_unchanged_since` columns | Long-running scheduler process |
| `refresh_jobs` table (filled by `run_refresh_job`) | `apscheduler` integration |
| `artifact_versions` table (filled by `probe_upstream`) | Cron expression parser (`croniter`) |
| `LibraryWriter.refresh_needed()` | Continuous-process entrypoint (new `deep_research.scheduler` package) |
| `LibraryWriter.probe_upstream()` | Webhook + email notifications |
| `LibraryWriter.run_refresh_job()` | Auto-recovery on scheduler crash |
| `library refresh` CLI one-shot command (manual invocation) | Daemonized scheduler service |

CLI command in P10.5b (manual — users cron it themselves until P12):

```bash
deep_research library refresh                            # all stale artifacts
deep_research library refresh --source-type blog         # filter
deep_research library refresh --tag RL                   # filter
deep_research library refresh --artifact-id <aid>        # one-shot
deep_research library refresh --dry-run                  # show what would be probed, no fetch
deep_research library refresh --re-analyze               # force re-analyze even if SHA matches
deep_research library refresh --once                     # explicit: run exactly one cycle (for cron wrappers)
```

P10.5b ships `--once` as the default behavior; P12's scheduler wraps the same `run_refresh_job()` in an `apscheduler.CronTrigger` loop. The implementation contract is identical — only the caller differs.

### Config additions

```yaml
pdl:
  enabled: true                                # opt-out within the library, not opt-out of the agent
    root_dir: ".deep_research_library"           # relative paths resolved once at config-load time against the cwd of the process; absolute paths used verbatim. `AgentTopConfig` stores the resolved absolute path so `LibraryWriter` never needs to re-resolve.

  storage:
    backend: "sqlite"                          # "sqlite" | "postgres"  (postgres lands in P12.0)
    sqlite:
      wal_mode: true                           # WAL for better read/write concurrency
      busy_timeout_ms: 5000

  refresh:
    enabled: true                              # foundation schema ships + CLI command works regardless
                                                # of this flag; only the future scheduler (P12) reads it
    stale_after_days_by_source_type:
      arxiv:          365                       # arxiv doesn't really rot; author corrections happen
      blog:            30
      html:            14
      research_report: 0                       # never refresh (we OWN this artifact)
    refresh_concurrency:  4                    # parallel re-fetches per job
    re_analyze_on_change:  true                # re-run analyze_{paper,source} on changed bytes
    notify_on_change:
      - "log"                                  # always; future options: "webhook", "email:..."
```

### Acceptance criteria (P10.5a — core storage + archival)

- [ ] Everything archived is content-addressed (SHA-256[:16]); identical PDFs fetched from arxiv-by-URL vs arxiv-by-arxiv_id are stored once.
- [ ] Every `run_research()` call commits its final report (markdown + JSON + weasyprint-PDF) to `pdl.root_dir/reports/YYYY/MM/DD/...` with a `<run_id>` for traceability.
- [ ] Every analyzed paper produces (a) an artifact row for the PDF, (b) an analyses row with key_findings + summary, (c) FTS-indexed extracted_text.
- [ ] Every blog post archived by `blog_search` (P10.0) produces an artifact row with kind=`pdf` (or kind=`html` if Playwright missing).
- [ ] **`StorageBackend` Protocol** conformance: `LibraryWriter` has zero direct sqlite3/sql calls; all SQL lives in `sqlite_backend.py`. Pop a `grep -n "sqlite3\|aiosqlite" deep_research/library/writer.py` and assert empty.
- [ ] **Schema migrations** are idempotent + ordered; bumping `schema_version` from v1 → v3 (synthetic empty-then-migrate test) applies all migrations cleanly with no duplicate-column errors.
- [ ] **`weasyprint` failure auto-falls back** to `xhtml2pdf` with a `WARNING` log; run completes successfully either way; both produce a readable PDF.
- [ ] **Both PDF renderers fail degraded path**: when `weasyprint` AND `xhtml2pdf` both error, fall back to a markdown-only artifact (`kind="report"`, `bytes_path` pointing at the `.md` file); emit a `WARNING`; the `reports` row is still inserted; the run does not crash.
- [ ] **`bytes_path` semantics**: for `kind=pdf` artifacts, `bytes_path` points to a single file (e.g. `artifacts/pdf/9f8e7d6c-openai.pdf`). For `kind=html` artifacts, `bytes_path` points to a directory containing `page.html` + `meta.json`. Document this convention in the `ArtifactRow` dataclass docstring.
- [ ] **Index/storage sizes**: an academic run with `max_papers=5` produces ≤5 PDF artifacts + 1 report PDF + ≤5 analyses rows. Storage impact ≤20MB. No JPEG persisted.
- [ ] Every existing test passes with `pdl.enabled=false` — zero behavior change for users who turn it off. This is covered by the P1/P2/P3/P4/P5/P6/P7/P8/P9 top-level acceptance criteria — no new top-level item is needed.

### Acceptance criteria (P10.5b — refresh foundation)

- [ ] **`LibraryWriter.refresh_needed(artifact_id)`** correctly returns True for stale-by-policy artifacts and False for fresh or research_report-kind artifacts.
- [ ] **`LibraryWriter.probe_upstream(artifact_id)`** correctly distinguishes upstream-unchanged (bumps `last_touched_at`, returns `(True, None)`) from upstream-changed (creates new artifact, inserts `artifact_versions` row, returns `(False, new_id)`).
- [ ] **`LibraryWriter.run_refresh_job()` is idempotent** — calling it twice in a row on an unchanged library second-call returns `considered=0, refreshed=0` (no spurious new-versions rows).
- [ ] **`library refresh` CLI command** exists with all documented flags (`--source-type`, `--tag`, `--artifact-id`, `--dry-run`, `--re-analyze`, `--once`); `--dry-run` produces zero network fetches.
- [ ] **Files**: `tests/test_library_writer.py` (artifact dedup, report archival, analysis insert, citation-edge insert, glossary upsert), `tests/test_library_storage_sqlite.py` (backend CRUD + migrations), `tests/test_library_pdf_render.py` (weasyprint primary + xhtml2pdf fallback), `tests/test_library_refresh.py` (`refresh_needed` / `probe_upstream` / `run_refresh_job` lifecycle + CLI flag handling).

### Risk register additions

See the canonical **Risk register** near the top of this file. P10.5a contributes risks #18–#23 (storage / weasyprint / concurrency / both-renderers-fail / Playwright-unavailable / SHA collision). P10.5b contributes risk #24 (upstream permanently 404s).

### Project structure additions (P10.5a)

```
deep_research/
└── library/                            # NEW package
    ├── __init__.py
    ├── writer.py                       # LibraryWriter (backend-agnostic)
    ├── pdf_render.py                   # weasyprint primary + xhtml2pdf fallback
    ├── cli.py                          # `library` CLI subcommand dispatcher
    └── storage/
        ├── __init__.py                 # get_backend(config) factory
        ├── base.py                     # StorageBackend Protocol
        ├── rows.py                     # ArtifactRow, ReportRow, AnalysisRow,
        │                                 #   CitationEdgeRow, TagRow, GlossaryEntry,
        │                                 #   RefreshJobRow, ArtifactVersionRow, SearchHit
        ├── sqlite_backend.py          # P10.5a default (the only backend in P10.5a)
        └── migrations/
            ├── sqlite/
            │   ├── 0001_initial.sql
            │   ├── 0002_add_glossary.sql
            │   └── 0003_add_refresh_foundation.sql
            └── postgres/
                └── README.md           # "Postgres migrations land in P12.0"
```

### Existing files modified (P10.5a — core storage + archival)

| File | Change |
|---|---|
| `agent.py` | Construct `LibraryWriter(storage=get_backend(config.pdl), …)` (only when `pdl.enabled`); wrap `run_research()` entry/exit; call `archive_report` after the path returns |
| `tools/arxiv.py` | After `arxiv_download_pdf` writes to disk, `await library.archive_pdf(...)` — defensive (writer unset: no-op) |
| `tools/fetch_page.py` | After successful content-extraction (blog or html), `await library.archive_html(...)` — opt via flag |
| `paths/url_source.py` | `url_source()` — after fetch returns content, `await library.ensure_artifact(...)` |
| `paths/academic.py` | After `analyze_paper_node` returns, `await library.record_analysis(...)`; after batch, `await library.record_citation_edges(...)` |
| `config.py` | Add `PDLConfig` pydantic schema (`enabled`, `root_dir`, `storage.{backend, sqlite.*}`, `refresh.{enabled, stale_after_days_by_source_type, refresh_concurrency, re_analyze_on_change, notify_on_change}`) |
| `config.example.yaml` | Add `pdl:` section with documented defaults |
| `pyproject.toml` | Add `weasyprint`, `aiosqlite`, `markdown` to default deps; `xhtml2pdf` to `pdf-fallback` extra; **reserve `postgres` extra** for P12.0 (declared but empty in P10.5a) |
| `README.md` | Add `pango` + `cairo` to system-binary prerequisites table; add library + `library refresh` CLI section to usage |

### Existing files modified (P10.5b — refresh foundation)

| File | Change |
|---|---|
| `library/cli.py` | Wire `--source-type`, `--tag`, `--artifact-id`, `--dry-run`, `--re-analyze`, `--once` flags into `run_refresh_job` |
| `config.py` | (no change — refresh config fields already landed in P10.5a) |

---
## P10.6 — Glossary generation

### Motivation

As the library accumulates across runs, terminology accumulates too. Users shouldn't have to Google "what does RM mean in this paper" when they re-encounter it three weeks later. A curated key-concepts + acronyms list, expended organically with each research run, lives in one markdown file the user can browse, search, version-control, and print.

### When glossary entries are produced

**Two phases of glossary generation:**

1. **Per-run glossary** (immediate, free): the synthesizing LLM call (`nodes/writer.py` for the deep path, `paths/academic.py:_synthesize_markdown` for academic, `paths/quick.py:_synthesize` for quick) is augmented with an extra JSON field `"glossary": [{term, kind, short_def, ...}]` in its output schema. Single LLM call → free — no extra round-trip.
   - The system message says: "If any non-trivial terms (or acronyms) appear in your answer, include a `glossary` array describing each. Aim for 0-10 entries; skip trivial words."
   - LLM is allowed to emit an empty array. No compensating LLM call if it omits the field.
2. **Cross-run glossary** (no LLM cost; rule-based dedup): a dedicated `nodes/glossarize.py` runner reads per-run entries from the DB and merges by `term_canonical` (lowercased, punctuation-stripped). Same acronym with different expansions → keep both (with a `WARN` log line). Same canonical term with new longer `long_def` → update the longer one. Mark `last_updated` on row change. This phase is **rule-based, no LLM call** — keeps cost zero.

The optional `library glossary --refresh` CLI subcommand (P12) runs an explicit reconcile LLM call to merge/split/reconcile entries when the cumulative rule-based merge starts producing duplicates or conflicts. Default behavior is rule-based only (free).

### `GlossaryEntry` shape

```python
@dataclass
class GlossaryEntry:
    term: str                              # display form ("RLHF")
    term_canonical: str                    # lowercased, punctuation-stripped ("rlhf")
    kind: Literal[
        "concept", "acronym", "method",
        "metric", "dataset", "model", "tool",
    ]
    short_def: str                         # <=1 sentence
    long_def: str | None                   # 1-3 sentences
    acronym_expansion: str | None          # "RLHF" -> "Reinforcement Learning from Human Feedback"
    related_terms: list[str]               # cross-link by canonical form
    confidence: float                       # 0..1, from analyzing-LLM confidence in its definition
    domain_tags: list[str]                 # ["RL", "alignment"]
```

### `glossary.md` format

Regenerated atomically each run (write to `.tmp`, then `os.rename`) so users never see a partially-written file. Header includes stats; body grouped by `domain_tags[0]`:

```markdown
# Personal Research Glossary

_Last updated: 2026-07-17 14:23 UTC  ·  347 terms  ·  12 domains  ·  40 first-seen-this-run_

---

## RL  ·  Alignment

### RLHF  ·  Reinforcement Learning from Human Feedback
**Concept**  ·  _First seen:_ [`20260717T1423RLHF-recursive`](reports/2026/07/17/20260717T1423RLHF-recursive.pdf)

A fine-tuning method that trains a reward model on human preference pairs and then optimizes the policy against it via PPO.

**Related:** PPO, RM, KL-penalty  ·  _Confidence: 0.93_

### PPO  ·  Proximal Policy Optimization
...

---

## NLP  ·  Tokenization

### BPE  ·  Byte-Pair Encoding
...
```

### CLI additions (P12 — implemented later, spec'd now)

```bash
deep_research library glossary                          # full markdown dump
deep_research library glossary --filter-tag RL          # filtered
deep_research library glossary --filter-run <run_id>    # which terms first showed up in this run
deep_research library glossary --find "tangent"        # FTS search
deep_research library glossary --export bibtex          # glossary as formal entries
deep_research library glossary --term RLHF              # detail view of one term
deep_research library glossary --refresh                # explicit LLM reconcile (opt-in; rare)
```

### LLM prompt augmentation (the only contract change with existing nodes)

For each of `paths/quick.py:_synthesize`, `paths/academic.py:_synthesize_markdown`, and `nodes/writer.py:write`:

- System message gets an appended paragraph: `If your answer includes non-trivial technical terms or acronyms, also emit a "glossary" array. Each entry: {term, kind, short_def, acronym_expansion?, related_terms?}. Aim for 0-10 entries; omit trivial words.`
- Response parsing already extracts `answer`/`citations`; add a parallel parser for `glossary`.
- If LLM omits `glossary` field → empty list. Never raise. Never call LLM again.

### Acceptance criteria

- [ ] Every `run_research()` call that produces a non-empty `Report.markdown` MAY append glossary entries (zero entries is allowed when the report is too short, contains an error, or the LLM omits the field).
- [ ] Glossary entries are deduplicated across runs by `term_canonical` (e.g. "rlhf" == "RLHF").
- [ ] Acronyms share a row with their expansion ("RLHF" row has `acronym_expansion="Reinforcement Learning from Human Feedback"`).
- [ ] `glossary.md` is regenerated atomically (write to `.tmp`, then rename) so users never see a partially-written file.
- [ ] Glossary generation does NOT fail the run if it errors; logged at `WARNING`; the run completes successfully and the Report returns.
- [ ] The synthesizing LLM prompt asks the model to optionally emit a `glossary` array; if omitted, no extra LLM call compensates.
- [ ] Cross-run dedup is rule-based (no LLM call); acronyms with conflicting expansions both kept (and a `WARN` logged).
- [ ] Test files: `tests/test_library_glossary.py` (per-run parse, cross-run dedup, acronym handling, markdown regeneration atomicity), `tests/test_nodes_writer_glossary.py` (LLM emits optional glossary field, omit-glossary is no-error), `tests/test_paths_quick_glossary.py`, `tests/test_paths_academic_glossary.py`.

### Risk register additions

See the canonical **Risk register** near the top of this file. P10.6 contributes risks #27–#30 (LLM glossary emission + cross-run dedup + UNIQUE semantics).

### Project structure additions (P10.6)

```
deep_research/
└── library/
    └── glossary.py            # NEW — markdown regeneration + FTS sync
└── nodes/
    └── glossarize.py           # NEW — rule-based cross-run merger (no LLM)
└── prompts/
    └── glossary_extract.txt    # NEW — the system-message paragraph added to deep/quick/academic synthesis
```

### Existing files modified

| File | Change |
|---|---|
| `nodes/writer.py:write` | Augment system message with glossary-extract paragraph; parse `glossary` from response; pass to `LibraryWriter.upsert_glossary_entries` |
| `paths/quick.py:_synthesize` | Same as above |
| `paths/academic.py:_synthesize_markdown` | Same as above |
| `library/writer.py` | `upsert_glossary_entries(entries, run_id) -> int` returns count of NEW entries added |

---
## Phasing notes (delta over the canonical table at top)

> The canonical phase plan lives near the top of this file. The rows below add **detailed scope deltas** for the P10.x family — they do not redefine phases. Where the two disagree, the canonical table wins.

| Phase | Delta / scope note |
|---|---|
| **P10.0** | Tool only; no `applied` path (that lands in P12.0). Optional academic-path blog-seeding shim = 5-line additive change to `paths/academic.py`. |
| **P10.5a** (split out) | Personal Digital Library v1 core with **pluggable storage backend** foundation. All `LibraryWriter` methods go through the Protocol; migrations `0001_initial` + `0002_add_glossary` + `0003_add_refresh_foundation` ship here so the columns exist from day one. |
| **P10.5b** (split out) | Refresh foundation logic — `LibraryWriter.{refresh_needed, probe_upstream, run_refresh_job}` + `library refresh` CLI one-shot command. Uses columns/tables already created by P10.5a's `0003` migration. Users cron `deep_research library refresh --once` themselves until P12.0's scheduler lands. |
| **P10.6** | Glossary generation: per-run LLM-call enrichment (no extra call) + rule-based cross-run dedup. `glossary.md` regenerated atomically each run. SQLite `glossary` table + FTS5 over definitions. |
| **P11** | Wire `asyncpraw`. Reddit access. |
| **P12.0** | (a) Postgres `StorageBackend` + asyncpg + conformance suite parameterized over both backends. (b) Long-running refresh scheduler (`apscheduler` + `croniter`) running `LibraryWriter.run_refresh_job` on a configurable cron; webhook + email notifications; daemonized `deep_research.scheduler` entrypoint. (c) `applied` path (`paths/applied.py`) — the **only** phase that introduces it. (d) Library CLI completes: `ls`, `find`, `show`, `tag`, `stats`, `prune`, `export-bibtex`, `glossary --refresh` LLM reconcile. (e) FastAPI microservice + Dockerfile + poppler setup. |
| **P12.5** | Optional web UI for browsing the library. |

### Refresh-scheduler timeline recap

- **P10.5a ships**: DB schema (`artifact_versions`, `refresh_jobs`, the three `artifacts` refresh columns) so the columns exist from day one — but no logic touches them yet.
- **P10.5b ships**: `LibraryWriter` public methods (`refresh_needed`, `probe_upstream`, `run_refresh_job`) + `library refresh` CLI one-shot command. Users cron `deep_research library refresh --once` themselves if they want periodic refresh before P12.
- **P12.0 ships**: long-running `deep_research scheduler` process; cron config honored; webhook/email notifications.

### Storage-backend timeline recap

- **P10.5a ships**: `StorageBackend` Protocol + SQLite backend (default). All `LibraryWriter` methods go through the Protocol.
- **P12.0 ships**: Postgres backend + `postgres` extra (`asyncpg`) + per-backend migrations + **conformance test suite** parameterized over both fixtures (SQLite always in CI; Postgres when `DEEP_RESEARCH_TEST_PG_DSN` is set).

---

## Conformance test suite (P12.0 spec — captured here for the P10.5a design contract)

Both backends MUST satisfy every test in these parameters files; drift is a release blocker.

```
tests/library/conftest.py            # fixtures: sqlite_backend_fixture, postgres_backend_fixture
tests/library/test_artifact_crud.py
tests/library/test_report_crud.py
tests/library/test_glossary_upsert.py
tests/library/test_refresh_jobs.py
tests/library/test_artifact_versions.py
tests/library/test_full_text_search.py
```

Each test file is `pytest`-parameterized over both fixtures. SQLite runs always; Postgres skips with a clean message when `DEEP_RESEARCH_TEST_PG_DSN` env var is unset (matches the existing `requires_tavily` / `requires_llm_endpoint` fixture pattern in `tests/conftest.py`).

---

## P13 — Library-first recall (prior knowledge injection)

### Motivation

Every deep research run today starts from scratch: the planner decomposes the query, the researcher hits Tavily/arxiv/Reddit, and the critic decides if more is needed. But the Personal Digital Library (P10.5a) already accumulates analyses, summaries, and key findings across runs. The second time you research "transformer attention mechanisms" the library may already contain 3 papers' worth of analyses, but the researcher re-fetches everything from the web.

P13 adds a **recall step** before each researcher dispatch: query the library's FTS5 index for prior analyses matching the sub-question. When matches are found, inject their summaries + key findings as "prior research context" into the researcher's system prompt. The researcher then uses web_search only for the *delta* — what the library doesn't already cover. Same for the academic path: before synthesizing, recall prior analyses of the same papers.

### Design

**New module**: `nodes/recall.py`

```python
async def recall(
    query: str,
    storage: StorageBackend | None,
    max_results: int = 5,
) -> list[dict]:
    """Query the library's FTS5 index for prior analyses matching `query`.

    Returns a list of dicts with keys: artifact_id, title, summary,
    key_findings, methodology, source_type, url. Empty list when
    storage is None or no matches found.
    """
```

- Uses `storage.full_text_search(query, kind="any", limit=max_results)` — the FTS5 index already exists over `analyses.summary` and `analyses.key_findings`.
- Results are formatted as a "Prior research context" markdown block and injected into the researcher's system prompt.
- When storage is None (PDL disabled), returns empty list — no change in behavior.

**Modified paths**:

- `paths/deep.py`: before dispatching each sub-question's researcher, call `recall(sub_q.question, writer.storage)`. If results found, prepend them as context to the researcher's prompt.
- `paths/academic.py`: before synthesizing, recall prior analyses of the same arxiv_ids. Inject as additional context.
- `paths/quick.py`: before the LLM synthesis, recall prior knowledge about the query.

**Threading**: the `LibraryWriter` is already available in every path (threaded from `agent.py`). The recall function accesses `writer.storage` — which is the `StorageBackend` Protocol — so it works with both SQLite and Postgres backends.

**Config additions**: none. The feature is always-on when PDL is enabled. Users who want to skip the recall step can disable PDL (`pdl.enabled: false`), which means `storage` is None and recall returns empty.

### Acceptance criteria

- [ ] When PDL is disabled (`pdl.enabled: false`), recall returns empty list — zero behavior change
- [ ] When PDL is enabled and the library contains analyses matching the sub-question, the researcher's system prompt includes prior context
- [ ] Prior context does NOT prevent the researcher from calling web_search — it only adds information; the researcher decides what to fetch
- [ ] Recall results are deduped by artifact_id before injection
- [ ] All existing tests pass (no regressions)

### Risk register additions

31. Recall returns stale/outdated summaries for papers whose content has changed upstream — mitigated by `refresh_after_at` staleness check; only recall artifacts whose `refresh_after_at` is still valid.
32. Recall results cause the researcher to skip web_search entirely when prior context is sufficient — this is the desired behavior; the researcher's tool-calling loop still decides what to fetch.
33. FTS5 keyword matching misses semantically-similar queries — mitigated by embedding-based recall (P13.1, future); FTS5 is the first layer, vector search is the second.

