"""analyze_paper node - structured LLM analysis of an arxiv paper (academic mode).

P7: implemented. Single chat-completions call with `prompts/analyze_paper.txt`
returns a `PaperAnalysis` (summary, key_findings, methodology, limitations,
key_references w/ arxiv_ids for recursion, figure_descriptions).

Supports optional vision image_url content blocks (rendered PDF pages) for
figure / table comprehension — attached when the caller passes
`page_image_data_urls` (P6 wires them via the pdf_render_pages tool).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from deep_research.state import PaperAnalysis

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "analyze_paper.txt"


def _build_messages(
    arxiv_id: str,
    paper_text: str,
    query: str,
    page_image_data_urls: list[str] | None,
) -> list[dict[str, Any]]:
    """Construct the chat messages list. When image data URLs are supplied,
    the user message is multi-content with one image_url block per page.
    """
    prompt_template = _PROMPT_FILE.read_text(encoding="utf-8")
    image_section = ""
    if page_image_data_urls:
        image_section = (
            "\n## Page images (rendered PDF pages sent via image_url content blocks):\n"
            f"({len(page_image_data_urls)} pages attached)\n"
        )
    prompt_text = (
        prompt_template.replace("{arxiv_id}", arxiv_id)
        .replace("{paper_text}", paper_text[:40000])  # guard context blowup
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


async def analyze(
    arxiv_id: str,
    paper_text: str,
    query: str,
    client: AsyncOpenAI,
    model: str,
    page_image_data_urls: list[str] | None = None,
    text_source: Literal["pdf", "abstract", "html"] = "pdf",
) -> PaperAnalysis:
    """Make the LLM call and parse the JSON into a `PaperAnalysis`.

    `text_source` controls how the paper_text is consumed:
      - "pdf" — full PDF text, standard prompt.
      - "abstract" — abstract-only text (paywalled / unavailable). The prompt
        is prefixed with `[ABSTRACT-ONLY]` so the LLM knows to limit claims to
        visible content. `key_references` are forced empty — abstract-only
        papers are leaf nodes and never enqueued for recursion.
      - "html" — HTML page text, same treatment as abstract for now.

    Degrades cleanly on invalid JSON / LLM exceptions by returning a
    `PaperAnalysis` with a marker title so the academic loop keeps running.
    """
    if text_source in ("abstract", "html"):
        paper_text = "[ABSTRACT-ONLY]\n" + paper_text
    messages = _build_messages(arxiv_id, paper_text, query, page_image_data_urls)
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
            return PaperAnalysis(
                title=f"[unparseable] {arxiv_id}",
                summary=raw[:3000],
            )
        # Coerce into PaperAnalysis. Filter out references with no arxiv_id.
        analysis = _coerce(arxiv_id, data)
        # Abstract-only / HTML nodes are leaf nodes — force no recursion.
        if text_source in ("abstract", "html"):
            analysis.key_references = []
        return analysis
    except Exception as e:
        logger.warning(
            "analyze_paper LLM call failed for %s: %s: %s", arxiv_id, type(e).__name__, e
        )
        return PaperAnalysis(
            title=f"[error] {arxiv_id}",
            summary=f"LLM analysis failed: {type(e).__name__}: {e}",
        )


_ARXIV_RX = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b")


def _bool_coerce(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1"}
    return bool(v)


def _list_of_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    return []


def _coerce(arxiv_id: str, data: dict[str, Any] | list) -> PaperAnalysis:
    """Loose-coerce a JSON-decoded LLM payload into a strict `PaperAnalysis`.

    Drops `key_references` items that lack a usable arxiv_id; the academic
    loop relies on those to enqueue follow-up papers, so any garbage the LLM
    emits is filtered here. Field types are kept strict-compatible with the
    pydantic model (`extra="forbid"`).
    """
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
            [str(a) for a in authors_raw if isinstance(a, (str, int, float))]
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

    payload = {
        "title": str(data.get("title", "") or f"(unknown title) {arxiv_id}"),
        "summary": str(data.get("summary", "") or ""),
        "key_findings": _list_of_str(data.get("key_findings")),
        "relevance_to_query": str(data.get("relevance_to_query", "") or ""),
        "methodology": str(data.get("methodology", "") or ""),
        "limitations": _list_of_str(data.get("limitations")),
        "is_key_reference": _bool_coerce(data.get("is_key_reference", False)),
        "key_references": cleaned_key_refs,
        "extraction_text": str(data.get("extraction_text", "") or ""),
        "figure_descriptions": _list_of_str(data.get("figure_descriptions")),
    }
    return PaperAnalysis.model_validate(payload)


def extract_key_reference_arxiv_ids(analysis: PaperAnalysis) -> list[str]:
    """Return the arxiv_ids of `key_references` worth recursing into.

    The LLM flag has already filtered out non-key refs by the time `analyze`
    returns; here we just collect the ids, dropping empty/garbage ones.
    """
    out: list[str] = []
    for ref in analysis.key_references:
        aid = (ref.arxiv_id or "").strip()
        if aid:
            out.append(aid)
    return out


__all__ = ["analyze", "extract_key_reference_arxiv_ids"]
