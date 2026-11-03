# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.15
ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim
ARG UV_VERSION=0.13.7

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM ${BASE_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
  UV_COMPILE_BYTECODE=1 \
  UV_PYTHON_DOWNLOADS=never \
  UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Keep dependency installation cached when only application code changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-dev --no-install-project

FROM ${BASE_IMAGE} AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --home-dir /app app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app/ /app/
ENV PATH=/app/.venv/bin:$PATH \
  PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8200
STOPSIGNAL SIGINT
CMD ["python", "main.py"]
