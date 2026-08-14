# Secondary LLM Routing — Design Plan

Status: approved (see `docs/SECONDARY_LLM_IMPLEMENTATION_LOG.md` for progress)

## Goal

By default one vLLM endpoint serves everything (current behavior). Optionally,
a **secondary LLM** — possibly without vision capability — can be defined for
text-heavy reasoning tasks: text analysis, coming up with research search
terms, and making better judgement on answering analysis questions.

Vision tasks (reading rendered PDF page images, HTML screenshots) always stay
on the primary, vision-capable model.

## Current state (as of planning)

- `LLMConfig` (`deep_research/config.py`) — one `base_url`/`api_key` with
  `text_model` + `vision_model` names on that single endpoint.
- One `AsyncOpenAI` client is opened in `agent.py` (`LLMClient(config.llm)`)
  and threaded through paths/nodes as `client`; each call site passes a model
  string (`config.llm.text_model` or `config.llm.vision_model`).
- Vision/text selection already happens at 4 call sites via
  `vision_model if images else text_model`:
  - `nodes/paper_analysis.py` (`analyze_paper_deep`)
  - `paths/academic.py`
  - `paths/url_source.py`
  - `library/attach.py`
- Images never flow through the researcher tool loop (tool results are text
  JSON), so the researcher is safe to run on a text-only model.

## Design

### 1. Config schema (`deep_research/config.py`)

Add optional secondary block under `llm:`:

```yaml
llm:
  base_url: "http://localhost:8000/v1"   # primary vLLM (vision-capable)
  api_key: "..."
  text_model: "..."
  vision_model: "..."
  secondary:                             # optional; omit = current behavior
    base_url: "http://localhost:8001/v1"
    api_key: "..."
    model: "qwen3-235b"
    max_context_tokens: 131072
    timeout_s: 240
    roles: null                          # null = all text roles; or subset
```

- Env overrides: `DEEP_RESEARCH_LLM_SECONDARY_BASE_URL`,
  `DEEP_RESEARCH_LLM_SECONDARY_API_KEY`, `DEEP_RESEARCH_LLM_SECONDARY_MODEL`.
- Validation: `roles` must be a subset of the known role names.

### 2. Routing layer (`deep_research/llm/router.py`, new)

- `Role` enum: `classifier`, `planner`, `researcher`, `critic`, `analysis`,
  `writer`, `post`.
  - `planner` — sub-question decomposition / search-term generation
  - `researcher` — tool loop (query generation + judgement during research)
  - `analysis` — text-only source/paper analysis, quick/applied synthesis,
    relevance scoring
  - `post` — glossary, auto-tag, library merge, titles
- `LLMRouter` async context manager: opens the primary `AsyncOpenAI` always,
  secondary only if configured.
- `resolve(role, has_images=False) -> ResolvedLLM(client, model,
  max_context_tokens)`:
  - `has_images=True` → primary `vision_model`, unconditionally (hard rule —
    secondary may lack vision).
  - `roles: null` → all text roles route to secondary; an explicit list
    narrows routing.
  - No secondary configured → everything resolves to primary (identical to
    current behavior).
- Runtime failure handling: if a call on the secondary endpoint errors, retry
  once on the primary with a warning. Implemented via a router-provided helper
  (e.g. `router.create(role, has_images=..., **chat_kwargs)`) or a small
  wrapper used at call sites.
- `LLMClient`/`open_llm` stay for backward compat.

### 3. Wiring (node signatures stay `(client, model)`)

- `agent.py` — open `LLMRouter` instead of `LLMClient`; thread router through
  `_route_and_dispatch` → paths.
- `paths/deep.py` — planner → `PLANNER`, researcher fan-out → `RESEARCHER`
  (use resolved `max_context_tokens` for the tool loop), critic → `CRITIC`,
  writer → `WRITER`.
- `paths/quick.py`, `paths/applied.py`, `paths/academic.py` synthesis /
  relevance scoring, classifier call in `agent.py`, `nodes/glossarize.py`,
  `nodes/auto_tag.py` → `ANALYSIS` / `CLASSIFIER` / `POST`.
- Vision-selection sites switch to
  `resolve(ANALYSIS, has_images=bool(images))`:
  - `nodes/paper_analysis.py`
  - `paths/academic.py`
  - `paths/url_source.py`
  - `library/attach.py`
- Library/webui entry points switch `open_llm`/`LLMClient` → router:
  - `webui/jobs.py`
  - `webui/routers/library.py`
  - `library/cli.py`
  - `library/merge.py`

### 4. Docs + tests

- `config.example.yaml` + README section documenting `llm.secondary`.
- Router unit tests: routing table, images-never-to-secondary, fallback path,
  `secondary=None` parity with current behavior.
- Update existing config/client tests.

## Decisions (confirmed with user)

1. Default routing when secondary configured: **all text roles** to secondary.
2. Researcher tool loop runs on the **secondary** model.
3. Secondary runtime error → **fallback to primary** (retry once + warning).

## Backward compatibility

No `secondary` block → every `resolve()` returns the primary client; behavior
is identical to today.
