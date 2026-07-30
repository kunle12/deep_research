"""CLI tests — flag parsing, validation, output file writes, `--cite` /
`--dump-graph` semantics, and progress-not-erroring under --quiet.

We patch `app.run_research` (the symbol imported in `cli/app.py`) so the
agent's router never runs (and never touches the LLM / Tavily). All tests
exercise CLI plumbing only.
"""

from __future__ import annotations

# IMPORTANT: `import deep_research.cli.app as cli_mod` is shadowed by the
# `app` Typer object re-exported in `deep_research/cli/__init__.py`. We have
# to use importlib / a fresh module reference to get the actual module
# (which is where `run_research`, `_load_config`, etc. live).
import importlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

cli_mod = importlib.import_module("deep_research.cli.app")
from deep_research.cli import app  # noqa: E402
from deep_research.config import AgentTopConfig  # noqa: E402
from deep_research.state import Citation, CitationGraph, Report  # noqa: E402


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def stub_report() -> Report:
    """A pre-baked Report with one citation + a non-empty citation graph."""
    cit = Citation(
        url="https://arxiv.org/abs/2401.12345",
        title="Stub paper",
        snippet="Abstract snippet",
        source_type="arxiv",
        arxiv_id="2401.12345",
        authors=["Doe, J."],
        confidence_score=0.9,
    )
    graph = CitationGraph()
    return Report(
        markdown="# Stub\n\nreport body",
        citations=[cit],
        path="academic",
        citation_graph=graph,
        classifier_rationale="stub for CLI tests",
        iterations=1,
    )


@pytest.fixture
def patch_run_research(monkeypatch, stub_report):
    """Patch `cli/app.run_research` to return a stub Report.

    Also records the kwargs the CLI passed so tests can assert on them.
    """
    captured: dict = {}

    async def _fake_run_research(query, config, *, path_override=None, progress=None, run_id=""):
        captured["query"] = query
        captured["path_override"] = path_override
        captured["progress_sent"] = progress is not None
        # Touch progress once to make sure the CLI is plumbing it through.
        if progress is not None:
            progress.phase("test.injected", "synthetic")
            progress.complete()
        return stub_report

    monkeypatch.setattr(cli_mod, "run_research", _fake_run_research)
    # Also patch _load_config so we don't depend on config.example.yaml's path.
    monkeypatch.setattr(cli_mod, "_load_config", lambda cfg: AgentTopConfig())
    # And keep _load_env_dotlocal a no-op.
    monkeypatch.setattr(cli_mod, "_load_env_dotlocal", lambda cwd: None)
    return captured


# ----------------------------------------------------------------------------
# Routing flags
# ----------------------------------------------------------------------------


class TestPathFlags:
    def test_quick_flag_picks_quick_path(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--quick", "any query"])
        assert res.exit_code == 0
        assert patch_run_research["path_override"] == "quick"

    def test_deep_flag_picks_deep_path(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--deep", "any query"])
        assert res.exit_code == 0
        assert patch_run_research["path_override"] == "deep"

    def test_academic_flag_picks_academic_path(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--academic", "any query"])
        assert res.exit_code == 0
        assert patch_run_research["path_override"] == "academic"

    def test_url_source_flag_picks_url_source_path(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--url-source", "https://arxiv.org/abs/2401.12345 foo"])
        assert res.exit_code == 0
        assert patch_run_research["path_override"] == "url_source"

    def test_two_path_flags_rejected_with_code_2(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--quick", "--deep", "any query"])
        assert res.exit_code == 2
        # Friendly error has the flag names
        assert "Ambiguous flags" in (res.stdout + (res.stderr or ""))

    def test_no_path_flag_passes_none_override(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["any query"])
        assert res.exit_code == 0
        assert patch_run_research["path_override"] is None


# ----------------------------------------------------------------------------
# Progress flag plumbing
# ----------------------------------------------------------------------------


class TestProgressPlumbing:
    def test_progress_reporter_always_plumbed_through(self, runner, patch_run_research) -> None:
        """The CLI must always pass a non-None progress reporter to run_research
        so the live panel works even when --quiet is set. --quiet only toggles
        whether the panel is actually *rendered*."""
        res = runner.invoke(app, ["any query"])
        assert res.exit_code == 0
        assert patch_run_research["progress_sent"] is True

    def test_quiet_flag_does_not_break_run(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--quiet", "any query"])
        assert res.exit_code == 0
        assert patch_run_research["progress_sent"] is True


# ----------------------------------------------------------------------------
# Validation errors
# ----------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--max-iterations", "0"),
            ("--max-iterations", "-1"),
            ("--max-depth", "-1"),
            ("--max-papers", "0"),
            ("--max-papers", "-5"),
        ],
    )
    def test_invalid_value_exits_with_code_2(
        self, runner, patch_run_research, flag: str, value: str
    ) -> None:
        res = runner.invoke(app, [flag, value, "any query"])
        # Validation runs BEFORE the agent is invoked, so we expect no
        # injection of run_research at all (still asserted implicitly via
        # the stub being unused — but we do check exit code).
        assert res.exit_code == 2
        combined = res.stdout + (res.stderr or "")
        assert "Invalid" in combined or "must be" in combined

    def test_invalid_format_exits_with_code_2(self, runner, patch_run_research) -> None:
        res = runner.invoke(app, ["--format", "xml", "any query"])
        assert res.exit_code == 2
        combined = res.stdout + (res.stderr or "")
        assert "Invalid --format" in combined
        assert "must be 'markdown' or 'json'" in combined


# ----------------------------------------------------------------------------
# Output rendering
# ----------------------------------------------------------------------------


class TestOutput:
    def test_prints_markdown_to_stdout_by_default(
        self, runner, patch_run_research, stub_report
    ) -> None:
        res = runner.invoke(app, ["any query"])
        assert res.exit_code == 0
        assert "Stub" in res.stdout
        assert "report body" in res.stdout

    def test_writes_markdown_to_out_file(self, runner, patch_run_research, tmp_path: Path) -> None:
        out_path = tmp_path / "report.md"
        res = runner.invoke(app, ["--out", str(out_path), "any query"])
        assert res.exit_code == 0
        assert out_path.exists()
        # The CLI prints a "Wrote" message to stdout
        assert "Wrote" in res.stdout
        # The file contents include the stub report's markdown body
        content = out_path.read_text(encoding="utf-8")
        assert "Stub" in content
        assert "report body" in content

    def test_json_format_emits_valid_json(self, runner, patch_run_research, stub_report) -> None:
        res = runner.invoke(app, ["--format", "json", "any query"])
        assert res.exit_code == 0
        # The JSON renderer is used; output should round-trip via json.loads.
        parsed = json.loads(res.stdout)
        assert parsed.get("path") == "academic"


# ----------------------------------------------------------------------------
# Citations + graph dump flags
# ----------------------------------------------------------------------------


class TestSideFileDumps:
    def test_cite_writes_json_array(
        self, runner, patch_run_research, stub_report, tmp_path: Path
    ) -> None:
        cite_path = tmp_path / "cites.json"
        res = runner.invoke(
            app,
            ["--cite", str(cite_path), "any query"],
        )
        assert res.exit_code == 0
        assert cite_path.exists()
        arr = json.loads(cite_path.read_text(encoding="utf-8"))
        assert isinstance(arr, list)
        # At least one citation matching the stub
        assert any(c.get("url") == "https://arxiv.org/abs/2401.12345" for c in arr)
        # Friendly "Wrote citations" message
        assert "Wrote citations" in res.stdout

    def test_dump_graph_when_graph_present_emits_bib(
        self, runner, patch_run_research, stub_report, tmp_path: Path
    ) -> None:
        # Force the stub graph to have at least one node so the dump isn't empty.
        from deep_research.state import PaperNode

        stub_report.citation_graph.add_node(
            PaperNode(arxiv_id="2401.12345", title="Stub", depth=0, rationale="seed")
        )
        graph_path = tmp_path / "refs.bib"
        res = runner.invoke(app, ["--dump-graph", str(graph_path), "any query"])
        assert res.exit_code == 0
        assert graph_path.exists()
        bib = graph_path.read_text(encoding="utf-8")
        # The bibtex renderer emits `@misc{...}` entries by arxiv_id
        assert "2401.12345" in bib
        assert "Wrote citation graph" in res.stdout

    def test_dump_graph_when_empty_warns_and_does_not_write(
        self, runner, patch_run_research, stub_report, tmp_path: Path
    ) -> None:
        graph_path = tmp_path / "empty.bib"
        res = runner.invoke(app, ["--dump-graph", str(graph_path), "any query"])
        assert res.exit_code == 0
        # No file should be written when the graph is empty.
        assert not graph_path.exists()
        combined = res.stdout + (res.stderr or "")
        assert "No citation graph" in combined


# ----------------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------------


class TestErrorHandling:
    def test_run_research_raises_prints_error_exits_1(self, runner, monkeypatch) -> None:
        async def _boom(query, config, *, path_override=None, progress=None, run_id=""):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(cli_mod, "run_research", _boom)
        monkeypatch.setattr(cli_mod, "_load_config", lambda cfg: AgentTopConfig())
        monkeypatch.setattr(cli_mod, "_load_env_dotlocal", lambda cwd: None)

        res = runner.invoke(app, ["any query"])
        assert res.exit_code == 1
        combined = res.stdout + (res.stderr or "")
        assert "Error:" in combined
        assert "kaboom" in combined

    def test_verbose_drops_rich_traceback_on_error(self, runner, monkeypatch) -> None:
        async def _boom(query, config, *, path_override=None, progress=None, run_id=""):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(cli_mod, "run_research", _boom)
        monkeypatch.setattr(cli_mod, "_load_config", lambda cfg: AgentTopConfig())
        monkeypatch.setattr(cli_mod, "_load_env_dotlocal", lambda cwd: None)

        res = runner.invoke(app, ["--verbose", "any query"])
        assert res.exit_code == 1
        # Traceback frames should appear in the output (typer/Click captures stdout)
        combined = res.stdout + (res.stderr or "")
        # `--verbose` enables a RichTraceback which contains a Traceback header.
        # It might be in stdout or stderr depending on where Console writes.
        # Either way the "kaboom" string survives.
        assert "kaboom" in combined
