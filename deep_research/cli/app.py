"""CLI entrypoint — typer-based shell over `agent.run_research()`.

PK flags:
  --quick        force quick path
  --deep         force deep path
  --academic     force academic path
  --url-source   force url_source mode

If none of the above is set, the agent auto-routes (URL detection first,
then classifier LLM call) unless `agent.classifier.enabled: false` in config.

Output controls:
  --out PATH                write the final markdown report to file
  --format {markdown,json}  output format
  --dump-graph PATH         write the BibTeX citation graph (academic mode) to .bib

Config:
  --config PATH             path to config.yaml (default: ./config.yaml or ./config.example.yaml)
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.traceback import Traceback as RichTraceback

from deep_research.agent import run_research
from deep_research.config import AgentTopConfig
from deep_research.report import (
    render_report_bibtex,
    render_report_json,
    render_report_markdown,
)

logger = logging.getLogger(__name__)
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="deep-research",
    help="Async deep-research agent with LLM-driven routing, PDF vision, and academic citation mining.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,  # we use RichTraceback manually
)


def _setup_logging(verbose: bool) -> None:
    """Config root logger with rich-friendly formatting."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence chatty deps
    for noisy in ("httpx", "httpcore", "openai._base_client", "asyncpraw", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_env_dotlocal(cwd: Path) -> None:
    """If .env.local exists in cwd, load env vars from it quietly.

    Optional convenience — the project's `pyproject.toml` lists `python-dotenv`
    under `dev`. We tolerate it being missing, in which case we silently skip.
    """
    p = cwd / ".env.local"
    if not p.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(p, override=False)
    except ImportError:
        # python-dotenv not installed — silently skip
        pass


def _load_config(config_path: Path | None) -> AgentTopConfig:
    """Find and load a config.yaml; fall back to defaults if none found."""
    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(config_path)
    else:
        candidates += [Path.cwd() / "config.yaml", Path.cwd() / "config.example.yaml"]
    for p in candidates:
        if p.exists():
            try:
                return AgentTopConfig.load_yaml(p)
            except Exception as e:
                err_console.print(f"[red]Error loading config {p}:[/red] {e}")
                raise typer.Exit(code=2)
    err_console.print("[yellow]No config.yaml found; using all defaults.[/yellow]")
    return AgentTopConfig()


def _resolve_path_override(
    quick: bool, deep: bool, academic: bool, url_source: bool
) -> str | None:
    """At most one of the path flags may be passed; return the corresponding value or None."""
    selected = [name for flag, name in [
        (quick, "quick"),
        (deep, "deep"),
        (academic, "academic"),
        (url_source, "url_source"),
    ] if flag]
    if len(selected) > 1:
        err_console.print(f"[red]Ambiguous flags — pick ONE of --quick/--deep/--academic/--url-source[/red]: {selected}")
        raise typer.Exit(code=2)
    return selected[0] if selected else None


@app.command()
def main(
    query: str = typer.Argument(..., help="The research query or 'URL [optional question]'"),
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    quick: bool = typer.Option(False, "--quick", help="Force the quick path (1 search + summarize)"),
    deep: bool = typer.Option(False, "--deep", help="Force the deep path (planner -> researcher -> critic -> writer)"),
    academic: bool = typer.Option(False, "--academic", help="Force the academic path (recursive citation mining)"),
    url_source: bool = typer.Option(False, "--url-source", help="Force URL-source mode (URL must be in query)"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write final report to this file"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="markdown | json"),
    dump_graph: Path | None = typer.Option(None, "--dump-graph", help="Write BibTeX citation graph to this file (academic mode)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Run a deep research query and print/write the report."""
    _setup_logging(verbose)
    _load_env_dotlocal(Path.cwd())
    config = _load_config(config_path)

    if fmt not in {"markdown", "json"}:
        err_console.print(f"[red]Invalid --format {fmt!r}[/red]; must be 'markdown' or 'json'")
        raise typer.Exit(code=2)

    path_override = _resolve_path_override(quick, deep, academic, url_source)

    import asyncio

    try:
        report = asyncio.run(
            run_research(query, config, path_override=path_override)
        )
    except KeyboardInterrupt:
        err_console.print("[yellow]Interrupted.[/yellow]")
        raise typer.Exit(code=130)
    except Exception as e:
        if verbose:
            err_console.print(RichTraceback())
        else:
            err_console.print(f"[red]Error:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    # Render final output
    if fmt == "markdown":
        rendered = render_report_markdown(report, config.output)
    else:
        rendered = render_report_json(report)

    if out is None:
        console.print(rendered)
    else:
        Path(out).write_text(rendered, encoding="utf-8")
        console.print(f"[green]Wrote[/green] {out}")

    # BibTeX graph dump (academic mode)
    if dump_graph is not None:
        bib = render_report_bibtex(report)
        if bib:
            Path(dump_graph).write_text(bib, encoding="utf-8")
            console.print(f"[green]Wrote citation graph[/green] {dump_graph}")
        else:
            err_console.print(f"[yellow]No citation graph found in report; nothing to dump to {dump_graph}[/yellow]")


if __name__ == "__main__":
    app()
