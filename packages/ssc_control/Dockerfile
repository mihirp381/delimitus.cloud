# The control plane (SSC-064): one image, three processes on Cloud Run in `ssc-control-<stage>`,
# chosen by the command the platform stack gives each: `python -m ssc_control.api`,
# `python -m ssc_control.worker` and `python -m ssc_control.identity serve`; the migration job runs
# Alembic from it too. Built from the repository root for linux/amd64:
# `docker buildx build --platform linux/amd64 -f packages/ssc_control/Dockerfile .`;
# Dockerfile.dockerignore limits what is sent.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH
RUN useradd --uid 10001 ssc && mkdir -p /app && chown ssc:ssc /app
USER ssc
WORKDIR /app
COPY --chown=ssc:ssc pyproject.toml uv.lock ./
COPY --chown=ssc:ssc packages packages
COPY --chown=ssc:ssc conformance/pyproject.toml conformance/pyproject.toml
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-dev --no-editable --package ssc-control && rm -rf /tmp/uv-cache
ENV PORT=8080
CMD ["python", "-m", "ssc_control.api"]
