"""Vision utility — convert PDF pages / images to downscaled JPEG bytes.

Image scaling policy: every page goes through the VLM (no pruning), but
downscaled to `max_dim` and JPEG-compressed at quality 80 BEFORE the LLM
sees it, so token cost stays bounded. PIL does the work; we return bytes.

Used by `tools/pdf.py` and `nodes/analyze_paper.py` / `analyze_source.py`.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterable

from PIL import Image

from deep_research.config import PdfVisionConfig


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


def build_image_content_block(jpeg_bytes: bytes, *, detail: str = "auto") -> dict:
    """Build an OpenAI chat-completions `image_url` content block."""
    return {
        "type": "image_url",
        "image_url": {"url": jpeg_bytes_to_data_url(jpeg_bytes), "detail": detail},
    }


def render_and_resize(
    pil_images: Iterable[Image.Image],
    cfg: PdfVisionConfig,
) -> list[bytes]:
    """Apply `resize_for_vlm` to each PIL image using the config values."""
    return [
        resize_for_vlm(img, max_dim=cfg.max_dim, jpeg_quality=cfg.jpeg_quality)
        for img in pil_images
    ]


__all__ = [
    "build_image_content_block",
    "jpeg_bytes_to_data_url",
    "render_and_resize",
    "resize_for_vlm",
]
