# Firecrawl Web-Search Backend — Integration Plan

Status: **phase 1 (search backend) implemented · phase 2 (fetch_page rescue) implemented**

## Goal

Add [Firecrawl](https://firecrawl.dev) as a third web-search backend for the
`web_search` tool, alongside Tavily and SearXNG, slotted in as **second
priority**:

```
tavily (primary) → firecrawl (second) → searxng (last-resort fallback)
```

No behavior change unless `FIRECRAWL_API_KEY` is set — without a key the
firecrawl backend is skipped exactly like Tavily is today.

## Research summary (Firecrawl search API)

- **Endpoint**: `POST https://api.firecrawl.dev/v2/search` with
  `Authorization: Bearer fc-...`. Body: `{"query": "...", "limit": N}`.
- **Response**: `{"success": true, "data": {"web": [{"url", "title",
  "description"}]}}`. **No relevance score** is returned (unlike Tavily) →
  normalize with `confidence_score=0.5`, same convention as SearXNG.
- **Cost**: 2 credits per 10 results (search only, no `scrapeOptions`).
  Free API key = 1,000 credits one-time; keyless use is possible but
  IP-rate-limited daily (we will require a key, like Tavily).
- **Rate limits**: free plan = 10 search req/min; 429 on excess. 408 on
  timeout, 500 on server errors — all retryable with backoff.
- **Python SDK**: `firecrawl-py` (`AsyncFirecrawl.search(...)`), but see
  decision below — we use raw `httpx` instead.
- **Extras available later** (not in this scope): `tbs` time filters,
  `categories: ["research"|"github"|"pdf"]`, `includeDomains`/
  `excludeDomains`, `sources: ["news"]`, and `scrapeOptions` for full-page
  markdown in one call.

## Design decisions

### D1 — Raw `httpx` instead of `firecrawl-py` SDK (recommended)

The SearXNG backend already uses raw `httpx`, and the Firecrawl search call is
a single POST with a simple JSON shape. Raw httpx:

- adds **no new dependency** (Tavily SDK was justified by its typed
  rate-limit errors; Firecrawl's 429/408/500 are plain HTTP status codes),
- keeps tests consistent — `respx` mocks the URL directly, as in
  `tests/test_tools_web_search.py`,
- avoids SDK version churn (v0/v1/v2 API migration history).

Alternative considered: `firecrawl-py` `AsyncFirecrawl` — fine, but buys us
nothing for a single endpoint.

### D2 — Priority slot

Default chain becomes `primary: tavily`, `fallback_chain: ["firecrawl",
"searxng"]`. Backends without a resolvable API key are dropped at registry
build time (existing pattern for tavily), so users without a Firecrawl key
keep today's behavior.

### D3 — Reuse Tavily's quota + retry patterns

- `rate_limit_retries` with exponential backoff on 429/408/5xx, then fall
  through to the next backend (mirrors `_tavily_with_retry`).
- Optional `max_calls_per_session` quota guard with the same atomic
  lock-and-decrement-on-failure pattern (credits are scarce on the free
  tier — 1,000 total).

### D4 — Scope: `web_search` tool only

`blog_search` (Tavily site: queries) and `scholar` (Serper/SearXNG) are left
untouched. Firecrawl's `includeDomains` makes it a natural future backend for
`blog_search`, and its scrape endpoint could later back `fetch_page` for
bot-blocked pages — both listed as follow-ups, not this change.

## Implementation steps

### 1. Config schema — `deep_research/config.py`

```python
class FirecrawlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_env: str = "FIRECRAWL_API_KEY"
    endpoint: str = "https://api.firecrawl.dev/v2/search"
    timeout_s: float = 30.0
    rate_limit_retries: int = 2      # retries on 429/408/5xx before falling back
    max_calls_per_session: int | None = None  # None = unlimited
```

Extend `SearchConfig`:

```python
primary: Literal["tavily", "searxng", "firecrawl"] = "tavily"
fallback_chain: list[Literal["tavily", "searxng", "firecrawl"]] = Field(
    default_factory=lambda: ["firecrawl", "searxng"]
)
firecrawl: FirecrawlConfig = Field(default_factory=FirecrawlConfig)

def resolve_firecrawl_key(self) -> str | None:
    return _env(self.firecrawl.api_key_env)
```

### 2. Backend implementation — `deep_research/tools/web_search.py`

Add `_firecrawl_search()` next to `_searxng_search()`:

```python
async def _firecrawl_search(
    query: str, max_results: int, api_key: str, endpoint: str,
    timeout_s: float, retries: int,
) -> list[Citation]:
    # POST endpoint {"query": query, "limit": max_results}
    # headers: Authorization: Bearer <key>
    # retry loop: 429/408/5xx → exponential backoff (2**attempt), raise when exhausted
    # map data.web[] → Citation(url, title, snippet=description,
    #     source_type="web", confidence_score=0.5, discovered_by=web_search)
```

Wire into `register()`:

- Drop `"firecrawl"` from `backends` when the key is unset (same warning
  pattern as Tavily).
- Add a firecrawl quota counter + lock (same as Tavily's) honoring
  `max_calls_per_session`.
- Add `elif backend == "firecrawl":` branch in `_call`.
- Update the module docstring and the "no usable backend" error message.

### 3. Tests — `tests/test_tools_web_search.py` (respx-mocked, existing style)

- `test_firecrawl_search_returns_citations` — `primary="firecrawl"`,
  fallback `[]`; mock POST endpoint → assert normalized citations
  (url/title/snippet, confidence 0.5).
- `test_firecrawl_fallback_when_tavily_unavailable` — tavily 502 →
  firecrawl serves (proves second-priority ordering).
- `test_firecrawl_rate_limit_then_searxng_fallback` — firecrawl 429 with
  `rate_limit_retries=1` → exhausts → searxng serves.
- `test_firecrawl_proactive_quota_fallback` — `max_calls_per_session=1`,
  second call routes to searxng.
- `test_firecrawl_skipped_without_key` — chain includes firecrawl but env
  unset → searxng serves directly, no firecrawl request made.
- `test_live_firecrawl_search` — gated on a new `requires_firecrawl`
  fixture in `tests/conftest.py` (mirrors `requires_tavily`).

### 4. Config & docs

- `config.example.yaml` — add commented `firecrawl:` block under `search:`
  and update `fallback_chain` example.
- `README.md` — mention Firecrawl as Option 3 in the search-backend setup
  section, add `FIRECRAWL_API_KEY` to the env-var table, note the 2
  credits/10 results cost.
- `.env.local` — no change committed (user-managed), but document
  `export FIRECRAWL_API_KEY=fc-...`.

### 5. Verification

- `uv run pytest tests/test_tools_web_search.py -q` (mocked tests)
- Full suite: `uv run pytest -q`
- Lint/typecheck per repo hooks (ruff / mypy via pre-commit).
- Optional live smoke: set `FIRECRAWL_API_KEY`, run
  `pytest -k test_live_firecrawl_search`.

## Out of scope / follow-ups

1. `blog_search` firecrawl backend via `includeDomains` (replaces Tavily
   site: queries when Tavily quota is exhausted).
2. `fetch_page` rescue path — **implemented (phase 2)**; design below.
3. Firecrawl `categories: ["research"]` as a `scholar` seed backend.
4. `tbs` time-window support plumbed through researcher queries.

### Follow-up 2: Firecrawl scrape rescue in `fetch_page` (implemented)

**Recommendation: not a general scraper, only a targeted rescue layer.**
The existing pipeline (httpx + trafilatura → browser MCP → Wayback) is free;
Firecrawl scrape costs 1 credit/page and a deep-research run fetches dozens
of pages, so routing all fetches through it would burn credits fast.

Where it adds value: pages that are **bot-blocked** (`classify_blocked_response`
verdict in `deep_research/tools/fetch_page.py`) — Firecrawl's proxy network
(`proxy: "auto"`, no credit surcharge) gets through most anti-bot walls, and
returns fresh content unlike stale Wayback snapshots.

Implemented (phase 2):

- Config under `fetch_page:`:
  ```yaml
  fetch_page:
    firecrawl_rescue: false                # opt-in; requires FIRECRAWL_API_KEY
    firecrawl_rescue_endpoint: "https://api.firecrawl.dev/v2/scrape"
    firecrawl_rescue_timeout_s: 60
    firecrawl_rescue_max_per_session: 20   # attempt cap per run
  ```
  Reuses `search.firecrawl.api_key_env` (one key for both features); skips
  silently when the key is unset.
- Inserted into the blocked branch (fetch_page.py), **before** the Wayback
  fallback:
  ```
  blocked verdict → firecrawl rescue (if enabled & quota left & not 404/pdf)
                  → wayback fallback (existing)
                  → set_blocked cache (existing)
  ```
- Calls `POST /v2/scrape` with `{"url", "formats": ["markdown"],
  "onlyMainContent": true, "proxy": "auto"}`; maps markdown straight to the
  tool output (no trafilatura — Firecrawl already extracts). One retry on 429.
- On rescue success: cached via `page_cache.aset` under kind `"firecrawl"`
  (html slot = page title) so the page is not re-paid within the cache TTL;
  the blocked verdict is **not** set. Rescued content is prefixed with a
  provenance annotation (mirrors the Wayback annotation) so the researcher
  knows it came via the Firecrawl API. On failure: fall through to Wayback.
- Quota guard is an atomic counter + lock that caps **attempts** (not
  successes) — a persistently failing rescue stops burning time/credits.
- **Single-flight per URL**: only one scrape per URL at a time; the in-flight
  marker is kept until the result is cached, so concurrent `fetch_page` calls
  for the same blocked page share one paid scrape instead of each spending a
  credit (verified race fixed).
- Rescue also fires from the **browser-blocked path** (a challenge rendered
  inside the browser_navigate fallback), matching the direct-HTTP path.
- PDFs (path suffix AND query-string signals — Firecrawl bills per page) and
  404s are never rescued.
- Tests: respx-mocked `/v2/scrape` for blocked→rescue→success (+ cache hit),
  rescue-fail→wayback, quota-exhausted→wayback, disabled-by-default,
  skip-pdf/not-found, concurrent single-flight, and browser-blocked→rescue.

## Risks / notes

- Free-tier credits (1,000, non-renewable) are only meaningful as an
  occasional fallback; sustained use needs a paid plan — the
  `max_calls_per_session` guard makes this controllable.
- Firecrawl search has no relevance score; ordering is provider-ranked, so
  `confidence_score=0.5` (same as SearXNG) keeps downstream weighting honest.
- Self-hosted Firecrawl exists but its search endpoint requires an upstream
  search provider key; we target the cloud API (endpoint is configurable
  anyway).
