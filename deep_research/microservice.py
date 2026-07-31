"""FastAPI microservice — wraps run_research as an HTTP endpoint.

P12(e): implemented. Provides a single POST /research endpoint that accepts
a query and optional config path, runs the agent, and returns the Report as JSON.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from deep_research import run_research
from deep_research.config import AgentTopConfig

logger = logging.getLogger(__name__)

app = FastAPI(title="Deep Research Agent", version="0.1.0")

_ALLOWED_CONFIG_DIR = Path.cwd()

# Hard timeout for a single research request. Deep research can take many
# minutes; this prevents the HTTP server from hanging indefinitely.
_REQUEST_TIMEOUT_S = 600  # 10 minutes


class ResearchRequest(BaseModel):
    query: str
    path_override: Literal["quick", "deep", "academic", "url_source"] | None = None
    config_path: str = "config.yaml"


class ResearchResponse(BaseModel):
    markdown: str
    path: str
    citations: list[dict[str, Any]]
    iterations: int = 0


@app.post("/research", response_model=ResearchResponse)
async def research_endpoint(request: ResearchRequest) -> ResearchResponse:
    """Run a research query and return the result."""

    config_file = Path(request.config_path).resolve()
    if not str(config_file).startswith(str(_ALLOWED_CONFIG_DIR)):
        raise HTTPException(status_code=400, detail="config_path outside allowed directory")

    config = AgentTopConfig.load_yaml(config_file)
    try:
        report = await asyncio.wait_for(
            run_research(
                query=request.query,
                config=config,
                path_override=request.path_override,
            ),
            timeout=_REQUEST_TIMEOUT_S,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Research exceeded {_REQUEST_TIMEOUT_S}s timeout",
        )
    except Exception as e:
        logger.exception("research failed")
        raise HTTPException(status_code=500, detail=str(e))

    return ResearchResponse(
        markdown=report.markdown,
        path=report.path,
        citations=[c.model_dump() for c in report.citations],
        iterations=report.iterations,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the FastAPI microservice."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
