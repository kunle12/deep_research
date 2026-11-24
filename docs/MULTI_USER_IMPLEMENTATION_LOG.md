# Multi-User PDL — Implementation Log

Dated, append-only log for the multi-user Personal Digital Library rollout.
Design: `docs/MULTI_USER_PLAN.md`. Conventions: `docs/AGENTS.md`.

Entries are never rewritten; superseded statements get a dated
*(Superseded <date>: …)* note.

---

## 2026-11-24/25 — Phase 0: invisible foundation — DONE

Scope (all landed):

- `deep_research/accounts/` package: `AccountStore` (registry.db: users,
  sessions, email_tokens, registry_meta), Argon2id password hashing
  (`passwords.py`), email senders (`emails.py`: SMTP via stdlib in a worker
  thread + console fallback), `AuthService` (register/verify/login/logout/
  reset/session resolution with sliding expiry + throttled touch,
  rate limiting), `bootstrap.py` (owner provisioning + legacy migration).
- `deep_research/library/storage/registry.py`: `LibraryRegistry` (per-user
  lazy backends; uuid-hex `user_id` validated against path traversal) +
  `StaticLibraryRegistry` (wraps one shared backend for single_user mode and
  injected test backends).
- Postgres backend: optional `schema=` constructor arg; every pooled acquire
  sets `search_path` via a `_acquire()` helper — SQL bodies untouched.
  Legacy Postgres migration moves `public` tables into `library_<user_id>`.
- Registry schema: `library/storage/migrations/sqlite/0002_registry.sql`.
- Config: `pdl.auth` (`mode`, `owner_email`, `session_ttl_days`,
  `registration`, `smtp`) — all defaulted, `extra="forbid"`.
- Web wiring: middleware resolves user + per-user backend per request;
  `deps.get_storage`/`get_root_dir` read prepared state (still sync, so all
  existing router handlers compile unchanged).

Design deviation from plan (documented in plan §7): the legacy migration and
per-user layout activate **only in `multi_user` mode** (or explicit CLI use);
`single_user` mode keeps the legacy `<root>/index.db` layout byte-for-byte.
Reason: the existing test suites seed libraries directly at `pdl.root_dir`
and do not inject backends (discovered in `tests/test_webui_api.py`), so an
always-on migration would have moved data under every existing test.

Pre-existing test debt fixed (verified failing on clean `main` via
`git stash`): 4 stale `tests/test_postgres_backend.py` mocks missing the
`relevance_score` column (3 analysis mocks + 1 full-text-search mock tuple).

Validation:

- `tests/test_accounts_store.py` (10), `tests/test_accounts_service.py` (21),
  `tests/test_library_registry.py` (16) — all pass.
- Existing suites unmodified and green: test_webui_api, test_research_api,
  test_merge_attach_api, test_config, test_get_backend, test_library_cli,
  test_library_storage_sqlite, test_library_writer, test_microservice*,
  test_scheduler, test_postgres_backend (79 + 24 pass).
- Full `uv run pytest` run executed (see Phase 1 entry for the combined run).

## 2026-11-25 — Phase 1: accounts & login — DONE

Scope (all landed):

- `webui/routers/auth.py`: `/api/auth/{register,verify,resend-verification,
  login,logout,me,request-password-reset,reset}` — `AuthError` status mapped
  to HTTP; session cookie `pdl_session` (`HttpOnly`, `SameSite=Lax`,
  `Secure` on https).
- CSRF gate in the app middleware: authenticated state-changing requests must
  carry `X-Requested-With: XMLHttpRequest` (cross-site forms/fetches cannot
  set custom headers); `/api/auth/*` exempt (no session yet / logout).
- `single_user`: `/api/auth/me` reports the synthetic owner with
  `auth_mode="single_user"`; auth mutation endpoints return 400.
- Frontend (vanilla JS, no build step): `views/login.js`
  (signin → register → verify steps, reset flow, handles
  `#/verify?email=…&token=…` and `#/reset?…` deep links), routes + account
  chip in `app.js`, `#account` element in `index.html`, auth styles in
  `styles.css`, CSRF header + auth API functions + 401→`#/login` redirect in
  `api.js`.
- `argon2-cffi` + `email-validator` added as runtime deps.

Validation:

- `tests/test_webui_auth.py` (13) — login wall, open health endpoint, 422
  validation, register→verify→login→me roundtrip with normalized email,
  wrong-password 401, CSRF 403/204-paths, logout revocation, per-user
  isolation (A's report invisible to B, direct run access 404),
  **end-to-end legacy migration through the web app** (seeded legacy library
  + pre-provisioned owner → login → report visible → files moved under
  `users/<uid>/`), password reset over HTTP, silent unknown-user reset.
- Full suite run: see Phase 2 entry.

## 2026-11-25 — Phase 2: background scoping — DONE

Scope (all landed):

- **Jobs** (`webui/jobs.py`): `ResearchJob.user_id`; concurrency cap is now
  **per user** (`_running_by_user`); the whole pipeline is scoped by handing
  runners a config with `pdl.root_dir` swapped (`_with_library_root`) — the
  default runner, attach runner, archival check, and webhook counts all
  resolve per-user libraries from config; runners never learn about users.
  Checkpoint ownership persists via a `<run_id>.user` sidecar; `restore_paused`
  restores ownership (missing sidecar → owner account).
- **Research router**: start/list are user-scoped; get/cancel/pause/resume/
  abandon/stream enforce ownership with **404 (not 403)** so job ids don't
  leak across accounts.
- **Scheduler**: refresh cycle iterates every user library
  (`_user_library_configs`), per-user backend/writer opened and closed per
  cycle; single_user mode unchanged.
- **Library CLI**: global `--user` / `DEEP_RESEARCH_USER` (app callback +
  `ctx.obj`); multi-user resolution via
  `accounts.bootstrap.resolve_user_library_config` (defaults to owner, runs
  the idempotent migration, per-user root); new `users` sub-app:
  `add` (--password prompt), `list`, `set-status`, `set-password`
  (--password-env for non-interactive use; revokes sessions).
- **Main CLI + microservice**: in multi_user mode both archive into the
  owner's (or `--user`'s) library via the same resolver — prevents silently
  creating an unowned `root/index.db`.

Frontend fixes found during browser verification (Playwright, smoke server):

- Boot now probes `GET /api/auth/status` (new always-200 endpoint) instead of
  letting `/api/auth/me` 401 — logged-out page loads previously logged six
  console errors.
- Login wall moved into the router: unauthenticated users never render data
  views, so no library fetch fires while logged out; signed-in users skip
  auth views. `dr:auth` re-probes auth state and re-renders.
- Logout clears the account chip and header stats and drops the authed flag
  **before** navigating (race: one render cycle used to fetch with a dead
  session → three 401 console errors).
- `Cache-Control: no-cache` on all responses: the SPA has no versioned asset
  URLs, so browsers' heuristic caching kept a stale `api.js` after a server
  upgrade (module ImportError). Forces etag revalidation instead.

Validation:

- `tests/test_multiuser_jobs.py` (3): per-user cap + ownership (404),
  runner receives the user's library root (end-to-end config derivation),
  checkpoint sidecar restore.
- `tests/test_multiuser_cli.py` (5): users add/list/set-status/set-password,
  `--user` library targeting + owner default + unknown-user error,
  scheduler per-user iteration, main CLI unknown-user rejection.
- **Full suite: 992 tests collected, 100% dots, 0 failed/error, exit 0.**
- Runtime smoke (real uvicorn server + Playwright browser, scratch library
  seeded with a legacy `index.db`): owner login via UI (email
  case-insensitive), migrated legacy report visible through the UI, register
  → verify (token from server log) → isolated empty library for a second
  user, CSRF 403/202, logout, **zero console errors across the whole
  round-trip**. All smoke processes killed afterwards.

## 2026-11-25 — Adversarial review pass — DONE

Reviewer: DeepSeek V4.1 Flash 1017 (`deepseek/deepseek-flash`) as an
independent subagent, per the build-and-verify workflow. Counts reported:
**3 HIGH · 6 MEDIUM · 8 LOW**. Every finding was independently verified
against the code before fixing; all were confirmed real (none refuted).

| ID | Sev | Finding | Disposition |
|----|-----|---------|-------------|
| H1 | HIGH | `LibraryRegistry.backend_for` TOCTOU — concurrent first opens leak an aiosqlite thread / asyncpg pool per user | **fixed**: per-user `asyncio.Lock` double-checked open; regression tests (same-user + cross-user concurrency) |
| H2 | HIGH | Postgres job/CLI/microservice scoping swaps only `root_dir`; `get_backend` built schema-less backends so all research wrote into `public` | **fixed**: `pdl.storage.schema` (runtime-managed, defaulted ""), honored by `get_backend`; `_with_library_root` and `resolve_user_library_config` set root **and** schema (artifact files live under root in both backends); regression test on `_with_library_root` |
| H3 | HIGH | Scheduler filesystem enumeration: postgres users invisible (→ refresh recreated legacy tables in `public`); empty `users/` fell back to the global root | **fixed**: `_user_library_configs` async; sqlite keeps dir enumeration but returns `[]` instead of the global config; postgres enumerates the account registry (active users) with schema+root scoping; tests incl. empty-registry case |
| M1 | MED | 401 left the SPA `authed=true` → infinite `#/` ⇄ `#/login` redirect loop on session expiry | **fixed**: api.js 401 handler dispatches `dr:auth` so `refreshAuth()` re-probes; verified manually in the browser |
| M2 | MED | Paused-job SSE: server ends the stream on `paused`; EventSource replays + reconnects forever | **fixed** (server-side): paused streams stay open with keepalives so resume events arrive on the same connection (client-side close would break the resume re-attach replay) |
| M3 | MED | Register returned `status:"exists"` → user enumeration + notification-email bombing vector | **fixed**: identical `pending` body for both outcomes; UI shows the neutral "check your email" message; test updated |
| M4 | MED | Rate limiting per-email only (plan promised per-IP + per-email) | **fixed**: `_check_rate` per-email AND per-IP buckets; routers pass `request.client.host`; tests for both dimensions |
| M5 | MED | `_prune` evicted paused (resumable) jobs first (`status != "running"`, `completed_at or 0`) | **fixed**: only terminal jobs (`done/failed/cancelled`) are prune-eligible; regression test |
| M6 | MED | Email-token consumption read-then-write → double-submit could mint two sessions | **fixed**: atomic `consume_email_token` (`UPDATE … WHERE used_at IS NULL AND expires_at > ?`); verify/reset use it; concurrency regression test |
| L1 | LOW | `validate_user_id` regex `$` accepted a trailing newline into paths | **fixed**: `\Z` + `fullmatch` in registry, jobs sidecar check, scheduler regex; test |
| L2 | LOW | Corrupt `.user` sidecar un-scoped a restored job to the global root | **fixed**: sidecar content validated (uuid hex) else attributed to owner; `_run` FAILS a job whose library can't be resolved instead of running unscoped; test |
| L3 | LOW | `.user` sidecars orphaned on the success path | **fixed**: `checkpoint.discard_checkpoint` removes the sidecar too |
| L4 | LOW | SSRF guard missed non-canonical IP literals (`http://2130706433/`) | **fixed**: decimal/hex integer forms normalized through `ipaddress` before the private check; test (pre-existing bug, swept while verifying) |
| L5 | LOW | CSRF 403 short-circuit skipped the security headers | **fixed**: `_apply_security_headers` helper applied on both paths; single_user no-CSRF posture documented in code |
| L6 | LOW | Login-CSRF via blanket `/api/auth/` exemption | **fixed**: `/api/auth/login` now requires the header when a session cookie exists (SPA always sends it); test covers 401/403/200 paths |
| L7 | LOW | `aiosqlite.connect(timeout=5000)` — seconds, not ms | **fixed**: `timeout=5` (+ the same latent trap in `sqlite_backend.py`), PRAGMA already correct so behavior unchanged |
| L8 | LOW | Migration race between concurrent processes; stale claim blocks startup | **fixed**: `meta_claim`/`meta_delete` (INSERT OR IGNORE) claim key + crashed-holder recovery + release on failure; test |

Clean categories the reviewer explicitly confirmed: no `AccountStore` op-lock
deadlocks; no middleware/deps auth bypass; session sliding expiry correct;
per-user job slot accounting balanced on all paths; SPA boot race absent;
wire formats consistent end-to-end; EventSource-under-middleware OK.

Validation after fixes:

- 10 new regression tests in `tests/test_review_fixes.py`; updated the
  enumeration + scheduler tests.
- Full suite: 992 tests, **exit 0, zero failures** (re-run after all fixes).
- Follow-up hardening while re-verifying: the `pdl.storage.schema` field was
  renamed to **`pdl.storage.pg_schema`** — `schema` shadows a pydantic
  `BaseModel` method (UserWarning + footgun).
- Browser re-verification (Playwright, scratch server): login wall with zero
  console errors; UI login; then a server-side session revocation
  (`users set-password`, which revokes sessions) followed by an in-page API
  call → exactly one 401 cycle, stable `#/login`, chip + stats cleared, **no
  redirect loop** (M1 verified end-to-end). All smoke processes killed.
