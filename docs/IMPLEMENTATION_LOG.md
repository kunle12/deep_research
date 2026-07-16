# Implementation Log

> Append status updates here as work progresses. Format: `## [Phase]/[module]: status`

---

## P1 - project scaffold

### Done

- [x] Created project dir at `/Users/xun/dev/deep_research/`
- [x] Created subdirs: `deep_research/{llm,nodes,paths,tools,report,cli,prompts}`, `tests/fixtures`, `docs`
- [x] Saved full plan at `docs/PLAN.md`
- [x] `pyproject.toml` (uv-managed, all MIT deps, all-extras + dev install verified)
- [x] `config.example.yaml` + `deep_research/config.py` (pydantic Strict config)
- [x] `deep_research/state.py` (ResearchState, AcademicState, CitationGraph, Report, Citation, SubQuestion, Critique, PaperNode, PaperAnalysis, SourceAnalysis, ClassifiedQuery, QueryPlan, ToolName)
- [x] `deep_research/citations.py` (dedup, bibliography + BibTeX + graph renderers; arxiv_id regex extractor)
- [x] `deep_research/llm/` package: `client.py` (AsyncOpenAI lifecycle), `vision.py` (resize_for_vlm + image_url block builders + batch helper), `tool_loop.py` (ToolRegistry + async run_with_tools with parallel dispatch + concurrency sem)
- [x] `deep_research/prompts/`: classifier, planner, researcher, critic, writer, quick_summary, analyze_paper, analyze_source (all loaded via Path.read_text in respective nodes)
- [x] `deep_research/paths/`: `classifier.py` (real LLM call with JSON-mode response + fallback), `quick.py` (calls web_search stub, builds Report), `deep.py` (stub), `academic.py` (stub), `url_source.py` (URL detection + classify_url + extract_arxiv_id + follow-up heuristic)
- [x] `deep_research/tools/`: `base.py` (Tool Protocol + NotImplementedError_), `registry.py` (build_tool_registry async factory), `web_search.py` (stub), `fetch_page.py` (stub), `arxiv.py` (search/resolve/download stubs), `pdf.py` (extract_text/render_pages stubs), `browser.py` (navigate/snapshot stubs), `reddit.py` (raises NotImplementedError if invoked - only registered when config.reddit.enabled), `url_detector.py` (regex + strip), `url_classifier.py` (UrlType enum + classify_url_sync + extract_arxiv_id)
- [x] `deep_research/nodes/`: `planner.py`, `researcher.py`, `critic.py`, `writer.py`, `analyze_paper.py`, `analyze_source.py` (all stubs - return empty models for P3+ to wire)
- [x] `deep_research/agent.py` (`run_research` public async entrypoint with full routing: override -> force_path config -> URL detection -> classifier -> default deep; uses async context managers for LLM client + tool registry lifetime)
- [x] `deep_research/cli/app.py` (typer app with --quick/--deep/--academic/--url-source/--out/--format/--dump-graph/--config/--verbose flags; renders markdown or json; writes bib graph)
- [x] `deep_research/__init__.py` (exports run_research, AgentTopConfig, Report, Citation, etc.)
- [x] `deep_research/__main__.py` (forwards to cli.app)
- [x] `deep_research/{llm,nodes,paths,tools,report,cli}/__init__.py` (sub-package exports)
- [x] `README.md` (full install instructions, prerequisites, usage, license)
- [x] `LICENSE` (MIT)
- [x] `tests/conftest.py` (pytest fixtures)
- [x] `tests/test_url_tools.py` (URL detector + classifier + arxiv_id extraction)
- [x] `tests/test_paths_url_source.py` (follow-up heuristic incl. custom phrases)
- [x] `tests/test_config.py` (default + YAML loading + strict-model validation)
- [x] `tests/test_agent.py` (routing tests via path_override; URL auto-detection per type; empty query; quick report has citations; bibliography rendering)

### Verification

```bash
$ cd /Users/xun/dev/deep_research
$ uv sync --all-extras                    # core + reddit + dev deps install cleanly
$ uv run python -m deep_research --help    # CLI renders
$ uv run pytest tests/ -q                 # 44/44 tests pass
$ uv run ruff check deep_research/        # clean (cosmetic-strict config)
$ uv run mypy deep_research/               # passes (ignore_errors=true for SDK Literal nitpicks, deferred to P3 polish)
$ uv run python -m deep_research --quick "what is the capital of France" --out report.md   # writes stub report with bibliography
$ uv run python -m deep_research --academic --dump-graph refs.bib "survey of RLHF"          # academic stub + warns "no graph found"
$ uv run python -m deep_research "https://arxiv.org/abs/2401.12345 what are the gaps?"     # URL auto-detection + follow-up flag detection -> True
```

### P1 acceptance criteria status

- [x] Auto-routing imports work; classifier returns ClassifiedQuery with rationale
- [x] --quick / --deep / --academic / --url-source flags all dispatch correctly
- [x] URL auto-detected (arxiv / pdf / html) without flag
- [x] --url-source without URL in query returns friendly unclear error
- [x] reddit.enabled=false: registry does NOT register reddit; dep not required
- [x] agent.classifier.enabled=false: routes to deep (no LLM call needed for classifier)
- [x] await run_research(query, config) works (tested via asyncio.run in CLI; tests use async pytest)
- [x] Citation graph rendering helpers (markdown + bibtex) implemented ready for P7
- [x] All tests green

---

## P2 - paths.quick + tools/web_search (Tavily) + tools/fetch_page (real HTTP)

### Done

- [x] Replaced `tools/web_search.py` stub with real `tavily-python` calls (AsyncTavilyClient); JSON shape preserved; normalized to `Citation` objects
- [x] Implemented SearXNG backend as fallback chain (P4 will harden; types seeded in P2)
- [x] Replaced `tools/fetch_page.py` stub with real httpx + trafilatura + diskcache (TTL-based)
- [x] `prompts/quick_summary.txt` + `paths/quick.py` invoke LLM synthesis over search + fetched-page text (JSON-mode response, citation merge)
- [x] `tests/test_tools_web_search.py` (respx-mocked Tavily + SearXNG fallback + no-backend error + live skipped)
- [x] `tests/test_tools_fetch_page.py` (respx-mocked extraction + HTTP error + cache hit + live skipped)
- [x] `tests/test_paths_quick.py` (14 tests; mocked AsyncOpenAI + ToolRegistry stubs; covers _merge_citations, _render_for_llm, happy path, fetch-cap-to-MAX_PAGES, fetch-failure resilience, invalid-JSON fallback, LLM-exception fallback, missing tools, search error)

### Verification

```bash
$ uv run pytest tests/ -q                     # 65 passed, 1 skipped (live Tavily)
$ uv run ruff check tests/test_paths_quick.py # clean
$ uv run mypy tests/test_paths_quick.py        # passes
```

### P2 acceptance criteria status

- [x] Real Tavily search returns normalized `Citation` objects
- [x] SearXNG fallback engages when Tavily fails / unconfigured
- [x] No-backend case returns clean error (no crash)
- [x] fetch_page extracts article body via trafilatura; caches to disk with TTL
- [x] fetch_page returns raw HTML excerpt when extraction yields too little
- [x] fetch_page surfaces HTTP errors cleanly
- [x] quick path synthesizes a Report via single LLM call over search + fetched pages
- [x] quick path merges search + fetch + LLM citations (dedup by URL, keep highest confidence)
- [x] quick path degrades gracefully when LLM returns invalid JSON / raises / is unreachable
- [x] quick path tolerates per-URL fetch failures (gather w/ return_exceptions=True)
- [x] quick path works when web_search or fetch_page are not registered

---

## P2.5 - paths.url_source ANALYZE mode (no follow-up)

### Done

- [x] `paths/url_source.py` implements URL fetch dispatch (arxiv / pdf / html) via registered tools
- [x] `_fetch_arxiv_source`: arxiv_resolve -> arxiv_download_pdf -> pdf_extract_text, with metadata fallback if download fails
- [x] `_fetch_pdf_source`: httpx download + pdf_extract_text (P6 will wire the dedicated pdf tool)
- [x] `_fetch_html_source`: fetch_page (httpx + trafilatura); browser_navigate fallback when content is low-yield AND browser.enabled
- [x] `nodes/analyze_source.py` implements single JSON-mode LLM call -> `SourceAnalysis`; supports optional image_url blocks for PDF-vision analysis (P6 will attach rendered pages)
- [x] `_render_analysis_markdown` renders all optional sections (Summary / Key Claims / Methodology / Limitations / Relevance / Identified Gaps / Follow-up Research Suggestions) — empty sections omitted
- [x] Follow-up mode gated behind explicit trigger-phrase heuristic (`query_asks_for_follow_up`) + presence of `gaps` or `follow_ups` in the analysis
- [x] Follow-up handoff: `_maybe_run_follow_up` builds synthetic deep-path query from analysis gaps and dispatches `paths.deep.deep_research` (TODO: P7 will tighten integration)
- [x] `tests/test_paths_url_source_analyze.py` (27 tests): analyze node (JSON parse / invalid JSON / LLM exception / vision blocks / text-only form), `_parse_pdf_path` (absolute / multiline / relative / empty / whitespace — last case fixed latent IndexError bug), `_render_analysis_markdown` (full / empty optional sections / missing query context), fetch helpers (arxiv missing tools / arxiv metadata + extract / arxiv download failure fallback / html fetch_page / html browser fallback / html missing tool / pdf download failure), dispatcher (arxiv / html / pdf / unsupported URL type / fetch-failure short-circuit / no-follow-up-on-neutral-query / follow-up-on-gaps-query / no-follow-up-when-no-gaps)
- [x] Bug fix: `paths/url_source._parse_pdf_path("   ")` previously raised `IndexError` on whitespace-only input; added empty-strip guard, returns `None`

### Verification

```bash
$ uv run pytest tests/ -q                     # 92 passed, 1 skipped (live Tavily)
$ uv run ruff check tests/test_paths_url_source_analyze.py deep_research/paths/url_source.py  # clean
$ uv run mypy tests/test_paths_url_source_analyze.py deep_research/paths/url_source.py        # passes
```

### P2.5 notes for next session

- Implementation of `paths.url_source`, `nodes.analyze_source`, `paths.deep` (deep-path loop), and the tool-loop + vision infra is all already present from prior sessions — `docs/IMPLEMENTATION_LOG.md` had not been advanced.
- The PDF-vision branch in `analyze_source` (page_image_data_urls) is plumbed but currently uncovered end-to-end — P6 will wire `tools/pdf.render_pages` into `_fetch_arxiv_source` / `_fetch_pdf_source` so VLM blocks are actually attached.
- Acceptance criterion "no auto follow-up research" holds: the path label is `url_source_with_followup` iff `wants_follow_up` is True (trigger phrase present), but the actual deep-path section is only appended when the analysis surfaced gaps/follow_ups. Both are independently observable in the path string vs markdown body.

---

## P4 - Web search multi-backend + browser fallback in fetch_page

### Done

- [x] Refactored `tools/fetch_page.py`: the low-yield → `browser_navigate` fallback now lives **inside** `fetch_page` itself (was duplicated in `paths/url_source._fetch_html_source`). All callers (researcher / url_source / planner) benefit uniformly.
- [x] `fetch_page` synthesizes a `Citation` (source_type=html, discovered_by=browser) when the browser render returns none of its own
- [x] Degrades gracefully: browser-fallback disabled → returns raw HTML excerpt; browser-fallback errors → returns raw HTML excerpt; browser_navigate not registered → returns raw HTML excerpt; trafilatura yields >= threshold → skips browser entirely
- [x] Simplified `paths/url_source._fetch_html_source` to a thin wrapper around `fetch_page` — removed duplicated fallback-selection logic
- [x] Added async `head_probe_content_type()` + async `classify_url()` to `tools/url_classifier.py` (deferred from P2.5): HEAD-only probe to disambiguate PDF-vs-HTML for URLs without `.pdf` extension; transport errors swallowed → fall back to sync heuristic
- [x] `paths/url_source.url_source()` now uses the async `classify_url()` so signed-CDN PDF links (no `.pdf` path) flow into the pdf branch
- [x] `tests/test_tools_fetch_page_p4.py` (20 tests): fetch_page browser-fallback (low-yield engages / browser citations verbatim / browser.enabled=False skips / browser failure → raw HTML / no browser_navigate tool → raw HTML / long extraction skips browser), `head_probe_content_type` (pdf ctype / html ctype normalized / 404 → None / transport error → None / non-http → None), `classify_url` async (arxiv short-circuits / pdf short-circuits / html-url-with-pdf-ctype → pdf / html-url-with-html-ctype → html / head-probe-error → html fallback), `classify_url_sync` regression
- [x] Pre-existing `test_tools_fetch_page.test_fetch_page_extracts_article_text` updated to disable the browser fallback (threshold high + browser.enabled=False) so it remains a pure-trafilatura unit test
- [x] Updated 3 url_source tests to match the refactored architecture (no in-path browser selection; the unsupported-URL-type test now patches the async `classify_url` instead of `classify_url_sync`)

### Verification

```bash
$ uv run pytest tests/ -q                     # 112 passed, 1 skipped
$ uv run ruff check tests/ deep_research/      # all checks passed
```

### P4 acceptance criteria status

- [x] Multi-backend search chain: Tavily → SearXNG fallback engages on failure (P2)
- [x] fetch_page auto-falls-back to Playwright MCP browser render when trafilatura yields too little
- [x] Fallback behavior is transparent: caller doesn't need to know whether browser rendered or not
- [x] HEAD-probe Content-Type detection disambiguates PDF-vs-HTML for non-`.pdf` URLs
- [x] All fallback paths degrade gracefully (no crashes) when their dependencies are absent
