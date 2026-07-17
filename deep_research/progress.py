"""Progress reporting hooks for the deep research agent.

Library callers of `run_research` are completely silent by default. The CLI
passes a `RichProgressReporter` so users see live status updates as the agent
moves through phases.

Design
------
`ProgressReporter` is a runtime-checkable `Protocol` with three methods:

- `phase(name, detail)` — top-level state change (e.g. routing, planning,
  researching, critic, writer, complete). Use to advance the display's
  overall status row. Idempotent; calling twice with the same name should
  be a no-op visually.
- `step(label, detail)` — fine-grained within-phase update (e.g. "fetching
  arxiv:2401.12345"). May be called many times.
- `complete()` — terminal; flushes any pending live display.

Both `phase` and `step` are best-effort. A no-op `NullReporter` is the
default; if the agent ever calls them from an exception handler they must
not themselves raise (or any try/finally teardown would be unreliable).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ProgressReporter(Protocol):
    """Abstract progress sink. All methods must be safe to call from any context."""

    def phase(self, name: str, detail: str = "") -> None: ...

    def step(self, label: str, detail: str = "") -> None: ...

    def complete(self) -> None: ...


class NullReporter:
    """Default no-op reporter. Library callers may pass `None` interchangeably."""

    def phase(self, name: str, detail: str = "") -> None:
        return None

    def step(self, label: str, detail: str = "") -> None:
        return None

    def complete(self) -> None:
        return None


__all__ = ["NullReporter", "ProgressReporter"]
