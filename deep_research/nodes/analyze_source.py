"""analyze_source node — LLM-driven structured analysis of a single URL.

P2.5: implemented. Single chat-completions call returns a SourceAnalysis
pydantic model. Supports optional vision image_url content blocks for PDF pages.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from deep_research.llm.router import LLMClientLike
from deep_research.llm.vision import MAX_TEXT_CHARS_WITH_IMAGES, is_context_overflow
from deep_research.state import SourceAnalysis

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "analyze_source.txt"


def _build_messages(
    url: str,
    source_type: str,
    content: str,
    user_query: str,
    page_image_data_urls: list[str] | None,
) -> list[dict[str, Any]]:
    """Construct the chat messages list with the prompt template + content + images."""
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    image_section = ""
    if page_image_data_urls:
        image_section = (
            "\n## Page images (rendered PDF pages sent via image_url content blocks):\n"
            f"({len(page_image_data_urls)} pages attached)\n"
        )
    max_content_chars = MAX_TEXT_CHARS_WITH_IMAGES if page_image_data_urls else 40000
    prompt_text = (
        prompt_template.replace("{url}", url)
        .replace("{source_type}", source_type)
        .replace("{content}", content[:max_content_chars])
        .replace("{query}", user_query or "")
        .replace("{image_pages_section}", image_section)
    )
    # Compose the user message; if image blocks present, use the multi-content form
    if page_image_data_urls:
        user_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for data_url in page_image_data_urls:
            user_blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        return [
            {
                "role": "system",
                "content": (
                    "You are an analyst. Respond with a SINGLE JSON object and NOTHING ELSE - "
                    "no markdown fences, no surrounding text."
                ),
            },
            {"role": "user", "content": user_blocks},
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are an analyst. Respond with a SINGLE JSON object and NOTHING ELSE - "
                "no markdown fences, no surrounding text."
            ),
        },
        {"role": "user", "content": prompt_text},
    ]


async def analyze(
    url: str,
    source_type: str,
    content: str,
    user_query: str,
    client: LLMClientLike,
    model: str,
    page_image_data_urls: list[str] | None = None,
) -> SourceAnalysis:
    """Make the LLM call and parse the JSON into a `SourceAnalysis`."""
    messages = _build_messages(url, source_type, content, user_query, page_image_data_urls)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return SourceAnalysis(
                title=f"[unparseable] {url}",
                summary=raw[:3000],
            )
        return SourceAnalysis.model_validate(data)
    except Exception as e:
        if page_image_data_urls and is_context_overflow(e):
            logger.info("analyze_source %s: overflow with images; retrying text-only: %s", url, e)
            return await analyze(
                url, source_type, content, user_query, client, model, page_image_data_urls=None
            )
        logger.warning("analyze_source LLM call failed: %s: %s", type(e).__name__, e)
        return SourceAnalysis(
            title=f"[error] {url}",
            summary=f"LLM analysis failed: {type(e).__name__}: {e}",
        )


__all__ = ["analyze"]
