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
| `paths/deep.py` | Deep research path (planner→researcher→critic→writer) |
| `paths/academic.py` | Academic citation-graph path |
| `llm/tool_loop.py` | LLM tool-calling loop with `run_with_tools()` |

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

---

## Browser Tool

- Playwright MCP spawned lazily on first call (expensive ~1-3s warm-up)
- Headless mode via `--headless` flag in `mcp_args` (default)
- Tool subset: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_evaluate`
- Graceful degradation: startup failures return `ToolResult(error=...)`, never crash
- Teardown via `reg._close_hooks` — killed when agent finishes

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
