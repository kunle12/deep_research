"""Tests for library CLI (P12.0)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from deep_research.library.cli import library_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_cli_glossary_no_pdl(runner):
    """glossary command errors when PDL is disabled."""
    import os
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("pdl:\n  enabled: false\n")
    result = runner.invoke(library_app, ["glossary", "--config", cfg_path])
    assert result.exit_code != 0


def test_cli_refresh_no_pdl(runner):
    """refresh command errors when PDL is disabled."""
    import os
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("pdl:\n  enabled: false\n")
    result = runner.invoke(library_app, ["refresh", "--config", cfg_path])
    assert result.exit_code != 0
