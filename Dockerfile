# syntax=docker/dockerfile:1.17-labs
# (we need this for --exclude in COPY)
# Keep this syntax directive! It's used to enable Docker BuildKit
##
# This was inspired by https://hynek.me/articles/docker-uv/
# It's a bit more complex than what I need so a decent amount of it is stripped out
##
# Allow override via build-arg
##
ARG BASE_IMAGE=alpine:latest
ARG PYTHON_VERSION="3.13"

## Build / prep
FROM ${BASE_IMAGE} AS build
# We have to "consume" args declared in global scope to make them available in this context
ARG PYTHON_VERSION

# Easier than curl ...
##
# Note: COPY does not support variable interpolation so we can't use ${UV_VERSION} here :(
# See: https://github.com/moby/moby/issues/34482
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# - Silence uv complaining about not being able to use hard links,
# - tell uv to byte-compile packages for faster application startups,
# - set the Python version to use
# - and finally declare `/app/venv` as the target for `uv sync`.
ENV UV_LINK_MODE=copy \
  UV_COMPILE_BYTECODE=1 \
  UV_PYTHON=python${PYTHON_VERSION} \
  UV_PROJECT_ENVIRONMENT=/app/.venv

# Set up non-root user to own everything
RUN addgroup -S app && adduser -S -D -h /app -G app app

# And then use the user for the rest of the build
USER app

# Copy bits and pieces to the container
COPY --chown=app:app ./app /app

# We need these for the dependencies that `uv` will install
# We won't copy them to the final container, though
COPY --chown=app:app ./pyproject.toml /app/pyproject.toml
COPY --chown=app:app ./uv.lock /app/uv.lock

# Install all the (non-dev) dependencies + the venv
RUN <<EOT
cd /app
uv sync --locked \
--no-dev
EOT


# So the /app directory has everything we need to run the script (and more!)
FROM ${BASE_IMAGE} AS runtime


# We need to mirror the app user/group from the build stage
# Set up non-root user to own everything
RUN addgroup -S app && adduser -S -D -h /app -G app app

# And then use the user for the rest of the build
USER app

# Ok, we have `app` user/group and an empty `/app` directory
# Copy everything over except the dependency/uv metadata
# Note, we need "experimental" syntax support for --exclude
# See: https://docs.docker.com/reference/dockerfile/#copy---exclude
COPY --from=build --chown=app:app --exclude=*.toml --exclude=*.lock --exclude=.cache /app /app

# Add the uv installed python/bins to the PATH
ENV PATH=/app/.venv/bin:$PATH

# See: https://hynek.me/articles/docker-signals/
STOPSIGNAL SIGINT
WORKDIR /app
# Because we put python in the path, we can just run the script and the shebang will take care of the rest
CMD ["./main.py"]
