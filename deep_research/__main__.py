"""`python -m deep_research` entrypoint — forwards to the typer CLI app."""

from deep_research.cli.app import app

if __name__ == "__main__":
    app()
