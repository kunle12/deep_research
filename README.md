# Deep Research Agent

A standalone, async-first Python agent that performs **web / deep / academic / single-source URL** research with the assistance of an LLM. Multi-modal LLM support enables vision-based PDF figure comprehension.

## Status

**Alpha** — Phases 1–8 complete. See [`docs/PLAN.md`](docs/PLAN.md) for the full design and [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md) for current progress.

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
```

### As a library (microservice-ready)

```python
import asyncio
from deep_research import run_research, AgentConfig

config = AgentConfig.load_yaml("config.yaml")

async def main():
    report = await run_research("Survey RLHF methods", config)
    print(report.markdown)
    # Save outputs:
    report.to_markdown_file("report.md")
    report.to_bibtex_file("refs.bib")  # if academic mode

asyncio.run(main())
```

---

## Architecture

See [`docs/PLAN.md`](docs/PLAN.md) for the full design document. In short:

```
deep_research/
├── agent.py            # run_research() public entrypoint
├── config.py           # AgentConfig pydantic schema
├── state.py            # ResearchState, Citation, Report
├── citations.py        # dedup + bibliography formatter
├── llm/                # OpenAI-compatible client, vision utils, tool-calling loop
├── paths/              # classifier, quick, deep, academic, url_source runners
├── nodes/              # planner, researcher, critic, writer, analyze_{paper,source}
├── tools/              # web_search, fetch_page, arxiv, pdf, browser (MCP), reddit (stub)
├── report/             # markdown, bibtex, json_export renderers
├── cli/app.py          # typer shell (thin wrapper around run_research)
└── prompts/            # .txt prompt templates
```

---

## License

MIT. See [`LICENSE`](LICENSE).

All declared Python dependencies are MIT- or BSD- or Apache-2.0-licensed. The `poppler` system binary (invoked via subprocess by `pdf2image`) is GPL-licensed but runs as an external process; it does not affect the license of this distributable package.
