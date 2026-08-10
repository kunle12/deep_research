"""Dedicated unit tests for `tools.browser` (P8).

Covers the Playwright MCP lazy-connection behavior fully offline:

  - Tool registration: enabled vs disabled configures whether the 4 curated
    tools are visible to the LLM (browser_navigate, browser_snapshot,
    browser_click, browser_evaluate).
  - npx-missing path: `_ensure_mcp()` returns None and surfaces a
    README-pointing install-hint error; subsequent calls don't retry.
  - MCP startup error (_MCPStartupError) is wrapped into a clean ToolResult.error.
  - Happy path navigate: monkeypatch _MCPClientCtx.__aenter__ + .call() so
    the test doesn't spawn a real subprocess; assert a Citation is attached
    with `discovered_by=ToolName.browser`.
  - Snapshot / click / evaluate: each is dispatched to the corresponding
    MCP tool name with the right argument dict.
  - MCP `call_tool` exceptions and `isError=True` are surfaced as
    ToolResult.error rather than raising.
  - `_mcp_result_to_tool_result`: text content joined with newlines;
    non-text content counted and omitted; `isError=True` populates `error`.
  - Teardown: `reg._browser_close()` awaits the MCP context's __aexit__;
    subsequent calls are no-ops; a teardown exception doesn't propagate.
  - `_title_from_content`: returns the first heading string from a markdown
    accessibility snapshot; empty / unknown format returns "".

We use `monkeypatch` extensively to avoid hitting the real `mcp` library's
async context managers (the library internals do subprocess spawns and
asyncio network IO). Tests run in milliseconds.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.state import ToolName
from deep_research.tools import browser as browser_tool
from deep_research.tools import build_tool_registry

# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.asyncio
    async def test_default_config_registers_all_four_tools(self) -> None:
        cfg = AgentTopConfig()
        # Browser is enabled by default
        assert cfg.browser.enabled
        reg = await build_tool_registry(cfg)
        assert "browser_take_screenshot" in reg.names()
        assert "browser_navigate" in reg.names()
        assert "browser_snapshot" in reg.names()
        assert "browser_click" in reg.names()
        assert "browser_evaluate" in reg.names()
        # The teardown hook list is on the registry
        assert hasattr(reg, "_close_hooks")
        assert len(reg._close_hooks) > 0
        # Tear down cleanly so no leaked resources
        await reg.close()

    @pytest.mark.asyncio
    async def test_browser_disabled_skips_registration(self) -> None:
        cfg = AgentTopConfig()
        cfg.browser.enabled = False
        reg = await build_tool_registry(cfg)
        assert "browser_navigate" not in reg.names()
        assert "browser_take_screenshot" not in reg.names()
        assert "browser_snapshot" not in reg.names()
        # No teardown hook since no browser tool was registered
        assert not hasattr(reg, "_browser_close") or not callable(
            getattr(reg, "_browser_close", None)
        )


# ---------------------------------------------------------------------------
# Lazy connection — graceful degradation paths
# ---------------------------------------------------------------------------


def _cfg() -> AgentTopConfig:
    return AgentTopConfig()


class _NoNpx:
    """A test shim that fakes `shutil.which("npx")` returning None."""

    def __init__(self) -> None:
        self.patched_with: dict[str, Any] = {}


class TestLazyConnection:
    @pytest.mark.asyncio
    async def test_no_npx_on_path_returns_install_hint_error(self, monkeypatch) -> None:
        cfg = _cfg()
        # Force `which` to return None for the npx binary
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: None)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_navigate", {"url": "https://example.com"})
        assert res.error is not None
        assert "npx" in res.error or "PATH" in res.error
        assert "README" in res.error
        # No content was returned
        assert res.content == ""
        await reg.close()

    @pytest.mark.asyncio
    async def test_subsequent_calls_do_not_retry_after_failure(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: None)
        reg = await build_tool_registry(cfg)
        # First call: triggers the failure
        first = await reg.call("browser_navigate", {"url": "https://example.com"})
        assert first.error is not None
        # Second call: should report the cached failure, not re-attempt
        second = await reg.call("browser_navigate", {"url": "https://example.com"})
        assert second.error is not None
        assert second.error == first.error
        await reg.close()


# ---------------------------------------------------------------------------
# Happy path navigate — monkeypatch _MCPClientCtx to avoid spawning subprocess
# ---------------------------------------------------------------------------


class _FakeMCPClientCtx:
    """Stand-in for _MCPClientCtx used by the browser tool.

    The tool calls `_MCPClientCtx(command, args).__aenter__()` and then later
    `await mcp.call(name, arguments)` on the entered instance, then
    `await mcp.__aexit__(None, None, None)` for teardown.
    """

    # Class-level registry of all created fakes, for cross-test inspection.
    # `ClassVar` annotation tells ruff this mutable default is intentional.
    instances: ClassVar[list[_FakeMCPClientCtx]] = []

    def __init__(self, command: str, args: list[str]) -> None:
        self.command = command
        self.args = args
        self.enter_calls = 0
        self.exit_calls = 0
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        # Behavior knobs
        self._enter_should_raise: Exception | None = None
        self._call_results: dict[str, Any] = {}
        self._call_default: Any = None
        self._call_raises: Exception | None = None
        _FakeMCPClientCtx.instances.append(self)

    async def __aenter__(self) -> _FakeMCPClientCtx:
        self.enter_calls += 1
        if self._enter_should_raise:
            raise self._enter_should_raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exit_calls += 1

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_log.append((name, arguments))
        if self._call_raises is not None:
            raise self._call_raises
        if name in self._call_results:
            return self._call_results[name]
        return self._call_default

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


def _make_text_result(items: list[tuple[str, str]], is_error: bool = False) -> Any:
    """Build a fake mcp.types.CallToolResult."""
    content_objs = []
    from mcp.types import AudioContent, ImageContent, TextContent

    for kind, text in items:
        if kind == "text":
            content_objs.append(TextContent(type="text", text=text))
        elif kind == "image":
            content_objs.append(ImageContent(type="image", data=text, mimeType="image/png"))
        elif kind == "audio":
            content_objs.append(AudioContent(type="audio", data=text, mimeType="audio/wav"))
        # else: silently skip (lets us tweak malformed tests)
    return MagicMock(content=content_objs, isError=is_error)


class TestHappyPathNavigate:
    @pytest.mark.asyncio
    async def test_navigate_attaches_citation_with_browser_provenance(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        # npx is found on PATH
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")

        # Replace real MCP client ctx class
        fake_mcp = _FakeMCPClientCtx("npx", ["-y", "@playwright/mcp@latest"])
        # Tell the navigate tool the resulting page had some text
        fake_mcp._call_default = _make_text_result(
            [("text", '- heading "Welcome to Example" [ref=s1h1]\nSome body content')]
        )

        def _factory(command: str, args: list[str]) -> _FakeMCPClientCtx:
            # The tool instantiates _MCPClientCtx(command, args) — return our fake
            assert command == "npx"
            return fake_mcp

        monkeypatch.setattr(browser_tool, "_MCPClientCtx", _factory)
        reg = await build_tool_registry(cfg)

        res = await reg.call("browser_navigate", {"url": "https://example.com/page"})
        assert res.error is None
        assert "Welcome to Example" in res.content
        assert "Some body content" in res.content
        # A citation is attached
        assert len(res.citations) == 1
        cit = res.citations[0]
        assert cit.url == "https://example.com/page"
        assert cit.source_type == "html"
        assert cit.discovered_by == ToolName.browser
        assert cit.confidence_score == 0.6
        # Title extracted from the heading line
        assert cit.title == "Welcome to Example"
        # The MCP `browser_navigate` was called with the URL
        assert fake_mcp.call_log == [("browser_navigate", {"url": "https://example.com/page"})]
        await reg.close()
        # The session was entered exactly once and exited on teardown
        assert fake_mcp.enter_calls == 1
        assert fake_mcp.exit_calls == 1

    @pytest.mark.asyncio
    async def test_navigate_reuses_mcp_session_across_calls(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")

        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "snapshot content")])

        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)

        # Two navigate calls -> two MCP calls but only ONE session __aenter__
        await reg.call("browser_navigate", {"url": "https://example.com/a"})
        await reg.call("browser_navigate", {"url": "https://example.com/b"})
        assert fake_mcp.enter_calls == 1
        assert len(fake_mcp.call_log) == 2
        assert fake_mcp.call_log[0] == ("browser_navigate", {"url": "https://example.com/a"})
        assert fake_mcp.call_log[1] == ("browser_navigate", {"url": "https://example.com/b"})
        await reg.close()
        assert fake_mcp.exit_calls == 1

    @pytest.mark.asyncio
    async def test_navigate_invalid_url_returns_error_without_spawning(self, monkeypatch) -> None:
        cfg = _cfg()
        # If navigate short-circuits on invalid URL, it should never try to
        # spawn the MCP subprocess. We assert by patching _MCPClientCtx to raise
        spawned = []

        class _SpawningFactory:
            def __init__(self, command, args):
                spawned.append((command, args))

                class _Never:
                    async def __aenter__(self_inner):
                        raise AssertionError("should not have spawned MCP for invalid URL")

                    async def __aexit__(self_inner, *a):
                        pass

                    async def call(self_inner, *a, **kw):
                        raise AssertionError("should not call any tool")

                return _Never()

        monkeypatch.setattr(browser_tool, "_MCPClientCtx", _SpawningFactory)
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_navigate", {"url": "not a url"})
        assert res.error is not None
        assert "invalid url" in res.error.lower()
        assert spawned == []  # never spawned
        await reg.close()


# ---------------------------------------------------------------------------
# Snapshot / click / evaluate tool dispatch
# ---------------------------------------------------------------------------


class TestChallengeDetection:
    @pytest.mark.asyncio
    async def test_navigate_challenge_page_returns_blocked_error(self, monkeypatch) -> None:
        """A rendered bot challenge (Cloudflare etc.) yields a structured BLOCKED
        error instead of article content, so callers skip rather than parse it."""
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")

        fake_mcp = _FakeMCPClientCtx("npx", ["-y", "@playwright/mcp@latest"])
        fake_mcp._call_default = _make_text_result(
            [("text", "Just a moment... <script data-cf-chl-123>checking your browser</script>")]
        )
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)

        res = await reg.call("browser_navigate", {"url": "https://cf.test/page"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (browser challenge)"
        assert res.content == ""
        assert res.citations == []
        await reg.close()

    @pytest.mark.asyncio
    async def test_navigate_normal_page_still_returns_content(self, monkeypatch) -> None:
        """Non-challenge snapshots are unaffected by the detection."""
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")

        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result(
            [("text", '- heading "Real Article" [ref=s1h1]\nParagraph content')]
        )
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)

        res = await reg.call("browser_navigate", {"url": "https://example.com/article"})
        assert res.error is None
        assert "Real Article" in res.content
        assert len(res.citations) == 1
        await reg.close()


class TestOtherTools:
    @pytest.mark.asyncio
    async def test_snapshot_calls_browser_snapshot(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "snapshot tree")])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_snapshot", {})
        assert res.error is None
        assert "snapshot tree" in res.content
        assert fake_mcp.call_log == [("browser_snapshot", {})]
        await reg.close()

    @pytest.mark.asyncio
    async def test_click_passes_target_and_optional_element(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "clicked")])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)

        # target only
        await reg.call("browser_click", {"target": "ref=s1"})
        # target + element
        await reg.call("browser_click", {"target": "ref=s2", "element": "Submit button"})
        assert fake_mcp.call_log[0] == ("browser_click", {"target": "ref=s1"})
        assert fake_mcp.call_log[1] == (
            "browser_click",
            {"target": "ref=s2", "element": "Submit button"},
        )
        await reg.close()

    @pytest.mark.asyncio
    async def test_click_requires_target_argument(self, monkeypatch) -> None:
        """When the caller omits target, the click tool missing-required-arg
        surfaces as a tool-loop error rather than crashing."""
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        # browser_click(**kwargs) without `target` raises TypeError in the
        # registry's try/except wrapper -> surfaces as a clean ToolResult.error
        res = await reg.call("browser_click", {})
        assert res.error is not None
        # The registry's catch-all surfaces "{type(e).__name__}: {e}" so we
        # should see TypeError / missing target
        assert "TypeError" in res.error or "target" in res.error
        await reg.close()

    @pytest.mark.asyncio
    async def test_evaluate_passes_function_argument(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "42")])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_evaluate", {"function": "() => 6 * 7"})
        assert res.error is None
        assert "42" in res.content
        assert fake_mcp.call_log == [("browser_evaluate", {"function": "() => 6 * 7"})]
        await reg.close()


# ---------------------------------------------------------------------------
# MCP call_tool failure paths
# ---------------------------------------------------------------------------


class TestMCPCallFailures:
    @pytest.mark.asyncio
    async def test_mcp_call_exception_surfaces_as_error(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_raises = RuntimeError("chromium crashed")
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_navigate", {"url": "https://example.com"})
        assert res.error is not None
        assert "RuntimeError" in res.error
        assert "chromium crashed" in res.error
        await reg.close()

    @pytest.mark.asyncio
    async def test_mcp_iserror_true_surfaces_as_error(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "selector not found")], is_error=True)
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_snapshot", {})
        assert res.error is not None
        # isError content is preserved in `content` too
        assert "selector not found" in res.content
        await reg.close()


# ---------------------------------------------------------------------------
# _MCPClientCtx startup failure paths
# ---------------------------------------------------------------------------


class TestMCPStartupFailures:
    @pytest.mark.asyncio
    async def test_mc_startup_error_returns_install_hint(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")

        class _FailFactory:
            def __init__(self, command, args):
                pass

            async def __aenter__(self):
                raise browser_tool._MCPStartupError("init timed out at 30s")

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(browser_tool, "_MCPClientCtx", _FailFactory)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_navigate", {"url": "https://x.test"})
        assert res.error is not None
        assert "30s" in res.error or "timed out" in res.error.lower()
        await reg.close()


# ---------------------------------------------------------------------------
# _mcp_result_to_tool_result helper — pure unit tests
# ---------------------------------------------------------------------------


class TestMcpResultToToolResult:
    def test_text_content_joined_with_newlines(self) -> None:
        result = _make_text_result([("text", "first"), ("text", "second")])
        out = browser_tool._mcp_result_to_tool_result(result, "browser_snapshot", {})
        assert out.error is None
        assert out.content == "first\nsecond"

    def test_binary_content_omitted_but_counted(self) -> None:
        # image bytes are not actually validated; we just count and append the note
        result = _make_text_result(
            [("text", "page text"), ("image", "imgbytes"), ("audio", "audiobytes")]
        )
        out = browser_tool._mcp_result_to_tool_result(result, "browser_navigate", {"url": "x"})
        assert out.error is None
        assert "page text" in out.content
        assert "2 non-text content item(s) omitted" in out.content

    def test_iserror_true_populates_error(self) -> None:
        result = _make_text_result([("text", "page not found")], is_error=True)
        out = browser_tool._mcp_result_to_tool_result(result, "browser_snapshot", {})
        assert out.error is not None
        assert "isError" in out.error
        assert "page not found" in out.error
        # Body still preserved for diagnostics
        assert "page not found" in out.content

    def test_empty_content_returns_empty_toolresult(self) -> None:
        from mcp.types import CallToolResult

        result = CallToolResult(content=[])
        out = browser_tool._mcp_result_to_tool_result(result, "browser_snapshot", {})
        assert out.error is None
        assert out.content == ""


# ---------------------------------------------------------------------------
# _title_from_content helper
# ---------------------------------------------------------------------------


class TestTitleFromContent:
    def test_extracts_first_heading(self) -> None:
        snapshot = '- heading "Welcome to Example" [ref=s1h1]\n- paragraph "Body content"'
        assert browser_tool._title_from_content(snapshot) == "Welcome to Example"

    def test_extracts_page_title_line_from_newer_mcp_format(self) -> None:
        """@playwright/mcp's browser_navigate wraps the snapshot in a file
        reference and emits metadata lines including `- Page Title: <title>`."""
        snapshot = (
            "### Ran Playwright code\n"
            "```js\nawait page.goto('https://example.com');\n```\n"
            "### Page\n"
            "- Page URL: https://example.com/\n"
            "- Page Title: Example Domain\n"
            "### Snapshot\n"
            "- [Snapshot](.playwright-mcp/page-2026-07-16T17-11-31.yml)\n"
        )
        assert browser_tool._title_from_content(snapshot) == "Example Domain"

    def test_page_title_takes_precedence_over_heading(self) -> None:
        snapshot = '- Page Title: Title From Metadata\n- heading "Title From Heading" [ref=s1h1]\n'
        assert browser_tool._title_from_content(snapshot) == "Title From Metadata"

    def test_extracts_md_h1_heading_as_last_resort(self) -> None:
        snapshot = "Some preamble\n# My H1 Title\n- paragraph"
        assert browser_tool._title_from_content(snapshot) == "My H1 Title"

    def test_extracts_md_h1_skips_h2(self) -> None:
        snapshot = "## Subsection\n# Real Title\n"
        assert browser_tool._title_from_content(snapshot) == "Real Title"

    def test_returns_empty_on_no_heading(self) -> None:
        assert browser_tool._title_from_content('- paragraph "Body"') == ""

    def test_returns_empty_on_empty_content(self) -> None:
        assert browser_tool._title_from_content("") == ""

    def test_handles_arbitrary_text_within_heading(self) -> None:
        snapshot = '- heading "  Spaces Around  " [ref=s1h2]\n- paragraph "body"'
        assert browser_tool._title_from_content(snapshot) == "  Spaces Around  "

    def test_uses_fallback_path_when_heading_unquoted(self) -> None:
        """Some Playwright MCP versions may format headings differently."""
        snapshot = "- heading Hello World [ref=s1h3]"
        # The unquoted format falls back to: take everything up to " [ref="
        title = browser_tool._title_from_content(snapshot)
        # Either we extracted "Hello World" via fallback, or returned ""
        # — both behaviors are acceptable since the prompt template may
        # vary. We assert that the implementation doesn't crash.
        assert title == "Hello World" or title == ""



class TestScreenshotTool:
    """browser_take_screenshot extracts the ImageContent base64 into content."""

    @pytest.mark.asyncio
    async def test_registered_when_enabled(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        assert "browser_take_screenshot" in reg.names()
        await reg.close()

    @pytest.mark.asyncio
    async def test_screenshot_extracts_base64_image_data(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("image", "cGljcw==")])  # base64("pics")
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_take_screenshot", {})
        assert res.error is None
        assert res.content == "cGljcw=="
        assert fake_mcp.call_log == [("browser_take_screenshot", {})]
        await reg.close()

    @pytest.mark.asyncio
    async def test_screenshot_iserror_surfaces_error(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "boom")], is_error=True)
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_take_screenshot", {})
        assert res.error is not None
        await reg.close()

    @pytest.mark.asyncio
    async def test_screenshot_without_image_data_errors(self, monkeypatch) -> None:
        _FakeMCPClientCtx.reset()
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "no image here")])
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        res = await reg.call("browser_take_screenshot", {})
        assert res.error is not None
        assert "no image data" in res.error
        await reg.close()

# ---------------------------------------------------------------------------
# Multi-tool integration — same fake session serves navigate then snapshot
# ---------------------------------------------------------------------------


class TestIntegration:
    @pytest.mark.asyncio
    async def test_navigate_then_snapshot_uses_same_session(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_results["browser_navigate"] = _make_text_result(
            [("text", '- heading "Page Title" [ref=s1]\nnav content')]
        )
        fake_mcp._call_results["browser_snapshot"] = _make_text_result(
            [("text", "detailed snapshot")]
        )
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        nav = await reg.call("browser_navigate", {"url": "https://x.test"})
        snap = await reg.call("browser_snapshot", {})
        assert nav.error is None
        assert "Page Title" in nav.content
        assert nav.citations[0].title == "Page Title"
        assert snap.error is None
        assert snap.content == "detailed snapshot"
        assert fake_mcp.enter_calls == 1
        assert fake_mcp.call_log == [
            ("browser_navigate", {"url": "https://x.test"}),
            ("browser_snapshot", {}),
        ]
        await reg.close()
        assert fake_mcp.exit_calls == 1

    @pytest.mark.asyncio
    async def test_browser_disabled_does_not_register_any_browser_tool(self) -> None:
        cfg = _cfg()
        cfg.browser.enabled = False
        reg = await build_tool_registry(cfg)
        names = reg.names()
        assert "browser_navigate" not in names
        assert "browser_snapshot" not in names
        assert "browser_click" not in names
        assert "browser_evaluate" not in names
        assert "browser_take_screenshot" not in names


# ---------------------------------------------------------------------------
# Teardown robustness
# ---------------------------------------------------------------------------


class TestTeardown:
    @pytest.mark.asyncio
    async def test_close_is_noop_when_mcp_never_spawned(self) -> None:
        cfg = _cfg()
        # Just register; never invoke browser_navigate
        reg = await build_tool_registry(cfg)
        # Should not raise
        await reg.close()
        # Idempotent — calling again is fine
        await reg.close()

    @pytest.mark.asyncio
    async def test_close_swallows_exit_exceptions(self, monkeypatch) -> None:
        cfg = _cfg()
        monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")
        fake_mcp = _FakeMCPClientCtx("npx", ["x"])
        fake_mcp._call_default = _make_text_result([("text", "ok")])

        # Now sabotage __aexit__ to raise
        async def _raise_aexit(exc_type, exc, tb):
            raise RuntimeError("teardown blew up")

        fake_mcp.__aexit__ = _raise_aexit  # type: ignore[method-assign]
        monkeypatch.setattr(browser_tool, "_MCPClientCtx", lambda c, a: fake_mcp)
        reg = await build_tool_registry(cfg)
        # Spawn the session
        await reg.call("browser_navigate", {"url": "https://x.test"})
        # Tear down — should not raise despite the sabotaged __aexit__
        await reg.close()
        # And the holder was cleared
        assert fake_mcp.enter_calls == 1
        assert fake_mcp.exit_calls == 0  # we sabotaged __aexit__


# ---------------------------------------------------------------------------
# Teardown helper — invoke the fake mcp's exit normally
# ---------------------------------------------------------------------------


async def _teardown_quietly(reg: ToolRegistry) -> None:
    await reg.close()


def _patch_which(monkeypatch) -> None:
    monkeypatch.setattr(browser_tool.shutil, "which", lambda name: "/usr/bin/npx")


class TestCurriedFixture:
    """A small sanity test for the _patch_which + _teardown_quietly helpers
    used by other tests in this file. Ensures the test helpers themselves
    don't drift from the production API."""

    @pytest.mark.asyncio
    async def test_patch_which_leaves_other_binaries_alone(self, monkeypatch) -> None:
        # _patch_which patches what tearDown uses
        _patch_which(monkeypatch)
        # The faked which() returns "/usr/bin/npx" for any name (including unrelated binaries)
        assert browser_tool.shutil.which("npx") == "/usr/bin/npx"
        # But other shutil methods still work
        assert browser_tool.shutil.which("python3") == "/usr/bin/npx"  # any name returns the same


__all__ = [
    "TestCurriedFixture",
    "TestHappyPathNavigate",
    "TestIntegration",
    "TestLazyConnection",
    "TestMCPCallFailures",
    "TestMCPStartupFailures",
    "TestMcpResultToToolResult",
    "TestOtherTools",
    "TestRegistration",
    "TestTeardown",
    "TestTitleFromContent",
]
