# The cell agent (SSC-017): `python -m ssc_agent` on Cloud Run in each cell. Built from the
# repository root: `docker build -f packages/ssc_agent/Dockerfile .`; Dockerfile.dockerignore
# limits what is sent.
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
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-dev --no-editable --package ssc-agent && rm -rf /tmp/uv-cache
CMD ["python", "-m", "ssc_agent"]
