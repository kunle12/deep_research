# Code Agent Reference

This document captures architectural decisions, patterns, and conventions for
future AI coding agents working on this codebase. Read this first before making
changes.

---

## Architecture Overview

- **Language**: Python 3.11–3.13, async-first, `uv` package manager
- **Orchestration**: raw `asyncio` — no LangGraph/LangChain/AutoGen
- **Entrypoint**: `await run_research(query, config) -> Report` in `agent.py`
- **CLI**: thin typer shell in `cli/app.py`
- **Config**: strict pydantic `AgentTopConfig` loaded from `config.yaml` at startup

### Directory layout (key files)

| File | Purpose |
|------|---------|
| `config.py` | All pydantic config schemas |
| `state.py` | Core data models: `Citation`, `ResearchState`, `ToolName` |
| `tools/registry.py` | `build_tool_registry()` — wires all tools |
| `tools/web_search.py` | Tavily + SearXNG web search |
| `tools/scholar.py` | Serper + SearXNG Google Scholar search |
| `tools/blog_search.py` | Tavily + direct-domain blog search |
| `tools/browser.py` | Playwright MCP headless browser |
| `tools/arxiv.py` | arxiv.org search + PDF download |
| `tools/fetch_page.py` | httpx + trafilatura page fetch |
| `paths/deep.py` | Deep research path (planner→researcher→critic→writer) with the critic-driven deep paper-analysis pass; checkpoint resume |
| `paths/academic.py` | Academic citation-graph path (uses the shared PDF helpers in `nodes/paper_analysis.py`) |
| `nodes/researcher.py` | One sub-question researcher: tool loop + used-only citation gating + strict final-answer parsing |
| `nodes/critic.py` | Deep-path critic: sufficient/gaps + `papers_to_analyze` (deep PDF selection) |
| `nodes/paper_analysis.py` | Shared PDF pipeline (`download_pdf_once`, `extract_text`, `render_pages`, `fetch_paper_text_fallback` — factored from `academic.py`) + `analyze_paper_deep` + `run_paper_analysis_pass` + `format_deep_analysis_context` |
| `nodes/analyze_paper.py` | Vision-aware structured paper analysis (adaptive image batching) — used by academic + deep paths |
| `citations.py` | URL normalization (`normalize_url`, `extract_urls_from_markdown`), used-only bibliography filter (`filter_citations_to_referenced`), bibliography/graph/BibTeX renderers |
| `checkpoint.py` | JSON checkpoint save/load for crash recovery of deep research state |
| `util.py` | Shared tiny helpers: `coerce_float()` (tolerant float parsing for LLM/API output) + `strip_arxiv_version()`. Import these — do NOT copy the body into new modules |
| `nodes/auto_tag.py` | Post-synthesis LLM call — extracts 3-5 topic tags from query + report |
| `prompts/auto_tag.txt` | Prompt template for auto-tag extraction |
| `prompts/glossary_extract.txt` | Prompt template for glossary extraction |
| `llm/tool_loop.py` | LLM tool-calling loop with `run_with_tools()`; per-call timeout via `ToolRegistry.call`; `ScopedToolRegistry` for per-researcher tool isolation; context management with summarisation at 75% of max context window |
| `webui/app.py` | P12.5 web UI app factory: `create_app(config_path, *, backend, research_runner)`; lifespan-owned storage, CSP headers, static mount |
| `webui/jobs.py` | In-memory `ResearchJobManager` — runs `run_research` as an asyncio task, broadcasts phase/step events to SSE subscribers |
| `webui/routers/library.py` | Library browsing API (reports, tags, artifacts, search, stats, PDF/markdown serving) |
| `webui/routers/research.py` | Research job API: start, status, cancel, SSE stream |
| `webui/static/` | Vanilla-JS SPA (no build step): `markdown.js` safe parser/renderer, views for list/report/research |

---

## Tool Registration Pattern

Every tool is registered via the `register(reg: ToolRegistry, config: AgentTopConfig)`
pattern. The `build_tool_registry()` in `registry.py` calls each tool's `register()`
conditionally based on config flags.

**Conventions:**
- Tool callables are `async def _call(query, max_results=10, **_) -> ToolResult`
- The schema is a dict constant (`SCHEMA` / `SEARCH_SCHEMA`) at module level
- Errors return `ToolResult(error=...)` — never raise to the LLM caller
- Citations are `Citation(...)` objects attached to `ToolResult.citations`

---

## Dynamic Refinement Pattern (deep path)

Researchers can emit refinement requests mid-loop via a `refine` tool. Three
actions: `drill_deeper` (new sub-question), `chase_reference` (follow a URL),
`revise_strategy` (best-effort self-correction hint — no enforcement).

**Key architecture decisions:**

- **`ScopedToolRegistry`** (`llm/tool_loop.py`): wraps the shared `ToolRegistry`
  so each parallel researcher gets its own `refine` tool + collector without
  mutating the shared registry (which raises on duplicate registration).
  `run_with_tools` accepts any object with `.schemas()` and `.call()`.
- **`SubQuestion.refinement_depth`**: separate from `depth` (academic-mode
  recursion). Prevents semantic overload between the two paths.
- **Three-level cap hierarchy**: `max_refinement_per_researcher` (per call),
  `max_total_refinements_per_iteration` (per iteration, in `flush_refinements`),
  `max_refinement_depth` (recursive nesting).
- **The per-iteration cap is soft**: `flush_refinements()` moves up to
  `max_total` refinements into the plan and keeps the overflow `pending` for
  the next iteration — capped-out refinements are never dropped.
- **Refinements are researched even if the critic says "sufficient"**: the
  deep loop only stops when there is no pending refinement work left.
- **Normalized dedup**: `absorb_refinements` compares `.strip().lower()` question
  text to catch case/whitespace variants.
- **`research()` returns a 3-tuple**: `(answer_md, citations, refinements)`.
  `paths/deep.py` accepts both 2-tuples (backward compat) and 3-tuples.

---

## Citation Hygiene Pattern (researcher.py + citations.py + report/markdown.py)

Reports only cite sources that were actually used — search hits the researcher
merely saw must never reach the bibliography.

- **Researcher gating** (`nodes/researcher.py::_gate_citations`): after the tool
  loop, only citations whose URL is referenced in the answer markdown OR
  explicitly listed in the final JSON `citations` array are returned. Prose-
  referenced URLs with no citation object get a minimal synthesized `Citation`.
  Tool-result citations (search hits, fetched pages) are candidates, not
  sources — most are dropped. The result is capped at
  `max_citations_per_researcher` (prose-referenced first, then by confidence).
- **Strict final-answer parsing** (`_parse_final_assistant`): only the LAST
  message counts as the answer, and only if it is an assistant message with
  content and NO `tool_calls`. If the turn budget expired mid-tool-call, the
  researcher raises `RuntimeError("researcher produced no final answer")` so the
  deep loop retries/marks the sub-question stuck instead of shipping intermediate
  chatter ("Let me search for that.") as a draft.
- **Bibliography = body-referenced** (`citations.py::filter_citations_to_referenced`):
  a citation is kept only if its URL appears in the report markdown OR it is
  referenced by arXiv id (`arxiv:ID` / `abs/ID` — academic mode cites Scholar
  hits this way). Applied when building the deep `Report.citations` and again at
  render time (`report/markdown.py`) as defense in depth for every path.
- **URL matching helpers live ONLY in `citations.py`**: `normalize_url()`
  (strips delimiters/trailing punctuation/trailing slash, lowercases) and
  `extract_urls_from_markdown()` (handles `<url>` autolinks, `[url]` text,
  `[label](url)`, bare URLs). Do NOT re-implement URL regexes in new modules.
- **Citation style is inline autolinks** `<https://…>` in prompts
  (`prompts/writer.txt`, `quick_summary.txt`, `researcher.txt`, `nodes/writer.py`)
  — `[https://…]` is not valid Markdown link syntax and must not be instructed.
- **fetch_page never attaches citations to error results** — only successful
  extractions carry a `Citation`.

---

## Deep Paper Analysis Pattern (critic-selected full PDF analysis)

The deep-path critic decides which arXiv papers deserve full PDF analysis;
analyses enrich the final report (Phase 1) and feed the research loop (Phase 2).

**Flow** (see also the "Deep paper analysis" section in `docs/PLAN.md`):

```
researchers -> critic (sufficient? gaps? papers_to_analyze?)
  -> run_paper_analysis_pass (after critic, BEFORE sufficient/break checks)
  -> state.deep_analyses[arxiv_id] = PaperAnalysis
  -> writer prompt gets "Deep paper analyses" section (rendered FIRST)
  -> next critic round + later researchers receive the digest (Phase 2)
```

**Selection** (`nodes/critic.py` + `prompts/critic.txt`):
- `_render_paper_candidates(state)` builds the candidate table from arxiv
  citations in `state.citations`: title, first author + year, ~250-char abstract,
  `referenced_by_count` (distinct drafts mentioning the paper — the cheap
  "notable referencing" signal), `cited_by_count` when known. Excludes already-
  analyzed/requested papers; capped at 40. Key references of analyzed papers
  become candidates (Phase 2).
- Prompt task 4: select using abstract relevance / notable referencing /
  foundational status; `[]` when nothing qualifies. Task 5 (Phase 2): use the
  analyses digest to propose follow-up gaps and key-reference nominations.
- Validation: arXiv-ID pattern only; priority clamped to [0,1]; dedupe by id
  keeping highest priority; scholar/web-only entries rejected (v1 analyzes
  downloadable arXiv PDFs only).

**Pipeline** (`nodes/paper_analysis.py`):
- Shared PDF helpers factored from `academic.py` — `download_pdf_once`,
  `extract_text`, `render_pages`, `fetch_paper_text_fallback` — ONE implementation
  for both paths. Do not add diverging copies back to `academic.py`.
- `analyze_paper_deep`: full PDF (text + optional vision when
  `pdf_vision.enabled`) -> abstract-only via `arxiv_resolve` -> skip.
- `run_paper_analysis_pass`:
  - Filter by `deep_analysis_min_priority`, dedupe, sort by priority desc.
  - **The cap bounds the whole RUN, not a single pass invocation**: remaining
    budget = `deep_analysis_max_papers` − `len(state.deep_analysis_requested)`.
    Subtracting the already-requested set is what prevents N critic rounds from
    analyzing N×cap papers. Never make this a per-call cap.
  - Mark selected ids in `state.deep_analysis_requested` BEFORE running (no
    same-run retry, even on failure — the set persists across checkpoints).
  - Run under `asyncio.Semaphore(deep_analysis_concurrency)`, each paper wrapped
    in `asyncio.wait_for(..., timeout=deep_analysis_timeout_s)`.
  - Library cache: reuse a prior `analyze_paper` analysis via
    `_cached_analysis`/`_analysis_from_row` when
    `deep_analysis_use_library_cache` (string key-references are validated
    against the arXiv-ID pattern).
  - Success: store in `state.deep_analyses`, absorb a citation for the paper,
    archive PDF + `record_analysis` in the library. Failures are logged/skipped —
    a failed analysis must never fail the deep run.

**Writer integration** (`nodes/writer.py`):
- The "Deep paper analyses" section is rendered FIRST in
  `_render_sections_for_prompt` (before sub-question drafts) so the trailing
  12k-char truncation cap can never drop it. Keep this ordering.

**Phase 2 feedback**:
- Critic's sections blob includes `format_deep_analysis_context(state.deep_analyses)`.
- `paths/deep.py::_run_one_researcher_with_recall` receives a per-iteration
  snapshot of `state.deep_analyses` and appends the digest to `prior_context`.
  Use a `None` default + init inside the function (a `dict(...)` default is a
  mutable-default bug).

---

## Search Fallback Chain Pattern

Both `web_search.py` and `scholar.py` use the same fallback pattern:

1. Config specifies `primary` + `fallback_chain` (ordered backends)
2. `register()` resolves the ordered list, deduplicates, and checks API keys
3. The inner `_call` iterates backends; on failure logs a warning and tries next
4. If all backends fail, returns `ToolResult(error=...)`

**Key behaviors:**
- **Empty result falls through**: a backend that returns 0 hits (no error) does
  NOT short-circuit the chain — the loop `continue`s to the next backend.
  Only a non-empty result `break`s out. (scholar.py)
- **Rate-limit retry**: Tavily 429/rate-limit/timeout errors get exponential backoff
  retry (`rate_limit_retries` config) before falling through to SearXNG. On the
  final retry the actual exception is re-raised (never a generic
  `RuntimeError`) so callers/logs keep the real cause. (web_search.py)
- **Proactive quota fallback**: `max_calls_per_session` config — after N calls,
  the tool skips the paid backend and uses SearXNG directly
- **Keyless mode handling**: `TavilyKeylessLimitError` is caught and retried using
  its `retry_after_seconds` field

---

## Bot-Detection / Blocked-Source Handling

When a site actively blocks automated retrieval, the agent's policy is to SKIP
and record the source — never to circumvent the block.

- **Machine-readable verdicts**: fetch/browser tools emit errors prefixed
  `BLOCKED:` (`state.BLOCKED_PREFIX`), e.g. `BLOCKED:bot_detection:cloudflare (403)`,
  `BLOCKED:rate_limited (429)`, `BLOCKED:not_found (404)`, `BLOCKED:http_error (500)`.
- **Detection**: `tools/fetch_page.py` classifies status codes AND body markers
  (`classify_blocked_response` / `detect_challenge_vendor`) because challenge
  pages often return HTTP 200. `tools/browser.py` runs the same marker detection
  on rendered snapshots.
- **No browser fallback for blocked pages**: a classified challenge returns the
  BLOCKED error immediately; the browser fallback is reserved for low-yield 200
  pages. If the browser itself renders a challenge, that verdict is propagated
  instead of the raw challenge HTML.
- **Negative cache**: blocked verdicts are cached per-URL for
  `fetch_page.blocked_cache_ttl_s` (default 3600s) so a blocked URL is not
  re-fetched every turn / iteration.
- **Wayback Machine auto-fallback**: when `fetch_page` classifies a response
  as blocked, it retries `https://web.archive.org/web/2/<url>` first
  (`fetch_page.archive_org_fallback`, default on). A usable snapshot is
  returned with explicit provenance and a citation pointing at the archive
  URL, cached under the original URL (kind `"archive"` in `_PageCache`), so
  repeat fetches hit the cache instead of re-fetching the blocked page or
  the archive. Archive.org URLs never trigger the fallback.
- **PDF downloads** (`paths/url_source.py`): a blocked direct-PDF download
  (403/429/404/5xx) is retried via the same `/web/2/<url>` snapshot; when the
  capture is a real PDF (magic `%PDF-` check), it is saved to the normal PDF
  cache, the extracted text is annotated with provenance, and the citation
  points at the archive URL (confidence 0.5). Same `archive_org_fallback`
  config knob.
- **LLM policy** (`prompts/researcher.txt`): on a `BLOCKED:` error, do not retry
  the same URL and do not bypass (no CAPTCHA solving, stealth browsing, proxies,
  fingerprint evasion). A legitimate alternative (archive.org snapshot, the
  site's RSS feed, the same content elsewhere) may be fetched instead. When
  `fetch_page` auto-retrieves a blocked source via Wayback, the content says
  so and the citation points at the archive URL.
- **Transparency**: `ResearchState.blocked_sources` / `Report.blocked_sources`
  collect skipped sources; the deep and url_source paths render an
  "Unavailable Sources" section in the final markdown so skips are visible.

## Rate-Limit Handling Conventions

| Tool | Backend | Retry strategy | Backoff |
|------|---------|----------------|---------|
| web_search | Tavily | `_tavily_with_retry()` — retries on `UsageLimitExceededError`, `TavilyKeylessLimitError`, `TimeoutError` | 2^attempt (1s, 2s, 4s) |
| scholar | Serper | `_backoff_retry()` — retries on 429/5xx | 2^attempt (1s, 2s) |
| scholar | SearXNG | None (local, no rate limits) | — |
| arxiv | arxiv.org | None (just spacing delay) | — |

All use `asyncio.Semaphore(concurrency)` + spacing delay between calls.

**Call-count quota state:**
Both `web_search.py` and `scholar.py` keep their per-backend call counters as
**closure locals inside `register()`** (created fresh on every registry build),
not module-level globals:
- `web_search.py`: `_tavily_call_count` — `nonlocal` in `_call`; decremented on
  failure so only executed calls count against the quota
- `scholar.py`: `_serper_call_count` — `nonlocal` in `_search`; decremented on
  failure like Tavily

Because they are closure locals, there is no cross-test/module pollution to
reset — do not reintroduce module-level counters for this. (The old
`_reset_web_search_globals` fixture was a no-op and has been removed.)

---

## Config Schema Conventions

- Every config field has a default — no required config.yaml values
- API keys read from environment vars, not config.yaml (security)
- New search backends get a dedicated pydantic model (e.g., `TavilyConfig`, `SearXNGConfig`)
- Rate-limit / quota config lives on the backend's config model:
  - `TavilyConfig.rate_limit_retries: int = 2`
  - `TavilyConfig.max_calls_per_session: int | None = None`
  - `ScholarSerperConfig.max_calls_per_session: int | None = None`
- Refinement knobs live on `AgentConfig`:
  - `max_refinement_depth: int = 2`
  - `max_refinement_per_researcher: int = 3`
  - `max_total_refinements_per_iteration: int = 6`
- Citation-hygiene knob on `AgentConfig`: `max_citations_per_researcher: int = 10`
- Deep paper-analysis knobs live on `AgentConfig`:
  - `deep_analysis_max_papers: int = 3` (0 disables; **per RUN**, see Deep Paper Analysis Pattern)
  - `deep_analysis_min_priority: float = 0.6`
  - `deep_analysis_concurrency: int = 2`
  - `deep_analysis_timeout_s: float = 900.0`
  - `deep_analysis_use_library_cache: bool = True`

---

## Browser Tool

- Playwright MCP spawned lazily on first call (expensive ~1-3s warm-up)
- **Lazy spawn is serialized under an `asyncio.Lock`** in `_ensure_mcp()` — the
  tool loop batches tool calls, and without the lock two concurrent browser
  calls could each spawn a subprocess and orphan one of them. Keep the
  double-checked-lock pattern if you touch this
- Headless mode via `--headless` flag in `mcp_args` (default)
- Tool subset: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_evaluate`
- Graceful degradation: startup failures return `ToolResult(error=...)`, never crash
- Teardown via `reg._close_hooks` — killed when agent finishes

---

---

## Library CLI Patterns

### Auto-tagging (P10.7)
- Post-synthesis LLM call (`nodes/auto_tag.py`) extracts 3-5 topic tags from `query` + report first 2000 chars
- Prompt: `prompts/auto_tag.txt` — expects `{"tags": ["tag1", "tag2"]}` JSON response
- Called in `agent.py` after glossary extraction and archiving, using the returned `artifact_id`
- Tags persisted via `writer.tag(artifact_id, tags, run_id=run_id)` — same FK rules apply (report must exist)
- **Post-synthesis steps are non-fatal**: glossary extraction and auto-tagging
  are wrapped in `try/except` in `agent.py`. A bad LLM float or a storage error
  logs a warning and the finished `Report` is still returned — never discard a
  completed report because of an enrichment step

### Tag CLI commands
- `deep-research-library tag <artifact_id> <tag_name>` — add
- `deep-research-library tag --remove/-r <artifact_id> <tag_name>` — remove
- `deep-research-library tag --list/-l <artifact_id>` — list
- `deep-research-library tag --rename-old <old> --rename-new <new>` — rename globally
- Backend methods: `get_tags_for_artifacts()` (batch, avoids N+1), `delete_tag()`, `rename_tag()`

### Delete command
- `deep-research-library delete <run_id_prefix>` — matches by prefix, shows ambiguity if multiple
- Deletes `.md` and `.pdf` files from `{root_dir}/reports/{year}/{month}/{day}/`
- DB cascade: analyses, tags, citation_edges, search_index deleted; glossary + artifact_versions NULLed

### ls output format
```
<started_at[:19]>  <run_id[:16]>  <path_taken:8s>  <original_query[:60]>  [tag1, tag2]
```
Tags are batch-fetched via `get_tags_for_artifacts()` to avoid N+1 queries.

---

## Web UI Patterns (P12.5)

Design: the "P12.5 — Web UI (design & implementation plan)" section in
`docs/PLAN.md`; phase tracker: the "P12.5 — Web UI (library browser)" section
in `docs/IMPLEMENTATION_LOG.md`.

### App factory

- `create_app(config_path, *, backend=None, research_runner=None)` builds the
  FastAPI app. `backend`/`research_runner` are injectable for tests; in
  production the storage backend is resolved from YAML at startup (lifespan)
  and closed on shutdown.
- Run with `uv run deep-research-web` (binds 127.0.0.1:8080) or
  `uv run uvicorn deep_research.webui.app:app`.
- Every response gets CSP (`default-src 'self'` …), `X-Content-Type-Options:
  nosniff`, and `Referrer-Policy: no-referrer`.

### Storage layer additions

The web UI relies on additive `StorageBackend` protocol methods implemented in
both SQLite and Postgres: `list_reports(limit, offset, *, tag, path)`,
`search_reports(...)` (escaped LIKE over query + markdown, lowercased both
sides for dialect parity), `count_reports(*, q, tag, path)`, `count_artifacts()`,
`list_tags(limit)`, `get_artifacts(ids)`. Keep them additive — CLI calls use
the old defaults.

### Research jobs + SSE

- `ResearchJobManager` is in-memory only (jobs die on server restart; finished
  reports are already archived). It generates the `run_id`, passes it into
  `run_research(..., run_id=..., progress=reporter)`, and verifies archival via
  the backend before emitting the terminal `done` event.
- `GET /api/research/jobs/{id}/stream` replays the event log, sends a status
  snapshot, then live events with 15s keepalives; it ends after
  `done`/`error`/`cancelled`.
- Cancelling calls `task.cancel()` — cooperative cleanup inside the agent is
  not guaranteed. `cancel()` marks the job `cancelling` and emits a
  `cancelling` event immediately (the `CancelledError` handler only runs at the
  next await point, so without this a cancel would return while the job still
  read `running`); the terminal `cancelled` event fires when the handler runs.

### Frontend conventions

- Plain ES modules under `webui/static/js/`; no bundler, no npm deps.
  `static/package.json` exists only to mark `"type": "module"` so node can run
  the parser tests.
- `markdown.js` is split into a pure AST parser (`parse`/`parseInline` — no
  DOM, unit-tested under node:test via `tests/webui/markdown.test.mjs`) and a
  DOM renderer. Text is only ever inserted via `textContent`; link protocols
  are restricted to http/https/mailto (`safeUrl`). Never feed raw markdown to
  `innerHTML`.
- Views own their DOM and return a cleanup function; `app.js` disposes the
  previous view's cleanup on route change.
- Hash routing: `#/` list, `#/report/<run_id>`. `?new=1` deep-links to the
  New Research modal.

### Test conventions

- API tests use `with TestClient(create_app(...))` — without the context
  manager the portal cancels background asyncio tasks, which turns job tests
  flaky.
- Inject a `FakeRunner` (`research_runner=...`) instead of calling the real
  LLM; see `tests/test_research_api.py`.
- The JS parser tests run via `pytest` → `node --test`; skipped when node is
  absent.

---

## Testing Conventions

- **Framework**: pytest + respx (HTTP mocking) + pytest-asyncio
- **Test file location**: `tests/` for general, `tests/tools/` for tool-specific
- **Live tests**: skipped unless env var is set (`requires_tavily`, `requires_serper`, etc.)
- **Coverage gate**: coverage must stay above 80% — enforced by
  `[tool.coverage.report] fail_under = 80` in `pyproject.toml` (current total: 87%).
  Run `coverage run -m pytest && coverage report`; `pytest --cov` works too (pytest-cov is a dev dep).
- **Cross-test isolation**: module-level globals reset via `pytest.fixture(autouse=True)`
  (see `test_tools_web_search.py::_reset_web_search_globals`)
- **Rate-limit tests**: mock 429/502 responses, verify retry + fallback behavior

---

## Common Pitfalls for AI Agents

1. **Module-level globals**: if you add genuinely module-level mutable state,
   always add an `autouse` fixture to reset it between tests. Note the
   `web_search`/`scholar` call counters are deliberately NOT module-level —
   they are closure locals inside `register()`, so each registry build starts
   at zero (see Call-count quota state)
2. **Tool error propagation**: Never raise from a tool callable — always return
   `ToolResult(error=...)`. The LLM loop catches exceptions but prefers structured errors
3. **Config env vars**: API keys are resolved in `config.py` via `_env()` — don't read
   `os.environ` directly in tool code
4. **MCP transport**: stdio by default; HTTP transport exists but is unused.
   If adding new MCP tools, keep the lazy-init pattern
5. **Shared ToolRegistry is immutable at runtime**: `ToolRegistry.register()`
   raises on duplicate names. Never register per-researcher tools on the shared
   registry — use `ScopedToolRegistry` to wrap it (see Dynamic Refinement Pattern)
6. **Per-run caps must subtract already-requested state**: the deep-analysis cap bounds the
   whole run — compute `remaining = cap - len(state.deep_analysis_requested)` or repeated
   critic rounds silently analyze N×cap papers. Also never use a mutable default
   (`dict(state.deep_analyses)`) in a function signature — ruff B006; use `None` + init inside.
7. **Writer analyses section ordering**: render "Deep paper analyses" BEFORE the drafts — the
   sections blob is truncated to 12k chars, and appending analyses last means long drafts drop them.
8. **URL matching belongs in `citations.py`**: use `normalize_url`/`extract_urls_from_markdown`/
   `filter_citations_to_referenced`; do not copy URL regexes into new modules.
9. **Vision context limits ≠ advertised context**: VLM servers (llama.cpp, vLLM)
   have a much lower effective limit for combined text+image payloads than their
   advertised `n_ctx`. A server advertising 131k context may fail at ~8k chars
   text + 1 image. Never stuff large text alongside images — cap text in image
   batch calls (`MAX_TEXT_CHARS_WITH_IMAGES` in `llm/vision.py`) and reserve
   full text for text-only synthesis. "Failed to tokenize prompt" does NOT mean
   "no vision support" — test with minimal payloads before concluding a
   capability is missing.

---

## Concurrency Safety Patterns

### Serialize shared-connection DB ops (sqlite_backend.py)
`SqliteStorageBackend` owns ONE shared `aiosqlite` connection, so every public
method is wrapped with `@_serialized` — an `asyncio.Lock` (`self._op_lock`)
held across the whole operation (including its final `commit()`). Without it,
multi-statement writes (e.g. `delete_report`: several DELETEs + one commit)
interleave their awaited statements with other handlers' statements, merging
independent implicit transactions on the single connection.

- Decorate EVERY public method (writes **and** reads — a read interleaving into
  an open write transaction would see partial uncommitted rows). Keep
  `connect`/`close`/`ensure_schema`/private `_*` helpers undecorated (they run
  once at eager `connect()`).
- Never call a decorated method from inside another decorated method — the
  lock is non-reentrant and would deadlock. Extract a private `_` helper for
  the shared body (e.g. `upsert_glossary_entries` calls `_upsert_glossary_entry`,
  not the public `upsert_glossary_entry`).

### Call-count quota checks (web_search.py, scholar.py)
Quota read + increment must be atomic under concurrent calls. Use `asyncio.Lock`:
```python
_tavily_call_lock = asyncio.Lock()
async with _tavily_call_lock:
    if _tavily_call_count >= max_calls:
        continue  # skip backend
    _tavily_call_count += 1
```

### Atomic max-papers claim (academic.py)
`_analyze_and_recurse` claims a paper's slot under an `asyncio.Lock`, and
**re-checks membership inside the lock**. The pre-check (`if base in processed`)
runs before the lock, so two tasks for the same paper (e.g. the same reference
enqueued by two parents in one batch) can both pass it; the in-lock re-check is
what actually prevents a double-claim:
```python
base = _strip_version(node.arxiv_id)
if base in processed:
    return
async with claim_lock:
    if base in processed:            # re-check — closes the pre-check TOCTOU
        return
    if processed_count >= cfg.max_papers:
        return
    processed.add(base)
    processed_count += 1
```

### Timeout with task cancellation (deep.py, academic.py)
Wrap coroutines in `asyncio.create_task` before passing to `wait_for` so the
underlying coroutine can be cancelled on timeout. The timeout value comes from
`config.agent.researcher_timeout_s` (default 3600s) — not `config.llm.timeout_s`,
which is the per-LLM-call timeout, not the per-researcher wall-clock budget:
```python
raw = [asyncio.create_task(c) for c in coros]
timed = [asyncio.wait_for(t, timeout=config.agent.researcher_timeout_s) for t in raw]
await asyncio.gather(*timed, return_exceptions=True)
```
Both `paths/deep.py` and `paths/academic.py` classify a `TimeoutError`
distinctly from other failures in their `/researcher raised/` log lines so
triage can tell a true outer-timeout (likely a hung tool or a slow model)
from a generic tool exception:
```python
rtype = "timeout" if isinstance(r, TimeoutError) else type(r).__name__
logger.warning("researcher for %s raised (%s): %s", sq.id, rtype, msg)
```

### Per-tool-call hard timeout (ToolRegistry)
`ToolRegistry.call` wraps each tool invocation in `asyncio.wait_for(..., timeout=...)`
so the inner task is actually **cancelled** when the deadline passes (no orphaned
tool calls). The timeout value is `config.agent.tool_timeout_s` (default `300.0`)
applied by `build_tool_registry()`. This is the *inner* guarantee: a single hung
tool (e.g. `fetch_page` on a non-responding server, or `pdf_render_pages` stuck
in `pdf2image`) cannot eat the researcher's whole budget — it surfaces as a
clean `ToolResult.error` instead.

**`0` disables the guard**: `build_tool_registry()` treats `tool_timeout_s <= 0`
as "no per-call timeout" (`set_tool_timeout(float("inf"))`). Disable per-call
timeouts in tests with `reg.set_tool_timeout(float("inf"))`.

**Nested tool calls must use `call_internal`, not `call`**: `ToolRegistry.call`
acquires the registry-wide semaphore (`max_concurrent_tools`). A tool that
internally invokes another tool (e.g. `fetch_page` calling `pdf_extract_text`
or `browser_navigate`) must call `reg.call_internal(...)` instead — it runs the
tool WITHOUT the semaphore (the outer call already holds one permit, and
re-acquiring the same non-reentrant semaphore would self-deadlock a batch that
saturates concurrency). The per-call timeout still applies.

Inner timeouts are what make the outer `wait_for` cancellation actually unblock.
Heavy sync blockers (`_sync_extract` in `tools/pdf.py`, `trafilatura.extract`,
diskcache, file writes in `library/writer.py`) run via `asyncio.to_thread`, so a
blocked call never stalls the event loop.

### Vision rendering budget (academic + deep paths)
Both `paths/academic.py` (`_analyze_and_recurse`) and the deep analysis pass
(`nodes/paper_analysis.py::analyze_paper_deep`) pass PDF pages to vision
rendering (`pdf_render_pages`) inside the same per-paper wall-clock budget
as the text fetch + LLM analysis. The shared helpers now live in
`nodes/paper_analysis.py`: `download_pdf_once` and `extract_text` (180s each)
and `render_pages` (300s total) have their own inner `asyncio.timeout`
boundaries so a slow render cannot eat the overall budget. Vision failure is
non-fatal: a `TimeoutError` from the render path returns `[]` and the analysis
downgrades to text-only mode.

### Vision analysis context budget (nodes/analyze_paper.py)
The `analyze_paper` node uses a two-phase approach for papers with images:

1. **Image batch calls** (`_analyze_image_batch`): processes page images in
   adaptive batches to extract figure descriptions and visible text. Paper
   text included alongside images is capped at `MAX_TEXT_CHARS_WITH_IMAGES`
   (4000 chars, in `llm/vision.py`) — just enough context to locate figures.
   This is critical because VLM servers have a much lower effective context
   limit for combined text+image payloads than for text alone (e.g. llama.cpp
   with native vision encoding fails at ~8k chars text + 1 image, despite
   advertising 131k context).

2. **Text-only synthesis** (`_synthesize_final`): merges all per-batch figure
   descriptions with the FULL paper text for the final structured analysis. No
   images — full context budget available. (The text budget is derived from
   `max_context_tokens`; the legacy `_MAX_PAPER_CHARS` cap only lives in the
   deprecated `_build_messages` path.)

**Adaptive batching:**
- `_compute_batch_size` estimates how many images fit using `TOKENS_PER_IMAGE`
  (1500 tokens/image for native vision encoding) plus the capped text budget.
- On context overflow: halve batch size → retry same images.
- On single-image overflow: degrade the current image's resolution via
  `IMAGE_DEGRADE_LADDER` (512px/q60 → 256px/q40). The ladder is **per-image** —
  it resets when an image is skipped, so one oversized page cannot consume every
  degradation step for the remaining pages. Skip the image only after all steps
  fail.
- Non-context errors (server 500, etc.) are non-fatal: skip batch, continue.

**Key constants (all in `llm/vision.py`):**
- `TOKENS_PER_IMAGE = 1500` — per-image cost for servers with native vision
  encoding (NOT base64-as-text; most VLM servers encode images as fixed-size
  vision tokens regardless of base64 length).
- `MAX_TEXT_CHARS_WITH_IMAGES = 4000` — text cap in image batch calls.

**Model selection:** `academic.py` passes `config.llm.vision_model` when images
are present, `config.llm.text_model` otherwise. Both default to the same model
but can be configured separately for deployments with dedicated vision endpoints.

### Turn-budget math
The tool loop's wall-clock demand is roughly
`researcher_max_turns × config.llm.timeout_s` (worst case, if every turn hits
the LLM timeout) **plus** tool I/O (bounded per call by `tool_timeout_s`).
Keep the LLM-only product comfortably below `researcher_timeout_s` or
researchers will hit the outer timeout even when nothing is broken.
Defaults: 12 × 240s = 2880s, ~720s of tool-I/O headroom under 3600s.
`run_with_tools` logs per-turn LLM ms + tool ms to make budget exhaustion
diagnosable.

---

## Context Management Pattern (tool_loop.py)

`run_with_tools()` now accepts `max_context_tokens` (default 131072). When the
estimated token count of the message list exceeds 75% of this value, older
turns are summarised into a single compressed "user" message.

**Key implementation details:**

- **Token estimation**: uses `tiktoken` with a fallback to `cl100k_base` for
  custom models like `qwen3.5-122b`. Counts message framing overhead (~4
  tokens/message) plus content tokens from both top-level string fields and
  nested `tool_calls` dicts.
- **Exchange-aware truncation**: walks backwards from the end of the message
  list. An "exchange" is an assistant/user message **plus every tool message
  that follows it** (a tool response belongs to the assistant directly before
  it — keeping them together is what keeps the history valid for the OpenAI
  API). Keeps the last 3 complete exchanges; summarises everything before that.
  Do NOT revert to pairing an assistant with its *preceding* tools — that
  orphans tool responses and drops unanswered `tool_calls`.
- **Summarisation is async and non-fatal**: `_summarize_turns()` calls the LLM
  with a dedicated summarisation prompt. If it fails (timeout, API error), the
  original messages are kept unchanged — the loop continues without context
  compression.
- **75% threshold**: triggers before the model hits its absolute limit, leaving
  headroom for the summarised conversation to continue. Configurable per
  deployment via `config.llm.max_context_tokens`.

**Call chain:**
`config.llm.max_context_tokens` → `deep.py` → `researcher.py` → `tool_loop.py`

---

## Checkpoint Resume Pattern (checkpoint.py + deep.py)

The deep research loop saves a JSON checkpoint once per iteration so a crashed
run can resume from where it left off.

**Checkpoint save points (`deep.py`):**
```
planner → iteration(N) researchers → critic → deep paper-analysis pass → flush refinements + enqueue gaps → save checkpoint → next iteration
                                                       ↘ (sufficient / force-stop) → save checkpoint → break
(the deep analysis pass runs right after the critic, BEFORE the sufficient/break checks, so
 analyses complete even on the final iteration and are available to the writer)
```
The checkpoint is saved **after** all state mutations for the iteration — i.e.
after refinements are flushed into the plan and critic gaps are enqueued, or
immediately before a `break`. Saving before the flush would strand
`pending_refinements` outside `plan.sub_questions`; on resume the top-of-loop
`if not pending: break` would silently drop them.

**Resume flow (`deep.py`, top of `deep_research()`):**
1. On startup, if `run_id` is set, calls `load_checkpoint(run_id)`
2. If a checkpoint exists and its `query` matches the current run, loads it and
   skips the planner entirely
3. The iteration loop starts from `range(state.iteration, iterations_cap)` —
   already-completed iterations are skipped
4. `is_covered()` prevents re-running researchers on sub-questions that already
   have drafts + citations
5. After the writer completes, the checkpoint is discarded via `discard_checkpoint()`

**What gets saved:**
- `ResearchState` serialised via pydantic `model_dump(mode="json")` — query,
  plan, sections, drafts, citations, iteration, pending_refinements,
  `deep_analyses`, `deep_analysis_requested` (new fields have pydantic defaults,
  so old checkpoints load unchanged)
- `run_id` metadata at top level for cross-reference

**Safety mechanisms:**
- Query validation: if the checkpoint's `query != original_query`, it's
  discarded and a fresh plan is created
- Non-fatal I/O: save/load/discard all catch exceptions and log warnings rather
  than crashing the agent
- Separate storage: checkpoints live in `./.cache/research_checkpoints/`,
  independent from the PDL SQLite store — no schema migration needed
- Atomic checkpoint writes: uses `tempfile` + `os.replace` so a crash mid-write
  never leaves a corrupted checkpoint
- Stuck sub-question detection: after `agent.max_subquestion_retries` (default 3)
  consecutive failures, a sub-question is forcibly marked covered with an empty
  draft, preventing infinite loops

---

## Security Patterns

### Path traversal sanitization (library/writer.py)
Always sanitize user-controllable strings before using them as file paths. The
PDF artifact slug is derived from the content `sha` (plus `arxiv_id` when
present) so distinct PDFs can never collide on the same file:
```python
# in _copy_pdf_to_store()
slug_base = (arxiv_id or sha) + "-" + (title or "untitled").replace("/", "_")[:32]
slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug_base).strip() or "unknown"
```

### Config-path containment (microservice.py)
The `/research` endpoint accepts a `config_path`. Verify the *resolved* path is
contained in the allowed directory with `is_relative_to` — a `startswith` prefix
check is fooled by sibling dirs (e.g. cwd `/proj` vs `/proj_evil`):
```python
config_file = Path(request.config_path).resolve()
if not config_file.is_relative_to(_ALLOWED_CONFIG_DIR):   # _ALLOWED_CONFIG_DIR is .resolve()d
    raise HTTPException(status_code=400, detail="config_path outside allowed directory")
```

### Request timeout must not block on cancellation (microservice.py)
Do NOT use `asyncio.wait_for(run_research(...), 600)` for the HTTP deadline:
the agent runs blocking work via `asyncio.to_thread`, which cannot be
cancelled, so `wait_for` blocks until cancellation completes and the 600s cap
is defeated. Instead run the agent as a child task and use `asyncio.wait(...,
timeout=...)`; on timeout `task.cancel()` + `await asyncio.gather(task,
return_exceptions=True)` so the response returns promptly while the thread
finishes in the background.

### Citation URL validation (researcher.py)
Reject non-HTTP URLs and non-dict citation entries from LLM output:
```python
if not isinstance(c, dict):
    continue
url = c.get("url")
if not url or not url.startswith(("http://", "https://", "ftp://")):
    continue
```

---

## Cache TTL Clock

`fetch_page`'s `_PageCache` uses wall-clock `time.time()` timestamps (persisted
in the diskcache value) rather than `time.monotonic()`, because the cache must
survive process restarts — `monotonic()` resets on reboot and would make every
entry look infinitely old. The known tradeoff: NTP/system-time jumps can cause
spurious expiry or indefinite retention. Only switch to `monotonic()` for
in-process caches that don't need to persist.
