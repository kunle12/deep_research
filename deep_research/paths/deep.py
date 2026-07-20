"""deep path — planner -> (parallel) researcher fan-out -> critic -> writer.

P3: implemented. The full deep-research loop:
  1. plan() — decompose user query into N sub-questions (breadth-knob)
  2. For each sub-question, dispatch a researcher in parallel via asyncio.gather
  3. critic.review(state) — decide if more research is needed
  4. If gaps, add them to the plan and loop (up to max_iterations)
  5. writer.write(state) — synthesize the final markdown report
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.critic import review as critic_review
from deep_research.nodes.planner import plan as planner_plan
from deep_research.nodes.recall import format_recall_context
from deep_research.nodes.recall import recall as recall_run
from deep_research.nodes.researcher import research as researcher_run
from deep_research.nodes.writer import write as writer_write
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.state import ClassifiedQuery, Report, ResearchState, SubQuestion

logger = logging.getLogger(__name__)


async def deep_research(
    classified: ClassifiedQuery,
    original_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Run the deep research loop."""

    reporter: ProgressReporter = ensure_reporter(progress)
    breadth = (
        classified.breadth_hint
        if classified.breadth_hint > 0
        else config.agent.max_subquestions
    )
    iterations_cap = config.agent.max_iterations

    # 1. Plan
    reporter.phase("deep.plan", f"decomposing (breadth ≤ {breadth})")
    plan_result = await planner_plan(original_query, client, config.llm.text_model, breadth=breadth)
    state = ResearchState(query=original_query, plan=plan_result)
    reporter.step("deep.plan", f"{len(plan_result.sub_questions)} sub-questions")

    # 2. Iteration loop
    for iteration in range(iterations_cap):
        state.iteration = iteration
        pending = [
            sq for sq in state.plan.sub_questions
            if not state.is_covered(sq)
        ]
        if not pending:
            logger.info("no pending sub-questions after iteration %d", iteration)
            break

        reporter.phase(
            "deep.research",
            f"iter {iteration + 1}/{iterations_cap}: {len(pending)} sub-q(s)"
        )
        logger.info(
            "deep iteration %d: %d pending sub-question(s)",
            iteration, len(pending),
        )
        # P13: recall prior context from library before researcher dispatch
        storage = writer.storage if isinstance(writer, LibraryWriter) else None
        # Wrap each researcher in a Task so we can explicitly cancel on timeout.
        # wait_for cancels the task on timeout, but sync operations (e.g. PDF
        # extraction via run_in_executor) may not honour cancellation mid-execution.
        raw_tasks = [_run_one_researcher_with_recall(sq, client, config, tools, storage) for sq in pending]
        tasks = [asyncio.create_task(t) for t in raw_tasks]
        results = await asyncio.gather(*[
            asyncio.wait_for(t, timeout=120) for t in tasks
        ], return_exceptions=True)
        for sq, r in zip(pending, results):
            if isinstance(r, Exception):
                logger.warning("researcher for %s raised: %s", sq.id, r)
                reporter.step("deep.research.fail", sq.id)
                continue
            if not isinstance(r, tuple) or len(r) != 2:
                logger.warning("researcher for %s returned unexpected type %r", sq.id, type(r).__name__)
                reporter.step("deep.research.fail", sq.id)
                continue
            answer_md, citations = r
            state.absorb_section(sq.id, citations, answer_md)
            reporter.step("deep.research.ok", f"{sq.id} ({len(citations)} cites)")

        # Critic
        reporter.phase("deep.critic", f"iter {iteration + 1}")
        critique = await critic_review(state, client, config.llm.text_model)
        logger.info(
            "critic iter=%d sufficient=%s gaps=%d", iteration, critique.sufficient, len(critique.gaps),
        )
        if critique.sufficient:
            reporter.step("deep.critic", "sufficient")
            break
        if not critique.gaps:
            logger.warning("critic said insufficient but returned no gaps — forcing stop")
            break
        reporter.step("deep.critic", f"gaps={len(critique.gaps)} → enqueuing")

        # Append gaps as new sub-questions (with dedup by question text)
        existing_qs = {sq.question for sq in state.plan.sub_questions}
        for gap in critique.gaps:
            if gap.question and gap.question not in existing_qs:
                state.plan.sub_questions.append(gap)
                existing_qs.add(gap.question)
                logger.info("added gap sub-question: %s", gap.question[:80])

    # 3. Synthesize final report
    reporter.phase("deep.writer", f"synthesizing {len(state.citations)} citations")
    final_md = await writer_write(state, client, config.llm.text_model, writer=writer, run_id=run_id)

    # 4. Project all assembled citations into a sorted list (by confidence desc)
    all_citations = sorted(
        state.citations.values(),
        key=lambda c: c.confidence_score,
        reverse=True,
    )

    reporter.phase("deep.done", f"{len(all_citations)} citations")
    from datetime import UTC, datetime
    return Report(
        markdown=final_md,
        citations=all_citations,
        path="deep",
        classifier_rationale=classified.rationale,
        iterations=state.iteration + 1 if state.plan.sub_questions else 0,
        created_at=datetime.now(UTC),
        query=original_query,
    )


async def _run_one_researcher_with_recall(
    sq: SubQuestion,
    client: AsyncOpenAI,
    config: AgentTopConfig,
    tools: ToolRegistry,
    storage: Any | None,
) -> tuple[str, list]:
    """Run the researcher for one sub-question with library recall for prior context."""
    prior_context = ""
    try:
        entries = await recall_run(sq.question, storage)
        if entries:
            prior_context = format_recall_context(entries)
    except Exception as e:
        logger.debug("recall failed for %s: %s", sq.id, e)

    return await researcher_run(
        sq, client, config.llm.text_model, tools, prior_context=prior_context,
    )


__all__ = ["deep_research"]
