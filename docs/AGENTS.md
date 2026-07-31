# Code Agent Reference

This document captures architectural decisions, patterns, and conventions for
future AI coding agents working on this codebase. Read this first before making
changes.

---

## Architecture Overview

- **Language**: Python 3.12+, async-first, `uv` package manager
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
| `paths/deep.py` | Deep research path (planner→researcher→critic→writer); checkpoint resume |
| `paths/academic.py` | Academic citation-graph path |
| `checkpoint.py` | JSON checkpoint save/load for crash recovery of deep research state |
| `nodes/auto_tag.py` | Post-synthesis LLM call — extracts 3-5 topic tags from query + report |
| `prompts/auto_tag.txt` | Prompt template for auto-tag extraction |
| `prompts/glossary_extract.txt` | Prompt template for glossary extraction |
| `llm/tool_loop.py` | LLM tool-calling loop with `run_with_tools()`; per-call timeout via `ToolRegistry.call`; `ScopedToolRegistry` for per-researcher tool isolation; context management with summarisation at 75% of max context window |

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
- **Normalized dedup**: `absorb_refinements` compares `.strip().lower()` question
  text to catch case/whitespace variants.
- **`research()` returns a 3-tuple**: `(answer_md, citations, refinements)`.
  `paths/deep.py` accepts both 2-tuples (backward compat) and 3-tuples.

---

## Search Fallback Chain Pattern

Both `web_search.py` and `scholar.py` use the same fallback pattern:

1. Config specifies `primary` + `fallback_chain` (ordered backends)
2. `register()` resolves the ordered list, deduplicates, and checks API keys
3. The inner `_call` iterates backends; on failure logs a warning and tries next
4. If all backends fail, returns `ToolResult(error=...)`

**Key behaviors added recently:**
- **Rate-limit retry**: Tavily 429/rate-limit/timeout errors get exponential backoff
  retry (`rate_limit_retries` config) before falling through to SearXNG
- **Proactive quota fallback**: `max_calls_per_session` config — after N calls,
  the tool skips the paid backend and uses SearXNG directly
- **Keyless mode handling**: `TavilyKeylessLimitError` is caught and retried using
  its `retry_after_seconds` field

---

## Rate-Limit Handling Conventions

| Tool | Backend | Retry strategy | Backoff |
|------|---------|----------------|---------|
| web_search | Tavily | `_tavily_with_retry()` — retries on `UsageLimitExceededError`, `TavilyKeylessLimitError`, `TimeoutError` | 2^attempt (1s, 2s, 4s) |
| scholar | Serper | `_backoff_retry()` — retries on 429/5xx | 2^attempt (1s, 2s) |
| scholar | SearXNG | None (local, no rate limits) | — |
| arxiv | arxiv.org | None (just spacing delay) | — |

All use `asyncio.Semaphore(concurrency)` + spacing delay between calls.

**Module-level globals for call counting:**
- `web_search.py`: `_tavily_call_count` — reset via fixture in tests
- `scholar.py`: `_serper_call_count` — local `nonlocal` inside `register()`

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

---

## Browser Tool

- Playwright MCP spawned lazily on first call (expensive ~1-3s warm-up)
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

## Testing Conventions

- **Framework**: pytest + respx (HTTP mocking) + pytest-asyncio
- **Test file location**: `tests/` for general, `tests/tools/` for tool-specific
- **Live tests**: skipped unless env var is set (`requires_tavily`, `requires_serper`, etc.)
- **Cross-test isolation**: module-level globals reset via `pytest.fixture(autouse=True)`
  (see `test_tools_web_search.py::_reset_web_search_globals`)
- **Rate-limit tests**: mock 429/502 responses, verify retry + fallback behavior

---

## Common Pitfalls for AI Agents

1. **Module-level globals**: `_tavily_call_count` persists across tests — always add
   an `autouse` fixture to reset it when adding new module-level state
2. **Tool error propagation**: Never raise from a tool callable — always return
   `ToolResult(error=...)`. The LLM loop catches exceptions but prefers structured errors
3. **Config env vars**: API keys are resolved in `config.py` via `_env()` — don't read
   `os.environ` directly in tool code
4. **MCP transport**: stdio by default; HTTP transport exists but is unused.
   If adding new MCP tools, keep the lazy-init pattern
5. **Shared ToolRegistry is immutable at runtime**: `ToolRegistry.register()`
   raises on duplicate names. Never register per-researcher tools on the shared
   registry — use `ScopedToolRegistry` to wrap it (see Dynamic Refinement Pattern)
6. **Vision context limits ≠ advertised context**: VLM servers (llama.cpp, vLLM)
   have a much lower effective limit for combined text+image payloads than their
   advertised `n_ctx`. A server advertising 131k context may fail at ~8k chars
   text + 1 image. Never stuff large text alongside images — cap text in image
   batch calls (`_MAX_IMAGE_BATCH_TEXT_CHARS`) and reserve full text for
   text-only synthesis. "Failed to tokenize prompt" does NOT mean "no vision
   support" — test with minimal payloads before concluding a capability is missing.

---

## Concurrency Safety Patterns

### Call-count quota checks (web_search.py, scholar.py)
Quota read + increment must be atomic under concurrent calls. Use `asyncio.Lock`:
```python
_tavily_call_lock = asyncio.Lock()
async with _tavily_call_lock:
    if _tavily_call_count >= max_calls:
        continue  # skip backend
    _tavily_call_count += 1
```

### Atomic max-papers cap (academic.py)
Use a plain `int` counter with `nonlocal` in closures. Python GIL makes `+= 1` atomic:
```python
processed_count: int = 0
# inner function:
    nonlocal processed_count
    if processed_count >= max_papers:
        return
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
`ToolRegistry.call` wraps each tool invocation in `asyncio.timeout(config.agent.tool_timeout_s)`
(default 120s). This is the *inner* guarantee: a single hung tool (e.g.
`fetch_page` on a non-responding server, or `pdf_render_pages` stuck in
`pdf2image`) cannot eat the researcher's whole budget — it surfaces as a
clean `ToolResult.error` instead. Disable via `reg.set_tool_timeout(float("inf"))`
in tests, or set `agent.tool_timeout_s` to a large value in config to widen
the budget for a particular deployment — `build_tool_registry()` only applies
the override when the value is `> 0`, so `0` is treated as "use the registry's
built-in 120s default" (set an explicit large value to actually widen it).
Inner timeouts are what make the outer `wait_for` cancellation actually
unblock: without them, sync work via `run_in_executor` on a non-cancellable
blocker would outlive the `wait_for` deadline. In this codebase, the heavy
sync blockers (`_sync_extract` in `tools/pdf.py`) use `asyncio.to_thread`,
which honours cancellation; the inner `asyncio.timeout` is belt-and-braces.

### Vision rendering budget (academic path)
`_analyze_and_recurse` in `paths/academic.py` passes PDF pages to vision
rendering (`pdf_render_pages`) inside the same per-paper wall-clock budget
as the text fetch + LLM analysis. Both `_fetch_paper_text` (180s per
sub-step) and `_render_paper_pages` (300s total) have their own inner
`asyncio.timeout` boundaries so a slow render cannot eat the researcher's
overall budget. Vision failure is non-fatal: a `TimeoutError` from the
render path returns `[]` and the analysis downgrades to text-only mode.

### Vision analysis context budget (nodes/analyze_paper.py)
The `analyze_paper` node uses a two-phase approach for papers with images:

1. **Image batch calls** (`_analyze_image_batch`): processes page images in
   adaptive batches to extract figure descriptions and visible text. Paper
   text included alongside images is capped at `_MAX_IMAGE_BATCH_TEXT_CHARS`
   (4000 chars) — just enough context to locate figures. This is critical
   because VLM servers have a much lower effective context limit for combined
   text+image payloads than for text alone (e.g. llama.cpp with native vision
   encoding fails at ~8k chars text + 1 image, despite advertising 131k context).

2. **Text-only synthesis** (`_synthesize_final`): merges all per-batch figure
   descriptions with the FULL paper text (up to `_MAX_PAPER_CHARS` = 40k chars)
   for the final structured analysis. No images — full context budget available.

**Adaptive batching:**
- `_compute_batch_size` estimates how many images fit using `_TOKENS_PER_IMAGE`
  (1500 tokens/image for native vision encoding) plus the capped text budget.
- On context overflow: halve batch size → retry same images.
- On single-image overflow: degrade image resolution via `_IMAGE_DEGRADE_LADDER`
  (512px/q60 → 256px/q40) → skip image if all steps fail.
- Non-context errors (server 500, etc.) are non-fatal: skip batch, continue.

**Key constants:**
- `_TOKENS_PER_IMAGE = 1500` — per-image cost for servers with native vision
  encoding (NOT base64-as-text; most VLM servers encode images as fixed-size
  vision tokens regardless of base64 length).
- `_MAX_IMAGE_BATCH_TEXT_CHARS = 4000` — text cap in image batch calls.
- `_MAX_PAPER_CHARS = 40000` — text cap in text-only synthesis.

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
  list, grouping assistant/user messages with their preceding tool-message
  chains into proper exchanges. Keeps the last 3 complete exchanges; summarises
  everything before that.
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

The deep research loop saves a JSON checkpoint after each critic invocation so
a crashed run can resume from where it left off.

**Checkpoint save points (`deep.py` line 159):**
```
planner → iteration(N) researchers → critic → save checkpoint → break/continue
```
Saved after the critic but before the loop decides to break or add gaps. This
captures all absorbed researcher results and flushed refinements.

**Resume flow (`deep.py` line 59-86):**
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
  plan, sections, drafts, citations, iteration, pending_refinements
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
Always sanitize user-controllable strings before using them as file paths:
```python
slug_base = (arxiv_id or sha) + "-" + title[:32]
slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug_base).strip() or "unknown"
```

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

Always use `time.monotonic()` for cache expiry checks, never `time.time()`, to
avoid spurious expiry or indefinite retention on NTP/system-time jumps
(fetch_page.py:75).
