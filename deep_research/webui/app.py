"""FastAPI app for the Deep Research Library web UI.

Serves the library browsing API (and, from Phase 2, the static frontend).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend
from deep_research.library.storage.base import StorageBackend
from deep_research.webui.jobs import ResearchJobManager, ResearchRunner
from deep_research.webui.routers.library import router as library_router
from deep_research.webui.routers.research import router as research_router

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


def create_app(
    config_path: str = _DEFAULT_CONFIG,
    *,
    backend: StorageBackend | None = None,
    research_runner: ResearchRunner | None = None,
) -> FastAPI:
    """Build the web UI application.

    `backend` is optional and mainly used by tests: when injected it is used
    as-is (and closed on shutdown); otherwise the backend is resolved from
    the YAML config at startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = AgentTopConfig.load_yaml(config_path)
        app.state.config = cfg
        if app.state.backend is None:
            app.state.backend = await get_backend(cfg)
        logger.info("library backend ready (%s)", type(app.state.backend).__name__)
        yield
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
    app.state.jobs = ResearchJobManager(
        config_path,
        runner=research_runner,
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    app.include_router(library_router)
    app.include_router(research_router)

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
