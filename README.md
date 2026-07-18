# Deep Research Agent

A standalone, async-first Python agent that performs **web / deep / academic / single-source URL** research with the assistance of an LLM. Multi-modal LLM support enables vision-based PDF figure comprehension.

## Status

**Phases 1–12 complete.** All planned features (auto-routing, deep/quick/academic/url-source/applied paths, PDF vision, Playwright MCP, citation-graph mining, blog search, personal digital library, glossary generation, Reddit access, Postgres backend, refresh scheduler, FastAPI microservice, CLI with rich live progress) are implemented and tested. See [`docs/PLAN.md`](docs/PLAN.md) and [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md).

---

## Table of contents

- [Design highlights](#design-highlights)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Configure](#configure)
- [Use](#use)
  - [CLI](#cli-recommended)
  - [As a library](#as-a-library-microservice-ready)
- [Personal Digital Library (planned, P10.5)](#personal-digital-library-planned-p10-5)
- [Architecture](#architecture)
- [Follow-up trigger phrases](#follow-up-trigger-phrases)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Design highlights

- **Four routing modes** (auto-classifier picks one, or override via flag):
  - `quick` — 1 web search + summarize top-k. ~5–15 seconds.
  - `deep` — planner → parallel sub-question research → critic loop → writer.
  - `academic` — bounded recursive citation graph traversal (depth ≤ 2, papers ≤ 15).
  - `url_source` — analyze a single URL (arxiv / PDF / HTML blog) and produce a structured report with optional follow-up research.
- **PDF vision**: every page is downscaled via PIL and sent through a multimodal LLM (Qwen3.5-VL etc.) for figure/equation comprehension.
- **No agent frameworks**: raw `asyncio` for orchestration (no LangChain/LangGraph).
- **CLI now**, but `await run_research(query, config) -> Report` is the importable **public microservice entrypoint**.
- **MIT-licensed stack**: no AGPL contamination. PDF rendering uses `pdf2image` (subprocess to system `poppler`, not bundled).

---

## Prerequisites

### Python

- **Python 3.11, 3.12, or 3.13** required.
- [uv](https://docs.astral.sh/uv/) for dependency management:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### System binary: poppler (for PDF rendering)

`tools/pdf.py` uses `pdf2image`, which shells out to `poppler-utils`. It must be installed on your system:

| OS | Command |
|---|---|
| macOS (Homebrew) | `brew install poppler` |
| Debian / Ubuntu | `sudo apt-get install poppler-utils` |
| Arch | `sudo pacman -S poppler` |
| Fedora | `sudo dnf install poppler-utils` |
| Windows | Download from [poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases) and set `pdf_vision.poppler_path` in config.yaml |

Verify:
```bash
pdftoppm -v
```

### Optional: Node.js LTS (for Playwright MCP in P8)

Dynamic-HTML pages fall back to a Playwright MCP subprocess launched via `npx`:

```bash
brew install node     # or: https://nodejs.org/
```

Only needed if you enable `browser.enabled: true` in `config.yaml`.

---

## Install

```bash
git clone <your-fork-url> deep_research
cd deep_research
uv sync                       # core deps
uv sync --extra reddit        # + Reddit access (optional)
uv sync --extra dev           # + dev/test tools
```

## Configure

Copy the example config and edit to taste:

```bash
cp config.example.yaml config.yaml
```

Minimum useful config:

```yaml
llm:
  base_url: "http://localhost:8000/v1"
  api_key: "any-key-your-server-expects"
  text_model: "qwen3.5-122b"
  vision_model: "qwen3.5-122b"

search:
  primary: "tavily"
  tavily:
    api_key_env: "TAVILY_API_KEY"   # export TAVILY_API_KEY=...
```

Environment variables respected:

- `OPENAI_API_KEY` (or whatever your LLM service expects)
- `TAVILY_API_KEY`
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` (when Reddit enabled)

---

## Use

### CLI (recommended)

Auto-routing (default):

```bash
uv run python -m deep_research "What is the capital of France?"
uv run python -m deep_research "Survey recent advances in RLHF"
uv run python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"
uv run python -m deep_research "https://blog.example.com/post — what are its gaps?"
```

Force a path:

```bash
uv run python -m deep_research --quick "..."
uv run python -m deep_research --deep "..." --max-iterations 5
uv run python -m deep_research --academic "..." --max-depth 2 --max-papers 20 --dump-graph refs.bib
uv run python -m deep_research --url-source "https://example.com/foo.pdf" "verify its claims"
```

Output controls:

```bash
uv run python -m deep_research "..." --out report.md --cite citations.json
uv run python -m deep_research "..." --format json --out report.json
uv run python -m deep_research "..." --quiet        # suppress the live progress panel; logs only
uv run python -m deep_research "..." --verbose      # debug-level logs + rich traceback on error
```

Library CLI:

```bash
uv run deep-research-library ls                     # list recent reports
uv run deep-research-library find "transformer"     # full-text search
uv run deep-research-library show <artifact_id>     # artifact details
uv run deep-research-library tag <id> <tag>         # tag an artifact
uv run deep-research-library stats                  # library statistics
uv run deep-research-library prune --older-than 90  # prune old artifacts
uv run deep-research-library export-bibtex refs.bib # export as BibTeX
uv run deep-research-library refresh                # refresh stale artifacts
uv run deep-research-library glossary               # browse glossary
```

Refresh scheduler:

```bash
uv run deep-research-scheduler                      # run refresh daemon
```

When stdout is a TTY (the common case), the CLI renders a live status panel (`phase` / `elapsed` / recent `step`s) using [`rich.live.Live`](https://rich.readthedocs.io/en/stable/live.html). When stdout is piped to a file or captured by a non-interactive consumer (e.g. unit tests, cron), the live panel auto-disables — no flicker, just the final report.
Pass `--quiet` / `-q` to force-disable the panel regardless of the TTY check.

### As a library (microservice-ready)

```python
import asyncio
from pathlib import Path
from deep_research import run_research, AgentTopConfig
from deep_research.report import render_report_bibtex

config = AgentTopConfig.load_yaml("config.yaml")

async def main():
    # `progress` is optional; library callers by default get a silent run.
    report = await run_research("Survey RLHF methods", config)
    print(report.markdown)
    # Save outputs manually (or use the report/ renderers):
    Path("report.md").write_text(report.markdown)
    if report.citation_graph and report.citation_graph.nodes:
        Path("refs.bib").write_text(render_report_bibtex(report))

asyncio.run(main())
```

`run_research()` accepts an optional `path_override` (`"quick"` / `"deep"` / `"academic"` / `"url_source"`) — exactly matching the `--quick` / `--deep` / `--academic` / `--url-source` CLI flags — and an optional `progress: ProgressReporter | None` for streaming status updates. Pass `None` (or `NullReporter()`) for silent runs; pass your own implementation of `ProgressReporter` to integrate progress into a UI or log pipeline.

---

## Personal Digital Library (P10.5, P12)

The Personal Digital Library (`pdl.enabled: true` by default) archives every research artifact to `.deep_research_library/`:

- **Artifact store**: PDFs, HTML pages, and reports archived with content-addressable dedup (SHA-256[:16])
- **Metadata DB**: SQLite `index.db` with artifacts, reports, analyses, citation edges, tags, glossary, FTS5 full-text search
- **Postgres backend** (P12.0): alternative storage via asyncpg when `pdl.storage.backend: "postgres"` with `DEEP_RESEARCH_PG_DSN` env var
- **Refresh scheduler** (P12.0): `deep-research-scheduler` daemon that periodically probes upstream URLs for changes
- **Library CLI** (P12.0): `deep-research-library ls|find|show|tag|stats|prune|export-bibtex|refresh|glossary`
- **Glossary** (P10.6): per-run LLM extraction, cross-run dedup, FTS5 search

### Prerequisites

| OS | Command | What |
|---|---|---|
| macOS (Homebrew) | `brew install pango cairo` | weasyprint native deps |
| Debian / Ubuntu | `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2` | weasyprint native deps |

When native deps absent, falls back to `xhtml2pdf` (logged as `WARNING`).

---

## Architecture

See [`docs/PLAN.md`](docs/PLAN.md) for the full design document. In short:

```
deep_research/
├── agent.py              # run_research() public entrypoint + routing
├── config.py             # AgentTopConfig pydantic schema
├── state.py              # ResearchState, Citation, Report, CitationGraph, ...
├── citations.py          # dedup + bibliography formatter (markdown + bibtex)
├── progress.py           # ProgressReporter Protocol + NullReporter
├── scheduler.py          # Refresh scheduler daemon (P12.0)
├── microservice.py       # FastAPI microservice (P12.0)
├── llm/                  # OpenAI-compatible async client, vision utils, tool-calling loop
├── paths/                # classifier, quick, deep, academic, url_source, applied runners
├── nodes/                # planner, researcher, critic, writer, analyze_{paper,source}, glossarize
├── tools/                # web_search, fetch_page, arxiv, pdf, browser, reddit, blog_search
├── report/               # markdown, bibtex, json_export renderers
├── library/              # Personal Digital Library (P10.5)
│   ├── writer.py         # LibraryWriter middleware
│   ├── cli.py            # Library CLI commands
│   └── storage/          # StorageBackend Protocol + SQLite + Postgres backends
├── cli/
│   ├── app.py            # typer shell
│   └── progress.py       # RichProgressReporter
└── prompts/              # .txt prompt templates
```

---

## Follow-up trigger phrases

URL-source mode only spawns the deep path for follow-up research when the user's query *explicitly* asks for critique / gaps / verification. The default phrase list (case-insensitive substring match):

```
gaps, what's missing, what is missing, omitted, not mentioned,
limitation, limitations, shortcoming, shortcomings, weakness, weaknesses, flaw, flaws,
counterexample, counterexamples, refute, refutation, disprove, disprove,
verify, validate, falsify, check the claims, fact-check, fact check,
comparison of, compare to, alternative, alternatives, competing,
what else, what other
```

Custom phrases can be appended via yaml:

```yaml
url_source:
  follow_up_trigger_phrases:
    - "pull request the methodology"
    - "where does the math break"
```

The list lives in the same `paths.url_source._DEFAULT_TRIGGER_PHRASES` constant and is documented in the risk register (item 12) of [`docs/PLAN.md`](docs/PLAN.md).

---

## Troubleshooting

### `poppler` PDF rendering errors

If `pdf_render_pages` returns an error like `PDFInfoNotInstalledError` or `poppler not installed`, install the system binary:

| OS | Command |
|---|---|
| macOS (Homebrew) | `brew install poppler` |
| Debian / Ubuntu | `sudo apt-get install poppler-utils` |
Verify with `pdftoppm -v`. Text extraction via `pypdf` + `pdfplumber` works without poppler — only vision-page rendering is gated on it.

### `[browser] npx not found on PATH` error

The Playwright MCP browser tool shells out to `npx -y @playwright/mcp@latest`. Install Node.js LTS:

```bash
brew install node     # or: https://nodejs.org/
```

Set `browser.enabled: false` in config.yaml if you don't need dynamic-HTML fetches (trafilatura-based extraction still works for static pages).

### `Could not synthesize via LLM (APIConnectionError)`

The CLI surfaces the raw error text. Common causes:
- LLM endpoint down or unreachable — check `llm.base_url` and that the server is up.
- Wrong `llm.api_key` for your OpenAI-compatible endpoint.
- Firewall / proxy blocking outbound HTTPS to the LLM host.

### Live progress panel flickers / leaks into piped output

The reporter auto-disables when stdout isn't a TTY, but if you want a fully clean log stream (no panel), pass `--quiet` / `-q`:

```bash
uv run python -m deep_research --quiet "..." > report.md
```

### Empty citation graph from `--academic` / `--dump-graph`

- Confirm `arxiv.enabled: true` (default).
- Confirm `OPENAI_API_KEY` and the LLM endpoint are reachable.
- Re-run with `--verbose` to see the per-paper analyze logs.

### Reddit tool errors

Reddit integration (P11) requires:
1. `uv sync --extra reddit` to install `asyncpraw`
2. `reddit.enabled: true` in config.yaml
3. `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` env vars set

If any of these are missing, the tool returns a clean error message (no crash). By default `reddit.enabled: false`, so the tool is not registered at all.

### Postgres backend

Set `pdl.storage.backend: "postgres"` and `DEEP_RESEARCH_PG_DSN` env var. Requires `uv sync --extra postgres` to install `asyncpg`.

### Refresh scheduler

Run `deep-research-scheduler` as a daemon. It probes upstream URLs on a cron schedule (default: every 6 hours). Configure via `pdl.refresh.*` yaml keys.

### FastAPI microservice

Run `uv run python -m deep_research.microservice` or use the Dockerfile:
```bash
docker build -t deep-research .
docker run -p 8080:8080 deep-research
```
POST `/research` with `{"query": "...", "config_path": "config.yaml"}`.

---

## License

MIT. See [`LICENSE`](LICENSE).

All declared Python dependencies are MIT- or BSD- or Apache-2.0-licensed. The `poppler` system binary (invoked via subprocess by `pdf2image`) is GPL-licensed but runs as an external process; it does not affect the license of this distributable package.
