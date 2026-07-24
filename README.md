# Deep Research Agent

An async-first Python agent that performs **web / deep / academic / single-source URL** research with the assistance of an LLM. Give it a question — it searches, reads, analyzes, and writes a structured report. Multi-modal LLM support enables vision-based PDF figure comprehension.

```bash
# Quick start — auto-routes based on your query
uv run python -m deep_research "What is the capital of France?"
uv run python -m deep_research "Survey recent advances in RLHF"
uv run python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"
```

---

## What you need

| Requirement | Default / how to get it |
|---|---|
| **LLM endpoint** (OpenAI-compatible) | Any service serving Qwen, Llama, GPT, etc. via `/v1` |
| **Web search** (optional but recommended) | Tavily API key, or a local SearXNG instance |
| **Python 3.11–3.13** | `uv` installed — see below |
| **poppler** (for PDF vision) | `brew install poppler` / `apt-get install poppler-utils` |

---

## Install

```bash
git clone <your-fork-url> deep_research
cd deep_research

# Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync
uv sync --extra dev   # pytest, ruff, mypy (optional)
uv sync --extra reddit # Reddit access (optional)
```

---

## Configure

Copy the example config and edit:

```bash
cp config.example.yaml config.yaml
```

### Minimal config (just an LLM)

```yaml
llm:
  base_url: "http://localhost:8000/v1"   # your LLM server
  api_key: "your-api-key"
  text_model: "qwen3.5-122b"
  vision_model: "qwen3.5-122b"
```

### With web search (recommended)

You need at least one search backend. Two options:

**Option 1 — Tavily** (cloud, no setup):

```yaml
search:
  primary: "tavily"
  tavily:
    api_key_env: "TAVILY_API_KEY"   # then: export TAVILY_API_KEY=your_key
```

Get a free API key at [tavily.com](https://tavily.com).

**Option 2 — Local SearXNG** (self-hosted, no API key needed):

See [docs/SEARXNG_SETUP.md](docs/SEARXNG_SETUP.md) for Docker and non-Docker setup instructions.

```yaml
search:
  primary: "searxng"
  searxng:
    url: "http://localhost:8080/search"
```

### With Google Scholar (academic mode only, optional)

Enable Scholar seeding to cover non-arxiv venues (Nature, ACM, IEEE, conferences):

**Option 1 — Serper API** (recommended, cloud):

Get a free API key at [serper.dev](https://serper.dev), then:

```yaml
scholar:
  enabled: true
  primary: "serper"
```

```bash
export SERPER_API_KEY=your_key
```

**Option 2 — SearXNG with the `scholar` engine** (self-hosted, free):

Enable the `scholar` engine in your SearXNG `settings.yml` `engines:` block, then:

```yaml
scholar:
  enabled: true
  primary: "searxng"
  searxng:
    url: "http://localhost:8080/search"
```

And add `"scholar"` to `academic.seed_backends`:

```yaml
academic:
  seed_backends: ["arxiv", "scholar"]
```

### Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` / `DEEP_RESEARCH_LLM_API_KEY` | LLM auth |
| `TAVILY_API_KEY` | Tavily search |
| `SERPER_API_KEY` | Google Scholar search (Serper backend) |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Reddit search (optional) |
| `DEEP_RESEARCH_PG_DSN` | Postgres connection string (optional) |

---

## Use

### CLI

Auto-routing (default — the agent picks the best path for your query):

```bash
uv run python -m deep_research "What is the capital of France?"
uv run python -m deep_research "Survey recent advances in RLHF"
uv run python -m deep_research "Summarize https://arxiv.org/abs/2401.12345"
uv run python -m deep_research "https://blog.example.com/post — what are its gaps?"
```

Force a specific research path:

```bash
uv run python -m deep_research --quick "who invented the wheel?"
uv run python -m deep_research --deep "compare transformer architectures" --max-iterations 5
uv run python -m deep_research --academic "RLHF safety" --max-depth 2 --max-papers 20 --dump-graph refs.bib
uv run python -m deep_research --url-source "https://example.com/foo.pdf" "verify its claims"
```

Output controls:

```bash
uv run python -m deep_research "..." --out report.md --cite citations.json
uv run python -m deep_research "..." --format json --out report.json
uv run python -m deep_research "..." --quiet          # no live progress panel
uv run python -m deep_research "..." --verbose        # debug logs + rich traceback
```

### Library CLI (personal research archive)

Every research run is automatically archived (enabled by default). Browse your
personal library:

```bash
uv run deep-research-library ls                    # list recent reports
uv run deep-research-library find "transformer"    # full-text search
uv run deep-research-library show <artifact_id>    # artifact details
uv run deep-research-library tag <id> <tag>        # tag an artifact
uv run deep-research-library stats                 # library statistics
uv run deep-research-library prune --older-than 90 # prune old artifacts
uv run deep-research-library export-bibtex refs.bib
uv run deep-research-library refresh               # refresh stale artifacts
uv run deep-research-library glossary              # list all glossary entries
uv run deep-research-library glossary --find "transformer"  # full-text search
uv run deep-research-library glossary --term RLHF          # detail view of one term
uv run deep-research-library glossary --filter-tag "nlp"   # filter by domain tag
uv run deep-research-library glossary --out glossary.json  # export as JSON
```

Refresh scheduler (daemon that auto-refreshes upstream URLs):

```bash
uv run deep-research-scheduler
```

### As a Python library

```python
import asyncio
from pathlib import Path
from deep_research import run_research, AgentTopConfig

config = AgentTopConfig.load_yaml("config.yaml")

async def main():
    report = await run_research("Survey RLHF methods", config)
    print(report.markdown)
    Path("report.md").write_text(report.markdown)

asyncio.run(main())
```

`run_research()` accepts optional `path_override` ("quick" / "deep" / "academic" / "url_source") and `progress` for streaming status updates.

### FastAPI microservice

```bash
uv run python -m deep_research.microservice
# or via Docker:
docker build -t deep-research .
docker run -p 8080:8080 deep-research

# POST /research with {"query": "...", "config_path": "config.yaml"}
```

---

## How it works

```
query → URL detected? → url_source path (analyze single URL + optional follow-up)
       → no URL        → classifier (one LLM call) decides:
                          ├─ quick   → 1 search + summarize (~5-15s)
                          ├─ deep    → planner → researcher → critic → writer loop
                          ├─ academic→ arxiv + optional Google Scholar seed → recursive citation graph mining (depth ≤ 2, papers ≤ 15)
                          └─ applied → blog-first research
```

The agent uses raw `asyncio` for orchestration — no LangChain, no LangGraph.
Every page fetch, PDF download, and LLM call is dispatched concurrently.

PDFs are rendered page-by-page through a multimodal LLM (vision) for figure /
equation comprehension. No external agent frameworks.

---

## Routing paths in detail

| Path | What it does | Best for |
|---|---|---|
| **quick** | Single web search + summarize top 3 results. ~5–15s. | Factual questions, "what is X" |
| **deep** | Planner decomposes query → parallel researcher tools → critic loop → final writer. Researchers can dynamically refine the plan mid-loop (chase references, drill into subtopics). | Multi-faceted questions, survey requests |
| **academic** | arxiv (+ optional Google Scholar) seed → recursive citation graph (depth ≤ 2, ≤ 15 papers) → synthesis + BibTeX. Non-arxiv venues (Nature, ACM, IEEE, conferences) covered when Scholar is enabled. | Literature review, "what does the literature say" |
| **url_source** | Classify URL → fetch (arxiv/pdf/html) → analyze_source LLM → optional follow-up deep research. | "Summarize this paper", "verify this blog" |
| **applied** | blog_search first → fetch top blog posts → synthesize practical report. | Implementation questions, "how do I build X" |

---

## Personal Digital Library

By default, every research artifact is archived to `.deep_research_library/`:

- **Content-addressable dedup**: same PDF fetched twice → stored once
- **SQLite metadata DB**: reports, analyses, tags, glossary, citation edges, full-text search
- **Postgres backend**: set `pdl.storage.backend: "postgres"` with `DEEP_RESEARCH_PG_DSN`
- **Refresh daemon**: `deep-research-scheduler` probes upstream URLs for changes
- **Glossary**: dedicated post-synthesis LLM extraction (JSON-only prompt, `response_format=json_object`), cross-run dedup, FTS5 search. Export via `--glossary-out` on the main CLI or `deep-research-library glossary --out glossary.json`.

PDF rendering uses weasyprint (falls back to xhtml2pdf if system deps missing).
Install native deps for best results:

| OS | Command |
|---|---|
| macOS | `brew install pango cairo` |
| Debian / Ubuntu | `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2` |

---

## Troubleshooting

### poppler not installed

`pdf_render_pages` requires poppler for vision-mode PDF rendering.
Text extraction (pypdf / pdfplumber) works without it.

| OS | Install |
|---|---|
| macOS | `brew install poppler` |
| Debian / Ubuntu | `sudo apt-get install poppler-utils` |

Verify: `pdftoppm -v`

### browser tool fails (`npx not found`)

Install Node.js LTS: `brew install node` or https://nodejs.org/.
Or disable browser: `browser.enabled: false` in config.yaml.

### LLM connection errors

- Check `llm.base_url` is correct and the server is running
- Check `llm.api_key` matches what your endpoint expects
- Firewall blocking outbound HTTPS to the LLM host

### Empty academic citation graph

- Confirm `arxiv.enabled: true` in config
- If using Scholar seeds (`seed_backends: ["arxiv", "scholar"]`), confirm `scholar.enabled: true` and either `SERPER_API_KEY` is set or SearXNG has the `scholar` engine enabled
- Confirm LLM endpoint is reachable
- Re-run with `--verbose` to see per-paper analysis logs

### Live progress panel flickers in piped output

Pass `--quiet` / `-q` to disable the panel:

```bash
uv run python -m deep_research --quiet "..." > report.md
```

---

## Reference

| File | Purpose |
|---|---|
| [`config.example.yaml`](config.example.yaml) | All config knobs with defaults |
| [`docs/PLAN.md`](docs/PLAN.md) | Full design document |
| [`docs/SEARXNG_SETUP.md`](docs/SEARXNG_SETUP.md) | Local SearXNG setup (Docker + non-Docker) |
| [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md) | Progress tracker |

---

## License

MIT. See [`LICENSE`](LICENSE).

All Python dependencies are MIT / BSD / Apache-2.0 licensed. The `poppler` system binary (invoked via subprocess by `pdf2image`) is GPL-licensed but runs as an external process — it does not affect the license of this package.
