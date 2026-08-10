"""Tests for `library.render_archive` — archive fetched HTML sources as PDF
(preferred) or image (fallback), never persisting pdf_render_pages output."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deep_research.config import AgentTopConfig
from deep_research.library import render_archive as ra
from deep_research.library.writer import LibraryWriter, NullLibraryWriter


@pytest.fixture
async def writer(tmp_path):
    from deep_research.library.storage.sqlite_backend import SqliteStorageBackend

    root = tmp_path
    backend = SqliteStorageBackend(db_path=str(root / "index.db"))
    await backend.connect()
    w = LibraryWriter(backend, str(root))
    yield w
    await backend.close()


def _cfg() -> AgentTopConfig:
    return AgentTopConfig()


async def test_null_writer_is_noop() -> None:
    res = await ra.archive_html_source(
        "https://blog.test/post",
        "article text",
        tools=MagicMock(),
        config=_cfg(),
        writer=NullLibraryWriter(),
    )
    assert res == ""


@pytest.mark.asyncio
async def test_pdf_render_archives_kind_pdf(writer, monkeypatch) -> None:
    """When HTML->PDF conversion succeeds, the source is archived as a PDF."""
    cfg = _cfg()
    pdf_bytes = b"%PDF-1.7 fake pdf bytes that are long enough"
    monkeypatch.setattr(ra, "_render_html_to_pdf", lambda html: pdf_bytes)
    tools = MagicMock()  # image fallback must not be reached
    aid = await ra.archive_html_source(
        "https://blog.test/post", "article text", tools=tools, config=cfg, writer=writer
    )
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art is not None
    assert art.kind == "pdf"
    assert art.source_type == "html"
    assert (writer.root_dir / art.bytes_path).read_bytes() == pdf_bytes


class _FakeBrowserTools:
    def __init__(self, screenshot_content: str, nav_error: str | None = None):
        self.calls: list[str] = []
        self._content = screenshot_content
        self._nav_error = nav_error

    def names(self) -> list[str]:
        return ["browser_navigate", "browser_take_screenshot"]

    async def call(self, name: str, arguments: dict | None = None):
        self.calls.append(name)
        if name == "browser_navigate":
            return SimpleNamespace(
                error=self._nav_error, content="nav ok", citations=[]
            )
        return SimpleNamespace(error=None, content=self._content, citations=[])


@pytest.mark.asyncio
async def test_image_fallback_archives_kind_image(writer, monkeypatch) -> None:
    """When PDF render is unusable but a browser screenshot works, archive image."""
    cfg = _cfg()
    monkeypatch.setattr(ra, "_render_html_to_pdf", lambda html: None)
    png = b"\x89PNG\r\n\x1a\n" + b"page screenshot"
    tools = _FakeBrowserTools(base64.b64encode(png).decode())
    aid = await ra.archive_html_source(
        "https://blog.test/post", "article text", tools=tools, config=cfg, writer=writer
    )
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art is not None
    assert art.kind == "image"
    assert art.source_type == "html"
    assert (writer.root_dir / art.bytes_path).read_bytes() == png
    # The browser navigated then screenshotted
    assert tools.calls == ["browser_navigate", "browser_take_screenshot"]


@pytest.mark.asyncio
async def test_image_fallback_browser_disabled_uses_plain_html(writer, monkeypatch) -> None:
    cfg = _cfg()
    cfg.browser.enabled = False
    monkeypatch.setattr(ra, "_render_html_to_pdf", lambda html: None)
    aid = await ra.archive_html_source(
        "https://blog.test/post",
        "article text",
        tools=MagicMock(),
        config=cfg,
        writer=writer,
    )
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art is not None
    assert art.kind == "html"
    assert art.source_url == "https://blog.test/post"


@pytest.mark.asyncio
async def test_all_fallbacks_fail_uses_plain_html(writer, monkeypatch) -> None:
    """PDF unusable + screenshot error -> the fetched HTML text is still archived."""
    cfg = _cfg()
    monkeypatch.setattr(ra, "_render_html_to_pdf", lambda html: None)
    tools = _FakeBrowserTools("not-base64!!!")
    aid = await ra.archive_html_source(
        "https://blog.test/post", "article text", tools=tools, config=cfg, writer=writer
    )
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art is not None
    assert art.kind == "html"


@pytest.mark.asyncio
async def test_pdf_flag_disabled_skips_pdf_render(writer, monkeypatch) -> None:
    """archive_html_as_pdf=False skips the renderer even when it would succeed."""
    cfg = _cfg()
    cfg.pdl.archive_html_as_pdf = False
    cfg.pdl.archive_html_image_fallback = False
    called = False

    def _spy(html: str):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(ra, "_render_html_to_pdf", _spy)
    aid = await ra.archive_html_source(
        "https://blog.test/post",
        "article text",
        tools=MagicMock(),
        config=cfg,
        writer=writer,
    )
    assert not called
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art.kind == "html"


# ---------------------------------------------------------------------------
# Real weasyprint renderer (integration) — exercises the actual conversion
# ---------------------------------------------------------------------------


def test_render_html_to_pdf_short_text_rejected() -> None:
    assert ra._render_html_to_pdf("") is None


def test_render_html_to_pdf_long_text_returns_valid_pdf() -> None:
    html = (
        "<html><body><h1>Title</h1>"
        "<p>Substantial article content that is comfortably above the minimum "
        "extractable-text threshold so the render is accepted.</p></body></html>"
    )
    data = ra._render_html_to_pdf(html)
    assert data is not None
    assert data.startswith(b"%PDF")

    import io

    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 1


def test_render_html_to_pdf_weasyprint_import_failure(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _block_weasyprint(name, *args, **kwargs):
        if name == "weasyprint":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_weasyprint)
    assert ra._render_html_to_pdf("x") is None


def test_render_html_to_pdf_weasyprint_render_failure(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    class _BoomHTML:
        def __init__(self, string):
            raise RuntimeError("render exploded")

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_BoomHTML))
    assert ra._render_html_to_pdf("x") is None


def test_render_html_to_pdf_non_pdf_output_rejected(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    class _NotPDF:
        def __init__(self, string):
            self._s = string

        def write_pdf(self, buf):
            buf.write(b"NOT A PDF")

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_NotPDF))
    assert ra._render_html_to_pdf("x") is None


def test_render_html_to_pdf_zero_pages_rejected(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    class _NoPagesHTML:
        def __init__(self, string):
            self._s = string

        def write_pdf(self, buf):
            buf.write(b"%PDF-1.7 " + b"0" * 600)

    class _Reader:
        def __init__(self, stream):
            self.pages = []

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_NoPagesHTML))
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=_Reader))
    assert ra._render_html_to_pdf("x") is None


def test_render_html_to_pdf_pypdf_parse_failure(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    class _OKHTML:
        def __init__(self, string):
            self._s = string

        def write_pdf(self, buf):
            buf.write(b"%PDF-1.7 " + b"0" * 600)

    class _BadReader:
        def __init__(self, stream):
            raise ValueError("unparseable")

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_OKHTML))
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=_BadReader))
    assert ra._render_html_to_pdf("x") is None


class _CaptureFake:
    def __init__(self, names, nav_error=None, shot_error=None, content="cGljcw=="):
        self._names = names
        self._nav_error = nav_error
        self._shot_error = shot_error
        self._content = content

    def names(self):
        return self._names

    async def call(self, name, arguments=None):
        if name == "browser_navigate":
            return SimpleNamespace(error=self._nav_error, content="ok")
        return SimpleNamespace(error=self._shot_error, content=self._content)


@pytest.mark.asyncio
async def test_capture_browser_disabled_returns_none() -> None:
    cfg = _cfg()
    cfg.browser.enabled = False
    assert await ra._capture_page_image("https://x", MagicMock(), cfg) is None


@pytest.mark.asyncio
async def test_capture_tools_missing_returns_none() -> None:
    cfg = _cfg()
    tools = _CaptureFake(["browser_navigate"])  # no screenshot tool
    assert await ra._capture_page_image("https://x", tools, cfg) is None


@pytest.mark.asyncio
async def test_capture_navigate_error_returns_none() -> None:
    cfg = _cfg()
    tools = _CaptureFake(
        ["browser_navigate", "browser_take_screenshot"], nav_error="BLOCKED:bot"
    )
    assert await ra._capture_page_image("https://x", tools, cfg) is None


@pytest.mark.asyncio
async def test_capture_screenshot_error_returns_none() -> None:
    cfg = _cfg()
    tools = _CaptureFake(
        ["browser_navigate", "browser_take_screenshot"], shot_error="screenshot failed"
    )
    assert await ra._capture_page_image("https://x", tools, cfg) is None
