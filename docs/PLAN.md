# Deep Research Agent — Master Plan

> **Status**: v5 — final, proceeded to implementation.
> **Last update**: Session start. Implementation underway.
> **Resume guidance**: If a new session is interrupted, read this file first; it contains every locked-in decision. Then read `/Users/xun/dev/deep_research/docs/IMPLEMENTATION_LOG.md` for progress state.

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
│   ├── __init__.py               # exports: run_research, AgentConfig, Report
│   ├── __main__.py               # python -m deep_research → cli.app()
│   ├── agent.py                  # run_research(query, config) → Report
│   ├── config.py                 # AgentConfig (pydantic, Strict)
│   ├── state.py                  # ResearchState + AcademicState + CitationGraph
│   ├── citations.py              # Citation model; dedup; bibliography + .bib generator
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
│   │   ├── academic.py           # recursive citation graphite traversal
│   │   └── url_source.py         # URL detection + analyze_source runner
│   │
│   ├── nodes/
│   │   ├── planner.py            # used by paths.deep
│   │   ├── researcher.py         # used by paths.deep
│   │   ├── critic.py             # used by paths.deep
│   │   ├── writer.py             # used by paths.deep + academic (synthesis)
│   │   ├── analyze_paper.py      # LLM call: summary + key_references (academic)
│   │   └── analyze_source.py     # LLM call: summary + claims + follow_ups (url_source)
│   │
│   ├── tools/
│   │   ├── base.py               # Tool Protocol + ToolRegistry
│   │   ├── web_search.py         # Tavily → SearXNG fallback chain
│   │   ├── fetch_page.py         # httpx + trafilatura + disk cache
│   │   ├── browser.py            # Playwright MCP client (async context manager)
│   │   ├── arxiv.py              # arxiv lib + httpx PDF download + resolve_id
│   │   ├── pdf.py                # pypdf + pdfplumber + pdf2image + vision embed builder
│   │   ├── reddit.py             # STUB: NotImplementedError; lazy asyncpraw import
│   │   ├── url_detector.py       # extract first URL from query text
│   │   └── url_classifier.py    # classify: arxiv | pdf | html
│   │
│   ├── report/
│   │   ├── markdown.py           # render Report (incl. academic graph bibliography)
│   │   ├── bibtex.py             # emit citation graph → .bib
│   │   └── json_export.py       # emit Report (incl. graph) → .json
│   │
│   ├── cache.py                  # disk cache wrapper for pages/pdfs
│   ├── cli/
│   │   └── app.py                # typer app
│   └── prompts/
│       ├── classifier.txt
│       ├── planner.txt
│       ├── researcher.txt
│       ├── critic.txt
│       ├── writer.txt
│       ├── quick_summary.txt
│       ├── analyze_paper.txt
│       └── analyze_source.txt
│
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── sample_page.html
    │   └── sample_paper.pdf
    ├── test_agent.py
    ├── test_classifier.py
    ├── test_paths_quick.py
    ├── test_paths_academic.py
    ├── test_paths_url_source.py
    ├── test_tools_arxiv.py
    ├── test_tools_pdf.py
    ├── test_tools_web_search.py
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

## Phase plan

| Phase | Scope | Deliverable |
|---|---|---|
| **P1** | Scaffold + README + config + state + path stubs returning placeholder Reports. Mock-LLM dry run of all routing paths. | All paths reachable end-to-end with fakes. |
| **P2** | paths.quick + tools/web_search (Tavily) + tools/fetch_page + citations. | Real quick results. |
| **P2.5** | paths.url_source ANALYZE mode (no follow-up). | URL analysis for arxiv + blogs. |
| **P3** | paths.deep full loop. | Deep report. |
| **P4** | Web search multi-backend + browser fallback in fetch_page. | Robust fetching. |
| **P5** | tools/arxiv full pipeline. Wired into deep path. | arxiv surfaced in deep. |
| **P6** | tools/pdf (pypdf+pdfplumber+pdf2image+vision). Used by url_source + deep + academic. | PDF vision understanding. |
| **P7** | paths.academic recursive mining + paths.url_source FOLLOW-UP mode. | Academic recursion + URL follow-up research. |
| **P8** | tools/browser via Playwright MCP. Surfaced to all paths. | Dynamic page fetch. |
| **P9** | CLI polish: flags, rich progress, finalized README. | Production-ready CLI. |
| **P10 (later)** | Wire asyncpraw. | Reddit access. |
| **P11 (later)** | FastAPI microservice + dockerfile + poppler setup. | Deployable service. |

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
- [ ] `python -m deep_research "..." --range academic --max-depth 2 --max-papers 10 --dump-graph refs.bib` produces Markdown + `refs.bib`.
- [ ] `python -m deep_research "..." --quick` bypasses classifier.
- [ ] `python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"` produces a 1-2 page Markdown analysis w/ summary, key claims, methodology, limitations; no auto follow-up research.
- [ ] `python -m deep_research "https://arxiv.org/abs/2401.12345 — what are the gaps?"` produces analysis + a separate "follow-up research" section.
- [ ] `python -m deep_research "https://blog.example.com/post — key claims?"` works for plain HTML; falls back to Playwright MCP if trafilatura returns low content.
- [ ] `python -m deep_research --url-source "https://example.com/paper.pdf"` auto-detects PDF and uses vision path.
- [ ] `--url-source` overrides regex detection.
- [ ] When no URL detected in query, `--url-source` flag raises a clean error.
- [ ] `reddit.enabled: false` allows runs without `asyncpraw` installed.
- [ ] `agent.classifier.enabled: false` routes everything to `deep`.
- [ ] `await run_research(query, config)` works from a fresh Python process (microservice-ready).
- [ ] Academic recursive: capped at `max_papers=3` completes in <5 min when seeded from 1 paper.
- [ ] Citation graph traversal dedups via `arxiv_id`.
- [ ] All tests green: `pytest -q`.

---

## Risk register

1. Vision cost at academic depth=2: up to ~375 page renders → ~100 VLM calls. Multi-minute runs accepted.
2. Classifier false-routes: returns `rationale` in report; users can pass flags to override.
3. Recursive mining loops on famous papers: `max_key_references_to_recurse: 5` cap — only top-5 LLM-scored refs enqueued per paper.
4. arXiv 3s rate limit: handled by `arxiv` lib + `asyncio.Semaphore(2)` around arxiv tool.
5. SearXNG public instances increasingly AI-restricted: README recommends self-hosting via Docker.
6. poppler missing at runtime: catch `PDFInfoNotInstalledError` at startup, emit clear error pointing to README.
7. MCP SDK v2 churn: pin `mcp>=1.27,<2`.
8. Node.js LTS absent: `npx -y @playwright/mcp@latest` will fail; detect & emit clear error in P8.
9. Paywalled / login-walled URLs in url_source mode: extractor's content-length heuristic flag auth-required; emit Report saying so.
10. Very large PDFs: `fetch_pdf_size_limit_mb` cap, clean error if exceeded.
11. JS-only blogs return empty HTML: auto-fallback to `tools.browser` when `trafilatura` extraction is <N chars (configurable).
12. Follow-up trigger heuristic too eager: documented trigger phrase list (visible in README); conservative default.

---

## Follow-up trigger phrases (URL-source follow-up mode)

Conservative default list (case-insensitive substring match):

```
gaps, what's missing, what is missing, omitted, not mentioned,
limitation, limitations, shortcoming, shortcomings, weakness, weaknesses, flaw, flaws,
counterexample, counterexamples, refute, refutation, disprove, disprove,
verify, validate, falsify, check the claims, fact-check, fact check,
comparison of, compare to, alternative, alternatives, competing,
what else, what other
```

User can extend via `config.url_source.follow_up_trigger_phrases: []`.

---

## Resume guidance

If implementation gets disrupted mid-session, do the following in the new session:

1. Read this file (`docs/PLAN.md`) to refresh full plan.
2. Read `/Users/xun/dev/deep_research/docs/IMPLEMENTATION_LOG.md` to see what's done.
3. Check `/Users/xun/dev/deep_research/` file tree (use `ls -la` or `find . -type f`).
4. Run `cd /Users/xun/dev/deep_research && uv sync` to verify deps still resolve.
5. Run `uv run python -m deep_research --help` to confirm CLI still works.
6. Continue from the next pending item in `IMPLEMENTATION_LOG.md`.

Update `IMPLEMENTATION_LOG.md` as you complete each module/phase.
