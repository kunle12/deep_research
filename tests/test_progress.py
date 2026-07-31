"""Unit tests for the ProgressReporter abstraction.

These tests target the no-op `NullReporter` (the default library behavior)
plus the non-rendering mode of `RichProgressReporter` (when the console is
not a tty or `enabled=False`). The live Live+Table output itself is
hand-verified in a terminal; here we only check the code paths that:
  - construct without failure
  - accept `phase` / `step` / `complete` calls without error
  - terminate cleanly on `start()` + `complete()`
  - implement the `ProgressReporter` Protocol
"""

from __future__ import annotations

import pytest

from deep_research.progress import NullReporter, ProgressReporter


class TestNullReporter:
    def test_is_a_progress_reporter(self) -> None:
        assert isinstance(NullReporter(), ProgressReporter)

    @pytest.mark.parametrize(
        "method,kwargs",
        [
            ("phase", {"name": "x", "detail": "y"}),
            ("step", {"label": "x", "detail": "y"}),
            ("complete", {}),
        ],
    )
    def test_noop_methods_return_none(self, method: str, kwargs: dict) -> None:
        r = NullReporter()
        result = getattr(r, method)(**kwargs)
        assert result is None

    def test_is_safe_to_call_many_times(self) -> None:
        r = NullReporter()
        for _ in range(1000):
            r.phase("p")
            r.step("s")
        r.complete()
        # No exception


class TestRichProgressReporterDisabled:
    """`rich` reporter with `enabled=False` exercises all the code paths that
    library callers and CLI `--quiet` users hit. None of these tests touch an
    actual `Live` instance."""

    def test_implements_protocol(self) -> None:
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        assert isinstance(r, ProgressReporter)

    def test_start_then_phase_step_complete_does_not_raise(self) -> None:
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        r.start()
        r.phase("init", "booting")
        for i in range(10):
            r.step(f"label.{i}", f"detail {i}")
        r.complete()
        # Calling complete twice is a no-op (idempotent teardown)
        r.complete()

    def test_stop_without_start_is_noop(self) -> None:
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        r.stop()  # should not raise

    def test_phase_after_complete_does_not_raise(self) -> None:
        """Defensive: an agent error handler might call phase() after complete()."""
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        r.start()
        r.complete()
        r.phase("after-complete", "should still work")  # silently accepted
        r.step("late-step")
        r.stop()

    def test_default_enabled_flag_uses_console_tty(self, capsys: pytest.CaptureFixture) -> None:
        """When `enabled=None` (the CLI default for non-`--quiet`), the
        reporter auto-disables when stdout is a pipe (not a tty)."""
        from io import StringIO

        from rich.console import Console

        from deep_research.cli.progress import RichProgressReporter

        # Force a non-tty console — unit tests never have a real tty.
        # StringIO conveniently avoids file-handle leaks; rich detects the
        # "is_terminal" flag from the file object and returns False here.
        non_tty = Console(force_terminal=False, file=StringIO())
        r = RichProgressReporter(console=non_tty, enabled=None)
        assert r._enabled is False

    def test_complete_marks_state_as_completed(self) -> None:
        """After complete(), the internal state's `completed` flag flips True."""
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        assert r._state.completed is False
        r.complete()
        assert r._state.completed is True

    def test_step_tail_is_bounded(self) -> None:
        """The rolling step deque shouldn't grow unboundedly. Verifies the
        maxlen cap kicks in for long runs."""
        from deep_research.cli.progress import RichProgressReporter

        r = RichProgressReporter(enabled=False)
        # Push 10x the cap to make sure the deque only keeps `_STEP_TAIL`
        for i in range(80):
            r.step(f"step.{i}", f"d{i}")
        # The cap constant is `_STEP_TAIL = 8` (source-of-truth).
        assert len(r._state.steps) == 8
        # Most recent survive — verify the *last* appended is the most-recent step
        last_ts, last_label, _ = list(r._state.steps)[-1]
        assert last_label == "step.79"
        # Elapsed is non-negative
        assert last_ts >= 0.0


class _RecordingReporter:
    """In-memory reporter used by `TestProgressReporterInPaths` to assert
    that the agent calls `phase` / `step` / `complete` as expected."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, str]] = []
        self.steps: list[tuple[str, str]] = []
        self.completed = False

    def phase(self, name: str, detail: str = "") -> None:
        self.phases.append((name, detail))

    def step(self, label: str, detail: str = "") -> None:
        self.steps.append((label, detail))

    def complete(self) -> None:
        self.completed = True


class TestProgressReporterInPaths:
    """Integration smoke — passes a recording reporter to `run_research` and
    verifies the agent calls phase/step at least once per path."""

    @staticmethod
    def _recorder() -> _RecordingReporter:
        return _RecordingReporter()

    @pytest.mark.asyncio
    async def test_quick_path_emits_phases_and_complete(self) -> None:
        from deep_research import run_research
        from deep_research.config import AgentTopConfig

        cfg = AgentTopConfig()
        cfg.agent.classifier.enabled = False  # avoid real LLM
        rec = self._recorder()
        report = await run_research("any q", cfg, path_override="quick", progress=rec)
        assert report.path == "quick"
        # First phase is the routing phase; later phases are quick.* names.
        phase_names = [p[0] for p in rec.phases]
        assert (
            "quick.search" in phase_names
            or "quick.synthesize" in phase_names
            or "quick.done" in phase_names
        )
        # Final phase is "quick.done" or similar.
        # `complete()` is called on every exit path — even the error path.
        assert rec.completed is True

    @pytest.mark.asyncio
    async def test_empty_query_calls_phase_error_and_complete(self) -> None:
        from deep_research import run_research
        from deep_research.config import AgentTopConfig

        cfg = AgentTopConfig()
        rec = self._recorder()
        report = await run_research("", cfg, progress=rec)
        assert report.path == "unclear"
        assert rec.phases and rec.phases[0][0] == "error"
        assert rec.completed is True
