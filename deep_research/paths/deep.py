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

from openai import AsyncOpenAI

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.critic import review as critic_review
from deep_research.nodes.planner import plan as planner_plan
from deep_research.nodes.researcher import research as researcher_run
from deep_research.nodes.writer import write as writer_write
from deep_research.state import ClassifiedQuery, ResearchState, SubQuestion

logger = logging.getLogger(__name__)


async def deep_research(
    classified: ClassifiedQuery,
    original_query: str,
    client: AsyncOpenAI,
    tools: ToolRegistry,
    config: AgentTopConfig,
) -> Report:  # noqa: F821 - forward ref
    """Run the deep research loop."""
    # Import here to avoid circulars at module-load time
    from deep_research.state import Report

    breadth = (
        classified.breadth_hint
        if classified.breadth_hint
        else config.agent.max_subquestions
    )
    iterations_cap = config.agent.max_iterations
    sem = asyncio.Semaphore(config.agent.max_concurrent_tools)

    # 1. Plan
    plan_result = await planner_plan(original_query, client, config.llm.text_model, breadth=breadth)
    state = ResearchState(query=original_query, plan=plan_result)

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

        logger.info(
            "deep iteration %d: %d pending sub-question(s)",
            iteration, len(pending),
        )
        # Parallel dispatch
        tasks = [_run_one_researcher(sq, client, config, tools, sem) for sq in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sq, r in zip(pending, results):
            if isinstance(r, Exception):
                logger.warning("researcher for %s raised: %s", sq.id, r)
                state.absorb_section(sq.id, [], f"(researcher failed: {type(r).__name__}: {r})")
                continue
            answer_md, citations = r
            state.absorb_section(sq.id, citations, answer_md)

        # Critic
        critique = await critic_review(state, client, config.llm.text_model)
        logger.info(
            "critic iter=%d sufficient=%s gaps=%d", iteration, critique.sufficient, len(critique.gaps),
        )
        if critique.sufficient or not critique.gaps:
            break

        # Append gaps as new sub-questions (with dedup by question text)
        existing_qs = {sq.question for sq in state.plan.sub_questions}
        for gap in critique.gaps:
            if gap.question and gap.question not in existing_qs:
                state.plan.sub_questions.append(gap)
                existing_qs.add(gap.question)
                logger.info("added gap sub-question: %s", gap.question[:80])

    # 3. Synthesize final report
    final_md = await writer_write(state, client, config.llm.text_model)

    # 4. Project all assembled citations into a sorted list (by confidence desc)
    all_citations = sorted(
        state.citations.values(),
        key=lambda c: c.confidence_score,
        reverse=True,
    )

    return Report(
        markdown=final_md,
        citations=all_citations,
        path="deep",
        classifier_rationale=classified.rationale,
        iterations=state.iteration + 1 if state.plan.sub_questions else 0,
    )


async def _run_one_researcher(
    sq: SubQuestion,
    client: AsyncOpenAI,
    config: AgentTopConfig,
    tools: ToolRegistry,
    sem: asyncio.Semaphore,
) -> tuple[str, list]:
    """Wrap the researcher call in a semaphore for concurrency control."""
    async with sem:
        return await researcher_run(
            sq, client, config.llm.text_model, tools,
        )


__all__ = ["deep_research"]
