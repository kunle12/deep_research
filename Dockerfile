FROM python:3.12-slim

# Install system deps: poppler for PDF rendering, pango/cairo for weasyprint
RUN apt-get update && apt-get install -y \
    poppler-utils \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set up the project
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

COPY deep_research/ deep_research/
COPY config.example.yaml config.yaml

# Expose the web UI port
EXPOSE 8080

# Default: run the FastAPI web UI (library browser). Binds 0.0.0.0 inside
# the container so the port can be forwarded; for a plain local install the
# `deep-research-web` entrypoint binds 127.0.0.1 instead.
CMD ["uv", "run", "uvicorn", "deep_research.webui.app:app", "--host", "0.0.0.0", "--port", "8080"]
