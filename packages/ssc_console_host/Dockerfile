# check=skip=SecretsUsedInArgOrEnv
# console.delimitus.com (SSC gap 1): the admin console's production build, served read-only by
# `python -m ssc_console_host` on Cloud Run in the control project, behind the control entry
# (`/v1` on the same host goes to the API). Built from the repository root:
# `docker build -f packages/ssc_console_host/Dockerfile .`; Dockerfile.dockerignore limits what is
# sent. Pushed to the platform registry as
# `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/ssc-console`, whose digest is the
# control setting `console_image`.
FROM node:24.20.0-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS console
# The auth host the console signs in through, and the API audience it asks for (decision 029).
# Neither is a secret (the check skipped above matches "AUTH" in the name). Never
# VITE_SSC_DEV_LOGIN: the token login stays out of this build.
ARG VITE_SSC_AUTH_URL=https://auth.delimitus.com
ARG VITE_SSC_API_AUDIENCE=https://api.delimitus.com
# What the console's Help page shows: the support address, and the trust pack's https address.
# Neither is a secret. Left empty, the page says "Ask your SSC contact" and shows no trust link.
ARG VITE_SSC_SUPPORT_EMAIL=
ARG VITE_SSC_TRUST_URL=
WORKDIR /console
COPY console/package.json console/package-lock.json console/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
RUN npm run build

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
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-dev --no-editable --package ssc-console-host && rm -rf /tmp/uv-cache
# Owned by root: the service can read the build and never change it.
COPY --from=console /console/dist /app/dist
ENV PORT=8080 \
    SSC_CONSOLE_DIST=/app/dist
CMD ["python", "-m", "ssc_console_host"]
