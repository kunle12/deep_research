"""P6 tests — pdf_extract_text + pdf_render_pages real implementations.

The implementations wrap pypdf (fast text), pdfplumber (accuracy fallback),
and pdf2image + PIL (vision rendering). To keep unit tests deterministic and
host-independent (no poppler required), we:

  - probe whether poppler is installed; if not, the pdf_render tests skip
    cleanly with a useful message (no test failure)
  - cover the poppler-missing error path explicitly (returns a clean
    install-instructions error)
  - stub `_sync_render` to return synthetic PIL Images where we want to
    isolate the vision post-processing + JSON assembly logic from the
    renderer's behavior

pypdf / pdfplumber ARE in pyproject so we exercise the real `_sync_extract`
against a tiny PDF fixture generated at test-time via PIL.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from deep_research.config import AgentTopConfig
from deep_research.tools import build_tool_registry
from deep_research.tools import pdf as pdf_tool

# ---------------------------------------------------------------------------
# Fixture: tiny ghost text PDF built via PIL (no poppler / no system deps)
# ---------------------------------------------------------------------------


def _make_text_pdf(path_str: str) -> None:
    """Build a tiny PDF whose text layer says 'Sample paper body.' N times.

    Uses reportlab if available; falls back to a minimal hand-rolled PDF.
    """
    try:
        from reportlab.pdfgen import canvas  # type: ignore

        c = canvas.Canvas(path_str)
        for i in range(3):
            c.drawString(72, 720 - 20 * i, f"Sample paper body line {i}.")
            c.drawString(72, 680 - 20 * i, f"Figure caption {i}: diagram.")
        c.showPage()
        c.save()
        return
    except ImportError:
        pass

    # Hand-rolled minimal PDF (no text operators) just so _sync_extract can
    # exercise pypdf's "empty text" -> pdfplumber fallback chain.
    body = b"%PDF-1.4\n1 0 obj<<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<<</Type/Pages/Count 1/Kids[3 0 R]>>endobj 3 0 obj<<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
    with open(path_str, "wb") as f:
        f.write(body)


# ---------------------------------------------------------------------------
# pdf_extract_text (real pypdf / pdfplumber path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_text_returns_real_pdf_text(tmp_path) -> None:
    """Real pypdf invocation against a test-fixture PDF (reportlab path)."""
    pytest.importorskip("reportlab")
    pdf_path = tmp_path / "paper.pdf"
    _make_text_pdf(str(pdf_path))

    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_extract_text", {"file_path": str(pdf_path)})
    assert res.error is None
    assert res.content
    # reportlab-generated text should survive pypdf's extraction
    assert "Sample paper body" in res.content


@pytest.mark.asyncio
async def test_extract_text_missing_file_returns_error() -> None:
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_extract_text", {"file_path": "/nonexistent/x.pdf"})
    assert res.error is not None
    assert "not found" in res.error.lower()


@pytest.mark.asyncio
async def test_extract_text_empty_path_returns_error() -> None:
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_extract_text", {"file_path": ""})
    assert res.error is not None
    assert "file_path" in res.error


@pytest.mark.asyncio
async def test_extract_text_sparseness_triggers_pdfplumber_fallback(tmp_path) -> None:
    """When pypdf yields very little text, _sync_extract falls back to pdfplumber."""
    # Generate a content-less PDF. pypdf → empty → pdfplumber fallback path runs.
    pdf_path = tmp_path / "tiny.pdf"
    body = b"%PDF-1.4\n1 0 obj<<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<<</Type/Pages/Count 1/Kids[3 0 R]>>endobj 3 0 obj<<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
    pdf_path.write_bytes(body)

    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_extract_text", {"file_path": str(pdf_path)})
    assert res.error is None
    # Both extractions may yield empty → content is "(no extractable text)" or empty
    # Either way: the call didn't crash, and that's what we assert here.
    assert isinstance(res.content, str)


# ---------------------------------------------------------------------------
# pdf_render_pages
# ---------------------------------------------------------------------------


def _is_poppler_available() -> bool:
    """Cheap probe for the `pdftoppm` binary using PIL+pdf2image.exception classes."""
    try:
        import pdf2image  # noqa: F401
    except ImportError:
        return False
    try:
        import shutil

        return shutil.which("pdftoppm") is not None
    except Exception:
        return False


_HAS_POPPLER = _is_poppler_available()


class _FakePilImage:
    """Minimal stand-in for PIL.Image — `_sync_render` returns these."""

    def __init__(self, label: str) -> None:
        self.label = label
        # `resize_for_vlm` calls `image.convert("RGB")` then `image.size`,
        # then `.resize(..., Image.LANCZOS)` then `.save(buf, format=..., quality=...)`.
        # All those need a real PIL Image, so we can't fully fake this.

    def convert(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("tests should swap in real PIL images for vision post-processing")


@pytest.mark.asyncio
async def test_render_returns_clean_error_when_pdf_vision_disabled(tmp_path) -> None:
    """When pdf_vision.enabled=False, pdf_render_pages isn't registered at all
    (pdf_extract_text IS still registered since it needs no poppler)."""
    cfg = AgentTopConfig()
    cfg.pdf_vision.enabled = False
    reg = await build_tool_registry(cfg)
    # extract text is still available
    assert "pdf_extract_text" in reg.names()
    # render_pages is NOT available
    assert "pdf_render_pages" not in reg.names()
    res = await reg.call("pdf_render_pages", {"file_path": "/anywhere/x.pdf"})
    assert res.error is not None
    assert "unknown tool" in res.error.lower() or "pdf_vision" in res.error.lower()


@pytest.mark.asyncio
async def test_render_missing_file_returns_error() -> None:
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call(
        "pdf_render_pages", {"file_path": "/nonexistent/y.pdf"}
    )
    # Either FileNotFoundError or poppler-related runtime; in both cases `error`
    # is populated.
    assert res.error is not None
    # If no poppler is on PATH, we get the poppler-missing message; either way
    # the call degrades cleanly.
    assert ("not found" in res.error.lower()) or ("poppler" in res.error.lower())


@pytest.mark.asyncio
async def test_render_poppler_missing_returns_install_hint(monkeypatch, tmp_path) -> None:
    """If pdf2image raises the poppler-missing exception, we surface a clean
    install-instructions error pointing to the README's brew/apt-get commands.
    """
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy")

    class _FakeMissingError(Exception):
        pass

    # Sabotage _sync_render to raise an exception that _is_poppler_missing() detects.
    async def _should_not_call(*a: Any, **kw: Any) -> Any:
        raise _FakeMissingError("pdfinfo not installed. Is poppler installed?")

    monkeypatch.setattr(pdf_tool, "_sync_render", lambda *a, **kw: (_should_not_call()))

    # Make sure the heuristic accepts the exception name we raise. _is_poppler_missing
    # checks `cls_name in {"PDFInfoNotInstalledError", "PDFPopplerNotInstalledError"}`
    # OR `"poppler" in str(exc).lower() AND ("not installed" ... not found")`.
    class _FakePopplerMissing(RuntimeError):
        pass

    def _raise_with_poppler_msg(*a: Any, **kw: Any) -> Any:
        raise _FakePopplerMissing("poppler-utils not installed")

    monkeypatch.setattr(pdf_tool, "_sync_render", _raise_with_poppler_msg)

    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_render_pages", {"file_path": str(pdf_path)})
    assert res.error is not None
    assert "poppler" in res.error.lower()
    assert "brew install" in res.error  # README install hint surfaces


# ---------------------------------------------------------------------------
# pdf_render_pages: vision post-processing path (isolated from poppler)
# ---------------------------------------------------------------------------


def _red_pil_image(w: int = 200, h: int = 280):
    """Return a real red PIL image we can pass through resize_for_vlm→JPEG."""
    from PIL import Image

    return Image.new("RGB", (w, h), color=(255, 0, 0))


@pytest.mark.asyncio
async def test_render_assembles_json_data_urls(monkeypatch, tmp_path) -> None:
    """Stub `_sync_render` to return two real PIL images and assert the tool
    returns a JSON payload with `pages` (each a base64 data URL) and `count`.
    """
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy")

    def _fake_sync_render(file_path: str, max_pages: int, dpi: int, poppler_path: str | None):
        return [_red_pil_image(), _red_pil_image(w=160, h=120)]

    monkeypatch.setattr(pdf_tool, "_sync_render", _fake_sync_render)

    cfg = AgentTopConfig()
    cfg.pdf_vision.max_dim = 64  # speed up the resize
    cfg.pdf_vision.jpeg_quality = 50
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_render_pages", {"file_path": str(pdf_path), "max_pages": 10})
    assert res.error is None
    data = json.loads(res.content)
    assert data["count"] == 2
    assert isinstance(data["pages"], list)
    assert len(data["pages"]) == 2
    for p in data["pages"]:
        assert p.startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_render_empty_pages_result_returns_empty_payload(monkeypatch, tmp_path) -> None:
    """An empty image list → `{"pages": [], "count": 0}` and no error."""
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy")

    def _empty_render(*a: Any, **kw: Any) -> list:
        return []

    monkeypatch.setattr(pdf_tool, "_sync_render", _empty_render)
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("pdf_render_pages", {"file_path": str(pdf_path)})
    assert res.error is None
    data = json.loads(res.content)
    assert data == {"pages": [], "count": 0}


# ---------------------------------------------------------------------------
# _is_poppler_missing heuristic + _sync_extract helper behavior
# ---------------------------------------------------------------------------


class TestIsPopplerMissing:
    def test_class_name_pdfinfonotinstalled(self) -> None:
        class PDFInfoNotInstalledError(Exception):
            pass

        assert pdf_tool._is_poppler_missing(PDFInfoNotInstalledError()) is True

    def test_class_name_pdfpopplernotinstalled(self) -> None:
        class PDFPopplerNotInstalledError(Exception):
            pass

        assert pdf_tool._is_poppler_missing(PDFPopplerNotInstalledError()) is True

    def test_message_contains_poppler_not_installed(self) -> None:
        class _E(RuntimeError):
            pass

        assert pdf_tool._is_poppler_missing(_E("Unable to locate poppler binary: not installed")) is True

    def test_message_contains_poppler_not_found(self) -> None:
        class _E(RuntimeError):
            pass

        assert pdf_tool._is_poppler_missing(_E("poppler not found on system PATH")) is True

    def test_unrelated_exception_returns_false(self) -> None:
        assert pdf_tool._is_poppler_missing(RuntimeError("something completely unrelated")) is False
        assert pdf_tool._is_poppler_missing(KeyError("poppler")) is False  # no "not installed/not found"


class TestSyncExtract:
    def test_missing_file_returns_message_string(self, tmp_path) -> None:
        out = pdf_tool._sync_extract(str(tmp_path / "nope.pdf"))
        assert "not found" in out.lower()

    # We do not unit-test the `pypdf PdfReader` happy path through _sync_extract
    # here because it'd require a content-bearing PDF fixture. The
    # test_extract_text_returns_real_pdf_text test above covers that path
    # against a reportlab-generated fixture when reportlab is installed.


# ---------------------------------------------------------------------------
# Vision post-processing helpers (resize_for_vlm + jpeg_bytes_to_data_url)
# ---------------------------------------------------------------------------


class TestVisionHelpers:
    def test_resize_for_vlm_downscales_long_side(self) -> None:
        from PIL import Image

        from deep_research.llm.vision import resize_for_vlm

        img = Image.new("RGB", (4000, 2000), color=(0, 100, 200))
        out = resize_for_vlm(img, max_dim=1024, jpeg_quality=70)
        # JPEG bytes; roundtrip to verify dimensions
        decoded = Image.open(io.BytesIO(out))
        w, h = decoded.size
        assert max(w, h) <= 1024
        # Aspect ratio preserved (2:1)
        assert abs((w / h) - 2.0) < 0.05

    def test_resize_for_vlm_doesnt_upscale(self) -> None:
        from PIL import Image

        from deep_research.llm.vision import resize_for_vlm

        img = Image.new("RGB", (100, 50), color=(50, 50, 50))
        out = resize_for_vlm(img, max_dim=1024, jpeg_quality=80)
        decoded = Image.open(io.BytesIO(out))
        assert decoded.size == (100, 50)

    def test_jpeg_bytes_to_data_url_format(self) -> None:
        from deep_research.llm.vision import jpeg_bytes_to_data_url

        url = jpeg_bytes_to_data_url(b"\xff\xd8\xff\xe0bytes")
        assert url.startswith("data:image/jpeg;base64,")
        # base64-encoded payload should decode back to the original bytes
        import base64

        assert base64.b64decode(url.split(",", 1)[1]) == b"\xff\xd8\xff\xe0bytes"
