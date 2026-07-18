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

# Expose the microservice port
EXPOSE 8080

# Default: run the FastAPI microservice
CMD ["uv", "run", "python", "-m", "deep_research.microservice"]
