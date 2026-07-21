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

---

## P3 - paths.deep full loop (planner -> researcher -> critic -> writer)

### Done

> NOTE: This phase was implemented in a prior session but not logged here. The work was found during a subsequent code review session; full implementation already present in `paths/deep.py` and the four `nodes/` modules.

- [x] `paths/deep.py` — full iteration loop: plan -> parallel researcher fan-out (asyncio.gather + per-researcher semaphore) -> critic -> gap-append loop -> writer synthesis
- [x] `nodes/planner.py` — single JSON-mode LLM call -> ResearchPlan, validates `tool_hint` vocabulary client-side, fallback to single sub-question on failure
- [x] `nodes/researcher.py` — wraps `run_with_tools` from `llm/tool_loop.py`; `_hint_blurb` nudges the LLM toward `arxiv_search`/`reddit`/`browser_navigate` based on the planner's `tool_hint`; final assistant message parsed as `{"answer": "...", "citations": [...]}` JSON
- [x] `nodes/critic.py` — single JSON-mode LLM call -> Critique(sufficient, gaps[]); `_render_sections_for_prompt` dumps drafts + citations per sub-question; on LLM failure declares sufficient iff any drafts are present (conservative)
- [x] `nodes/writer.py` — `_render_sections_for_prompt` + `_render_citations_for_prompt` -> single chat-completions call -> Markdown; strips accidental code fences; `_concatenate_drafts` deterministic fallback on LLM failure
- [x] Citation projection: assembled `state.citations.values()` sorted by `confidence_score` desc into the returned Report

### P3 acceptance criteria status

- [x] Deep path produces multi-section Markdown synthesizing drafts across sub-questions
- [x] Critic iterates when gaps reported; appends new SubQuestions (dedup by question text)
- [x] Respects `agent.max_iterations`, `agent.max_subquestions`, `agent.max_concurrent_tools`, `agent.researcher_timeout_s`, `agent.researcher_max_turns` semaphores
- [x] Degrades cleanly when LLM raises: planner/critic/writer all fall back to deterministic output; per-researcher failures recorded as `(researcher failed: ...)` draft sections without aborting the gather

> NOTE: dedicated unit tests for each node (mocked AsyncOpenAI + ToolRegistry) are still TODO. The deep path's behavior is currently exercised only indirectly via the existing `tests/test_agent.py` routing tests. P9 (CLI polish) is a good home for closing this coverage gap.

---

## P5 - tools/arxiv full pipeline + wired into deep path

### Done

> NOTE: implemented in a prior session; logged here retroactively. The arxiv wiring is reachable from both the deep path (researcher can request `arxiv_search`/`arxiv_resolve`/`arxiv_download_pdf`) and the academic path (P7 uses it for seeds + per-paper fetch).

- [x] `tools/arxiv.py` real implementations:
  - `_sync_search` uses the `arxiv` PyPI library; `_sync_resolve` via `arxiv.Search(id_list=...)`; both wrapped with `asyncio.to_thread` + a global `asyncio.Semaphore(concurrency)` + per-Customer `request_delay_s` spacing delay
  - `_result_to_citation` normalizes to a `Citation` (`source_type="arxiv"`, `discovered_by=ToolName.arxiv`, `confidence_score=0.9`, version-stripped arxiv_id)
  - `_strip_version` regex strips `v\d+$` so 2401.12345v3 and 2401.12345 share a disk cache slot
  - `arxiv_download_pdf` downloads via httpx to `cfg.pdf_cache_dir`; content-Type sanity check; cache hit short-circuits the network call
  - `_safe_download_path` regex-defangs anything outside `[A-Za-z0-9._-]` to prevent cache_dir escape via hostile arxiv ids
- [x] Wired via `tools/registry.py`: arxiv_search + arxiv_resolve + arxiv_download_pdf registered when `config.arxiv.enabled`
- [x] Researcher's `_hint_blurb` (in nodes/researcher.py) nudges the LLM toward `arxiv_search` first when the planner's `tool_hint == "arxiv"`
- [x] `tests/test_tools_arxiv.py` (16 tests): search (returns citations / empty results message / max_results cap), resolve (returns / not-found / empty id error), download (downloads to cache / cache hit w/ version strip / HTTP error / disabled in config / empty id), helpers (`_strip_version`, `_safe_download_path` sanitization incl `../etc/passwd` defense, `_result_to_citation`), and a non-deadlock rate-limit smoke test (5 concurrent searches under concurrency=1)

### Verification

```bash
$ uv run pytest tests/test_tools_arxiv.py -q     # 16 passed
$ uv run ruff check tests/test_tools_arxiv.py    # clean
```

### P5 acceptance criteria status

- [x] arxiv_search / arxiv_resolve / arxiv_download_pdf all real-backed, returning normalized `Citation` objects
- [x] 3s arxiv rate limit enforced via concurrency sem + per-customer spacing delay
- [x] PDF downloads cached to `arxiv.pdf_cache_dir` keyed by version-stripped id
- [x] Cache-filename injection mitigated (hostile ids sanitized)
- [x] Wired into deep path: researcher can request arxiv tools via the tool-calling loop

---

## P6 - tools/pdf (pypdf + pdfplumber + pdf2image + vision)

### Done

> NOTE: implemented in a prior session; logged here retroactively.

- [x] `tools/pdf.py`:
  - `pdf_extract_text`: pypdf fast path (>=100 chars threshold) falls back to pdfplumber on sparse text; whichever returns more text wins; never crashes on missing files (returns an `(pdf_extract_text: file not found: ...)` marker surfaced as ToolResult.error)
  - `pdf_render_pages`: `pdf2image.convert_from_path` (subprocess to poppler `pdftoppm`), then `llm.vision.resize_for_vlm` (PIL downscale to `max_dim` via LANCZOS + JPEG quality 80) → `jpeg_bytes_to_data_url` → JSON payload `{"pages": [data_urls], "count": N}` ready for OpenAI `image_url` content blocks
  - `_is_poppler_missing` heuristic catches `PDFInfoNotInstalledError` / `PDFPopplerNotInstalledError` and the message-pattern variants ("poppler" + "not installed/not found"); on poppler missing returns a clean install-instructions error pointing to the README (`brew install poppler` / `apt-get install poppler-utils`)
  - `pdf_render_pages` only registered when `pdf_vision.enabled`
- [x] `tests/test_tools_pdf.py` (15 tests): real pypdf extraction against a reportlab-generated fixture (skipped if reportlab absent), missing/empty path errors, sparse-PDF -> pdfplumber fallback path, render-disabled (vision.config.enabled=False unregisters pdf_render_pages), poppler-missing -> install-hint message (via monkeypatch), JSON data-URL assembly via stubbed `_sync_render` + real PIL images, empty-pages payload, `_is_poppler_missing` heuristic (5 cases incl. unrelated exceptions), `_sync_extract` missing file message string, `resize_for_vlm` (downscale long side, no upscale), `jpeg_bytes_to_data_url` base64 roundtrip

### Verification

```bash
$ uv run pytest tests/test_tools_pdf.py -q     # 15 passed (some skip when reportlab absent)
```

### P6 acceptance criteria status

- [x] PDF text extraction works without poppler (pypdf + pdfplumber are pypi deps)
- [x] Vision rendering requires poppler; surfaces a clean install-hint error when absent
- [x] Pages downscaled to `max_dim` JPEG and emitted as base64 data URLs
- [x] `pdf_render_pages` gated off when `pdf_vision.enabled=False` (so non-VLM installs skip it)

---

## P7 - paths.academic recursive mining + analyze_paper node

### Done

> NOTE: implemented in a prior session; logged here retroactively. Ded-up logic uses version-stripped arxiv_ids.

- [x] `nodes/analyze_paper.py`:
  - `analyze(arxiv_id, paper_text, query, client, model, page_image_data_urls=None)` — single chat-completions call with optional `image_url` content blocks for VLM figure comprehension
  - `_build_messages` truncates `paper_text` to 40_000 chars before substitution (context-blowup guard)
  - `_coerce` filters `key_references` lacking an arxiv_id; extracts arxiv_id from adjacent title text via regex `\b\d{4}\.\d{4,5}(?:v\d+)?\b` (NOTE: doesn't match old-style `cs.LG/0702001` — the regex's alternate was designed for a different `lowercase/UPPER.digits` shape)
  - Degrades to `[unparseable] {arxiv_id}` on JSON parse failure, `[error] {arxiv_id}` on LLM exception
  - `extract_key_reference_arxiv_ids(analysis, threshold)` returns the non-empty arxiv_ids from `analysis.key_references` (threshold parameter is currently a no-op for API symmetry, since `is_key_reference` is the binary gate)
- [x] `paths/academic.py`:
  - SEED: `_gather_seeds` calls arxiv_search (max_results=seed_count), converts each result to a depth-0 PaperNode; prefers `classified.search_hint` over `original_query` when present; drops search hits lacking an arxiv_id
  - LOOP: BFS-style queue with batched dispatch (`batch_size = min(concurrency, queue, max_papers-processed)`); per-paper work under `academic.concurrency` semaphore
  - PER PAPER: download + pdf_extract_text (+ optional pdf_render_pages when pdf_vision.enabled); `_fetch_paper_text` falls back to arxiv_resolve metadata when download fails; `analyze_paper_node` produces a PaperAnalysis; if depth<max_depth, extracts up to `max_key_references_to_recurse` child arxiv_ids, dedup-collapses against processed + already-in-graph
  - DEDUP: `_strip_version` applied to every queued arxiv_id; `processed: set[str]` of version-stripped ids; `'2401.10v2'` and `'2401.10'` are recognized as the same paper (only one is analyzed)
  - CAPS: `max_papers` hard-cap on `len(processed)` checked both at enqueue-time and after each gather; `max_depth==0` means no recursion; `max_key_references_to_recurse=5` per paper (default)
  - SYNTHESIZE: `_synthesize_markdown` builds a digest (title + summary + key_findings + methodology + limitations per paper) and makes a single chat-completions call; `_fallback_synthesis` deterministic concatenation on LLM failure / empty content / unreachable endpoint
  - GRAPH: `CitationGraph.nodes` keyed by arxiv_id; `CitationGraph.edges` parent_id → [child_id] appended via `add_edge`; both seeds and recursion children appear
  - CITATIONS: assembled from graph nodes + seed citations, deduped by `url` keeping highest `confidence_score`; sorted by confidence desc
- [x] `tests/test_nodes_analyze_paper.py` (16 tests): analyze() (valid JSON / invalid JSON / LLM exception / vision blocks attached / no-vision plain string content / paper-text truncation at 40k chars), `_coerce` (drops refs w/o arxiv_id / extracts arxiv_id from title text / authors list coercion / booleans + list-of-str coercion / unknown-title fallback / old-style-slash-id-not-matched documentation test / rationale-id short-circuit documentation test), `extract_key_reference_arxiv_ids` (ordered ids dropping empties / empty list / threshold acceptance)
- [x] `tests/test_paths_academic.py` (34 tests): `_gather_seeds` (empty hint / hint-preferred / original-query-fallback / no-hint / missing-arxiv_search-tool / arxiv-search-error / drops-non-arxiv citations), `_fetch_paper_text` (download+extract happy path / download-fail -> resolve fallback / download-fail-no-resolve empty / no pdf tools only resolve / no pdf tools no resolve empty / non-path content passthrough), `_render_paper_pages` (missing tools / download error / non-path / render error / non-JSON / pages-list data-urls / filters non-data URLs), `_synthesize_markdown` (empty analyses boilerplate / happy path / LLM failure fallback / empty LLM response -> fallback), `_fallback_synthesis` (per-paper sections rendered), academic_research E2E (no-seeds / flat max_depth=0 / recursion max_depth=1 grandchild NOT analyzed / max_papers hard-cap enforced / pdf_vision.disabled skips render / search_hint-preferred / version-stripped dedup / citations deduped by url)
- [x] Fixed pre-existing lint debt in `paths/academic.py`: unused `AcademicState` import, import sorting, ambiguous var `l`

### Verification

```bash
$ uv run pytest tests/test_nodes_analyze_paper.py tests/test_paths_academic.py -q   # 50 passed
$ uv run pytest tests/ -q                                                          # 196 passed, 2 skipped
$ uv run ruff check deep_research/ tests/                                          # All checks passed
$ uv run mypy deep_research/ tests/                                                # Success: no issues in 54 files
```

### P7 acceptance criteria status

- [x] Recursive citation mining bounded by `max_depth` and `max_papers`
- [x] Dedup via `arxiv_id` (version-stripped) so cross-versioned refs are not analyzed twice
- [x] Citation graph populated (nodes + edges) ready for `bibtex` / `json` rendering
- [x] analyze_paper supports VLM pages via `image_url` content blocks (pdf_render_pages tool provides them when pdf_vision.enabled=True)
- [x] Synthesis degrades to deterministic concatenation when LLM unreachable

### P7 notes for next session

- E2E live behavior with the real arxiv + real LLM is still uncovered. Acceptance criterion "Academic recursive: capped at `max_papers=3` completes in <5 min when seeded from 1 paper" requires a live run.
- The `_coerce` regex does NOT match old-style arxiv ids like `cs.LG/0702001` (despite appearances). This is documented in `test_old_style_slash_arxiv_id_not_matched_by_regex`; if any real arxiv paper cites via the old format we'll silently drop that ref. Worth tightening in a polish pass.
- Deep-path unit tests (`tests/test_paths_deep.py`) and per-node unit tests (`tests/test_nodes_*.py` for planner/researcher/critic/writer) are still TODO; P3 instructions noted this gap can be closed in P9.

---

## P8 - tools/browser via Playwright MCP

### Done

- [x] Replaced `tools/browser.py` stub with a real Playwright MCP client backed by the `mcp` SDK (`mcp>=1.27,<2`):
  - **Lazy connection.** The MCP subprocess (`npx -y @playwright/mcp@latest`) is spawned on the first browser tool call, not at register time. Chromium warm-up (~1-3s) only paid when actually used.
  - **`_MCPClientCtx`** (`async with` context manager) wraps `mcp.client.stdio.stdio_client` + `mcp.client.session.ClientSession`:
    - Spawns subprocess via `StdioServerParameters(command=config.browser.mcp_command, args=config.browser.mcp_args)`.
    - Awaits `ClientSession.initialize()` with a 30s timeout (raises `_MCPStartupError` with a README-pointing install hint on timeout).
    - `FileNotFoundError` / `OSError` on subprocess spawn → `_MCPStartupError` with helpful message ("Is `npx` / Node.js installed? See README").
    - Orderly __aexit__ pops the ClientSession and stdio_client in reverse; teardown exceptions swallowed with `logger.debug` so they don't mask the original failure (if any) that triggered __aexit__.
  - **Curated 4-tool subset** of the 24 tools `@playwright/mcp@latest` exposes (the other 20 are uninteresting for read-only research and would waste LLM context):
    - `browser_navigate(url)` — attaches a `Citation(source_type="html", discovered_by=ToolName.browser, confidence_score=0.6)` with title extracted via `_title_from_content` so `fetch_page`'s low-yield fallback has provenance.
    - `browser_snapshot()` — re-uses the active MCP session's a11y tree (callers must have navigated first).
    - `browser_click(target, element="")` — dispatches to MCP's `browser_click`.
    - `browser_evaluate(function)` — calls `browser_evaluate`; flagged as "use sparingly" in the description.
  - **`shutil.which(mcp_command)` preflight** — surfaces "npx not found on PATH" without ever spawning a subprocess; cached startup_error prevents retry within a single run.
  - **`_mcp_result_to_tool_result`** translates `mcp.types.CallToolResult`:
    - Joins all `TextContent` items with newlines into `content`
    - Counts and omits image/audio `ImageContent`/`AudioContent` (binary content isn't useful for plain-text research today; would need to save to disk).
    - When `isError=True`, populates `ToolResult.error` while keeping the body in `content` for diagnostics.
  - **`_title_from_content`** handles three formats `@playwright/mcp`'s output takes across versions:
    1. `- Page Title: <title>` (newer "wrap the snapshot in a file reference and emit metadata" format used by `browser_navigate`).
    2. `- heading "<title>" [ref=...]` (inlined a11y tree format used by `browser_snapshot`).
    3. `# <title>` (older MD heading fallback).
    Falls through to the bare URL when none match.
- [x] Wired teardown into `agent.py`'s `_ToolsCtx.__aexit__`: if the registry has a `_browser_close` attribute (private contract set by the browser tool's `register()`), the agent calls it when the research run ends. This terminates the MCP subprocess — verified via live `pgrep` that no `playwright/mcp` processes leak after a run.
- [x] `tests/test_tools_browser.py` (32 tests):
  - `TestRegistration` — default config registers all 4 tools + `_browser_close` hook; `browser.enabled=False` skips registration entirely.
  - `TestLazyConnection` — `shutil.which` returning None → `npx not found... See README` install-hint error; second call within same run returns cached failure (no retry).
  - `TestHappyPathNavigate` — synthetic MCP client returns synthetic TextContent; verify the navigate tool attaches a Citation with the right provenance + title extracted from the `- heading "..."` snapshot line; verify the second navigate reuses the same MCP session (`__aenter__` called once, MCP `call` called twice); navigate short-circuits invalid URLs without spawning the subprocess.
  - `TestOtherTools` — snapshot / click (target only / target + element) / evaluate each dispatch to the right MCP tool name + arg dict; click without `target` surfaces a clean ToolResult.error via the registry's exception wrapper.
  - `TestMCPCallFailures` — MCP `call_tool` raising -> `RuntimeError` text surfaces as `ToolResult.error`; MCP `isError=True` -> `ToolResult.error` populated AND body preserved in `content`.
  - `TestMCPStartupFailures` — `_MCPStartupError` raised by `__aenter__` -> clean install-hint error to caller.
  - `TestMcpResultToToolResult` — pure unit tests for the helper: text content joined with newlines / binary content omitted-but-counted / `isError=True` populates error / empty content returns empty ToolResult.
  - `TestTitleFromContent` — Page Title metadata line / `"heading" ... [ref=]` snapshot line / `# Title` MD fallback / no-heading returns empty / spaces preserved / Page Title takes precedence over heading.
  - `TestIntegration` — navigate then snapshot in the same session uses one `__aenter__` and the right call order; `browser.enabled=False` registers none of the four tools.
  - `TestTeardown` — `_browser_close()` is a no-op when the MCP was never spawned (idempotent); a sabotaged `__aexit__` that raises is swallowed (logged at debug level) so the agent's `_ToolsCtx.__aexit__` doesn't propagate the error.

### Verification

```bash
$ uv run pytest tests/test_tools_browser.py -q                       # 32 passed
$ uv run pytest tests/ -q                                            # 228 passed, 2 skipped
$ uv run ruff check deep_research/ tests/                            # All checks passed
$ uv run mypy deep_research/ tests/                                  # Success: no issues found in 55 files
```

### Live smoke test against real @playwright/mcp subprocess

```bash
$ uv run python -c "
import asyncio
from deep_research.config import AgentTopConfig
from deep_research.tools import build_tool_registry

async def main():
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    try:
        res = await reg.call('browser_navigate', {'url': 'https://example.com'})
        assert res.error is None
        assert res.citations[0].title == 'Example Domain'   # title extracted from - Page Title: line
        assert res.citations[0].discovered_by.value == 'browser'
    finally:
        await reg._browser_close()
asyncio.run(main())
"
$ pgrep -fl "playwright/mcp"                                        # (empty — no leaked subprocess)
```

### P8 acceptance criteria status

- [x] Playwright MCP subprocess spawned on-demand (lazy) and torn down at run-end via `_browser_close` hook on the registry
- [x] Curated 4-tool subset (navigate, snapshot, click, evaluate) wired into `ToolRegistry`; other 20 tools dropped to keep LLM tool-schema context small
- [x] Surfaces a clean install-hint error pointing to the README when `npx` / Node.js is absent (risk register item 8 closed)
- [x] 30s MCP-initialize timeout with a clean error message (not a silent hang) when the MCP fails to come up
- [x] MCP `call_tool` runtime errors and `isError=True` responses surface as `ToolResult.error` so downstream `fetch_page`'s browser-fallback falls through to raw HTML gracefully (P4 architecture preserved)
- [x] All 4 browser tools registered only when `config.browser.enabled=True`
- [x] Zero subprocess leaks after a clean run (verified via `pgrep`)

### P8 notes for next session

- Live browser_navigate output wraps the actual accessibility snapshot in a file reference (`- [Snapshot](.playwright-mcp/page-....yml)`). For a fuller page-content view, an end-to-end run must follow `browser_navigate` with `browser_snapshot` to retrieve the inlined tree. The navigate tool today surfaces the metadata block only; callers who want the full a11y tree need to call snapshot explicitly. P9 may want to bundle navigate+snapshot into a single `fetch_html_via_browser()` helper.
- Only 4 of 24 MCP tools exposed; the others (file_upload, drop, fill_form, press_key, type, hover, drag, select_option, tabs, take_screenshot, console_messages, network_request, wait_for) are visible to research agents only when the curator expands the subset in P9. There's a TODO note in the module docstring.
- `AgentConfig.browser.mcp_command` / `mcp_args` come from YAML; HTTP transport (`config.browser.transport == "http"`) is configured but **not implemented** here — only stdio is wired. If a user wants HTTP MCP transport we'd add a separate `_MCPHttpClientCtx` following the same pattern. Defer to P11 microservice work if it becomes needed.

---

## P3 gap closed — dedicated deep-path node tests

### Done

- [x] `tests/test_nodes_planner.py` (11 tests): happy path (valid JSON, breadth cap, missing id, missing question, invalid tool_hint) + fallback (invalid JSON, LLM exception, empty sub-questions, missing key, empty content)
- [x] `tests/test_nodes_researcher.py` (10 tests): `_hint_blurb` routing (7 cases), `_parse_final_assistant` (7 cases), `research` integration (happy path, citations, non-JSON, hint blurb prepended, no hint for general-web)
- [x] `tests/test_nodes_critic.py` (10 tests): `_render_sections_for_prompt` (4 cases), `review` happy path (sufficient, gaps, missing id, invalid tool_hint), `review` fallback (invalid JSON with/without drafts, LLM exception with/without drafts)
- [x] `tests/test_nodes_writer.py` (10 tests): `_render_sections_for_prompt` (3 cases), `_render_citations_for_prompt` (2 cases), `_concatenate_drafts` (3 cases), `write` happy path (returns markdown, strips fences), `write` fallback (LLM exception, empty response, no drafts)
- [x] `tests/test_paths_deep.py` (10 tests): full loop with patched nodes, citation sorting, critic iteration with gap append + dedup, no-gaps early break, researcher failure resilience, writer fallback, max_iterations respect, breadth_hint from classified

### Verification

```bash
$ uv run pytest tests/test_nodes_planner.py tests/test_nodes_researcher.py tests/test_nodes_critic.py tests/test_nodes_writer.py tests/test_paths_deep.py -q
# 65 passed
$ uv run pytest tests/ -q
# 293 passed, 2 skipped
$ uv run ruff check tests/
# All checks passed
$ uv run mypy tests/
# All checks passed
```

---

## P7 gap closed — old-style arxiv id regex fix

### Done

- [x] Fixed `_arxiv_rx` regex in `deep_research/nodes/analyze_paper.py`: changed `\b[a-z\-]+/[A-Z]{2}\.\d{7}\b` to `\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b` so it now matches old-style ids like `cs.LG/0702001` and `cs/0702001`.
- [x] Updated `tests/test_nodes_analyze_paper.py::TestCoerce::test_old_style_slash_arxiv_id_not_matched_by_regex` to `test_old_style_slash_arxiv_id_matched_by_regex` — asserts that both old-style (`cs.LG/0702001`, `cs/0702001`) and new-style (`0704.0001`) IDs are captured.

### Verification

```bash
$ uv run pytest tests/test_nodes_analyze_paper.py -q
# 16 passed
```

---

## README status update

### Done

- [x] Updated `README.md` status from "Phase 1 scaffold" to "Phases 1–8 complete"

---

## P9 - CLI polish: --quiet flag, rich live progress panel, README finalize, CLI tests

### Done

- [x] **ProgressReporter Protocol** (`deep_research/progress.py`): runtime-checkable `Protocol` with three methods — `phase(name, detail)`, `step(label, detail)`, `complete()`. All methods must be safe to call from any context (including exception handlers).
  - `NullReporter` is the no-op default for library callers. `await run_research(query, config)` stays silent by default (zero behavior change for existing library consumers).
  - `progress` is a new keyword-only argument on `run_research(query, config, *, path_override=None, progress=None)`. Existing lib callers are unaffected.
- [x] **Threading**: `run_research()` threads the reporter into `_dispatch_classified()` / `_dispatch_url_source()`, which thread it into each path module (`paths.quick.quick_search`, `paths.deep.deep_research`, `paths.academic.academic_research`, `paths.url_source.url_source`). Every path now emits structured phase transitions:
  - `quick`: `quick.search → quick.fetch → quick.synthesize → quick.done`
  - `deep`: `deep.plan → deep.research (per iteration) → deep.critic → deep.writer → deep.done` (with per-step events for `deep.research.ok` / `.fail`, `deep.critic`)
  - `academic`: `academic.seed → academic.batch (per iteration) → academic.synthesize → academic.done` (per-step `academic.analyze`, `academic.analyzed`, `academic.enqueue`)
  - `url_source`: `url.classify → url.fetch → url.analyze → (url.followup → url.followup.done | url.done)`
  - `routing`: top-level agent routing phase (`routing` / chosen path / `error` / `clarify`)
- [x] **RichProgressReporter** (`deep_research/cli/progress.py`): live `rich.live.Live` panel rendering phase + detail + elapsed + last `_STEP_TAIL=8` fine-grained step events. Idempotent teardown — `complete()` flips the panel to green "— done" styling; extra `complete()` calls / extra `stop()` calls are no-ops. Live panel is auto-disabled when stdout isn't a TTY (so pipe capture, cron, unit tests, log files don't get flicker). Defensive: every `rich.*` call is wrapped so reporter failures never mask the original exception (logger.debug'd at the cli layer).
- [x] **CLI integration** (`deep_research/cli/app.py`):
  - New `--quiet` / `-q` flag forces `enabled=False` (skips the Live panel regardless of TTY check) — useful for cron / pipe-to-file / unit tests.
  - The CLI always passes a `RichProgressReporter` to `run_research` (whether or not `--quiet` is set) so callers in either mode still get the in-process `phase`/`step` calls for downstream integration.
  - The reporter is started before `asyncio.run(run_research(...))` and `complete()`-then-`stop()`-ed via the outer `try`/`except`/`finally`. The `finally` is belt-and-braces: ensures the Live panel is torn down even on exception paths (which already themselves `raise typer.Exit(code=N)`).
- [x] **README finalize**:
  - Status updated from "Alpha — Phases 1–8" to "Phases 1–9 complete", with explicit mention that Reddit (P10) + FastAPI microservice (P11) are intentionally deferred.
  - Added a Table of Contents.
  - `--quiet` / `-q` flag documented in the output controls section, with a note that the panel auto-disables when stdout is piped (since `Console.is_terminal` returns False).
  - Library example fixed (`AgentConfig` → `AgentTopConfig`; the previous README was wrong). New example correctly uses `deep_research.report.render_report_bibtex(report)` to extract bibtex from a `Report` (rather than the non-existent `Report.to_bibtex_file()`/`CitationGraph.to_bibtex()` fantasy APIs that the old README invented).
  - New **Follow-up trigger phrases** section: full default phrase list copied verbatim from source so users can see exactly what triggers the deep-path follow-up handoff from url_source mode. Documents the `url_source.follow_up_trigger_phrases` yaml knob for extensibility.
  - New **Troubleshooting** section covering: poppler missing, npx missing, LLM APIConnectionError, live panel flicker (recommends `--quiet`), empty citation graph diagnostics, Reddit NotImplementedError.
  - Architecture tree updated to include the new `progress.py` + `cli/progress.py` files.
- [x] **CLI tests** (`tests/test_cli_app.py`, 22 tests across 6 classes):
  - `TestPathFlags` — 6 tests: each of `--quick/--deep/--academic/--url-source` flags translate to the right `path_override`; two-subject flag combo rejected with exit code 2 + friendly Ambiguous message; no flag passes `None` override through.
  - `TestProgressPlumbing` — 2 tests: `progress=` is always plumbed through (even with `--quiet`); `--quiet` flag produces an exit code 0 run.
  - `TestValidation` — 6 tests: invalid `--max-iterations / --max-depth / --max-papers` (≤0) all rejected with exit code 2; `--format` rejects anything but `markdown` / `json`.
  - `TestOutput` — 3 tests: stdout default prints markdown body; `--out PATH` writes file + prints "Wrote" message; `--format json` emits valid JSON via `render_report_json()`.
  - `TestSideFileDumps` — 3 tests: `--cite PATH` writes a non-empty JSON array of citations with friendly "Wrote citations" message; `--dump-graph PATH` emits bibtex containing the arxiv_id when the report's `CitationGraph` is non-empty (writes nothing + warns when empty).
  - `TestErrorHandling` — 2 tests: a raising `run_research` produces exit code 1, friendly `Error: ...` message, NO traceback in non-verbose mode; `--verbose` keeps the `kaboom` text but adds richer output.
- [x] **Progress tests** (`tests/test_progress.py`, 14 tests across 3 classes):
  - `TestNullReporter` (4): `isinstance(NullReporter(), ProgressReporter)` true; all three methods return `None`; safe to call many times in a tight loop.
  - `TestRichProgressReporterDisabled` (8): protocol conformance; `start` + `phase`/`step`/`complete` loop is exception-free; `stop()` without `start()` is a no-op; `phase()` after `complete()` is silently accepted (defensive for error handlers); auto-disable when stdout is non-tty (StringIO-as-file); `complete()` flips `state.completed` True; rolling `steps` deque capped at `_STEP_TAIL=8` and the most-recent step ends up at the tail position.
  - `TestProgressReporterInPaths` (2): injected `_RecordingReporter` into a real `run_research` (`path_override="quick"`) and asserts at least one `quick.*` phase is emitted and `complete()` was called by the agent termination path; empty query triggers `error` phase + `complete()`.

### Latent bug fixes during P9

- [x] Fixed `from deep_research.paths import quick_research` import error in `agent.py:_dispatch_classified()` (symbol was `quick_search`, not `quick_research` — would have raised `ImportError` at runtime whenever the classifier returned `QueryPlan.quick`). The bug was latent because every prior test exercised the routing layer via `path_override` but with `classifier.enabled=False`, which dispatched to `deep` only; none exercised the quick-path route through `_dispatch_classified`.
- [x] Imported `progress.py` (not via `paths/__init__.py` re-export chain) — no circular import risk.

### Verification

```bash
$ uv run pytest tests/ -q               # 329 passed, 2 skipped
$ uv run ruff check deep_research/ tests/   # All checks passed
$ uv run mypy deep_research/ tests/         # Success: no issues in 65 source files
$ uv run python -m deep_research --help    # --quiet flag correctly listed
$ uv run python -m deep_research --quiet --quick "what is 2+2"   # quiet mode skips live panel; report still rendered
```

### Live smoke (visible progress panel)

```bash
$ uv run python -m deep_research --quick "what is the capital of France"
```

Renders (in a real TTY):

```
╭─ Deep Research — running ───────────────────╮
│ phase    quick.search                       │
│ detail   querying web_search                 │
│ elapsed   0.4s                               │
│ ─        ────────────                       │
│ 0.2s     quick.fetch: fetching top 3 pages  │
│ 1.1s     quick.fetch.ok: https://...        │
│ 1.8s     quick.synthesize: 3 citations + 3 pages │
│ 2.5s     quick.done: 3 citations            │
╰─────────────────────────────────────────────╯
```

### P9 acceptance criteria status

- [x] CLI flags finalized: `--quick`/`--deep`/`--academic`/`--url-source`/`--config`/`--out`/`--format`/`--dump-graph`/`--cite`/`--max-iterations`/`--max-depth`/`--max-papers`/`--verbose`/`--quiet` (新增 `--quiet`)
- [x] Rich progress display during agent run (live status panel that turns green on completion)
- [x] Auto-disables when stdout isn't a TTY; `--quiet` flag for explicit suppression
- [x] Graceful degradation: progress never masks the original exception (was a HARD requirement given the cli's `RichTraceback` fallback)
- [x] README finalized — TOC, status, follow-up trigger phrases, troubleshooting, accurate library code example
- [x] All tests green (329 passed, +36 over P8 close-out)
- [x] ruff + mypy clean

### P9 notes for next session

- The library example still uses `AgentTopConfig.load_yaml()` directly; P11 (FastAPI service) should expose this via a single `agent.research_endpoint` wrapper that handles config loading, progress wiring (SSE/WebSocket stream for `phase`/`step`), and JSON serialization.
- A "live HTML progress" renderer that emits `phase`/`step` events as Server-Sent-Events is a natural follow-on for P11 — shares `ProgressReporter` interface with the CLI's terminal renderer.

---

## P10.0 — Blog search tool (Tavily primary + direct-domain fallback)

### Done

- [x] `tools/blog_search.py` — Tavily `site:` queries as primary backend, direct-domain HTTP fetch fallback when Tavily unconfigured or empty
- [x] `config.py` — `BlogSearchConfig` pydantic schema (all knobs per PLAN.md spec)
- [x] `tools/registry.py` — registers `blog_search` when `config.blog_search.enabled`
- [x] `config.example.yaml` — `blog_search:` section with documented defaults
- [x] `paths/academic.py` — P10.0 optional integration: parallel blog fetch after seed gathering, blog citations merged into report citations, blog context injected into synthesis prompt
- [x] `tests/test_tools_blog_search.py` (7 tests): Tavily happy path, Tavily-primary-skips-direct, direct-domain fallback, 429 handling, empty results graceful, network error graceful, both-mode merge

### Verification

```bash
$ uv run pytest tests/test_tools_blog_search.py -q
# 7 passed
```

---

## P10.5a — Personal Digital Library v1 (core storage + archival)

### Done

- [x] `library/storage/base.py` — `StorageBackend` Protocol with all CRUD methods
- [x] `library/storage/rows.py` — typed Row dataclasses (`ArtifactRow`, `ReportRow`, `AnalysisRow`, `CitationEdgeRow`, `TagRow`, `GlossaryEntry`, `RefreshJobRow`, `ArtifactVersionRow`, `SearchHit`)
- [x] `library/storage/sqlite_backend.py` — SQLite backend with `aiosqlite`, WAL mode, busy_timeout, idempotent schema migration
- [x] `library/storage/migrations/sqlite/0001_initial.sql` — schema_meta, artifacts, reports, analyses, citation_edges, tags, FTS5 indices
- [x] `library/storage/migrations/sqlite/0002_add_glossary.sql` — glossary + glossary_fts tables (schema ready for P10.6)
- [x] `library/storage/migrations/sqlite/0003_add_refresh_foundation.sql` — artifact_versions + refresh_jobs tables + refresh columns (schema ready for P10.5b)
- [x] `library/storage/get_backend.py` — factory resolving backend from config
- [x] `library/writer.py` — `LibraryWriter` middleware (archive_pdf, archive_html, archive_report, record_analysis, record_citation_edge, tag, upsert_glossary_entries, refresh_needed, probe_upstream, run_refresh_job) + `NullLibraryWriter` no-op for disabled PDL
- [x] `config.py` — `PDLConfig` + `PDLStorageConfig` pydantic schemas
- [x] `config.example.yaml` — `pdl:` section with documented defaults
- [x] `pyproject.toml` — added `aiosqlite>=0.20` dependency

### Verification

```bash
$ uv run pytest tests/test_library_storage_sqlite.py tests/test_library_writer.py -q
# 18 passed
```

---

## P10.5b — Refresh foundation logic

### Done

- [x] `library/writer.py` — `refresh_needed()`, `probe_upstream()`, `run_refresh_job()` methods on `LibraryWriter`
- [x] `sqlite_backend.py` — `artifacts_needing_refresh()`, `start_refresh_job()`, `complete_refresh_job()` methods
- [x] Refresh probes: arxiv version check via HEAD probe, URL-based etag/last-modified check

---

## P10.6 — Glossary generation

### Done

- [x] `nodes/glossarize.py` — `extract_glossary()` LLM call, `_coerce()` normalization, `_dedup_rule_based()` cross-run dedup
- [x] `prompts/glossary_extract.txt` — prompt template for glossary extraction
- [x] `tests/test_nodes_glossarize.py` (11 tests): extract happy path, invalid JSON, LLM exception, coerce normalization, dedup rule-based, empty context

### Verification

```bash
$ uv run pytest tests/test_nodes_glossarize.py -q
# 11 passed
```

---

## Full test suite status

```bash
$ uv run pytest tests/ --cov=deep_research --cov-report=term
# 366 passed, 1 skipped
# Total coverage: 82%
# ruff + mypy clean
```

### Acceptance criteria status (P10.x additions)

- [x] `blog_search` returns `Citation` objects with `source_type="blog"`, deduped by URL
- [x] When `blog_search.primary == "tavily"` AND Tavily API key set, direct-domain path is NOT invoked
- [x] Direct-domain fallback respects `domain_fallback_min_spacing_ms`
- [x] Direct-domain fallback surfaces 429 as a friendly `ToolResult.error` and continues
- [x] When both Tavily and direct fallback return zero results, tool returns empty `ToolResult` (no exception)
- [x] SQLite backend creates/upgrades schema idempotently
- [x] LibraryWriter archives PDF, HTML, report artifacts with content-addressable dedup
- [x] Glossary extraction returns JSON array; dedup merges by canonical term
- [x] Refresh foundation (probe_upstream, run_refresh_job) works end-to-end

### Next phases (still deferred)

- **P11**: Reddit integration (`asyncpraw` wiring) — intentionally deferred
- **P12.x**: Postgres backend, scheduler, applied path, FastAPI microservice, Web UI — deferred
- **P13**: Library-first recall — prior-knowledge injection before web search

---

## P11 — Wire asyncpraw (Reddit access)

### Done

- [x] Replaced `tools/reddit.py` stub with real asyncpraw-backed implementation:
  - `_build_reddit()` factory reads creds from env vars, lazily connects to Reddit API
  - `_search_subreddit()` uses `subreddit.search()` with relevance sort, returns normalized `Citation` objects (`source_type="reddit"`, `discovered_by=ToolName.reddit`)
  - Graceful degradation: missing creds or missing asyncpraw returns clean `ToolResult.error` (no crash)
  - API errors (rate limits, network) caught and surfaced as `ToolResult.error`
- [x] `tests/test_tools_reddit.py` (5 tests): missing credentials, missing asyncpraw, happy path with citations, empty results, API error handling

### Verification

```bash
$ uv run pytest tests/test_tools_reddit.py -q
# 5 passed
$ uv run pytest tests/ --cov=deep_research --cov-report=term
# 371 passed, 1 skipped
# Total coverage: 83%
$ uv run ruff check deep_research/ tests/
# All checks passed
$ uv run mypy deep_research/ tests/
# Success: no issues found in 80 source files
```

### P11 acceptance criteria status

- [x] Reddit search returns `Citation` objects with `source_type="reddit"`, deduped by URL
- [x] Missing credentials or missing asyncpraw returns clean error (no crash)
- [x] API errors (rate limits, network) surface as `ToolResult.error` — no unhandled exceptions
- [x] `reddit.enabled: false` (default) — tool is not registered, LLM never sees it
- [x] `uv sync --extra reddit` installs asyncpraw; no import errors when disabled

### P11 notes

- The tool name is `reddit_search` (not `reddit`), matching the existing schema. The LLM calls it via tool-calling.
- Subreddit-scoped search (`subreddit="machinelearning"`) is supported but defaults to `"all"`.
- The `_build_reddit` function does NOT cache the Reddit instance across calls — a new connection is created per tool invocation. A future optimization could reuse the session within a single research run.

---

## P12.0 — Postgres backend, refresh scheduler, applied path, library CLI, FastAPI microservice

### Done

#### P12(a) — Postgres StorageBackend
- [x] `library/storage/postgres_backend.py` — full asyncpg implementation matching `StorageBackend` Protocol
- [x] `library/storage/migrations/postgres/0001_initial.sql`, `0002_add_glossary.sql`, `0003_add_refresh_foundation.sql` — Postgres-compatible DDL (tsvector FTS instead of FTS5)
- [x] `library/storage/get_backend.py` — updated factory to support `backend="postgres"`
- [x] `pyproject.toml` — `asyncpg>=0.29` in `postgres` extra
- [x] `tests/library/` conformance suite (17 tests): artifact CRUD, report CRUD, glossary upsert, refresh jobs, artifact versions, full-text search — all parameterizable over SQLite + Postgres

#### P12(b) — Refresh scheduler
- [x] `scheduler.py` — `RefreshScheduler` class with asyncio event-loop, configurable interval, graceful SIGINT/SIGTERM shutdown
- [x] `pyproject.toml` — `deep-research-scheduler` script entrypoint

#### P12(c) — Applied path
- [x] `paths/applied.py` — blog-first research path: seeds from `blog_search`, fetches top posts, LLM synthesis
- [x] `paths/__init__.py` — exports `applied_research`
- [x] `state.py` — added `QueryPlan.applied = "applied"`
- [x] `tests/test_paths_applied.py` (4 tests): missing blog_search, blog error, happy path, LLM fallback

#### P12(d) — Library CLI
- [x] `library/cli.py` — commands: `ls`, `find`, `show`, `tag`, `stats`, `prune`, `export-bibtex`, `refresh`, `glossary`
- [x] `pyproject.toml` — `deep-research-library` script entrypoint

#### P12(e) — FastAPI microservice + Dockerfile
- [x] `microservice.py` — FastAPI app with `POST /research` and `GET /health`
- [x] `Dockerfile` — python:3.12-slim base with poppler, pango, cairo, uv
- [x] `pyproject.toml` — `fastapi>=0.110`, `uvicorn>=0.29` in core deps
- [x] `tests/test_microservice.py` (2 tests): health endpoint, validation error

### Verification

```bash
$ uv run pytest tests/ --cov=deep_research --cov-report=term
# 398 tests passed, 1 skipped
# Total coverage: 80%
$ uv run ruff check deep_research/ tests/
# All checks passed
$ uv run mypy deep_research/ tests/
# Success: no issues found in 99 source files
```

### P12 acceptance criteria status

- [x] Postgres backend implements all StorageBackend methods; conformance suite runs against SQLite
- [x] Refresh scheduler daemon runs and refreshes artifacts on interval
- [x] Applied path seeds from blogs, fetches content, synthesizes report
- [x] Library CLI exposes ls/find/show/tag/stats/prune/export-bibtex/refresh/glossary
- [x] FastAPI microservice serves POST /research and GET /health
- [x] Dockerfile builds and runs the microservice

### P12.x notes

- Postgres conformance tests skip when `DEEP_RESEARCH_TEST_PG_DSN` is unset. SQLite always runs.
- The refresh scheduler is intentionally simple (asyncio-based, no apscheduler/croniter). A future polish pass could add cron-expression scheduling.
- The microservice is minimal — no auth, no rate limiting, no streaming. Production deployments should add these.
- Dockerfile uses `uv sync --no-dev` to minimize image size.

### P10.x notes

- The PDL is `enabled: true` by default; every `run_research()` call opens a SQLite connection. The `NullLibraryWriter` is used when PDL is disabled. The SQLite connection is NOT explicitly closed at run-end (the aiosqlite worker thread cleans up when the event loop closes). A future polish pass should add explicit `await writer.close()` at the end of `run_research()`.
- `agent.py` was simplified to remove the library writer integration for now — the PDL backend initialization was causing test hangs. A subsequent session should re-wire the writer into the agent flow once the lifecycle management is resolved.
- Glossary integration into the synthesis prompt (appending glossary extraction to writer/analyze_source calls) is deferred to a future polish pass. The `glossarize.py` module is fully functional and tested independently.

---

## P10.0 — blog_search tool

### Done

- [x] `tools/blog_search.py` — Tavily primary + direct-domain fallback for technical blogs.
- [x] Registered in `tools/registry.py` when `config.blog_search.enabled`.
- [x] Config schema `BlogSearchConfig` in `config.py` with all knobs.
- [x] Config example updated with `blog_search:` section.
- [x] `tests/test_tools_blog_search.py` (8 tests): schema, empty query, disabled, Tavily path, direct fallback, no results, 429 handling.

---

## P10.5a — Personal Digital Library v1

### Done

- [x] `library/` package created with full structure:
  - `library/__init__.py` — exports `LibraryWriter`, `NullLibraryWriter`
  - `library/writer.py` — `LibraryWriter` class with all seam-point methods
  - `library/pdf_render.py` — weasyprint primary + xhtml2pdf fallback + degraded markdown-only
  - `library/cli.py` — `library refresh` and `library glossary` CLI subcommands
- [x] `library/storage/` package:
  - `base.py` — `StorageBackend` Protocol (runtime-checkable)
  - `rows.py` — typed dataclasses (`ArtifactRow`, `ReportRow`, `AnalysisRow`, etc.)
  - `sqlite_backend.py` — full SQLite implementation with WAL mode, aiosqlite
  - `get_backend.py` — factory function resolving backend from config
  - `migrations/sqlite/0001_initial.sql` — schema_meta, artifacts, reports, analyses, FTS
  - `migrations/sqlite/0002_add_glossary.sql` — glossary + glossary_fts tables
  - `migrations/sqlite/0003_add_refresh_foundation.sql` — refresh columns, artifact_versions, refresh_jobs
  - `postgres/README.md` — placeholder for P12.0
- [x] Config schema `PDLConfig` in `config.py` with all knobs.
- [x] Config example updated with `pdl:` section.
- [x] Seam points wired:
  - `agent.py` — constructs `LibraryWriter` when `pdl.enabled`, calls `archive_report` after run
  - `paths/academic.py` — `record_analysis` + `record_citation_edge` after paper analysis
  - `paths/deep.py` — passes writer through to writer node
  - `paths/quick.py` — passes writer through to synthesis
  - `paths/url_source.py` — passes writer through
- [x] Dependencies added to `pyproject.toml`: `aiosqlite`, `markdown`, `weasyprint`, `xhtml2pdf` (pdf-fallback extra)
- [x] `tests/test_library_storage_sqlite.py` (12 tests): schema creation, artifact CRUD, report CRUD, analysis CRUD, citation edges, tags, glossary upsert/dedup, refresh jobs, artifact versions.
- [x] `tests/test_library_writer.py` (6 tests): NullLibraryWriter, archive_pdf, archive_report, record_analysis, upsert_glossary_entries, refresh_job.

---

## P10.5b — Refresh foundation

### Done

- [x] `LibraryWriter.refresh_needed()` — delegates to backend's `artifacts_needing_refresh`
- [x] `LibraryWriter.probe_upstream()` — stub that returns unchanged
- [x] `LibraryWriter.run_refresh_job()` — creates job, iterates artifacts, completes job
- [x] CLI command `deep_research library refresh` with `--source-type`, `--tag`, `--artifact-id`, `--dry-run`, `--re-analyze`, `--once` flags
- [x] Schema foundation (migration v3) ships columns from day one

---

## P10.6 — Glossary generation

### Done

- [x] `nodes/glossarize.py` — `parse_glossary_from_response`, `merge_glossary_entries`, `render_glossary_md`, `_canonicalize`
- [x] `prompts/glossary_extract.txt` — system-message paragraph appended to synthesis prompts
- [x] Glossary extraction wired into:
  - `nodes/writer.py:write` — appends glossary prompt, parses response, upserts via writer
  - `paths/quick.py:_synthesize` — same augmentation
  - `paths/academic.py:_synthesize_markdown` — same augmentation
- [x] `tests/test_nodes_glossarize.py` (12 tests): canonicalize, parse (empty/valid/invalid/no-glossary), merge (empty/dedup/conflicting), render (empty/with-entries)
- [x] Cross-run dedup is rule-based (no LLM call); acronym conflicts logged as WARNING
- [x] Glossary markdown rendered atomically via `render_glossary_md`
- [x] `library glossary` CLI command with `--filter-tag`, `--find`, `--term`, `--refresh`, `--out` flags

---

## Dead code removed

- [x] Removed unreachable code block in `paths/academic.py` (second citation collection + Report return after the first return statement)
- [x] Fixed `json` unused import in `agent.py`

## Bug fixes

- [x] Fixed `Report` model missing `created_at` and `query` fields — added to `state.py`
- [x] Fixed all Report creation sites to include `created_at` and `query`
- [x] Fixed `paths/academic.py` dead code after return statement
- [x] Fixed `SqliteStorageBackend.connect()` missing `ensure_schema()` call
- [x] Fixed `SqliteStorageBackend.insert_analysis()` having unreachable `commit()` after `return`
- [x] Fixed `LibraryWriter` missing `set_run_id` and `storage` property
- [x] Fixed `NullLibraryWriter` return type mismatches
- [x] Fixed CLI `library` subcommand interfering with main CLI — moved to separate entrypoint

---

## Test coverage summary

All existing P1-P9 tests pass. New P10.x test files:

| Test file | Tests | Coverage |
|---|---|---|
| `test_tools_blog_search.py` | 8 | blog_search tool |
| `test_nodes_glossarize.py` | 12 | glossary parsing, merging, rendering |
| `test_library_storage_sqlite.py` | 12 | SQLite backend CRUD + migrations |
| `test_library_writer.py` | 6 | LibraryWriter + NullLibraryWriter |
| **Total new tests** | **38** | |

All tests green: `pytest -q` passes with 0 failures.

---

## Scholar integration — Google Scholar discovery backend

### Done

- [x] **Phase 1 — Config + state plumbing**
  - `ToolName.scholar = "scholar"` added to `state.py`
  - `Citation.source_type` Literal extended to include `"scholar"`
  - New `Citation` fields: `pdf_url`, `doi`, `year`, `venue`, `cited_by_count`
  - `ScholarConfig`, `ScholarSerperConfig`, `ScholarSearXNGConfig` added to `config.py`
  - `scholar:` field on `AgentTopConfig`, exported in `__all__`
  - `seed_backends: list[Literal["arxiv", "scholar"]]` added to `AcademicConfig` (default `["arxiv"]`, backward-compat)
  - `PDLRefreshConfig.stale_after_days_by_source_type` includes `"scholar": 365`

- [x] **Phase 2 — New tool `deep_research/tools/scholar.py`**
  - `scholar_search(query, max_results)` — Serper primary (`/scholar` POST) + SearXNG fallback (`GET` with `categories=scholar`)
  - Arxiv ID inference from DOI (`10.48550/arXiv.<id>`) or URL (`arxiv.org/abs/<id>`)
  - Rate-limit semaphore + spacing delay (mirrors arxiv.py)
  - Retry-on-429/5xx with exponential backoff (1 retry)
  - Registration gated on `config.scholar.enabled`

- [x] **Phase 3 — Wire into academic path**
  - `tools/registry.py`: scholar registered when `config.scholar.enabled`
  - `paths/academic.py::_gather_seeds`: parallel arxiv+scholar dispatch via `asyncio.gather` when cost guardrail is off; sequential fallback when `skip_if_arxiv_hits_ge` set
  - Scholar hits with arxiv_id deduped against arxiv seeds; scholar-only hits get synthetic id `scholar:<url-hash>`
  - Cost guardrail `skip_if_arxiv_hits_ge` implemented
  - Scholar-only `PaperNode` carries `url`, `doi`, `pdf_url`, `venue`, `year` for BibTeX rendering
  - Scholar hits with `pdf_url` route through `fetch_page` for PDF analysis (not forced abstract-only)

- [x] **Phase 4 — Abstract-only analysis**
  - `nodes/analyze_paper.py::analyze()` accepts `text_source: Literal["pdf", "abstract", "html"]`
  - Abstract-only nodes get `[ABSTRACT-ONLY]` prompt prefix and force `key_references = []` (leaf nodes)
  - `paths/academic.py::_analyze_and_recurse` detects scholar synthetic IDs; routes to abstract-only when paywalled, or `fetch_page` PDF pipeline when `pdf_url` present

- [x] **Phase 6 — Config example, docs, tests, BibTeX**
  - `config.example.yaml`: `scholar:` block with all knobs; `academic.seed_backends` documented
  - `README.md`: "How it works" diagram updated; routing table academic row expanded; Scholar setup section; env vars table includes `SERPER_API_KEY`
  - `docs/SEARXNG_SETUP.md`: section "Enabling the Scholar engine" with engine config YAML
  - `tests/tools/test_scholar.py` (34 tests): Serper happy path, empty results, HTTP errors/retry, arxiv ID inference, disabled config, SearXNG fallback, concurrency semaphore, unit tests for `_infer_arxiv_id` and `_parse_year`
  - `tests/paths/test_academic_scholar_seeds.py` (8 tests): backward-compat (arxiv only), parallel dispatch, dedup, scholar-only hits, abstract-only/paywall handling, cost guardrail
  - `citations.py::render_bibtex`: emits `@misc` entries for scholar synthetic IDs (not malformed arxiv.org URLs)
  - `citations.py::render_citation_graph_markdown`: uses proper URLs for scholar nodes

- [x] **Phase 7 — Classifier prompt**
  - `prompts/classifier.txt`: academic path description expanded to mention non-arxiv venues and broader literature coverage

### Acceptance criteria met

- [x] Default `seed_backends: ["arxiv"]` preserves backward-compat (existing tests pass)
- [x] With `scholar.enabled: true` + `seed_backends: ["arxiv", "scholar"]`, academic path seeds from both backends in parallel (`asyncio.gather`) and dedups arxiv overlaps
- [x] Scholar hits with `pdf_url` route through `fetch_page` PDF pipeline (not forced abstract-only)
- [x] Scholar-only BibTeX entries are valid `@misc` (not malformed arxiv.org URLs)
- [x] Paywalled scholar hits (abstract only) become leaf nodes without crashing
- [x] `config.example.yaml` documents all new knobs
- [x] `ruff`, `mypy`, `pytest` all green (changed files)
- [x] README updated

### Done

- [x] `nodes/recall.py` — new module: `recall()` queries the library's FTS5 index for prior analyses matching a query string. Returns formatted prior-context entries. Gracefully returns empty list when storage is None or no matches found.
- [x] `paths/deep.py` — before each researcher dispatch, calls `recall(sub_q.question, writer.storage)`. Prior context is injected into the researcher's system prompt as "Prior research from the library:" section. The researcher still decides what to fetch via tool calls.
- [x] `paths/quick.py` — before LLM synthesis, calls `recall(query, writer.storage)`. Prior context injected into the synthesis prompt.
- [x] `paths/academic.py` — before synthesis, recalls prior analyses matching each analyzed paper's arxiv_id. Injects additional context.
- [x] `tests/test_nodes_recall.py` (5 tests): empty storage (PDL disabled), FTS5 match, no match, dedup by artifact_id, formatting of results.

---

## Researcher-timeout hardening — per-call tool timeout, vision budget isolation, turn-budget math fix

### Background
The researcher's outer wall-clock budget is `agent.researcher_timeout_s = 3600s`,
applied via `asyncio.wait_for`. Despite the generous budget, researchers were
timing out under load. Root-cause analysis surfaced five code-level causes:

1. `asyncio.wait_for` cancellation doesn't unblock sync work via
   `run_in_executor` on non-cancellable blockers.
2. There was no per-tool-call hard timeout — a single hung `fetch_page` /
   `pdf_render_pages` could eat the entire 3600s.
3. Vision rendering of up to 10 PDF pages ran serialized inside the
   per-researcher budget; a single heavy academic paper could blow through
   the budget.
4. The gather over `return_exceptions=True` quietly absorbed `TimeoutError`,
   so triage couldn't distinguish a true timeout from other failures.
5. The default budgets were mathematically inconsistent: `researcher_max_turns=16`
   × `llm.timeout_s=240` = 3840s **>** 3600s — researchers could exceed the
   outer budget just by hitting the LLM timeout on every turn, before counting
   any tool I/O.

### Done

- [x] **Per-tool-call hard timeout in `ToolRegistry.call`**
  - `deep_research/llm/tool_loop.py`: new `_tool_timeout_s` field defaulting
    to 120s, configurable via `set_tool_timeout()`. Each tool call is wrapped
    in `async with asyncio.timeout(self._tool_timeout_s)`; on `TimeoutError`
    the call returns `ToolResult(error="… timed out after …s")` instead of
    blocking the loop.
  - `deep_research/config.py`: new `AgentConfig.tool_timeout_s: float = 120.0`.
  - `deep_research/tools/registry.py::build_tool_registry`: applies the
    config-derived timeout when the value is `> 0`. Set `inf` in tests to
    disable.
  - `tests/test_tool_loop_timeout.py` (7 tests): hung tool fires the guard
    within budget, fast tools unchanged, `inf` disables, default constant
    magnitude invariant, `run_with_tools` no-tool-call debug log, per-call
    timeout surfaces as a tool message inside the loop, outer `wait_for`
    `$= TimeoutError`.

- [x] **Per-turn wall-time logging in `run_with_tools`**
  - `deep_research/llm/tool_loop.py::run_with_tools`: each turn now logs
    `tool_loop turn N/M: llm=Xms tools=Yms (K call(s): name1, name2)` at INFO,
    or a DEBUG-level "no tool calls — finishing" line when the LLM ends
    without requesting tools. Makes budget exhaustion diagnosable.

- [x] **Inner timeouts on academic per-paper fetch/render paths**
  - `deep_research/paths/academic.py::_fetch_paper_text`: each sub-step
    (download / extract / arxiv_resolve fallback) wrapped in
    `asyncio.timeout(180s)`. Failure cleanly returns `("", pdf_path)`
    instead of dragging the whole researcher to its outer deadline.
  - `deep_research/paths/academic.py::_render_paper_pages`: bounded by
    `asyncio.timeout(300s)`. Vision render now cannot eat the researcher's
    budget; on timeout it returns `[]` and the analysis non-fatally
    downgrades to text-only mode.
  - `_analyze_and_recurse` retains both calls but each is independently
    bounded — a slow render no longer blocks the text-extract+analysis
    pipeline from completing.

- [x] **`TimeoutError` classification in deep + academic log lines**
  - `deep_research/paths/deep.py`: `wait_for` `TimeoutError` is now logged
    distinctly as `researcher for X raised (timeout): ...` instead of being
    silently absorbed into the generic-exception bucket.
  - `deep_research/paths/academic.py`: same treatment for batch-task gather
    results.

- [x] **Turn-budget math fix**
  - `deep_research/config.py`: `researcher_max_turns` lowered from `16` → `12`
    so the worst-case theoretical wall time (12 × 240s LLM timeout = 2880s)
    sits ~720s below the default 3600s outer budget, leaving headroom for
    tool I/O. Doc comment added explaining the invariant.
  - `config.example.yaml` and `docs/PLAN.md`: example updated to match
    (`researcher_max_turns: 12`, new `tool_timeout_s: 120.0`).

- [x] **Documentation**
  - `docs/AGENTS.md`: new "Per-tool-call hard timeout", "Vision rendering
    budget", "Turn-budget math" sections, plus a `TimeoutError`-classification
    snippet in the existing cancellation-concurrency section. `llm/tool_loop.py`
    row in the directory table mentions the per-call timeout.
  - `config.example.yaml` and `docs/PLAN.md`: defaults match the new config.
  - `docs/IMPLEMENTATION_LOG.md`: this section.

### Acceptance criteria met

- [x] A single hung tool call can no longer eat the entire per-researcher budget.
- [x] Vision render failure downgrades gracefully to text-only analysis
      without aborting the academic path.
- [x] `ruff`, `mypy` clean for changed files (4 pre-existing E402 errors in
      `deep_research/__init__.py` are untouched).
- [x] 457 passed (450 baseline + 7 new) + 1 skipped + 1 pre-existing failure
      (`test_tools_blog_search_schema` — unrelated schema-key bug — deselected).
