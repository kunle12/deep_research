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
