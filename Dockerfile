# CostSentinel, for a local or real run. Not used by CI, which runs on the host.
#
# Two stages so the runtime image carries the virtualenv and the source but not uv,
# the build cache or the dev dependencies.
#
# The image installs the `memory` extra and nothing else. The `azure` and `llm`
# extras are deliberately absent: a container that cannot reach Azure cannot read a
# client estate by accident, and anyone who wants a real sweep in a container is
# making a conscious decision that deserves its own image. See SECURITY.md.

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer, so editing source does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra memory

COPY src ./src
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra memory


FROM python:3.12-slim AS runtime

# A non-root user, because the only thing this process needs to write is its own
# state directory and nothing in the image should be mutable at runtime.
RUN useradd --create-home --uid 10001 costsentinel

WORKDIR /app

COPY --from=build --chown=costsentinel:costsentinel /app/.venv /app/.venv
COPY --from=build --chown=costsentinel:costsentinel /app/src /app/src
COPY --from=build --chown=costsentinel:costsentinel /app/scripts /app/scripts

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/var/lib/costsentinel

# Defaults are the offline ones, exactly as on a developer's machine: the mock
# estate, the fake model, the fake embedder. Compose overrides MEMORY_BACKEND.
ENV MODE=mock \
    LLM_BACKEND=fake \
    EMBEDDER_BACKEND=fake

RUN mkdir -p /var/lib/costsentinel && chown costsentinel:costsentinel /var/lib/costsentinel
VOLUME ["/var/lib/costsentinel"]

USER costsentinel
EXPOSE 8000

# The API, not a sweep: a container that ran a scan on start-up would be a container
# that scans every time it restarts.
CMD ["uvicorn", "costsentinel.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
