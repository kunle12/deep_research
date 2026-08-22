# REST API Reference

This document describes the HTTP API exposed by this repository. There are two
servers:

1. **Web UI / library API** — `deep_research.webui.app` (the recommended HTTP
   interface; also serves the vanilla-JS frontend).
2. **Minimal microservice** — `deep_research.microservice` (single `POST
   /research` for programmatic use).

Both are FastAPI apps, so a machine-readable OpenAPI spec is served at runtime
at `/openapi.json` and browsable at `/docs`. A generated snapshot of each spec
is committed alongside this doc:

- `deep_research/api.openapi.json` — the web UI app.
- `deep_research/microservice.openapi.json` — the minimal microservice.

The web UI binds `127.0.0.1:8080` by default (`uv run deep-research-web`, or
`uv run uvicorn deep_research.webui.app:app --host 127.0.0.1 --port 8080`);
the Dockerfile runs it on `0.0.0.0:8080`.

---

## Web UI API (`/api`)

All responses are JSON unless noted. Destructive endpoints are gated on
`?confirm=true`. Security headers (CSP, `X-Content-Type-Options: nosniff`,
`Referrer-Policy`) are set on every response.

### Health

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/health` | `{"status": "ok"}` |

### Research jobs — `/api/research`

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/research` | Start a research job. Body `ResearchStartRequest`. Returns `202` + `job_id`. |
| GET | `/api/research/jobs` | List all jobs (most recent first) as `ResearchJobStatus[]`. |
| GET | `/api/research/jobs/{job_id}` | Status of one job. `404` if not found. |
| POST | `/api/research/jobs/{job_id}/cancel` | Cancel + discard checkpoint. `404` if not found. |
| POST | `/api/research/jobs/{job_id}/pause` | Pause a running job (per-iteration granularity). `409` if not running. |
| POST | `/api/research/jobs/{job_id}/resume` | Resume a paused job. `409` if not paused / another job is running. |
| POST | `/api/research/jobs/{job_id}/abandon` | Abandon a finished job. `409` if still running (cancel first). |
| GET | `/api/research/jobs/{job_id}/stream` | SSE event stream for one job (see below). |

**`POST /api/research` request body** (`ResearchStartRequest`):

| Field | Type | Notes |
|-------|------|-------|
| `query` | `str` | Required, 1–1000 chars. |
| `path_override` | `str?` | `"quick"` \| `"deep"` \| `"academic"` \| `"url_source"`. |
| `attach_to_run_id` | `str?` | Attach mode: `query` must be an `http(s)` URL and the target report must exist. |

Only one research job runs at a time; a second `POST` returns `409` ("a research
job is already running"). Jobs live in memory, so a restart drops running jobs,
but checkpoints survive on disk and are restored automatically as resumable
paused jobs.

**SSE stream** (`GET /api/research/jobs/{job_id}/stream`) — `text/event-stream`.
Emits a `data:` JSON line per event with a `type` field; a status snapshot is
sent first so late subscribers see the current state, then keepalive comments
every ~15s while idle. Terminal event types end the stream.

### Reports — `/api/reports`

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/reports` | List/search reports (see query params). |
| GET | `/api/reports/{run_id}` | Full detail: markdown, citations, tags, glossary, download URLs. |
| GET | `/api/reports/{run_id}/markdown` | Plain markdown, `text/markdown`, `Content-Disposition: inline`. |
| GET | `/api/reports/{run_id}/glossary/markdown` | Glossary as markdown download. |
| GET | `/api/reports/{run_id}/bibliography` | Bibliography as markdown download. |
| GET | `/api/reports/{run_id}/bibliography/bib` | BibTeX (`.bib`) download, `application/x-bibtex`. |
| GET | `/api/reports/{run_id}/pdf` | Archived report PDF, `application/pdf`. `404` if none archived. |
| POST | `/api/reports/{run_id}/tags` | Add a tag. Body `{tag: str}`. Returns `TagUpdateResponse`. |
| DELETE | `/api/reports/{run_id}/tags?tag=...` | Remove a tag. Returns `TagUpdateResponse`. |
| PATCH | `/api/reports/{run_id}` | Rename. Body `RenameReportRequest {query}`; rewrites the first markdown heading to match. |
| POST | `/api/reports/{run_id}/merge` | Merge with other reports (see below). |
| DELETE | `/api/reports/{run_id}?confirm=true` | Delete report + its files. `400` without `confirm=true`. |
| DELETE | `/api/reports/{run_id}/references?confirm=true` | Remove a reference (see below). |

**`GET /api/reports` query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `limit` | `int` | 50 | 1–200. |
| `offset` | `int` | 0 | |
| `q` | `str?` | — | Full-text search (1–200 chars). |
| `tag` | `str?` | — | Filter by tag (≤64 chars). |
| `path` | `str?` | — | Filter by path taken (≤32 chars). |

Response `ReportListResponse`: `{total, limit, offset, items: ReportListItem[]}`.
Each item carries `run_id`, `started_at`, `completed_at`, `query`, `path`,
`iterations`, `tags`, `snippet`, `citation_count`, `markdown_length`, `has_pdf`.

**`POST /api/reports/{run_id}/merge`** body (`MergeReportsRequest`):
`other_run_ids` (1–19), optional `name`, optional `delete_sources`. Runs inline
(a single LLM call); returns `MergeReportsResponse {run_id, query}`. `422` if
fewer than two distinct reports; `404` if a listed report is missing.

**`DELETE /api/reports/{run_id}/references`** body
(`DeleteReportReferenceRequest`): `url` (required, 3–2000 chars) and optional
`arxiv_id` (≤64). Removes every citation matching the URL or arXiv id,
regenerates the markdown `## Bibliography` section, and deletes the locally
archived copy if no other report still cites it. Returns the updated
`ReportDetail`. `400` without `confirm=true`, `404` if the reference is absent.

### Artifacts — `/api/artifacts`

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/artifacts` | List artifacts (see query params). |
| GET | `/api/artifacts/{artifact_id}` | Detail: analyses, citation edges, tags, image URL. |
| GET | `/api/artifacts/{artifact_id}/pdf` | Serve an archived PDF artifact. |
| GET | `/api/artifacts/{artifact_id}/image` | Serve an archived screenshot (`kind="image"`), `image/png`. |
| DELETE | `/api/artifacts/{artifact_id}?confirm=true` | Delete artifact + files. `409` if it is a report's own output. |
| POST | `/api/arxiv/pdf` | On-demand arXiv PDF archive (see below). |

**`GET /api/artifacts` query params:**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `limit` | `int` | 50 | 1–200. |
| `offset` | `int` | 0 | |
| `q` | `str?` | — | Full-text search (1–200 chars). |
| `kind` | `str?` | — | Filter by kind (≤32 chars, e.g. `pdf`, `image`). |

**`POST /api/arxiv/pdf`** body (`ArxivPdfBody`): `arxiv_id` (3–64 chars).
Downloads + archives one arXiv paper PDF, returns
`ArxivPdfResponse {local_pdf_url, archived, error}`. `422` for invalid id;
returns `error` (not an HTTP error) when downloads are disabled or the paper is
not open access.

### Tags, search, stats

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/tags?limit=` | `TagInfo[]` `{tag, count}`; default limit 200, max 1000. |
| GET | `/api/search?q=&limit=` | Combined report + artifact search. `q` required (1–200), `limit` default 20 (1–100). Returns `SearchResponse {q, reports, artifacts}`. |
| GET | `/api/stats` | `StatsResponse {reports, artifacts, tags}` counts. |

---

## Minimal microservice (`microservice.py`)

A single synchronous `POST /research` that runs the agent to completion and
returns the report — no job queue, no SSE. Useful for one-shot programmatic
calls.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/health` | `{"status": "ok"}`. |
| POST | `/research` | Run a full research query. |

**`POST /research` request body** (`ResearchRequest`):

| Field | Type | Notes |
|-------|------|-------|
| `query` | `str` | Required. |
| `path_override` | `str?` | `"quick"` \| `"deep"` \| `"academic"` \| `"url_source"`. |
| `config_path` | `str` | Default `"config.yaml"`; must resolve inside the working directory. |

Response `ResearchResponse`: `{markdown, path, citations: [], iterations}`.

Errors: `400` if `config_path` escapes the allowed directory, `500` on internal
failure, `504` if the request exceeds the 10-minute hard timeout.

---

## Regenerating the committed specs

The committed `*.openapi.json` files are generated from the running apps:

```bash
uv run python -c "
import json
from deep_research.webui.app import app
json.dump(app.openapi(), open('deep_research/api.openapi.json', 'w'), indent=2, sort_keys=True)
from deep_research.microservice import app as ms
json.dump(ms.openapi(), open('deep_research/microservice.openapi.json', 'w'), indent=2, sort_keys=True)
"
```

Re-run this whenever a router/endpoint or request/response model changes, and
update the tables above to match.
