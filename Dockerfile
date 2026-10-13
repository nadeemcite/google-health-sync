FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Chromium renders the weekly PDF; Inter/Noto stand in for the macOS system font.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-inter fonts-noto-core tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY *.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 18787
CMD [".venv/bin/python", "healthsync.py", "serve"]
