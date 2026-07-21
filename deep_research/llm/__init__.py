"""LLM layer."""

from deep_research.llm.client import LLMClient, open_llm
from deep_research.llm.tool_loop import ToolRegistry, ToolResult, run_with_tools
from deep_research.llm.vision import (
    build_image_content_block,
    jpeg_bytes_to_data_url,
    render_and_resize,
    resize_for_vlm,
)

__all__ = [
    "LLMClient",
    "ToolRegistry",
    "ToolResult",
    "build_image_content_block",
    "jpeg_bytes_to_data_url",
    "open_llm",
    "render_and_resize",
    "resize_for_vlm",
    "run_with_tools",
]
