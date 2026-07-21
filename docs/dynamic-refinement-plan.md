# Dynamic Refinement During Research — Implementation Plan

## Overview

Add a **`refine`** tool to the researcher's tool-calling loop that lets the LLM emit refinement requests as "side effects" during its tool loop. These requests are collected, validated, and flushed into `ResearchState` after the researcher finishes — so they become available to the next iteration.

---

## Step 1 — Add refinement state to `ResearchState`

**File:** `deep_research/state.py`

**Changes:**

```python
# Add new field to ResearchState (after line 106)
class ResearchState(BaseModel):
    ...
    iteration: int = 0
    # NEW: refinements emitted by researchers mid-loop
    pending_refinements: list[SubQuestion] = Field(default_factory=list)

# Add new methods:
    def absorb_refinements(self, refinements: list[SubQuestion]) -> None:
        """Deduplicate and absorb refinements emitted by a researcher."""
        existing_qs = {sq.question for sq in self.plan.sub_questions}
        existing_qs.update(sq.question for sq in self.pending_refinements)
        for r in refinements:
            if r.question and r.question not in existing_qs:
                self.pending_refinements.append(r)
                existing_qs.add(r.question)

    def flush_refinements(self) -> list[SubQuestion]:
        """Move pending refinements into the plan and return them."""
        flushed = list(self.pending_refinements)
        self.plan.sub_questions.extend(flushed)
        self.pending_refinements.clear()
        return flushed
```

---

## Step 2 — Add the `refine` tool schema and handler

**File:** `deep_research/nodes/researcher.py`

**Schema:**

```python
REFINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "refine",
        "description": (
            "Call this during research to dynamically refine the research plan. "
            "Use cases:\n"
            "1. You found an important reference that should be followed — set action='chase_reference'.\n"
            "2. You discovered an interesting subtopic that needs its own investigation — "
            "set action='drill_deeper'.\n"
            "3. You realize the sub-question needs a different search strategy — "
            "set action='revise_strategy'.\n\n"
            "This does NOT replace answering the current sub-question. "
            "It merely queues refinement for the next iteration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["chase_reference", "drill_deeper", "revise_strategy"],
                    "description": "What kind of refinement to perform",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "For drill_deeper: the new sub-question to investigate. "
                        "For chase_reference: a question about the reference's content. "
                        "For revise_strategy: not used."
                    ),
                },
                "reference_url": {
                    "type": "string",
                    "description": (
                        "For chase_reference: the URL or arxiv ID of the reference to follow. "
                        "For drill_deeper: optional URL hint for where to start."
                    ),
                },
                "tool_hint": {
                    "type": "string",
                    "enum": ["general-web", "arxiv", "reddit", "browser-required"],
                    "description": "Optional tool hint for the new sub-question.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this refinement is needed (for the report's provenance).",
                },
            },
            "required": ["action", "rationale"],
        },
    },
}
```

**Wrapper logic inside `research()` (before calling `run_with_tools`):**

```python
# Register the refine tool into the registry
refine_calls_collector: list[dict] = []

async def _refine_handler(**kwargs) -> ToolResult:
    """Collect refine calls and return a success ack."""
    refine_calls_collector.append(dict(kwargs))
    return ToolResult(content="Refinement queued. Continue your research.")

tools.register("refine", _refine_handler, REFINE_SCHEMA["function"])
```

**After `run_with_tools` returns**, convert collected calls to `SubQuestion` objects:

```python
refinements: list[SubQuestion] = []
depth = sub_q.depth + 1
for call in refine_calls_collector:
    action = call.get("action")
    if action == "drill_deeper" and call.get("question"):
        refinements.append(SubQuestion(
            id=f"{sub_q.id}.refine{len(refinements)+1}",
            question=call["question"],
            tool_hint=call.get("tool_hint", "general-web"),
            depth=depth,
            rationale=call.get("rationale", ""),
        ))
    elif action == "chase_reference" and call.get("reference_url"):
        refinements.append(SubQuestion(
            id=f"{sub_q.id}.ref{len(refinements)+1}",
            question=call.get("question", f"Follow reference: {call['reference_url']}"),
            tool_hint=call.get("tool_hint", "general-web"),
            depth=depth,
            rationale=call.get("rationale", ""),
            parent_arxiv_id=call["reference_url"] if "arxiv" in call["reference_url"] else None,
        ))
    # revise_strategy: handled differently — see Step 4

return (answer_md, citations, refinements)  # extend the tuple
```

**Update the `research()` signature and caller** — change return type to `tuple[str, list[Citation], list[SubQuestion]]`.

---

## Step 3 — Absorb refinements in the deep loop

**File:** `deep_research/paths/deep.py`

**Changes around lines 90–102:**

After each researcher result, absorb refinements into state:

```python
for sq, r in zip(pending, results):
    if isinstance(r, Exception):
        ...  # existing error handling
        continue
    if not isinstance(r, tuple) or len(r) not in (2, 3):
        ...  # existing error handling
        continue
    answer_md, citations = r[0], r[1]
    state.absorb_section(sq.id, citations, answer_md)
    # NEW: absorb refinements if present
    if len(r) == 3:
        refinements = r[2]
        state.absorb_refinements(refinements)
        if refinements:
            logger.info("researcher %s emitted %d refinements", sq.id, len(refinements))
```

**Before the critic call (around line 104):**

```python
# Flush any refinements emitted by researchers into the plan
state.flush_refinements()
```

---

## Step 4 — `revise_strategy` handling (Option A — simple ack)

`revise_strategy` is different from drill_deeper/chase_reference because it needs to affect the *current* researcher's ongoing work, not the next iteration.

**Option A (simple):** Treat it like a prompt injection — the refine handler returns a message that changes the LLM's system instructions mid-loop:

```python
async def _refine_handler(**kwargs) -> ToolResult:
    if kwargs.get("action") == "revise_strategy":
        strategy = kwargs.get("rationale", "")
        return ToolResult(content=(
            f"Strategy update noted: {strategy}. "
            "You should adjust your remaining tool calls accordingly."
        ))
    # ... else collect for later
```

The tool response goes straight back to the LLM, and the LLM can self-correct. No extra plumbing needed.

**Recommendation:** Use Option A. It's zero-code-change for the loop.

---

## Step 5 — Update the researcher prompt

**File:** `deep_research/prompts/researcher.txt`

Append instructions about the `refine` tool at the end:

```text
## Dynamic refinement (optional)

You have access to a `refine` tool. Use it when:

1. **chase_reference** — you found an article/paper that is clearly important
   and should be investigated as its own sub-question. Pass its URL and a
   brief rationale.
2. **drill_deeper** — you uncovered an interesting subtopic that deserves
   its own dedicated research. Provide the new question and any hints.
3. **revise_strategy** — you realize the current approach won't work
   (e.g., the sources don't match the sub-question). Provide a revised
   strategy in the rationale.

Calling `refine` does NOT end your work — you still need to answer the
current sub-question. Refinements take effect in the next iteration.
```

---

## Step 6 — Add configuration knobs

**File:** `deep_research/config.py`

Add to `AgentConfig`:

```python
class AgentConfig(BaseModel):
    ...
    max_refinement_depth: int = 2       # how many levels of drill-deeper nesting
    max_refinement_per_researcher: int = 3  # max refine calls per researcher
```

The `research()` function should enforce `max_refinement_per_researcher` by capping the collector.

---

## Step 7 — Depth capping for recursive drilling

In `researcher.py`, when constructing refinements, skip if `depth >= config.max_refinement_depth`:

```python
depth = sub_q.depth + 1
if depth > config.max_refinement_depth:
    logger.info("refinement skipped: depth %d exceeds max %d", depth, config.max_refinement_depth)
    continue
```

---

## Step 8 — Tests

**New file:** `tests/test_refine_tool.py`

Cover:

1. LLM calls `refine("drill_deeper")` → refinement is collected and returned
2. LLM calls `refine("chase_reference")` with URL → SubQuestion created with correct `parent_arxiv_id`
3. LLM calls `refine("revise_strategy")` → ack returned, no collector entry
4. Depth cap prevents refinements beyond `max_refinement_depth`
5. Multiple refine calls within same researcher → all collected
6. Refine with missing `question` (drill_deeper) → gracefully skipped
7. Integration: refinements are flushed into plan before critic runs
8. Dedup by question text in `absorb_refinements`

---

## File Change Summary

| File | Lines changed | What |
|---|---|---|
| `state.py` | +4 fields, +2 methods | `pending_refinements`, `absorb_refinements()`, `flush_refinements()` |
| `nodes/researcher.py` | ~30 lines | `REFINE_SCHEMA`, handler, collector, return tuple extension |
| `paths/deep.py` | ~15 lines | Absorb refinements from results, flush before critic |
| `prompts/researcher.txt` | +16 lines | Instructions for when/how to use `refine` |
| `config.py` | +2 fields | `max_refinement_depth`, `max_refinement_per_researcher` |
| `tests/test_refine_tool.py` | ~120 lines | 8 test cases |

---

## Execution Order

1. `state.py` — add fields and methods
2. `config.py` — add knobs
3. `nodes/researcher.py` — add schema, handler, collector, return refinements
4. `paths/deep.py` — absorb refinements, flush before critic
5. `prompts/researcher.txt` — add instructions
6. `tests/test_refine_tool.py` — write and verify
7. Run `pytest tests/` to confirm nothing breaks
