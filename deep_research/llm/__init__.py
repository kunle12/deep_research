"""LLM layer."""

from deep_research.llm.client import LLMClient, open_llm
from deep_research.llm.router import FallbackClient, LLMClientLike, LLMRouter, ResolvedLLM
from deep_research.llm.tool_loop import ToolRegistry, ToolResult, run_with_tools
from deep_research.llm.vision import (
    IMAGE_DEGRADE_LADDER,
    MAX_TEXT_CHARS_WITH_IMAGES,
    TOKENS_PER_IMAGE,
    degrade_image,
    is_context_overflow,
    jpeg_bytes_to_data_url,
    resize_for_vlm,
)

__all__ = [
    "IMAGE_DEGRADE_LADDER",
    "MAX_TEXT_CHARS_WITH_IMAGES",
    "TOKENS_PER_IMAGE",
    "FallbackClient",
    "LLMClient",
    "LLMClientLike",
    "LLMRouter",
    "ResolvedLLM",
    "ToolRegistry",
    "ToolResult",
    "degrade_image",
    "is_context_overflow",
    "jpeg_bytes_to_data_url",
    "open_llm",
    "resize_for_vlm",
    "run_with_tools",
]
