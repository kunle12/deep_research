"""Shared arXiv-PDF archiving for citations (post-run batch + web UI on-demand)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from deep_research.library.writer import LibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.tools.pdf_utils import parse_pdf_path
from deep_research.util import strip_arxiv_version

logger = logging.getLogger(__name__)


async def archive_cited_pdf(
    arxiv_id: str,
    *,
    title: str | None,
    tools: ToolRegistry,
    writer: LibraryWriter,
    timeout_s: float = 180.0,
) -> str | None:
    """Download + archive one arXiv paper PDF; return the artifact_id or None.

    Already-archived papers are skipped, and every failure mode (download
    error, unparseable path, network timeout) degrades to ``None`` so callers
    never see an exception from a single paper.
    """
    aid = (arxiv_id or "").strip()
    if not aid or aid.startswith("scholar:"):
        return None
    base = strip_arxiv_version(aid)
    try:
        if await writer.storage.find_artifact_by_arxiv_id(base) is not None:
            return None
    except Exception:
        pass
    try:
        async with asyncio.timeout(timeout_s):
            result = await tools.call("arxiv_download_pdf", {"arxiv_id": aid})
        if result.error is not None:
            logger.info("cited-pdf archive skipped %s: %s", aid, result.error)
            return None
        pdf_path = parse_pdf_path(result.content or "")
        if pdf_path is None:
            logger.info("cited-pdf archive skipped %s: no local path returned", aid)
            return None
        artifact_id = await writer.archive_pdf(
            Path(pdf_path),
            arxiv_id=base,
            source_url=f"https://arxiv.org/abs/{base}",
            title=title or None,
            source_type="arxiv",
        )
        logger.info("archived cited arXiv PDF %s -> %s", base, artifact_id)
        return artifact_id or None
    except Exception as e:
        logger.debug("cited-pdf archive failed %s: %s", aid, type(e).__name__)
        return None


__all__ = ["archive_cited_pdf"]
