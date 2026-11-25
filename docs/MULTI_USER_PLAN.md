# Multi-User Personal Digital Library — Plan

Proposal: let the Personal Digital Library (PDL) serve **multiple users, each with
their own library**, where every account is tied to a **valid (verified) email
address** — without destabilizing the existing single-user experience.

## 1. Goals & non-goals

**Goals**

- Many users on one deployment; each sees only their own reports, artifacts,
  tags, analyses, glossary, and citation graph.
- Accounts are identified by an email address that is syntactically valid and
  verified by a confirmation email.
- Password login is the primary method now; the auth design is **SSO-ready**
  so a custom SSO (unified login) can be added later as an additive change
  (§3, Phase 4).
- Existing single-user deployments keep working with zero behavior change by
  default; existing `.deep_research_library/` data is migrated automatically.
- Minimal blast radius: no changes to the `StorageBackend` protocol, either
  backend implementation, `LibraryWriter`, or the library/attach/merge modules.

**Non-goals (for this phase)**

- Sharing/collaboration between libraries, cross-user search, admin UI.
- Custom SSO / OIDC federation (planned as Phase 4, not this phase; see §3).
- Cross-user blob dedup (deliberately traded away; see §10).

## 2. Core insight: there is already a single "which library?" seam

Today the running app resolves the library exactly once:

- `webui/app.py` — `app.state.backend = await get_backend(cfg)` at startup,
  closed at shutdown.
- `webui/deps.py::get_storage(request)` — hands that one backend to every
  handler.
- `LibraryWriter(storage, root_dir, refresh_policy)` — a writer is just
  *backend + directory*, constructed ad hoc by runners and the CLI.

So instead of threading a `user_id` through 43 protocol methods × 2 backends
(86 query sites to get right forever), we change **only the resolution seam**:

> One `StorageBackend` instance still represents "one personal library".
> Multi-user means resolving it **per user** instead of per process.

Isolation becomes physical — a bug cannot leak user A's data into user B's
query because there is no query that can even see both. Per-user SQLite locks
also stop being global contention points (today every library operation
serializes on one `asyncio.Lock`).

## 3. Account model

New global store (never mixed into a per-user library DB):

```sql
-- registry.db (SQLite) or public schema (Postgres)
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,          -- uuid4 hex; the ONLY on-disk identifier
    email         TEXT NOT NULL UNIQUE,      -- normalized (lowercased, trimmed)
    display_name  TEXT,
    password_hash TEXT,                      -- Argon2id (argon2-cffi); NULL until set
    status        TEXT NOT NULL CHECK (status IN ('pending','active','disabled')),
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,            -- SHA-256 of the cookie token
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    auth_method TEXT NOT NULL DEFAULT 'password',  -- audit only; see §3 SSO
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,             -- SHA-256 of the emailed token
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('verify','reset')),
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
```

Schema files follow the existing pattern:
`library/storage/migrations/{sqlite,postgres}/0002_registry.sql`, applied by a
small `AccountStore.ensure_schema()` (same idempotent style as the library).

### Email = identity, "valid" means verified

1. **Syntax**: `pydantic.EmailStr` (`email-validator`) at every API boundary.
2. **Normalization**: lowercase + trim before any comparison or storage; the
   `UNIQUE` constraint is on the normalized form.
3. **Verification**: registration creates the account with `status='pending'`
   and emails a single-use token (32 random bytes, stored hashed, 24 h TTL).
   Confirming flips the account to `active`. `pending` accounts cannot log in.
4. **Login**: email + password (Argon2id). Password reset reuses the same
   emailed-token mechanism (`purpose='reset'`).

Pragmatics for a self-hosted tool: SMTP is only required to *verify and
recover*, not on every login (unlike magic-link-only auth, where a broken SMTP
server locks everyone out). If SMTP is not configured in `multi_user` mode, the
built-in `ConsoleEmailSender` prints the verification/reset link to the server
log so a single admin user can still bootstrap.

### SSO-ready identity model (future custom SSO / unified login)

> **Implemented 25 Nov 2026 (trusted-header variant):** the console's guard
> asserts `X-Auth-User` through nginx; `pdl.auth.sso` enables the trusted-proxy
> path, JIT-provisions by normalized email, and can make SSO the only
> credential (`required: true`). See README "Trusted-header SSO" and
> `tests/test_webui_sso.py`. The `auth_identities` migration below was not
> needed for this variant — the proxy asserts a verified email, and user_id
> remains the storage anchor.

Password login is deliberately structured as "just the first credential
source", so the later custom SSO integration is additive:

- **`user_id` is the stable identity anchor** — never the email, never the
  password. SSO subjects, directory IDs, and email re-assignments never touch
  the storage layout (§4 paths use `user_id` only).
- **`password_hash` is nullable** — SSO-provisioned users can exist without a
  local password, and one account can hold both (SSO login + local fallback).
- **Session minting is credential-agnostic**: `AuthService` separates
  "verify credentials" from `_mint_session(user)`; sessions record `auth_method`
  for audit only. The web layer (`require_user`) never knows or cares how a
  session was created — this is what makes SSO a drop-in.
- **The future migration is additive-only**: a `0003_sso.sql` adds

  ```sql
  CREATE TABLE IF NOT EXISTS auth_identities (
      provider      TEXT NOT NULL,           -- 'local', 'sso:<name>', …
      subject       TEXT NOT NULL,           -- stable id at the provider
      user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
      email_claim   TEXT,
      created_at    TEXT NOT NULL,
      last_login_at TEXT,
      PRIMARY KEY (provider, subject)
  );
  ```

  plus one protocol in `accounts/`:

  ```python
  class IdentityProvider(Protocol):
      async def authenticate(self, request: Request) -> AuthenticatedIdentity: ...
  ```

  Password auth becomes the first implementation (`LocalPasswordProvider`);
  the custom SSO becomes another — sessions, storage, and the web layer are
  untouched.
- **Provisioning & linking rules** (decided at SSO time, sketched now): the SSO
  must assert a *verified* email claim; JIT-provision on first login, and
  auto-link to an existing account only when the normalized email matches an
  `active` account; any mismatch falls back to admin CLI resolution rather
  than silent account takeover.
- Config gains a `pdl.auth.sso:` section only when SSO ships —
  `extra="forbid"` keeps premature knobs out of `config.yaml`.

## 4. Library isolation: one directory per user

```
<pdl.root_dir>/
├── registry.db              # accounts, sessions, tokens (global)
└── users/
    └── <user_id>/           # one personal library per user —
        ├── index.db         #   existing schema, byte-for-byte unchanged
        ├── artifacts/
        └── reports/
```

- `user_id` (uuid hex) is the only path component — never the email (emails
  change, can contain odd characters, and would leak identity into paths).
- Each user's `index.db` keeps today's schema exactly; FTS5 search, glossary,
  tags, dedup all behave per-user automatically.
- **Postgres**: one database, **schema per user** (`library_<user_id>`);
  `PostgresStorageBackend` gains an optional `schema=` constructor argument
  (constructor, not protocol change) and qualifies tables accordingly. Registry
  tables live in `public`.
- Because `artifact_id` is a content hash, identical artifacts share IDs across
  libraries — harmless now, and it keeps the door open to a shared blob store
  later (§10).

New `LibraryRegistry` (`library/storage/registry.py`):

```python
class LibraryRegistry:
    """user_id -> lazily opened, cached StorageBackend (+ root_dir / writer)."""
    def backend_for(self, user_id: str) -> StorageBackend: ...
    def writer_for(self, user_id: str) -> LibraryWriter: ...
    async def close_all(self) -> None: ...
```

Opened backends are cached per user and closed at app shutdown. In
`single_user` mode there is exactly one user and the registry degenerates to
today's behavior.

## 5. Auth in the web app

New package:

```
deep_research/accounts/
├── rows.py      # UserRow, SessionRow, EmailTokenRow (dataclass style of rows.py)
├── store.py     # AccountStore: create/find/verify users, sessions, tokens
├── service.py   # AuthService: register / verify / login / logout / reset
└── emails.py    # EmailSender protocol: SmtpEmailSender, ConsoleEmailSender
deep_research/webui/
├── auth.py          # deps: current_user(), require_user() — cookie session
└── routers/auth.py  # /api/auth/* routes
```

API surface:

| Route | Purpose |
|---|---|
| `POST /api/auth/register` | create pending account, send verification email |
| `POST /api/auth/verify` | confirm token → activate + sign in |
| `POST /api/auth/resend-verification` | re-send (rate-limited) |
| `POST /api/auth/login` | email + password → session cookie |
| `POST /api/auth/logout` | revoke session |
| `GET /api/auth/me` | current profile |
| `POST /api/auth/request-password-reset` / `POST /api/auth/reset` | recovery |

**The critical change is invisible to routers.** `get_storage` keeps its name
and return type; internally it becomes:

```python
def get_storage(request: Request) -> StorageBackend:
    user = require_user(request)            # 401 unless valid session
    return request.app.state.registry.backend_for(user.user_id)
```

Every handler in `routers/library.py` compiles unchanged; it simply receives
the caller's own library. Same for `get_root_dir` (per-user root).

**Sessions**: opaque 32-byte token in an `HttpOnly; SameSite=Lax` cookie
(`Secure` when behind TLS), only the SHA-256 stored server-side, 30-day sliding
expiry, revoked on logout. How a session was minted is irrelevant to the rest
of the app (recorded only as `sessions.auth_method`) — that is what makes the
future SSO integration a drop-in (§3). CSRF: `SameSite=Lax` plus a middleware check that
state-changing requests carry the JSON content type / `X-Requested-With` header
the SPA already sends (login/register exempt). This slots next to the existing
CSP middleware in `create_app`.

**Auth modes** (`pdl.auth.mode`):

- `single_user` *(default)* — no login wall; `current_user` returns an
  auto-provisioned owner account. Existing deployments and **every existing
  test** behave exactly as today.
- `multi_user` — login wall; the SPA gains a `#/login` view (`js/views/login.js`)
  and `api.js` redirects to it on 401; topbar shows email + logout.

Housekeeping per `docs/AGENTS.md`: when the auth router lands, regenerate the
committed OpenAPI snapshots via `app.openapi()`
(`deep_research/api.openapi.json`) and extend `docs/REST_API.md` with the
`/api/auth/*` endpoints. `create_app`'s injectable `backend`/`research_runner`
test seams stay; the registry is injectable the same way for auth tests.

## 6. Research jobs, scheduler, CLI

- **Jobs** (`webui/jobs.py`): `ResearchJob` records `user_id`; capacity stays
  `max_concurrent=1` **per user** (configurable); SSE/job endpoints enforce
  ownership (404, not 403, for other users' jobs). `restore_paused()` maps each
  paused job back to its owner's registry entry.
- **Runners never learn about users.** The default runner builds its
  `LibraryWriter` from `config.pdl.root_dir`; before invoking, the job manager
  hands it a per-user config derived via `cfg.model_copy(deep=True)` with
  `pdl.root_dir` pointed at `users/<user_id>/`. Attach runner: same.
- **Scheduler**: refresh iterates `registry` users, refreshing each library
  with its own backend/writer.
- **CLI**: `deep-research-library` gains `--user EMAIL` (or
  `DEEP_RESEARCH_USER`) to pick the target library, plus admin subcommands
  (`users add/list/set-status`) that operate on `registry.db` directly — the
  out-of-band bootstrap for the first user or SMTP-less recovery.

## 7. Migration of existing data

One-time, idempotent, run at startup **in `multi_user` mode** when
`pdl.root_dir/index.db` exists and no registry is initialized. *(Implemented
deviation, 2026-11-25: the migration deliberately does NOT run in
`single_user` mode — existing deployments and test suites keep the legacy
layout byte-for-byte until multi-user is actually enabled, which makes the
"existing tests pass unmodified" acceptance bar in §12 achievable. The
migration can also be exercised explicitly via the CLI.)*:

1. Create `registry.db` and its schema.
2. Provision the owner account: `pdl.auth.owner_email` (required in config for
   the migration; `active`, no password until first login/reset).
3. `rename()` the existing library into `users/<owner_id>/` (same volume →
   atomic). For Postgres: `ALTER SCHEMA public RENAME TO library_<owner_id>`.
4. Record `schema_meta.migrated_from = 'single-user-v1'` in the library and a
   completion marker in the registry; never re-run.

Rollback = move the directory back.

## 8. Configuration

```yaml
pdl:
  auth:
    mode: single_user        # single_user (default) | multi_user
    owner_email: ""          # owner identity for single_user + migration
    session_ttl_days: 30
    registration: open       # open | closed (closed = admin invites via CLI)
    smtp:
      host: ""
      port: 587
      use_starttls: true
      username_env: DEEP_RESEARCH_SMTP_USER
      password_env: DEEP_RESEARCH_SMTP_PASSWORD
      from_address: ""
```

Pydantic schemas in `config.py` follow the house style (`extra="forbid"`,
everything defaulted so `config.example.yaml` stays valid unchanged).
Secrets come from env, matching the existing `*_env` pattern
(`DEEP_RESEARCH_PG_DSN`, `TAVILY_API_KEY`, …). New runtime dep:
`argon2-cffi` (+ `aiosmtplib` for async SMTP).

## 9. Security notes

- Passwords: Argon2id with per-password salt; never logged. Login, register,
  verify, and reset are rate-limited per IP + per email (in-memory limiter).
- Tokens: verification/reset/session tokens are 32 random bytes, stored only as
  SHA-256 hashes, single-use, expiring.
- User enumeration: `register` / `request-password-reset` always respond 202;
  duplicate-email registrations send a "someone tried to register" email.
- Sessions: `HttpOnly` cookie, hashed at rest, revocable; generic 401s.
- Path safety: only uuid-hex `user_id`s touch the filesystem; no email-derived
  paths. Existing path-traversal and SSRF guards are unaffected.
- Email validation depth: syntax via `email-validator`; DNS-deliverability
  checking is optional (off by default so offline/air-gapped hosts still work).

## 10. Conscious trade-offs

- **No cross-user blob dedup**: two users archiving the same paper store two
  copies. Acceptable — dedup was a within-library feature. Because blobs are
  content-addressed, a shared content-addressed store can be added later by
  repointing `bytes_path` resolution, with zero schema change.
- **N SQLite connections instead of 1**: opened lazily and cached; per-user
  write locks reduce contention versus today's single global lock.
- **No cross-user search**: by definition — these are personal libraries.

## 11. Alternatives considered

- **Shared schema + `user_id` column everywhere** (classic multi-tenant):
  touches all 43 protocol methods in both backends, every FTS/glossary query,
  and the content-hash dedup logic; isolation then relies on remembering
  `WHERE user_id = ?` in ~86 query sites, and SQLite has no RLS to backstop it.
  Highest fidelity for a SaaS, highest risk here — rejected.
- **One process per user**: operationally heavy, fragments the web UI — rejected.
- **Custom SSO (unified login)**: planned, not rejected — see §3 and Phase 4.
  Password login remains the primary method; SSO slots in as an additional
  `IdentityProvider`. What was rejected is identity-*only*-via-OIDC as the
  initial design (external dependency, uneven email-verification guarantees,
  wrong fit for a self-hosted tool).

## 12. Testing plan

- `tests/test_accounts.py` — registration, normalization, verification (happy,
  expired, reused token), login, lockout, reset; session minting and resolution
  are tested independently of credential verification (with `auth_method`
  recorded), so a future SSO `IdentityProvider` can be added against a fake
  provider in tests.
- `tests/test_library_registry.py` — lazy per-user open/close, isolation
  (user A writes, user B cannot see it), legacy-directory migration.
- `tests/test_webui_auth.py` — 401 without session; full register → verify →
  login → list-reports flow with an injected fake `EmailSender` (same pattern
  as `FakeRunner`); ownership enforcement on jobs. Use
  `with TestClient(create_app(...))` (context manager required) and an injected
  registry, per the AGENTS.md test conventions.
- **Existing suites pass unmodified** — `single_user` is the default, and the
  storage layer is untouched; that is the acceptance bar for §2.

## 13. Phased rollout

1. **Phase 0 — invisible foundation**: `AccountStore` + registry migrations +
   `LibraryRegistry` + legacy migration + per-user layout. `single_user` mode
   only; zero UX change; all existing tests green.
2. **Phase 1 — accounts & login**: `accounts/` package, `/api/auth/*`,
   session deps, login UI, `multi_user` mode, user-scoped `get_storage`.
3. **Phase 2 — background scoping**: per-user jobs, scheduler iteration,
   CLI `--user` + admin subcommands.
4. **Phase 3 — optional extras**: magic-link login, invite/admin UI, shared
   blob CAS.
5. **Phase 4 — custom SSO (unified login)**: additive `0003_sso.sql` +
   `auth_identities` table; `IdentityProvider` implementations (local password
   first, then the custom SSO); JIT provisioning and verified-email-claim
   linking per §3; optional "SSO required" mode that disables password login.
   *(Note, 2026-11-25: `0003_sharing.sql` now occupies the next migration
   slot — the SSO migration becomes `0004_sso.sql`.)*

## 14. Sharing (view-only) — implemented addendum, 2026-11-25

Decisions (user-confirmed): **links + named accounts**, **individual reports
only**, **view-only** (no copy/import).

- One authorization core (`accounts/shares.py::ShareService`): a share row in
  the global registry (`0003_sharing.sql`: `shares` + `share_grants`) is live
  when not revoked and not expired; access = hold the secret link token
  (`GET /api/share/{token}[/asset]`, no session needed — the token IS the
  capability) **or** hold a named grant (`GET /api/shared/{share_id}[/asset]`,
  session + grant row). The raw link token is shown once at creation and can
  be rotated; listings never return it (SHA-256 at rest, like sessions).
- Shared reads open the OWNER's library through `LibraryRegistry` on the
  server — grantees never write into another library, so sharing is read-only
  by construction and the per-user isolation model (§2/§4) is untouched.
- Owner management: `POST/GET /api/shares`, `DELETE /api/shares/{id}`
  (revoke), `POST /api/shares/{id}/rotate`, grant add/remove. Creation
  validates the run in the CALLER's library and resolves grantee emails
  BEFORE inserting the row (a failed request leaves no orphan share).
- `GET /api/shared` ("Shared with me") is the grantee's listing;
  `single_user` mode disables management routes (400) and `GET /api/shared`
  returns `[]`.
- Frontend: report-page **Share** dialog (create link / add people / revoke /
  rotate), a **Shared with me** view (`#/shared`), and read-only report
  rendering for both access paths (`#/shared/<id>`, `#/share/<token>`) with
  all mutation affordances hidden.
- Post-review hardening (2026-11-25, see implementation log): report deletion
  cascades share removal; disabled owners' shares die everywhere; malformed
  grantee emails map to 422; rotate-vs-revoke race returns 404; self-grants
  filtered; grant rows carry `user_id` for per-grantee removal from the
  dialog; citation `local_*` links stripped from shared payloads.
