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
from datetime import UTC, datetime
from typing import Any

from deep_research.checkpoint import (
    discard_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from deep_research.citations import filter_citations_to_referenced
from deep_research.config import AgentTopConfig, LLMRole
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.router import LLMRouter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.critic import review as critic_review
from deep_research.nodes.paper_analysis import (
    format_deep_analysis_context,
    run_paper_analysis_pass,
)
from deep_research.nodes.planner import plan as planner_plan
from deep_research.nodes.recall import format_recall_context
from deep_research.nodes.recall import recall as recall_run
from deep_research.nodes.researcher import research as researcher_run
from deep_research.nodes.writer import write as writer_write
from deep_research.progress import ProgressReporter, ensure_reporter
from deep_research.report.markdown import render_blocked_sources_markdown
from deep_research.state import (
    ClassifiedQuery,
    PaperAnalysis,
    Report,
    ResearchState,
    SubQuestion,
)

logger = logging.getLogger(__name__)


async def deep_research(
    classified: ClassifiedQuery,
    original_query: str,
    router: LLMRouter,
    tools: ToolRegistry,
    config: AgentTopConfig,
    progress: ProgressReporter | None = None,
    writer: LibraryWriter | NullLibraryWriter | None = None,
    run_id: str = "",
) -> Report:
    """Run the deep research loop."""

    reporter: ProgressReporter = ensure_reporter(progress)
    breadth = (
        classified.breadth_hint if classified.breadth_hint > 0 else config.agent.max_subquestions
    )
    iterations_cap = config.agent.max_iterations

    # 1. Plan (or resume from checkpoint)
    state = ResearchState(query=original_query)
    _checkpoint_discarded = False
    if run_id:
        loaded = load_checkpoint(run_id)
        if loaded is not None:
            candidate, _ = loaded
            if candidate.query == original_query:
                state = candidate
                reporter.phase("deep.resume", f"resuming iteration {state.iteration}")
                logger.info("resuming deep research from iteration %d", state.iteration)
            else:
                logger.warning(
                    "checkpoint query mismatch: expected %r, got %r — discarding",
                    original_query,
                    candidate.query,
                )
                discard_checkpoint(run_id)
                _checkpoint_discarded = True
    if not state.plan.sub_questions:
        # No plan yet — run planner (fresh start or discarded checkpoint)
        reporter.phase("deep.plan", f"decomposing (breadth ≤ {breadth})")
        planner = router.resolve(LLMRole.PLANNER)
        plan_result = await planner_plan(
            original_query,
            planner.client,
            planner.model,
            breadth=breadth,
        )
        state.plan = plan_result
        reporter.step("deep.plan", f"{len(plan_result.sub_questions)} sub-questions")

    # 2. Iteration loop
    # Track how many times each sub-question has failed to detect stuck ones
    _sq_attempts: dict[str, int] = {}
    _max_stuck_retries = config.agent.max_subquestion_retries
    # Flag to stop early on KeyboardInterrupt
    _interrupted = False

    for iteration in range(state.iteration, iterations_cap):
        state.iteration = iteration
        pending = [sq for sq in state.plan.sub_questions if not state.is_covered(sq)]
        if not pending:
            logger.info("no pending sub-questions after iteration %d", iteration)
            # Save checkpoint before breaking so resume can pick up the final state
            if run_id:
                save_checkpoint(state, run_id)
            break

        reporter.phase(
            "deep.research", f"iter {iteration + 1}/{iterations_cap}: {len(pending)} sub-q(s)"
        )
        logger.info(
            "deep iteration %d: %d pending sub-question(s)",
            iteration,
            len(pending),
        )
        # P13: recall prior context from library before researcher dispatch
        storage = writer.storage if isinstance(writer, LibraryWriter) else None
        timeout = config.agent.researcher_timeout_s

        # Each researcher runs with its own timeout. Individual tool calls
        # inside the researcher already have per-call timeouts via
        # ToolRegistry.call, so a hung tool surfaces as a clean error result
        # rather than dead weight. If the researcher's total wall-clock
        # exceeds timeout, we catch TimeoutError and skip that result.
        async def _run_one_with_timeout(
            sq,
            _storage=storage,
            _timeout=timeout,
            _analyses=None,
        ):
            if _analyses is None:
                _analyses = dict(state.deep_analyses)
            try:
                return await asyncio.wait_for(
                    _run_one_researcher_with_recall(
                        sq,
                        router,
                        config,
                        tools,
                        _storage,
                        deep_analyses=_analyses,
                    ),
                    timeout=_timeout,
                )
            except TimeoutError:
                logger.warning("researcher for %s timed out after %ds", sq.id, _timeout)
                raise  # let gather return_exceptions catch it

        results = await asyncio.gather(
            *[_run_one_with_timeout(sq) for sq in pending],
            return_exceptions=True,
        )
        for sq, r in zip(pending, results):
            if isinstance(r, KeyboardInterrupt | SystemExit):
                _interrupted = True
                logger.warning("researcher interrupted — stopping early")
                break
            if isinstance(r, Exception):
                rtype = "timeout" if isinstance(r, TimeoutError) else type(r).__name__
                msg = str(r) or rtype
                logger.warning("researcher for %s raised (%s): %s", sq.id, rtype, msg)
                reporter.step("deep.research.fail", sq.id)
                # Track stuck sub-questions so they don't loop forever
                _sq_attempts[sq.id] = _sq_attempts.get(sq.id, 0) + 1
                if _sq_attempts[sq.id] >= _max_stuck_retries:
                    logger.warning(
                        "sub-question %s failed %d times — marking covered with empty draft",
                        sq.id,
                        _max_stuck_retries,
                    )
                    state.absorb_section(sq.id, [], "(research timed out)")
                continue
            if not isinstance(r, tuple) or len(r) not in (2, 3, 4):
                logger.warning(
                    "researcher for %s returned unexpected type %r", sq.id, type(r).__name__
                )
                reporter.step("deep.research.fail", sq.id)
                continue
            answer_md, citations = r[0], r[1]
            state.absorb_section(sq.id, citations, answer_md)
            reporter.step("deep.research.ok", f"{sq.id} ({len(citations)} cites)")
            if len(r) >= 3:
                refinements = r[2]
                state.absorb_refinements(refinements)
                if refinements:
                    logger.info("researcher %s emitted %d refinements", sq.id, len(refinements))
                    reporter.step("deep.research.refine", f"{sq.id} (+{len(refinements)})")
            if len(r) >= 4:
                state.absorb_blocked_sources(r[3], sq_id=sq.id)
                if r[3]:
                    reporter.step(
                        "deep.research.blocked",
                        f"{sq.id} ({len(r[3])} source(s) skipped)",
                    )

        if _interrupted:
            break

        # Critic
        reporter.phase("deep.critic", f"iter {iteration + 1}")
        critic = router.resolve(LLMRole.CRITIC)
        critique = await critic_review(state, critic.client, critic.model)
        logger.info(
            "critic iter=%d sufficient=%s gaps=%d refinements=%d papers_to_analyze=%d",
            iteration,
            critique.sufficient,
            len(critique.gaps),
            len(state.pending_refinements),
            len(critique.papers_to_analyze),
        )

        # Critic-selected deep paper analysis: run before the sufficient/break
        # checks so analyses happen even when the loop ends, and so Phase 2
        # can feed them into the next critic iteration.
        if critique.papers_to_analyze:
            await run_paper_analysis_pass(
                state,
                critique.papers_to_analyze,
                query=original_query,
                router=router,
                config=config,
                tools=tools,
                writer=writer,
                reporter=reporter,
                run_id=run_id,
            )

        # Refinements queued by researchers are explicit follow-up work; run
        # them even if the critic considers the current coverage sufficient.
        if critique.sufficient and not state.pending_refinements:
            reporter.step("deep.critic", "sufficient")
            # Save checkpoint before breaking so resume can pick up the final state
            if run_id:
                save_checkpoint(state, run_id)
            break
        if critique.sufficient and state.pending_refinements:
            logger.info(
                "critic sufficient but %d refinement(s) pending — researching them",
                len(state.pending_refinements),
            )

        flushed = state.flush_refinements(
            max_total=config.agent.max_total_refinements_per_iteration,
        )
        if flushed:
            logger.info("flushed %d refinements into plan", len(flushed))
            reporter.step("deep.refine.flush", f"{len(flushed)} new sub-q(s)")

        if not critique.gaps and not flushed:
            logger.warning("critic said insufficient but returned no gaps — forcing stop")
            if run_id:
                save_checkpoint(state, run_id)
            break
        if critique.gaps:
            reporter.step("deep.critic", f"gaps={len(critique.gaps)} → enqueuing")

            # Append gaps as new sub-questions (with dedup by question text)
            existing_qs = {sq.question for sq in state.plan.sub_questions}
            for gap in critique.gaps:
                if gap.question and gap.question not in existing_qs:
                    state.plan.sub_questions.append(gap)
                    existing_qs.add(gap.question)
                    logger.info("added gap sub-question: %s", gap.question[:80])

        # Persist progress AFTER all state mutations for this iteration (flush
        # + gap enqueue) so a resumed run sees a coherent plan. Saving before
        # the flush would strand pending_refinements: on resume they would not
        # be in plan.sub_questions, and the top-of-loop `if not pending: break`
        # would silently drop them.
        if run_id:
            save_checkpoint(state, run_id)

    # 3. Synthesize final report
    reporter.phase("deep.writer", f"synthesizing {len(state.citations)} citations")
    writer_llm = router.resolve(LLMRole.WRITER)
    final_md = await writer_write(
        state, writer_llm.client, writer_llm.model, writer=writer, run_id=run_id
    )

    # 4. Project all assembled citations into a sorted list (by confidence
    # desc), keeping only sources the final report body actually references.
    # Search hits / fetched pages that never made it into the synthesized
    # markdown must not appear in the report's bibliography.
    all_citations = sorted(
        filter_citations_to_referenced(final_md, list(state.citations.values())),
        key=lambda c: c.confidence_score,
        reverse=True,
    )

    blocked_md = render_blocked_sources_markdown(state.blocked_sources)
    if blocked_md:
        final_md = final_md.rstrip() + "\n\n" + blocked_md

    # Clean up checkpoint — research completed successfully.
    # Skip if already discarded earlier (e.g. stale checkpoint on resume).
    if run_id and not _checkpoint_discarded:
        discard_checkpoint(run_id)

    reporter.phase("deep.done", f"{len(all_citations)} citations")

    return Report(
        markdown=final_md,
        citations=all_citations,
        blocked_sources=state.blocked_sources,
        path="deep",
        classifier_rationale=classified.rationale,
        iterations=state.iteration + 1 if state.plan.sub_questions else 0,
        created_at=datetime.now(UTC),
        query=original_query,
    )


async def _run_one_researcher_with_recall(
    sq: SubQuestion,
    router: LLMRouter,
    config: AgentTopConfig,
    tools: ToolRegistry,
    storage: Any | None,
    deep_analyses: dict[str, PaperAnalysis] | None = None,
) -> tuple[str, list, list]:
    """Run the researcher for one sub-question with library recall for prior context."""
    prior_context = ""
    try:
        entries = await recall_run(sq.question, storage)
        if entries:
            prior_context = format_recall_context(entries)
    except Exception as e:
        logger.debug("recall failed for %s: %s", sq.id, e)

    # Phase 2: deep analyses from earlier critic rounds inform later
    # researchers so their searches build on analyzed findings.
    if deep_analyses:
        digest = format_deep_analysis_context(deep_analyses)
        if digest:
            prior_context = prior_context + "\n\n" + digest if prior_context else digest

    researcher = router.resolve(LLMRole.RESEARCHER)
    return await researcher_run(
        sq,
        researcher.client,
        researcher.model,
        tools,
        max_turns=config.agent.researcher_max_turns,
        prior_context=prior_context,
        max_refinement_per_researcher=config.agent.max_refinement_per_researcher,
        max_refinement_depth=config.agent.max_refinement_depth,
        max_context_tokens=researcher.max_context_tokens,
        max_citations_per_researcher=config.agent.max_citations_per_researcher,
    )


__all__ = ["deep_research"]
