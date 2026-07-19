"""browser tool — Playwright MCP async client.

P8: real implementation. Spawns an MCP stdio subprocess via
`npx -y @playwright/mcp@latest` (configurable in `config.browser`), opens a
`ClientSession`, and bridges a curated subset of the Playwright MCP tools
(`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_evaluate`)
into our `ToolRegistry`.

Design choices:

1. **Lazy connection.** Spawning a chromium-backed MCP server is expensive
   (~1-3s warm-up). Many research runs won't need the browser at all (Tavily
   + fetch_page get most content). So the MCP subprocess is spawned on the
   first `browser_navigate` / `browser_snapshot` / etc. call, not at
   `register()` time.

2. **Graceful degradation.** Subprocess spawn failures, missing `npx`,
   initial-connection timeouts, and MCP call errors all return a clean
   `ToolResult(error=...)` with a README-pointing hint. The downstream
   `fetch_page` already detects browser errors and falls back to raw HTML,
   so a broken browser does not break a research run.

3. **Curated tool subset.** @playwright/mcp@latest exposes 24 tools. We
   expose 4 to keep the LLM's tool-schema context small: navigate, snapshot,
   click, evaluate. The other 20 (file_upload, drop, fill_form, press_key,
   type, hover, drag, select_option, tabs, take_screenshot, console_messages,
   network_request, etc.) are uninteresting for read-only research and would
   waste context tokens.indy Expand the subset from config in P9 if it proves
   useful.

4. **Teardown.** The MCP subprocess is killed in `_MCPClientCtx.close()`.
   The agent calls `reg._browser_close()` from its `_ToolsCtx.__aexit__`
   so the chromium process is reaped when the research run ends.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (LLM-facing)
# ---------------------------------------------------------------------------


NAVIGATE_SCHEMA = {
    "type": "function",
    "description": (
        "Open a URL in a headless browser (Playwright MCP, --headless). "
        "Useful for JS-heavy pages where fetch_page returns little "
        "content. Returns the page's accessibility snapshot summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to navigate to"},
        },
        "required": ["url"],
    },
}


SNAPSHOT_SCHEMA = {
    "type": "function",
    "description": (
        "Capture an accessibility-tree snapshot of the current page. "
        "The page must have been opened by browser_navigate first."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


CLICK_SCHEMA = {
    "type": "function",
    "description": (
        "Click an element on the current page. `target` is an element "
        "reference from browser_snapshot's accessibility tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Element reference from the snapshot",
            },
            "element": {
                "type": "string",
                "description": "Human-readable element description",
            },
        },
        "required": ["target"],
    },
}


EVALUATE_SCHEMA = {
    "type": "function",
    "description": (
        "Evaluate a JavaScript expression on the current page and return "
        "the result as text. Use sparingly; prefer browser_navigate + "
        "browser_snapshot for content extraction. Use only when the "
        "page exposes JS hooks not in the DOM."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "function": {
                "type": "string",
                "description": "JavaScript function body to evaluate",
            },
        },
        "required": ["function"],
    },
}


# ---------------------------------------------------------------------------
# MCP client lifecycle
# ---------------------------------------------------------------------------


# Hard timeout for the initial MCP session.initialize() call. If the MCP
# server takes longer than this to come up, we assume it's broken (rare on a
# warm box; common on first npx fetch). 30s is generous; the @playwright/mcp
# warm-up is typically 1-3s.
_INIT_TIMEOUT_S = 30.0


class _MCPClientCtx:
    """Async context-managed wrapper around stdio_client + ClientSession.

    Use:
        async with _MCPClientCtx(params) as mcp:
            result = await mcp.call("browser_navigate", {"url": "..."})
    """

    def __init__(self, command: str, args: list[str]) -> None:
        self._command = command
        self._args = list(args)
        self._stack: asyncio.TaskGroup | None = None
        # We use an ExitStack-style wrapper since stdio_client + ClientSession
        # are both `async with`. We keep them directly so close() can drive
        # teardown without relying on AsyncExitStack's exception trails.
        self._read: Any | None = None
        self._write: Any | None = None
        self._session: Any | None = None
        self._stdio_cm: Any | None = None
        self._session_cm: Any | None = None

    async def __aenter__(self) -> _MCPClientCtx:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=self._command, args=self._args)
        self._stdio_cm = stdio_client(params)
        try:
            transport = await self._stdio_cm.__aenter__()
            self._read, self._write = transport
        except FileNotFoundError as e:
            raise _MCPStartupError(
                f"failed to spawn MCP subprocess: {type(e).__name__}: {e}. "
                f"Command was: {self._command} {self._args!r}. "
                "Is `npx` / Node.js installed? See README."
            ) from e
        except OSError as e:
            raise _MCPStartupError(
                f"OS error spawning MCP subprocess: {type(e).__name__}: {e}. "
                "See README's browser setup section."
            ) from e

        self._session_cm = ClientSession(self._read, self._write)
        try:
            self._session = await self._session_cm.__aenter__()
        except Exception as e:
            # Tear down the stdio transport we already started
            await self._stdio_cm.__aexit__(type(e), e, e.__traceback__)
            raise _MCPStartupError(
                f"failed to create MCP ClientSession: {type(e).__name__}: {e}"
            ) from e

        try:
            await asyncio.wait_for(self._session.initialize(), timeout=_INIT_TIMEOUT_S)
        except TimeoutError:
            await self._session_cm.__aexit__(None, None, None)
            await self._stdio_cm.__aexit__(None, None, None)
            self._session = None
            raise _MCPStartupError(
                f"Playwright MCP server did not initialize within {_INIT_TIMEOUT_S:.0f}s. "
                "Verify `npx -y @playwright/mcp@latest` works on your host "
                "(first run downloads chromium; subsequent runs are faster)."
            )
        except Exception as e:
            await self._session_cm.__aexit__(type(e), e, e.__traceback__)
            await self._stdio_cm.__aexit__(type(e), e, e.__traceback__)
            self._session = None
            raise _MCPStartupError(
                f"MCP initialize() failed: {type(e).__name__}: {e}"
            ) from e

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Tear down in reverse order. Swallow teardown errors so we don't
        # mask the original exception (if any) that triggered __aexit__.
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(exc_type, exc, tb)
            except Exception as e:
                logger.debug("MCP ClientSession teardown raised: %s: %s", type(e).__name__, e)
            self._session_cm = None
            self._session = None
        if self._stdio_cm is not None:
            try:
                await self._stdio_cm.__aexit__(exc_type, exc, tb)
            except Exception as e:
                logger.debug("stdio_client teardown raised: %s: %s", type(e).__name__, e)
            self._stdio_cm = None
            self._read = None
            self._write = None

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a remote MCP tool. Returns the CallToolResult from the session."""
        if self._session is None:
            raise RuntimeError("MCP session not initialized")
        return await self._session.call_tool(name, arguments)


class _MCPStartupError(RuntimeError):
    """Raised when we cannot bring up the MCP subprocess / session."""


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    """Register the curated Playwright browser tools.

    The MCP connection is established lazily on the first call. After that,
    the same session reuses across all calls until the agent tears down via
    `reg._browser_close()`.
    """
    cfg = config.browser
    command = cfg.mcp_command
    args = list(cfg.mcp_args)

    # Hold the MCP client in a single-slot list so the local closures can
    # mutate it without `nonlocal` declarations.
    mcp_holder: list[_MCPClientCtx | None] = [None]
    startup_error: list[str] = []

    async def _ensure_mcp() -> _MCPClientCtx | None:
        if mcp_holder[0] is not None:
            return mcp_holder[0]
        if startup_error:
            # If we already failed once, do not retry within a single run.
            # (A subsequent run will get a fresh register() call.)
            return None

        # Pre-flight: is the npx command even on PATH? Otherwise the spawn
        # failure will be unwind-friendly.
        if not shutil.which(command):
            msg = (
                f"`{command}` not found on PATH. The browser tool requires "
                "Node.js + npx. See README's browser setup section."
            )
            startup_error.append(msg)
            logger.warning("browser tool: %s", msg)
            return None

        try:
            mcp = _MCPClientCtx(command, args)
            await mcp.__aenter__()
        except _MCPStartupError as e:
            startup_error.append(str(e))
            logger.warning("browser tool disabled: %s", e)
            # _MCPClientCtx.__aenter__ already cleaned up its partial state.
            return None
        mcp_holder[0] = mcp
        return mcp

    async def _invoke(mcp_name: str, arguments: dict[str, Any]) -> ToolResult:
        mcp = await _ensure_mcp()
        if mcp is None:
            err = startup_error[0] if startup_error else (
                "browser tool unavailable (no MCP connection)"
            )
            return ToolResult(content="", error=err)
        try:
            result = await mcp.call(mcp_name, arguments)
        except Exception as e:
            logger.warning(
                "MCP tool %s raised: %s: %s", mcp_name, type(e).__name__, e
            )
            return ToolResult(
                content="",
                error=f"MCP call {mcp_name} failed: {type(e).__name__}: {e}",
            )
        return _mcp_result_to_tool_result(result, mcp_name, arguments)

    async def _navigate(url: str, **_: Any) -> ToolResult:
        if not url or not url.startswith(("http://", "https://")):
            return ToolResult(content="", error=f"invalid url: {url!r}")
        result = await _invoke("browser_navigate", {"url": url})
        if result.error:
            return result
        # Attach a Citation so downstream know where this content came from.
        # fetch_page's low-yield fallback relies on this for provenance.
        cit = Citation(
            url=url,
            title=_title_from_content(result.content) or url,
            snippet=result.content[:200] if result.content else "",
            source_type="html",
            confidence_score=0.6,
            discovered_by=ToolName.browser,
        )
        # Avoid overwriting citations the MCP might have surfaced (none today
        # but the type signature allows it).
        result.citations = result.citations or [cit]
        return result

    async def _snapshot(**_: Any) -> ToolResult:
        return await _invoke("browser_snapshot", {})

    async def _click(target: str, element: str = "", **_: Any) -> ToolResult:
        args: dict[str, Any] = {"target": target}
        if element:
            args["element"] = element
        return await _invoke("browser_click", args)

    async def _evaluate(function: str, **_: Any) -> ToolResult:
        return await _invoke("browser_evaluate", {"function": function})

    reg.register("browser_navigate", _navigate, NAVIGATE_SCHEMA)
    reg.register("browser_snapshot", _snapshot, SNAPSHOT_SCHEMA)
    reg.register("browser_click", _click, CLICK_SCHEMA)
    reg.register("browser_evaluate", _evaluate, EVALUATE_SCHEMA)

    async def _close() -> None:
        mcp = mcp_holder[0]
        if mcp is not None:
            mcp_holder[0] = None
            try:
                await mcp.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("browser MCP close raised: %s: %s", type(e).__name__, e)

    # Register the close hook on the registry so ToolRegistry.close() tears
    # down the MCP subprocess when the agent finishes.
    reg._close_hooks.append(_close)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_result_to_tool_result(
    mcp_result: Any,
    mcp_name: str,
    arguments: dict[str, Any],
) -> ToolResult:
    """Translate a `mcp.types.CallToolResult` into our `ToolResult`.

    MCP `content` is a list of `TextContent` / `ImageContent` / `AudioContent` /
    `EmbeddedResource`. We only surface text content; binary content gets counted
    but not extracted (binary content isn't useful in plain-text research; it'd
    have to be saved as a separate file and we don't support that today).
    If `isError` is set, the content is the server-side error message.
    """
    is_error = bool(getattr(mcp_result, "isError", False))
    content_items = getattr(mcp_result, "content", None) or []
    text_parts: list[str] = []
    binary_count = 0
    for item in content_items:
        # mcp.types.TextContent has a `type` field == "text" and a `text` field
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text_parts.append(getattr(item, "text", ""))
        else:
            binary_count += 1
    body = "\n".join(text_parts)
    if binary_count:
        body += f"\n({binary_count} non-text content item(s) omitted)"
    if is_error:
        return ToolResult(
            content=body,
            error=f"browser_{mcp_name.removeprefix('browser_')} returned isError: {body[:500]}",
        )
    return ToolResult(content=body, citations=[])


def _title_from_content(content: str) -> str:
    """Best-effort: extract a title from an MCP browser tool output.

    `@playwright/mcp` versions vary the format. We try, in order:

    1. `- Page Title: <title>` line (newer `browser_navigate`/`browser_snapshot`
       outputs wrap the actual snapshot in a file reference and include a
       metadata block with this line).
    2. `- heading "<title>" [ref=...]` line (inlined accessibility snapshots).
    3. `# <title>` heading line (older snapshot inline format).
    """
    if not content:
        return ""
    # 1. `- Page Title: <title>` (newer MCP wrap format)
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- Page Title:"):
            after = stripped[len("- Page Title:"):].strip()
            if after:
                return after
    # 2. `- heading "<title>" [ref=...]` (inlined a11y tree)
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- heading "):
            after = stripped[len("- heading "):]
            if after.startswith('"'):
                end = after.find('"', 1)
                if end > 0:
                    return after[1:end]
            # Fallback: take everything up to " [ref="
            ref = after.find(" [ref=")
            if ref > 0:
                return after[:ref].strip(' "')
    # 3. `# <title>` heading (older inlined snapshot)
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


__all__ = ["register"]
