# syntax=docker/dockerfile:1.7-labs

# epublate Docker image
# ---------------------
# Two-stage build:
#  1. ``builder`` uses the official ``uv`` image to resolve and install
#     the project + extras into ``/app/.venv`` against the locked
#     ``uv.lock``. Tests are intentionally NOT run here — CI already
#     does that on every push and re-running them inside the image
#     would slow every pull. The ``--frozen`` flag refuses to fall back
#     to a remote resolver, matching the bootstrap contract in
#     ``AGENTS.md`` §5.
#  2. ``runtime`` is a slim Debian + Python 3.13 base with only the
#     pre-built virtualenv, the package source, and an unprivileged
#     user. We add Java (``default-jre-headless``) so the optional
#     ``epubcheck`` integration works out-of-the-box; it's a small cost
#     versus a frustrating "install Java to validate ePubs" runbook
#     entry.
#
# Default entrypoint is ``epublate``. The TUI works inside the image
# but Textual needs a real TTY, so curators invoke it with
# ``docker run --rm -it --tty ghcr.io/<owner>/epublate ...``. Headless
# CLI flows (``epublate translate``, ``--mock-llm`` smoke tests, etc.)
# work without a TTY.
#
# Build:
#   docker build -t epublate:dev .
# Run TUI:
#   docker run --rm -it ghcr.io/<owner>/epublate
# Run a CLI command with a host-mounted project dir:
#   docker run --rm \
#     -v "$PWD/projects:/projects" \
#     -e EPUBLATE_PROJECTS_ROOT=/projects \
#     ghcr.io/<owner>/epublate translate --mock-llm /projects/MyBook

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.9.6

# ---------------------------------------------------------------------
# Stage 1 — build venv with uv
# ---------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Resolve deps first against the lockfile so this layer caches
#    cleanly until ``pyproject.toml`` / ``uv.lock`` change. We exclude
#    the project itself in this layer (``--no-install-project``) so
#    edits to ``src/`` don't bust the dependency cache.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra epubcheck --no-install-project

# 2) Now bring in the source and install ``epublate`` itself.
COPY src ./src
COPY LICENSE ./LICENSE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra epubcheck

# ---------------------------------------------------------------------
# Stage 2 — minimal runtime
# ---------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APP_USER=epublate
ARG APP_UID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    EPUBLATE_PROJECTS_ROOT=/data/projects \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid "${APP_UID}" --shell /bin/bash "${APP_USER}" \
 && mkdir -p /data/projects /data/config /data/share \
 && chown -R "${APP_USER}:${APP_USER}" /data /home/${APP_USER}

WORKDIR /app

COPY --from=builder --chown=${APP_USER}:${APP_USER} /app /app

USER ${APP_USER}

# Provide ``/data`` as a stable mount point so curators can persist
# projects, recents, and config across container restarts:
#   docker run --rm -it -v epublate-data:/data ghcr.io/<owner>/epublate
VOLUME ["/data"]

# Smoke-check at build time so a broken venv fails fast.
RUN epublate --version

ENTRYPOINT ["/usr/bin/tini", "--", "epublate"]
CMD []

LABEL org.opencontainers.image.title="epublate" \
      org.opencontainers.image.description="Rich TUI that translates ePub story books with an LLM, preserving format and a per-project lore bible." \
      org.opencontainers.image.source="https://github.com/madpin/epublate" \
      org.opencontainers.image.licenses="MIT"
