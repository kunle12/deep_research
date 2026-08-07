"""Vision utility — convert PDF pages / images to downscaled JPEG bytes.

Image scaling policy: every page goes through the VLM (no pruning), but
downscaled to `max_dim` and JPEG-compressed at quality 80 BEFORE the LLM
sees it, so token cost stays bounded. PIL does the work; we return bytes.

Also provides shared context-overflow detection and image degradation
helpers used by `nodes/analyze_paper.py` and `nodes/analyze_source.py`.

Used by `tools/pdf.py` and `nodes/analyze_paper.py` / `analyze_source.py`.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

# ---------------------------------------------------------------------------
# Context-overflow detection (shared by analyze_paper + analyze_source)
# ---------------------------------------------------------------------------

CONTEXT_ERROR_MARKERS = (
    "tokenize",
    "context length",
    "context_length",
    "maximum context",
    "too long",
    "too many tokens",
    "reduce the length",
    "prompt is too",
)


def is_context_overflow(exc: Exception) -> bool:
    """Heuristic: does this exception look like a prompt-too-long / tokenization
    overflow from the server?"""
    msg = str(exc).lower()
    return any(marker in msg for marker in CONTEXT_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Vision budget constants (shared by analyze_paper + analyze_source)
# ---------------------------------------------------------------------------

# Hard cap on text chars included alongside images in a single LLM call.
# VLM servers have a much lower effective context for combined text+image
# payloads than for text alone.
MAX_TEXT_CHARS_WITH_IMAGES = 4000

# Estimated vision tokens per image for servers with native vision encoding.
# Most VLM servers encode a 1024px image as ~1000-2000 vision tokens
# regardless of base64 size.
TOKENS_PER_IMAGE = 1500

# Image degradation ladder: (max_dim, jpeg_quality) pairs tried in order
# when a single image still overflows the context.
IMAGE_DEGRADE_LADDER: list[tuple[int, int]] = [
    (512, 60),
    (256, 40),
]

# ---------------------------------------------------------------------------
# Image encoding helpers
# ---------------------------------------------------------------------------


def resize_for_vlm(
    image: Image.Image,
    max_dim: int = 1024,
    jpeg_quality: int = 80,
) -> bytes:
    """Resize `image` so its longest side ≤ `max_dim`, return JPEG bytes.

    Preserves aspect ratio; uses LANCZOS for high-quality downscale.
    """
    image = image.convert("RGB")
    w, h = image.size
    scale = min(1.0, max_dim / float(max(w, h)))
    if scale < 1.0:
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        image = image.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    return buf.getvalue()


def jpeg_bytes_to_data_url(jpeg_bytes: bytes) -> str:
    """Wrap JPEG bytes as a `data:image/jpeg;base64,...` URL for OpenAI image_url content."""
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def degrade_image(data_url: str, max_dim: int, jpeg_quality: int) -> str:
    """Re-encode a base64 data URL at lower resolution/quality.

    Decodes the JPEG, resizes with LANCZOS, re-encodes.  Returns a new data URL.
    Falls back to the original URL on any decode error.
    """
    _header, _, b64_data = data_url.partition(",")
    if not b64_data:
        return data_url
    try:
        raw = base64.b64decode(b64_data)
        img = Image.open(io.BytesIO(raw))
        jpeg = resize_for_vlm(img, max_dim=max_dim, jpeg_quality=jpeg_quality)
        return jpeg_bytes_to_data_url(jpeg)
    except Exception:
        return data_url


__all__ = [
    "CONTEXT_ERROR_MARKERS",
    "IMAGE_DEGRADE_LADDER",
    "MAX_TEXT_CHARS_WITH_IMAGES",
    "TOKENS_PER_IMAGE",
    "degrade_image",
    "is_context_overflow",
    "jpeg_bytes_to_data_url",
    "resize_for_vlm",
]
