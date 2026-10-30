# delimitus.com (SSC-065, decision 028): the landing page and the pilot request form,
# `python -m ssc_landing` on Cloud Run in the control project, behind the control entry. Built
# from the repository root: `docker build -f packages/ssc_landing/Dockerfile .`;
# Dockerfile.dockerignore limits what is sent. Pushed to the platform registry as
# `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/ssc-landing`, whose digest is the
# control setting `landing_image`.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY conformance/pyproject.toml conformance/pyproject.toml
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-dev --no-editable --package ssc-landing

# Only the installed environment and the two pages ship; no source tree, owned by root.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
RUN useradd --uid 10001 ssc
COPY --from=builder /app/.venv /app/.venv
COPY landing/index.html /app/landing/index.html
COPY landing/privacy.html /app/landing/privacy.html
USER ssc
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PORT=8080 \
    SSC_LANDING_PAGE=/app/landing/index.html
CMD ["python", "-m", "ssc_landing"]
