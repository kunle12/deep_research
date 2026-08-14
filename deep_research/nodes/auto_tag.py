"""Auto-tag node (P10.7).

After the report is archived, a lightweight LLM call extracts topic tags from
the query and report text, then persists them to the library via writer.tag().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deep_research.library.writer import LibraryWriter
from deep_research.llm.router import LLMClientLike

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "auto_tag.txt"


async def auto_tag_report(
    query: str,
    report_text: str,
    artifact_id: str,
    llm: LLMClientLike,
    model: str,
    writer: LibraryWriter | None,
    run_id: str,
) -> list[str]:
    """Make a lightweight LLM call to extract topic tags, then persist them.

    Returns the list of tag strings that were saved (empty if nothing extracted).
    """
    if not isinstance(writer, LibraryWriter) or not run_id or not artifact_id:
        return []

    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    user_msg = prompt_text.replace("{query}", query).replace("{context}", report_text[:2000])

    try:
        resp = await llm.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a topic tag extraction assistant. Output ONLY valid JSON — no markdown, no code fences.",
                },
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("Auto-tag LLM call failed: %s: %s", type(e).__name__, e)
        return []

    try:
        data = json.loads(raw)
        tags = data.get("tags", []) if isinstance(data, dict) else []
        if not isinstance(tags, list):
            tags = []
    except json.JSONDecodeError:
        logger.warning("Auto-tag LLM returned invalid JSON: %s", raw[:200])
        return []

    if tags:
        try:
            await writer.tag(artifact_id, tags, run_id=run_id)
        except Exception as e:
            # Persistence must never discard a finished report — log and drop.
            logger.warning("auto-tag persistence failed: %s: %s", type(e).__name__, e)
            return []

    return tags


__all__ = ["auto_tag_report"]
