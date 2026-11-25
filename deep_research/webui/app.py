"""FastAPI app for the Deep Research Library web UI.

Serves the library browsing API and the static frontend. Since the multi-user
rollout (docs/MULTI_USER_PLAN.md) the request middleware also resolves the
caller's account and their personal library: in ``multi_user`` mode via a
session cookie against the account registry, in ``single_user`` mode by
mapping every request to the shared backend (legacy behavior, unchanged).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from deep_research.accounts.bootstrap import migrate_legacy_library
from deep_research.accounts.emails import build_email_sender
from deep_research.accounts.rows import UserRow
from deep_research.accounts.service import SESSION_COOKIE, AuthError, AuthService
from deep_research.accounts.store import AccountStore
from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend
from deep_research.library.storage.base import StorageBackend
from deep_research.library.storage.registry import LibraryRegistry, StaticLibraryRegistry
from deep_research.webui.jobs import AttachRunner, ResearchJobManager, ResearchRunner
from deep_research.webui.routers.auth import router as auth_router
from deep_research.webui.routers.library import router as library_router
from deep_research.webui.routers.research import router as research_router
from deep_research.webui.routers.shares import router as shares_router

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = "config.yaml"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Methods that can change state; with an authenticated session they must carry
# the SPA's X-Requested-With header (cross-site forms/fetches cannot set it).
_CSRF_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# /api/auth/* is exempt (no session yet / revocation) — EXCEPT /api/auth/login:
# requiring the header there blocks login-CSRF (a third party silently signing
# the victim into an attacker-controlled account).
_CSRF_EXEMPT_PREFIX = "/api/auth/"
_CSRF_LOGIN_PATH = "/api/auth/login"

# Local credential endpoints. When SSO is `required`, a request that did not
# arrive through the trusted proxy cannot use any of these — the app port is
# not a second front door around the console.
_SSO_LOCAL_CREDENTIAL_PATHS = frozenset(
    {
        "/api/auth/register",
        "/api/auth/verify",
        "/api/auth/resend-verification",
        "/api/auth/login",
        "/api/auth/request-password-reset",
        "/api/auth/reset",
    }
)


def _apply_security_headers(response) -> None:
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # The SPA has no build step or versioned asset URLs: force etag
    # revalidation so browser-cached modules from a previous deployment can
    # never shadow a newer one (stale-module ImportError).
    response.headers.setdefault("Cache-Control", "no-cache")


def _synthetic_owner(cfg: AgentTopConfig) -> UserRow:
    """Owner identity for single_user mode (no registry DB involved)."""
    return UserRow(
        user_id="owner",
        email=cfg.pdl.auth.owner_email or "owner@localhost",
        display_name="Owner",
        status="active",
    )


def create_app(
    config_path: str = _DEFAULT_CONFIG,
    *,
    backend: StorageBackend | None = None,
    research_runner: ResearchRunner | None = None,
    attach_runner: AttachRunner | None = None,
    checkpoint_dir: Path | None = None,
) -> FastAPI:
    """Build the web UI application.

    `backend` is optional and mainly used by tests: when injected it is used
    as-is (and closed on shutdown); otherwise the backend is resolved from
    the YAML config at startup. In multi_user mode the backend is *not* used —
    per-user libraries come from the LibraryRegistry instead.

    `checkpoint_dir` lets tests isolate pause/resume state to a temp dir;
    production uses the engine's default checkpoint location.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg: AgentTopConfig = AgentTopConfig.load_yaml(config_path)
        app.state.config = cfg
        app.state.auth_mode = cfg.pdl.auth.mode
        app.state.auth = None
        app.state.accounts_store = None
        store: AccountStore | None = None
        registry: LibraryRegistry | StaticLibraryRegistry
        try:
            if cfg.pdl.auth.mode == "multi_user":
                if not cfg.pdl.auth.owner_email:
                    raise RuntimeError(
                        "pdl.auth.owner_email must be set when pdl.auth.mode is multi_user"
                    )
                store = AccountStore(Path(cfg.pdl.root_dir) / "registry.db")
                await store.connect()
                app.state.accounts_store = store
                app.state.auth = AuthService(
                    store, build_email_sender(cfg.pdl.auth.smtp), cfg.pdl.auth
                )
                app.state.owner = await app.state.auth.provision_owner(cfg.pdl.auth.owner_email)
                # One-time: move a legacy single-user library into the owner's
                # per-user directory (idempotent, marker-guarded).
                await migrate_legacy_library(cfg, store, app.state.owner)
                app.state.backend = None
                registry = LibraryRegistry(cfg)
            else:
                if app.state.backend is None:
                    app.state.backend = await get_backend(cfg)
                app.state.owner = _synthetic_owner(cfg)
                registry = StaticLibraryRegistry(app.state.backend, cfg.pdl.root_dir)
            app.state.registry = registry
            logger.info(
                "library ready (mode=%s, backend=%s)",
                cfg.pdl.auth.mode,
                type(registry).__name__,
            )
            # A previous run's checkpoint files survive on disk; surface them as
            # resumable paused jobs so nothing is lost after a restart.
            restored = app.state.jobs.restore_paused(
                owner_user_id=app.state.owner.user_id if app.state.owner else None
            )
            if restored:
                logger.info("restored %d paused research job(s) after restart", len(restored))
            yield
        finally:
            reg = getattr(app.state, "registry", None)
            if reg is not None:
                await reg.close_all()
                app.state.registry = None
            if store is not None:
                await store.close()
                app.state.accounts_store = None
            if app.state.backend is not None:
                await app.state.backend.close()
                app.state.backend = None

    app = FastAPI(
        title="Deep Research Library Web UI",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.backend = backend
    app.state.config_path = config_path
    app.state.config = None

    def _library_for_user(user_id: str | None) -> tuple[str, str | None] | None:
        """(library_root, postgres_schema) for *user_id*; None = unresolvable."""
        registry = getattr(app.state, "registry", None)
        if registry is None or not user_id:
            return None
        try:
            return str(registry.root_for(user_id)), registry.schema_for(user_id)
        except ValueError:
            return None

    app.state.jobs = ResearchJobManager(
        config_path,
        runner=research_runner,
        attach_runner=attach_runner,
        checkpoint_dir=checkpoint_dir,
        library_for_user=_library_for_user,
    )

    @app.middleware("http")
    async def security_and_auth(request, call_next):
        # -- Authentication & per-user library resolution (before handlers) --
        state = request.app.state
        auth: AuthService | None = getattr(state, "auth", None)
        request.state.sso = False
        if auth is not None:
            # multi_user: a trusted reverse proxy may assert the identity with
            # a header (the console's guard). The header only counts from a
            # configured proxy address; nginx overwrites any client-supplied
            # value, so a browser cannot forge it. Otherwise fall back to the
            # local session cookie unless SSO is required.
            cfg = getattr(state, "config", None)
            sso = cfg.pdl.auth.sso if cfg is not None and getattr(cfg, "pdl", None) else None
            path = request.url.path
            sso_user: UserRow | None = None
            if sso is not None and sso.enabled:
                asserted = request.headers.get(sso.header)
                client_host = request.client.host if request.client else ""
                if asserted and client_host in sso.trusted_proxies:
                    try:
                        sso_user = await auth.provision_sso_identity(asserted)
                    except AuthError as exc:
                        response = JSONResponse({"detail": str(exc)}, status_code=exc.status)
                        _apply_security_headers(response)
                        return response
                    request.state.sso = True
                if sso.required and sso_user is None and path in _SSO_LOCAL_CREDENTIAL_PATHS:
                    response = JSONResponse({"detail": "single sign-on required"}, status_code=403)
                    _apply_security_headers(response)
                    return response
            if sso_user is not None:
                user = sso_user
            elif sso is not None and sso.enabled and sso.required:
                # SSO is the only credential: a stale/local cookie must not
                # become a second front door around the console.
                user = None
            else:
                user = await auth.resolve_session(request.cookies.get(SESSION_COOKIE))
            csrf_exempt = path.startswith(_CSRF_EXEMPT_PREFIX) and path != _CSRF_LOGIN_PATH
            if (
                user is not None
                and request.method in _CSRF_METHODS
                and not csrf_exempt
                and request.headers.get("x-requested-with") != "XMLHttpRequest"
            ):
                # Cross-site forms/fetches cannot set this custom header.
                response = JSONResponse({"detail": "CSRF check failed"}, status_code=403)
                _apply_security_headers(response)
                return response
        else:
            # single_user: every request belongs to the owner (legacy behavior;
            # no credentials are in play, so CSRF protection is not required).
            user = getattr(state, "owner", None)
        request.state.user = user
        registry = getattr(state, "registry", None)
        if user is not None and registry is not None:
            request.state.storage = await registry.backend_for(user.user_id)
            request.state.library_root = str(registry.root_for(user.user_id))

        response = await call_next(request)
        _apply_security_headers(response)
        return response

    app.include_router(auth_router)
    app.include_router(library_router)
    app.include_router(research_router)
    app.include_router(shares_router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("deep_research.webui.app:app", host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
