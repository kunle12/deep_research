# Secondary LLM Routing — Implementation Log

Append-only log. Update after each step so work can resume after interruption.
Plan: `docs/SECONDARY_LLM_PLAN.md`.

## Steps

- [x] 1. Config schema: `SecondaryLLMConfig` + `LLMRole` + `LLMConfig.secondary` + env overrides (`config.py`)
- [x] 2. Router: `Role`, `ResolvedLLM`, `LLMRouter`, `FallbackClient` (`deep_research/llm/router.py`) + export in `llm/__init__.py`
- [x] 3. `agent.py`: open router, thread through routing/dispatch, classifier/glossary/auto-tag resolution
- [x] 4. `paths/deep.py`: planner/researcher/critic/writer resolution (+ researcher `max_context_tokens`)
- [x] 5. `paths/quick.py`, `paths/applied.py`, `paths/classifier.py`: WRITER/ANALYSIS resolution; node annotations → `LLMClientLike`
- [x] 6. `paths/academic.py`: relevance scoring + digest + vision-selection site
- [x] 7. `paths/url_source.py`: vision-selection site (+ follow-up handoff passes router)
- [x] 8. `nodes/paper_analysis.py`: vision-selection site (analyze_paper_deep + run_paper_analysis_pass now take `router`)
- [x] 9. `library/attach.py` vision-selection site (takes `router`); `library/merge.py` (takes `router`, POST role), `library/cli.py`, `webui/jobs.py`, `webui/routers/library.py` entry points
- [x] 10. Docs: `config.example.yaml` (secondary block), README (secondary LLM section + env vars)
- [x] 11. Tests: `tests/test_llm_router.py` (new), `test_config.py` secondary tests; updated quick/academic/url_source/paper_analysis/merge/attach tests to wrap clients in `_fake_router`
- [x] 12. Verify: `uv run pytest` (all pass), `uv run ruff check` (clean), `uv run mypy deep_research/` (clean)

## Notes

- Node signatures stay `(client, model)`; resolution happens at callers.
- Images must NEVER route to secondary (enforced in `LLMRouter.resolve`).
- Secondary clients are wrapped in `FallbackClient` (transparent secondary→primary retry with model swap + warn-once).
- `vision_api_key` in `LLMConfig` is defined but unused today — leave as-is.
- Tests that call `quick_search`/`academic_research`/`url_source`/`run_paper_analysis_pass`/`merge_reports`/`attach_source` now pass a `_fake_router(client)` helper.

## Session log

- 2026-08-13: plan approved; log created. Steps 1-4, 8 done (config, router, agent, deep path, paper_analysis). Next: quick/applied/classifier paths.
- 2026-08-13: steps 5-9, 11, 12 done (all paths, library/webui entry points, tests updated + new router tests, full verification green). Remaining: step 10 docs (config.example.yaml + README).
- 2026-08-13: step 10 done (config.example.yaml secondary block + README section/env vars). ALL STEPS COMPLETE — 819 tests pass, ruff clean, mypy clean.
- 2026-08-14: hardening review fixes (all green — 822 tests, ruff, mypy):
  - **Circuit breaker** (`llm/router.py`): shared per-run `_SecondaryBreaker` across all `FallbackClient`s — a dead/slow secondary is attempted ONCE, then all later calls go straight to primary. Secondary client now uses `max_retries=0` so a hung endpoint burns one `timeout_s`, not ~3x. 400-class request errors (e.g. context overflow) retry on primary WITHOUT opening the circuit, so an undersized model stays in play.
  - **Warn-once is now per-run, not per-FallbackClient** (parallel researchers no longer each log a warning).
  - **config.example.yaml** ships `secondary.enabled: false` so a copy-pasted config stays single-endpoint.
  - **Env-only enable** (`config.py`): setting `DEEP_RESEARCH_LLM_SECONDARY_*` now enables the secondary even with no YAML block (previously silently a no-op).
  - **Researcher never offered image-producing tools** (`nodes/researcher.py`): `pdf_render_pages` / `browser_take_screenshot` excluded from the researcher tool schema so a base64 image blob can't flow into a text-only secondary's history.
  - **Text-only synthesis routing** (`nodes/analyze_paper.py` + callers): the final no-image synthesis of a vision-analyzed paper now routes to the text route (`synthesis_*` args), so the secondary is used even when pages were rendered.
