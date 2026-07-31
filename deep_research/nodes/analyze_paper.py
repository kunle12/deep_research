"""analyze_paper node - structured LLM analysis of an arxiv paper (academic mode).

P7: implemented. Multi-stage vision-aware analysis:

  1. If no images → direct text-only synthesis (single call).
  2. If images are present → adaptive batching:
       - Process all images in batches (batch_size ≤ 5).
       - If a batch fails with a tokenization error, halve the batch size
         and retry the same images.
       - Merge per-batch figure_descriptions + extraction_text.
       - Final synthesis call (no images) with full paper text + all
         per-batch results → complete PaperAnalysis.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from deep_research.llm.tokens import count_text_tokens
from deep_research.llm.vision import (
    IMAGE_DEGRADE_LADDER,
    MAX_TEXT_CHARS_WITH_IMAGES,
    TOKENS_PER_IMAGE,
    degrade_image,
    is_context_overflow,
)
from deep_research.state import PaperAnalysis
from deep_research.util import coerce_float

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "analyze_paper.txt"
_IMAGE_BATCH_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent / "prompts" / "analyze_paper_images.txt"
)

# Reserve for system prompt, JSON schema instructions, and response.
_RESERVED_TOKENS = 4096
# Hard cap on paper text chars for the text-only synthesis call.
# ~40k chars ≈ 10k tokens.
_MAX_PAPER_CHARS = 40000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_system() -> dict[str, Any]:
    return {
        "role": "system",
        "content": (
            "You are an academic paper analyst. Respond with a SINGLE JSON object and "
            "NOTHING ELSE - no markdown fences, no surrounding text."
        ),
    }


async def _call(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    *,
    use_json: bool,
) -> Any:
    """Thin wrapper around `client.chat.completions.create`."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
    }
    if use_json:
        kwargs["response_format"] = {"type": "json_object"}
    return await client.chat.completions.create(**kwargs)


def _compute_batch_size(
    paper_text: str,
    query: str,
    images: list[str],
    max_context_tokens: int,
    model: str,
) -> int:
    """Return the max number of images that can fit in a single LLM call
    alongside a small text context and prompt overhead.

    Uses a fixed per-image token estimate appropriate for servers with
    native vision encoding.
    """
    if not images:
        return 1
    prompt_template = _IMAGE_BATCH_PROMPT_FILE.read_text(encoding="utf-8")
    base_prompt_tokens = count_text_tokens(
        prompt_template.replace("{arxiv_id}", "")
        .replace("{paper_text}", "")
        .replace("{query}", query or "")
        .replace("{image_pages_section}", ""),
        model,
    )
    text_tokens = count_text_tokens(paper_text[:MAX_TEXT_CHARS_WITH_IMAGES], model)
    available = max_context_tokens - _RESERVED_TOKENS - base_prompt_tokens - text_tokens
    return max(1, min(5, available // TOKENS_PER_IMAGE))


# ---------------------------------------------------------------------------
# Per-batch image analysis
# ---------------------------------------------------------------------------


async def _analyze_image_batch(
    arxiv_id: str,
    paper_text: str,
    query: str,
    image_batch: list[str],
    client: AsyncOpenAI,
    model: str,
    max_context_tokens: int,
) -> dict[str, Any]:
    """Process a single batch of page images. Returns a JSON dict with
    `figure_descriptions` and `extraction_text` for this batch."""
    prompt_template = _IMAGE_BATCH_PROMPT_FILE.read_text(encoding="utf-8")
    n = len(image_batch)
    image_section = (
        f"\n## Page images (rendered PDF pages — {n} pages attached):\n({n} pages attached)\n"
    )

    # Token budget for paper text in this batch call.
    # Image batch calls only need brief text context to locate figures;
    # the full paper text is used in the text-only synthesis call.
    base_tokens = count_text_tokens(
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", "")
        .replace("{query}", query or "")
        .replace("{image_pages_section}", image_section),
        model,
    )
    image_tokens = n * TOKENS_PER_IMAGE
    available = max_context_tokens - _RESERVED_TOKENS - image_tokens - base_tokens
    max_chars = min(MAX_TEXT_CHARS_WITH_IMAGES, max(500, available * 4))

    prompt_text = (
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", paper_text[:max_chars])
        .replace("{query}", query or "")
        .replace("{image_pages_section}", image_section)
    )

    messages: list[dict[str, Any]] = [
        _build_system(),
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}]
            + [{"type": "image_url", "image_url": {"url": url}} for url in image_batch],
        },
    ]

    logger.debug(
        "analyze_paper %s batch(%d images): ~%d chars",
        arxiv_id,
        n,
        len(str(messages)),
    )

    try:
        resp = await _call(client, model, messages, use_json=True)
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return {"figure_descriptions": [], "extraction_text": ""}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"figure_descriptions": [], "extraction_text": ""}
        return {
            "figure_descriptions": _list_of_str(data.get("figure_descriptions")),
            "extraction_text": str(data.get("extraction_text", "") or ""),
        }
    except Exception as e:
        if is_context_overflow(e):
            # Re-raise so the caller can retry with smaller batches
            raise
        logger.warning(
            "analyze_paper %s batch(%d images) failed: %s: %s",
            arxiv_id,
            n,
            type(e).__name__,
            e,
        )
        # Non-fatal for non-context errors — return empty so the final
        # synthesis still runs
        return {"figure_descriptions": [], "extraction_text": ""}


# ---------------------------------------------------------------------------
# Final synthesis (no images)
# ---------------------------------------------------------------------------


async def _synthesize_final(
    arxiv_id: str,
    paper_text: str,
    query: str,
    merged_figure_descriptions: list[str],
    merged_extraction_text: str,
    client: AsyncOpenAI,
    model: str,
    max_context_tokens: int,
    text_source: Literal["pdf", "abstract", "html"],
) -> PaperAnalysis:
    """Final synthesis call with full paper text + all per-batch figure
    descriptions. No images — full context budget is available for text."""
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")

    # Inject pre-extracted figure descriptions into the prompt
    fig_section = (
        "\n## Previously extracted figure descriptions (from all pages):\n"
        + "\n".join(f"  - {d}" for d in merged_figure_descriptions)
        + "\n\n"
        if merged_figure_descriptions
        else ""
    )
    extra_section = (
        "\n## Previously extracted relevant text (from page images):\n"
        + merged_extraction_text
        + "\n\n"
        if merged_extraction_text
        else ""
    )

    # No images → no image_section. Use full budget for paper text, but
    # subtract the tokens that fig_section + extra_section will consume.
    base_tokens = count_text_tokens(
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", "")
        .replace("{query}", query or "")
        .replace("{image_pages_section}", ""),
        model,
    )
    fig_extra_tokens = count_text_tokens(fig_section + extra_section, model)
    available = max_context_tokens - _RESERVED_TOKENS - base_tokens - fig_extra_tokens
    # No hard cap — use full available budget for paper text
    max_chars = max(1000, available * 4)

    prompt_text = (
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", paper_text[:max_chars] + fig_section + extra_section)
        .replace("{query}", query or "")
        .replace("{image_pages_section}", "")
    )

    messages = [
        _build_system(),
        {"role": "user", "content": prompt_text},
    ]

    logger.debug(
        "analyze_paper %s final synthesis: ~%d chars, %d pre-extracted figures",
        arxiv_id,
        len(str(messages)),
        len(merged_figure_descriptions),
    )

    try:
        resp = await _call(client, model, messages, use_json=True)
        raw = resp.choices[0].message.content or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return PaperAnalysis(
                title=f"[unparseable] {arxiv_id}",
                summary=raw[:3000],
            )
        analysis = _coerce(arxiv_id, data)
        if text_source in ("abstract", "html"):
            analysis.key_references = []
        return analysis
    except Exception as e:
        if is_context_overflow(e):
            # Retry once with halved text if the synthesis itself overflows
            logger.warning(
                "analyze_paper %s final synthesis too long; retrying with halved text: %s",
                arxiv_id,
                e,
            )
            half_chars = max(2000, max_chars // 2)
            prompt_text2 = (
                prompt_template.replace("{arxiv_id}", arxiv_id)
                .replace(
                    "{paper_text}",
                    paper_text[:half_chars] + fig_section + extra_section,
                )
                .replace("{query}", query or "")
                .replace("{image_pages_section}", "")
            )
            messages2 = [
                _build_system(),
                {"role": "user", "content": prompt_text2},
            ]
            try:
                resp2 = await _call(client, model, messages2, use_json=False)
                raw2 = resp2.choices[0].message.content or ""
                try:
                    data2 = json.loads(raw2)
                except json.JSONDecodeError:
                    return PaperAnalysis(
                        title=f"[unparseable] {arxiv_id}",
                        summary=raw2[:3000],
                    )
                analysis2 = _coerce(arxiv_id, data2)
                if text_source in ("abstract", "html"):
                    analysis2.key_references = []
                return analysis2
            except Exception as e2:
                logger.warning(
                    "analyze_paper %s final synthesis (retry) also failed: %s: %s",
                    arxiv_id,
                    type(e2).__name__,
                    e2,
                )
                return PaperAnalysis(
                    title=f"[error] {arxiv_id}",
                    summary=f"LLM analysis failed: {type(e2).__name__}: {e2}",
                )

        logger.warning(
            "analyze_paper %s final synthesis failed: %s: %s",
            arxiv_id,
            type(e).__name__,
            e,
        )
        return PaperAnalysis(
            title=f"[error] {arxiv_id}",
            summary=f"LLM analysis failed: {type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Adaptive batching orchestration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def analyze(
    arxiv_id: str,
    paper_text: str,
    query: str,
    client: AsyncOpenAI,
    model: str,
    page_image_data_urls: list[str] | None = None,
    text_source: Literal["pdf", "abstract", "html"] = "pdf",
    max_context_tokens: int = 131072,
) -> PaperAnalysis:
    """Analyze an arXiv paper with adaptive vision batching.

    When `page_image_data_urls` is provided and the images fit in a single
    LLM call, uses the fast single-shot path.  If they don't fit, splits
    images into adaptive batches, processes each batch, merges results,
    then does a final no-image synthesis with full paper text + all
    per-batch figure descriptions.

    Degrades cleanly on invalid JSON / LLM exceptions by returning a
    `PaperAnalysis` with a marker title so the academic loop keeps running.
    """
    if text_source in ("abstract", "html"):
        paper_text = "[ABSTRACT-ONLY]\n" + paper_text

    n_images = len(page_image_data_urls) if page_image_data_urls else 0

    # --- Text-only path: no images ---
    if n_images == 0:
        return await _synthesize_final(
            arxiv_id,
            paper_text,
            query,
            [],
            "",
            client,
            model,
            max_context_tokens,
            text_source,
        )

    # --- Multi-batch path: always process images in adaptive batches ---
    # Adaptive batching: start with computed batch_size, halve on failure.
    # When batch_size=1 still overflows, degrade image quality/resolution
    # via IMAGE_DEGRADE_LADDER before giving up on the image.

    remaining = list(page_image_data_urls)
    batch_size = _compute_batch_size(paper_text, query, remaining, max_context_tokens, model)
    batch_size = max(1, min(batch_size, n_images))
    degrade_idx = 0

    logger.info(
        "analyze_paper %s: adaptive batching with initial batch_size=%d, %d images total",
        arxiv_id,
        batch_size,
        n_images,
    )

    all_figures: list[str] = []
    all_extractions: list[str] = []

    while remaining:
        batch = remaining[:batch_size]
        try:
            result = await _analyze_image_batch(
                arxiv_id,
                paper_text,
                query,
                batch,
                client,
                model,
                max_context_tokens,
            )
            figs = result.get("figure_descriptions", [])
            ext = result.get("extraction_text", "")
            all_figures.extend(figs)
            if ext:
                all_extractions.append(ext)
            remaining = remaining[batch_size:]
        except Exception as e:
            if is_context_overflow(e) and batch_size > 1:
                logger.info(
                    "analyze_paper %s batch(%d images) overflow; halving batch "
                    "size to %d and retrying same images: %s",
                    arxiv_id,
                    batch_size,
                    max(1, batch_size // 2),
                    e,
                )
                batch_size = max(1, batch_size // 2)
                continue
            elif is_context_overflow(e) and batch_size == 1:
                if degrade_idx < len(IMAGE_DEGRADE_LADDER):
                    max_dim, quality = IMAGE_DEGRADE_LADDER[degrade_idx]
                    logger.info(
                        "analyze_paper %s single image overflow; degrading to "
                        "%dpx/q%d and retrying: %s",
                        arxiv_id,
                        max_dim,
                        quality,
                        e,
                    )
                    # Degrade only the current image (batch_size == 1). The
                    # ladder is per-image: resetting only on skip means one
                    # oversized image can no longer consume every degradation
                    # step for all remaining pages.
                    degraded = [degrade_image(url, max_dim, quality) for url in batch]
                    remaining[:batch_size] = degraded
                    degrade_idx += 1
                    continue
                else:
                    logger.warning(
                        "analyze_paper %s single image overflow after all "
                        "degradation steps; skipping image: %s",
                        arxiv_id,
                        e,
                    )
                    remaining = remaining[1:]
                    degrade_idx = 0
            else:
                logger.warning(
                    "analyze_paper %s batch(%d images) non-overflow error; advancing: %s: %s",
                    arxiv_id,
                    batch_size,
                    type(e).__name__,
                    e,
                )
                remaining = remaining[batch_size:]

    # Final synthesis with all per-batch results
    merged_extraction = "\n\n".join(all_extractions) if all_extractions else ""
    return await _synthesize_final(
        arxiv_id,
        paper_text,
        query,
        all_figures,
        merged_extraction,
        client,
        model,
        max_context_tokens,
        text_source,
    )


# ---------------------------------------------------------------------------
# Legacy helpers (preserved for backward compat and test coverage)
# ---------------------------------------------------------------------------


def _build_messages(
    arxiv_id: str,
    paper_text: str,
    query: str,
    page_image_data_urls: list[str] | None,
    model: str = "gpt-4",
    max_context_tokens: int = 131072,
    max_images_override: int | None = None,
    max_paper_chars_override: int | None = None,
) -> list[dict[str, Any]]:
    """Legacy message builder — kept for backward compat and single-shot path."""
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")

    if max_images_override is not None:
        max_images = max_images_override
    else:
        max_images = max(0, (max_context_tokens - _RESERVED_TOKENS) // 8000)
    if page_image_data_urls and len(page_image_data_urls) > max_images:
        logger.info(
            "analyze_paper %s: limiting images from %d to %d (context=%d tokens)",
            arxiv_id,
            len(page_image_data_urls),
            max_images,
            max_context_tokens,
        )
        page_image_data_urls = page_image_data_urls[:max_images]

    n_images = len(page_image_data_urls) if page_image_data_urls else 0
    image_section = ""
    if page_image_data_urls:
        image_section = (
            "\n## Page images (rendered PDF pages sent via image_url content blocks):\n"
            f"({n_images} pages attached)\n"
        )

    image_tokens = n_images * TOKENS_PER_IMAGE
    base_prompt_tokens = count_text_tokens(
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", "")
        .replace("{query}", query or "")
        .replace("{image_pages_section}", image_section),
        model,
    )
    available_tokens = max_context_tokens - _RESERVED_TOKENS - image_tokens - base_prompt_tokens
    if max_paper_chars_override is not None:
        max_paper_chars = max_paper_chars_override
    else:
        max_paper_chars = min(_MAX_PAPER_CHARS, max(1000, available_tokens * 4))

    if len(paper_text) > max_paper_chars:
        logger.debug(
            "analyze_paper %s: truncating paper_text from %d to %d chars "
            "(budget=%d tokens, images=%d)",
            arxiv_id,
            len(paper_text),
            max_paper_chars,
            available_tokens,
            n_images,
        )

    prompt_text = (
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", paper_text[:max_paper_chars])
        .replace("{query}", query or "")
        .replace("{image_pages_section}", image_section)
    )

    system = {
        "role": "system",
        "content": (
            "You are an academic paper analyst. Respond with a SINGLE JSON object and "
            "NOTHING ELSE - no markdown fences, no surrounding text."
        ),
    }

    if page_image_data_urls:
        user_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for data_url in page_image_data_urls:
            user_blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        return [system, {"role": "user", "content": user_blocks}]
    return [system, {"role": "user", "content": prompt_text}]


_ARXIV_RX = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b")


def _bool_coerce(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1"}
    return bool(v)


def _list_of_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, str | int | float)]
    return []


def _coerce(arxiv_id: str, data: dict[str, Any] | list) -> PaperAnalysis:
    if not isinstance(data, dict):
        return PaperAnalysis(
            title=f"[unparseable] {arxiv_id}",
            summary=f"LLM returned a {type(data).__name__} instead of a JSON object",
        )
    cleaned_key_refs: list[dict[str, Any]] = []
    for ref in data.get("key_references") or []:
        if not isinstance(ref, dict):
            continue
        aid = ref.get("arxiv_id")
        if not aid:
            cand = ref.get("title", "") or ref.get("rationale", "") or ""
            m = _ARXIV_RX.search(cand)
            if m:
                aid = m.group(0)
        if not aid:
            continue
        title = str(ref.get("title", "") or "")
        rationale = str(ref.get("rationale", "") or "")
        authors_raw = ref.get("authors") or []
        authors = (
            [str(a) for a in authors_raw if isinstance(a, str | int | float)]
            if isinstance(authors_raw, list)
            else []
        )
        cleaned_key_refs.append(
            {
                "arxiv_id": str(aid),
                "title": title,
                "rationale": rationale,
                "authors": authors,
            }
        )

    raw_rel = data.get("relevance_score")
    rel_score = coerce_float(raw_rel, -1.0)
    if rel_score < 0.0:
        logger.warning(
            "analyze_paper %s: missing/invalid relevance_score (%r); defaulting to "
            "1.0 — the relevance gate will NOT exclude this paper",
            arxiv_id,
            raw_rel,
        )
        rel_score = 1.0
    rel_score = max(0.0, min(1.0, rel_score))

    payload = {
        "title": str(data.get("title", "") or f"(unknown title) {arxiv_id}"),
        "summary": str(data.get("summary", "") or ""),
        "key_findings": _list_of_str(data.get("key_findings")),
        "relevance_to_query": str(data.get("relevance_to_query", "") or ""),
        "relevance_score": rel_score,
        "methodology": str(data.get("methodology", "") or ""),
        "limitations": _list_of_str(data.get("limitations")),
        "is_key_reference": _bool_coerce(data.get("is_key_reference", False)),
        "key_references": cleaned_key_refs,
        "extraction_text": str(data.get("extraction_text", "") or ""),
        "figure_descriptions": _list_of_str(data.get("figure_descriptions")),
    }
    return PaperAnalysis.model_validate(payload)


def extract_key_reference_arxiv_ids(analysis: PaperAnalysis) -> list[str]:
    out: list[str] = []
    for ref in analysis.key_references:
        aid = (ref.arxiv_id or "").strip()
        if aid:
            out.append(aid)
    return out


__all__ = ["analyze", "extract_key_reference_arxiv_ids"]
