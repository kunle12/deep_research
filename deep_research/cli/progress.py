"""Rich (terminal) progress reporter for the CLI.

Renders a live-updating panel showing:
  - current phase (e.g. "deep.plan", "academic.batch") + detail line
  - a rolling tail of the most recent fine-grained steps
  - elapsed time since the run started

All `phase` / `step` calls are safe to invoke from `run_research`:
  - Once `complete()` is called, the live display is stopped and the
    panel's final state is printed once (when stdout is a TTY).
  - Safe to call after exceptions — the agent tries to ensure `complete()` is
    always invoked via its outer try/finally, but the reporter is also robust
    to being abandoned mid-stream (the `Live` object is wrapped in a `try`).
  - When stdout/stderr is NOT a tty (e.g. piped to a file), the live display
    is disabled and only the final summary is printed so non-interactive
    consumers see a clean log line, no flicker.

Notes
-----
- `rich.live.Live(refresh_per_second=8)` is intentionally *not* `transient=True`
  so the last panel state remains visible after the run completes — useful
  when a long academic run prints its final panel and the user scrolls back.
- One rolling step-tail list keeps memory bounded. We append dicts to keep
  threading-friendly (no shared mutable state outside `dict.update` which is
  atomic for individual key assignments in CPython).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


_STEP_TAIL = 8  # how many recent steps to keep visible


@dataclass
class _State:
    phase: str = "init"
    phase_detail: str = ""
    steps: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=_STEP_TAIL))
    started: float = field(default_factory=time.monotonic)
    completed: bool = False


class RichProgressReporter:
    """Live rich-console reporter. Use as a context manager or directly."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        transient: bool = False,
        enabled: bool | None = None,
    ) -> None:
        self._console = console or Console()
        # Auto-detect tty if `enabled` is None
        self._enabled = enabled if enabled is not None else bool(self._console.is_terminal)
        self._transient = transient
        self._state = _State()
        self._live: Live | None = None
        self._started_real: float = time.time()

    # -- Lifecycle ---------------------------------------------------

    def start(self) -> None:
        if not self._enabled or self._live is not None:
            return
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=8,
            transient=self._transient,
        )
        try:
            self._live.start()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("rich Live.start() failed: %s: %s", type(e).__name__, e)
            self._live = None
            self._enabled = False

    def stop(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("rich Live.stop() failed: %s: %s", type(e).__name__, e)
            finally:
                self._live = None

    # -- ProgressReporter protocol ------------------------------------

    def phase(self, name: str, detail: str = "") -> None:
        self._state.phase = name
        self._state.phase_detail = detail
        self._refresh()

    def step(self, label: str, detail: str = "") -> None:
        self._state.steps.append((time.monotonic() - self._state.started, label, detail))
        self._refresh()

    def complete(self) -> None:
        self._state.completed = True
        self._refresh()
        self.stop()

    # -- Rendering ---------------------------------------------------

    def _refresh(self) -> None:
        if not self._enabled or self._live is None:
            return
        try:
            self._live.update(self._render())
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("rich Live.update() failed: %s: %s", type(e).__name__, e)

    def _render(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(justify="left", style="cyan", no_wrap=True)
        table.add_column(justify="left", style="white")
        # Phase row
        phase_style = "bold green" if self._state.completed else "bold yellow"
        table.add_row(
            Text("phase", style="dim"),
            Text(self._state.phase, style=phase_style),
        )
        if self._state.phase_detail:
            table.add_row(Text("detail", style="dim"), Text(self._state.phase_detail))
        # Elapsed
        elapsed = time.monotonic() - self._state.started
        table.add_row(Text("elapsed", style="dim"), Text(_fmt_elapsed(elapsed)))
        # Steps tail
        if self._state.steps:
            table.add_row(Text("─", style="dim"), Text("─" * 12, style="dim"))
            for ts, label, detail in list(self._state.steps):
                txt = detail if detail else label
                table.add_row(Text(_fmt_elapsed(ts), style="dim"), Text(f"{label}: {txt}"))
        title = "Deep Research" + (" — done" if self._state.completed else " — running")
        return Panel(table, title=title, border_style="blue", expand=False)


def _fmt_elapsed(seconds: float) -> str:
    """Compact h:mm:ss form."""
    if seconds < 60:
        return f"{seconds:5.1f}s"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


__all__ = ["RichProgressReporter"]
