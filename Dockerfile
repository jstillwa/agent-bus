# syntax=docker/dockerfile:1

# --- Stage 1: build the Web UI static assets -------------------------------
FROM node:24-slim AS frontend
RUN corepack enable && corepack prepare pnpm@10.5.2 --activate
WORKDIR /build/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# --- Stage 2: build the Rust extension + Python wheel -----------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md Cargo.toml Cargo.lock ./
COPY src/ src/
COPY agent_bus/ agent_bus/
# Ensure any pre-existing local .so or build artifacts are not packaged into the wheel build
RUN rm -f agent_bus/*.so
COPY --from=frontend /build/agent_bus/web/static/ agent_bus/web/static/
RUN uv pip install --system maturin \
    && maturin build --release --out dist

# --- Stage 3: runtime --------------------------------------------------------
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN WHL=$(ls /tmp/*.whl) && pip install --no-cache-dir "${WHL}[web]" && rm -f /tmp/*.whl

ENV PYTHONUNBUFFERED=1 \
    AGENT_BUS_DB=/data/agent_bus.sqlite

EXPOSE 8080
CMD ["agent-bus", "serve"]
