"""Checkpoint save/load for the deep-research loop.

Saves `ResearchState` as JSON after each iteration so a crashed run can
resume from where it left off — skipping the planner and re-using all
previously completed researcher work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from deep_research.state import ResearchState

logger = logging.getLogger(__name__)

_CHECKPOINT_DIR = Path("./.cache/research_checkpoints")


def _checkpoint_path(run_id: str) -> Path:
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return _CHECKPOINT_DIR / f"{run_id}.json"


def save_checkpoint(state: ResearchState, run_id: str, **extra: Any) -> None:
    """Write a JSON checkpoint of *state* to disk.

    *extra* — additional metadata (e.g. config snapshot) merged at top level.
    """
    path = _checkpoint_path(run_id)
    payload: dict[str, Any] = {
        "state": state.model_dump(mode="json"),
        "run_id": run_id,
    }
    payload.update(extra)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("checkpoint saved: %s (iteration %d)", path, state.iteration)
    except Exception as e:
        logger.warning("checkpoint save failed: %s: %s", type(e).__name__, e)


def load_checkpoint(run_id: str) -> tuple[ResearchState, dict[str, Any]] | None:
    """Load a checkpoint for *run_id*.

    Returns ``(state, metadata)`` or ``None`` if no checkpoint exists or
    loading fails.
    """
    path = _checkpoint_path(run_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        state = ResearchState.model_validate(raw["state"])
        extra = {k: v for k, v in raw.items() if k != "state"}
        logger.info("checkpoint loaded: %s (iteration %d)", path, state.iteration)
        return state, extra
    except Exception as e:
        logger.warning("checkpoint load failed: %s: %s — starting fresh", type(e).__name__, e)
        return None


def discard_checkpoint(run_id: str) -> None:
    """Remove the checkpoint file for *run_id* (e.g. after successful finish)."""
    path = _checkpoint_path(run_id)
    try:
        path.unlink(missing_ok=True)
        logger.debug("checkpoint discarded: %s", path)
    except Exception as e:
        logger.warning("checkpoint discard failed: %s: %s", type(e).__name__, e)


def find_checkpoint_for_query(query: str) -> tuple[ResearchState, dict[str, Any]] | None:
    """Scan checkpoint directory for the latest checkpoint matching *query*.

    Returns ``(state, metadata)`` or ``None`` if no matching checkpoint found.
    The latest checkpoint is determined by file modification time (mtime).
    """
    if not _CHECKPOINT_DIR.exists():
        return None

    candidates: list[tuple[Path, float, dict[str, Any]]] = []
    for f in _CHECKPOINT_DIR.iterdir():
        if not f.is_file() or not f.name.endswith(".json"):
            continue
        try:
            raw = json.loads(f.read_text())
            state_raw = raw.get("state")
            if state_raw is None:
                continue
            if state_raw.get("query") != query:
                continue
            run_id = raw.get("run_id")
            if not run_id:
                logger.debug("skipping checkpoint %s: missing run_id", f)
                continue
            candidates.append((f, f.stat().st_mtime, raw))
        except Exception as e:
            logger.warning("skipping unparseable checkpoint %s: %s", f, e)
            continue

    if not candidates:
        return None

    # Sort by mtime descending, pick the most recent
    candidates.sort(key=lambda t: t[1], reverse=True)
    _path, _mtime, raw = candidates[0]
    logger.info("auto-detected checkpoint: %s", _path)

    try:
        state = ResearchState.model_validate(raw["state"])
        extra = {k: v for k, v in raw.items() if k != "state"}
        return state, extra
    except Exception as e:
        logger.warning("checkpoint load failed: %s: %s — starting fresh", type(e).__name__, e)
        return None
