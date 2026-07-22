# Dynamic Refinement During Research — Implementation Plan

## Overview

Add a **`refine`** tool to the researcher's tool-calling loop that lets the LLM emit refinement requests as "side effects" during its tool loop. These requests are collected, validated, and flushed into `ResearchState` after the researcher finishes — so they become available to the next iteration.

---

## Design Decisions

### D1 — Per-researcher scoped registry (not shared mutation)

`ToolRegistry.register()` raises `ValueError` on duplicate names (`tool_loop.py:91`).
All parallel researchers share the **same** `ToolRegistry` instance, so registering
`"refine"` inside `research()` would crash on the second concurrent call and leak
collector state across researchers.

**Solution:** Introduce a lightweight `ScopedToolRegistry` that wraps the shared
parent registry and adds per-researcher tools. Each `research()` call creates its
own scope with its own collector — the shared registry is never mutated.

### D2 — Separate `refinement_depth` field

`SubQuestion.depth` is documented as "recursion depth in academic mode"
(`state.py:64`). Repurposing it for deep-path refinement nesting creates
ambiguity. A dedicated `refinement_depth: int = 0` field keeps the two
concepts independent.

### D3 — `revise_strategy` is best-effort

Option A (echo a strategy-update message back to the LLM) provides **no
enforcement guarantee** — the LLM may ignore the suggestion. This is
acceptable for v1 because it requires zero loop changes, but the prompt
wording should make the best-effort nature clear to avoid false expectations.

### D4 — Three-level cap hierarchy

| Cap | Scope | Config knob | Default |
|-----|-------|-------------|---------|
| Per-researcher | single `research()` call | `max_refinement_per_researcher` | 3 |
| Per-iteration | all researchers in one iteration | `max_total_refinements_per_iteration` | 6 |
| Depth | recursive drill-deeper nesting | `max_refinement_depth` | 2 |

Without the per-iteration cap, 6 researchers × 3 refinements = 18 new
sub-questions per iteration (plus critic gaps) can explode the plan and
prevent convergence within `max_iterations`.

### D5 — Normalized dedup

`absorb_refinements` deduplicates by question text. Exact-string matching
misses semantically identical refinements that differ in casing or whitespace.
Normalize with `.strip().lower()` before comparison.

---

## Step 1 — Add refinement state to `ResearchState`

**File:** `deep_research/state.py`

**Changes:**

```python
# Add new field to SubQuestion (after line 67)
class SubQuestion(BaseModel):
    ...
    parent_arxiv_id: str | None = None
    # NEW: how many refine-drill levels deep this question is (deep path only)
    refinement_depth: int = 0

# Add new field to ResearchState (after line 106)
class ResearchState(BaseModel):
    ...
    iteration: int = 0
    # NEW: refinements emitted by researchers mid-loop
    pending_refinements: list[SubQuestion] = Field(default_factory=list)

# Add new methods:
    def absorb_refinements(self, refinements: list[SubQuestion]) -> None:
        """Deduplicate and absorb refinements emitted by a researcher."""
        existing_qs = {sq.question.strip().lower() for sq in self.plan.sub_questions}
        existing_qs.update(sq.question.strip().lower() for sq in self.pending_refinements)
        for r in refinements:
            key = r.question.strip().lower()
            if r.question and key not in existing_qs:
                self.pending_refinements.append(r)
                existing_qs.add(key)

    def flush_refinements(self, max_total: int | None = None) -> list[SubQuestion]:
        """Move pending refinements into the plan and return them.

        If max_total is set, only flush up to that many (excess is discarded).
        """
        flushed = list(self.pending_refinements)
        if max_total is not None and len(flushed) > max_total:
            logger.info(
                "flush_refinements: capping %d → %d", len(flushed), max_total,
            )
            flushed = flushed[:max_total]
        self.plan.sub_questions.extend(flushed)
        self.pending_refinements.clear()
        return flushed
```

---

## Step 2 — Add `ScopedToolRegistry` and the `refine` tool

### 2a — ScopedToolRegistry

**File:** `deep_research/llm/tool_loop.py`

Add a lightweight wrapper so each researcher can register per-call tools
without mutating the shared registry:

```python
class ScopedToolRegistry:
    """Wraps a parent ToolRegistry, adding per-scope tools.

    Used by the researcher to inject the `refine` tool without mutating
    the shared registry (which would crash on duplicate registration
    when multiple researchers run in parallel).
    """

    def __init__(self, parent: ToolRegistry) -> None:
        self._parent = parent
        self._extra_tools: dict[str, ToolFunc] = {}
        self._extra_schemas: list[dict] = []

    def register(self, name: str, func: ToolFunc, schema: dict) -> None:
        wrapped = {
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {}),
            },
        }
        self._extra_tools[name] = func
        self._extra_schemas.append(wrapped)

    def names(self) -> list[str]:
        return self._parent.names() + list(self._extra_tools)

    def schemas(self) -> list[dict]:
        return self._parent.schemas() + self._extra_schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name in self._extra_tools:
            try:
                return await self._extra_tools[name](**arguments)
            except Exception as e:
                logger.exception("scoped tool %s raised", name)
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")
        return await self._parent.call(name, arguments)
```

`run_with_tools` already accepts any object with `.schemas()` and `.call()`,
so no changes to the loop are needed — just pass the scoped registry.

### 2b — Refine tool schema and handler

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

**Updated `research()` signature** — add config knobs as explicit parameters
(keeps the function decoupled from `AgentTopConfig`):

```python
async def research(
    sub_q: SubQuestion,
    client: AsyncOpenAI,
    model: str,
    tools: ToolRegistry,
    max_turns: int = 8,
    prior_context: str = "",
    max_refinement_per_researcher: int = 3,
    max_refinement_depth: int = 2,
) -> tuple[str, list[Citation], list[SubQuestion]]:
```

**Wrapper logic inside `research()` (before calling `run_with_tools`):**

```python
scoped = ScopedToolRegistry(tools)
refine_calls_collector: list[dict] = []

async def _refine_handler(**kwargs) -> ToolResult:
    if kwargs.get("action") == "revise_strategy":
        strategy = kwargs.get("rationale", "")
        return ToolResult(content=(
            f"Strategy update noted: {strategy}. "
            "You should adjust your remaining tool calls accordingly."
        ))
    if len(refine_calls_collector) >= max_refinement_per_researcher:
        return ToolResult(content="Refinement budget exhausted for this sub-question.")
    refine_calls_collector.append(dict(kwargs))
    return ToolResult(content="Refinement queued. Continue your research.")

scoped.register("refine", _refine_handler, REFINE_SCHEMA["function"])
```

Pass `scoped` (not `tools`) to `run_with_tools`.

**After `run_with_tools` returns**, convert collected calls to `SubQuestion` objects:

```python
refinements: list[SubQuestion] = []
ref_depth = sub_q.refinement_depth + 1
for call in refine_calls_collector:
    if ref_depth > max_refinement_depth:
        logger.info("refinement skipped: depth %d exceeds max %d", ref_depth, max_refinement_depth)
        break
    action = call.get("action")
    if action == "drill_deeper" and call.get("question"):
        refinements.append(SubQuestion(
            id=f"{sub_q.id}.refine{len(refinements)+1}",
            question=call["question"],
            tool_hint=call.get("tool_hint", "general-web"),
            refinement_depth=ref_depth,
            rationale=call.get("rationale", ""),
        ))
    elif action == "chase_reference" and call.get("reference_url"):
        refinements.append(SubQuestion(
            id=f"{sub_q.id}.ref{len(refinements)+1}",
            question=call.get("question", f"Follow reference: {call['reference_url']}"),
            tool_hint=call.get("tool_hint", "general-web"),
            refinement_depth=ref_depth,
            rationale=call.get("rationale", ""),
            parent_arxiv_id=call["reference_url"] if "arxiv" in call["reference_url"] else None,
        ))

return (answer_md, citations, refinements)
```

---

## Step 3 — Absorb refinements in the deep loop

**File:** `deep_research/paths/deep.py`

### 3a — Update `_run_one_researcher_with_recall`

Change return annotation and forward new config knobs:

```python
async def _run_one_researcher_with_recall(
    sq: SubQuestion,
    client: AsyncOpenAI,
    config: AgentTopConfig,
    tools: ToolRegistry,
    storage: Any | None,
) -> tuple[str, list, list]:
    ...
    return await researcher_run(
        sq, client, config.llm.text_model, tools,
        max_turns=config.agent.researcher_max_turns,
        prior_context=prior_context,
        max_refinement_per_researcher=config.agent.max_refinement_per_researcher,
        max_refinement_depth=config.agent.max_refinement_depth,
    )
```

### 3b — Absorb refinements from results

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
            reporter.step("deep.research.refine", f"{sq.id} (+{len(refinements)})")
```

### 3c — Flush before critic with global cap

```python
flushed = state.flush_refinements(
    max_total=config.agent.max_total_refinements_per_iteration,
)
if flushed:
    logger.info("flushed %d refinements into plan", len(flushed))
    reporter.step("deep.refine.flush", f"{len(flushed)} new sub-q(s)")
```

---

## Step 4 — `revise_strategy` handling (Option A — best-effort ack)

`revise_strategy` is different from drill_deeper/chase_reference because it needs to affect the *current* researcher's ongoing work, not the next iteration.

**Option A (simple):** Treat it like a prompt injection — the refine handler returns a message that nudges the LLM to self-correct mid-loop:

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

**Caveat:** This is best-effort — the LLM may ignore the suggestion. There is
no enforcement mechanism. Acceptable for v1; a future version could inject a
system-message override into the conversation history for stronger steering.

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
   strategy in the rationale. Note: this is a best-effort hint to yourself;
   it does not change any system configuration.

Calling `refine` does NOT end your work — you still need to answer the
current sub-question. Refinements take effect in the next iteration.
You may call `refine` at most a few times per sub-question.
```

---

## Step 6 — Add configuration knobs

**File:** `deep_research/config.py`

Add to `AgentConfig`:

```python
class AgentConfig(BaseModel):
    ...
    max_refinement_depth: int = 2
    max_refinement_per_researcher: int = 3
    max_total_refinements_per_iteration: int = 6
```

| Knob | Purpose |
|------|---------|
| `max_refinement_depth` | Max recursive drill-deeper nesting levels |
| `max_refinement_per_researcher` | Max `refine` calls per single researcher |
| `max_total_refinements_per_iteration` | Global cap per iteration (prevents plan explosion) |

---

## Step 7 — Depth capping for recursive drilling

In `researcher.py`, when constructing refinements, skip if
`refinement_depth >= max_refinement_depth`:

```python
ref_depth = sub_q.refinement_depth + 1
if ref_depth > max_refinement_depth:
    logger.info("refinement skipped: depth %d exceeds max %d", ref_depth, max_refinement_depth)
    break
```

Uses `SubQuestion.refinement_depth` (not `depth`) to avoid overloading the
academic-path field.

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
8. Dedup by normalized question text in `absorb_refinements` (case/whitespace insensitive)
9. **Concurrent isolation:** two researchers running in parallel both call `refine` → each gets only its own refinements (validates `ScopedToolRegistry`)
10. **Per-researcher cap:** exceeding `max_refinement_per_researcher` returns budget-exhausted ack, no extra collector entry
11. **Global iteration cap:** `flush_refinements(max_total=N)` discards excess refinements
12. **ScopedToolRegistry:** parent tools still callable through the scope; scoped tool not visible to parent

---

## File Change Summary

| File | Lines changed | What |
|---|---|---|
| `state.py` | +1 field (SubQuestion), +1 field (ResearchState), +2 methods | `refinement_depth`, `pending_refinements`, `absorb_refinements()`, `flush_refinements()` |
| `llm/tool_loop.py` | +~40 lines | `ScopedToolRegistry` class |
| `nodes/researcher.py` | ~40 lines | `REFINE_SCHEMA`, scoped handler, collector, return tuple extension, new params |
| `paths/deep.py` | ~20 lines | Absorb refinements, flush with cap, progress reporting, wrapper update |
| `prompts/researcher.txt` | +18 lines | Instructions for when/how to use `refine` |
| `config.py` | +3 fields | `max_refinement_depth`, `max_refinement_per_researcher`, `max_total_refinements_per_iteration` |
| `tests/test_refine_tool.py` | ~180 lines | 12 test cases |

---

## Execution Order

1. `llm/tool_loop.py` — add `ScopedToolRegistry`
2. `state.py` — add `refinement_depth`, `pending_refinements`, methods
3. `config.py` — add knobs
4. `nodes/researcher.py` — add schema, scoped handler, collector, return refinements
5. `paths/deep.py` — absorb refinements, flush with cap before critic, progress reporting
6. `prompts/researcher.txt` — add instructions
7. `tests/test_refine_tool.py` — write and verify
8. Run `pytest tests/` to confirm nothing breaks
