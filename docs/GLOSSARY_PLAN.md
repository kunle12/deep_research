# Glossary Extraction Plan

## Problem

`deep-research-library glossary` returns empty because:
1. Glossary entries are only created as a side-effect of research runs
2. The glossary prompt (`prompts/glossary_extract.txt:15`) says "Output ONLY valid JSON" but is appended to system messages that ask for Markdown reports (academic path, deep/writer path). The LLM follows the primary instruction → outputs Markdown → `json.loads()` fails → no entries saved.
3. The quick path works (uses `response_format=json_object`) but academic/deep paths don't.

## Plan

### 1. Fix the contradictory prompt

**File**: `prompts/glossary_extract.txt`
- Change "Output ONLY valid JSON — no markdown, no code fences" to "At the end of your response, include a glossary section with a JSON array of extracted terms."
- The LLM can now output a markdown report AND include a glossary JSON block at the end.

### 2. Update the glossary parser to handle markdown+JSON

**File**: `nodes/glossarize.py`
- `parse_glossary_from_response`: Try `json.loads()` first (works for quick path's JSON-only output). If that fails, use regex to extract a JSON block from markdown (works for academic/deep paths).
- `extract_and_save_glossary`: Return `list[GlossaryEntry]` so callers can store them on the Report.

### 3. Store glossary entries on Report

**File**: `state.py`
- Add `glossary_entries: list[GlossaryEntry] = []` to `Report` dataclass.

**Files**: `paths/quick.py`, `paths/academic.py`, `nodes/writer.py`
- Store returned glossary entries on the Report object before returning.

### 4. Save glossary as a separate JSON file

**File**: `cli/app.py`
- Add `--glossary-out` / `-go` CLI option.
- After report is rendered and saved, if `report.glossary_entries` is non-empty, write them to the specified path (or `{out}_glossary.json` as default).

### 5. Wire `--find` to backend FTS

**File**: `library/cli.py`
- Change `--find` to call `backend.glossary_search()` instead of Python substring matching.
- Add `--limit` / `-L` option for result count.

### 6. Fix Postgres backend

**File**: `library/storage/postgres_backend.py`
- Add `upsert_glossary_entries` method (plural) to match SQLite backend and what `LibraryWriter` expects.

### Files changed summary

| File | Change |
|---|---|
| `prompts/glossary_extract.txt` | Fix contradictory instruction |
| `nodes/glossarize.py` | Add markdown+JSON parsing, return entries |
| `state.py` | Add `glossary_entries` to Report |
| `paths/quick.py` | Store glossary entries on Report |
| `paths/academic.py` | Store glossary entries on Report |
| `nodes/writer.py` | Store glossary entries on Report |
| `cli/app.py` | Add `--glossary-out` flag, save glossary JSON |
| `library/cli.py` | Wire `--find` to `backend.glossary_search()`, add `--limit` |
| `library/storage/postgres_backend.py` | Add `upsert_glossary_entries` |

No extra LLM calls, no schema changes, no contradictory prompts.
