"""LLM layer."""

from deep_research.llm.client import LLMClient, open_llm
from deep_research.llm.tool_loop import ToolRegistry, ToolResult, run_with_tools
from deep_research.llm.vision import (
    IMAGE_DEGRADE_LADDER,
    MAX_TEXT_CHARS_WITH_IMAGES,
    TOKENS_PER_IMAGE,
    build_image_content_block,
    degrade_image,
    is_context_overflow,
    jpeg_bytes_to_data_url,
    render_and_resize,
    resize_for_vlm,
)

__all__ = [
    "IMAGE_DEGRADE_LADDER",
    "MAX_TEXT_CHARS_WITH_IMAGES",
    "TOKENS_PER_IMAGE",
    "LLMClient",
    "ToolRegistry",
    "ToolResult",
    "build_image_content_block",
    "degrade_image",
    "is_context_overflow",
    "jpeg_bytes_to_data_url",
    "open_llm",
    "render_and_resize",
    "resize_for_vlm",
    "run_with_tools",
]
