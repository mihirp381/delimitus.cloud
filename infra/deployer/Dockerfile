# The cell deployer (SSC-087): `python -I -m ssc_infra.deployer <cell label> <flag>` as the Cloud
# Run job `ssc-cell-deployer` in ssc-platform-0. Built from the repository root:
# `docker build -f infra/deployer/Dockerfile .`; Dockerfile.dockerignore limits what is sent.
# The entrypoint is fixed: a run's overrides can replace only its arguments and environment.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ARG PULUMI_VERSION=3.266.0
ARG PULUMI_GCP_VERSION=9.37.0
COPY --from=uv /uv /usr/local/bin/uv
ADD https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-x64.tar.gz /tmp/pulumi.tar.gz
RUN tar -xzf /tmp/pulumi.tar.gz -C /opt && rm /tmp/pulumi.tar.gz \
    && useradd --uid 10001 --create-home ssc && mkdir -p /app && chown ssc:ssc /app
USER ssc
WORKDIR /app/infra
COPY --chown=ssc:ssc pyproject.toml /app/pyproject.toml
COPY --chown=ssc:ssc packages/ssc_contracts /app/packages/ssc_contracts
COPY --chown=ssc:ssc packages/ssc_shared /app/packages/ssc_shared
COPY --chown=ssc:ssc infra/pyproject.toml infra/uv.lock ./
RUN UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache \
    uv sync --locked --no-dev && rm -rf /tmp/uv-cache
COPY --chown=ssc:ssc infra/Pulumi.yaml infra/__main__.py ./
COPY --chown=ssc:ssc infra/ssc_infra ssc_infra
RUN echo /app/infra > .venv/lib/python3.14/site-packages/ssc_infra.pth \
    && PULUMI_HOME=/app/.pulumi /opt/pulumi/pulumi plugin install resource gcp ${PULUMI_GCP_VERSION}
ENTRYPOINT ["/app/infra/.venv/bin/python", "-I", "-m", "ssc_infra.deployer"]
