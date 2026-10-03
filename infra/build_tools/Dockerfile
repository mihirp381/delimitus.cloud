# The build tools image (SSC-015): every step of a cell's Cloud Build runs in it
# (`ssc_agent.cloud_build`). bash, curl, coreutils and tar from the pinned Alpine release; the
# Docker CLI from the pinned official image; buildx, gitleaks and Railpack downloaded by version
# and checked by sha256. linux/amd64, as Cloud Build's default pool runs. Runs as root because
# Cloud Build mounts the host's Docker socket into each step. Built from this directory:
# `docker build --platform linux/amd64 infra/build_tools`.
FROM docker:29.8.1-cli@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS docker

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
ARG BUILDX_VERSION=0.37.2
ARG GITLEAKS_VERSION=8.30.1
ARG RAILPACK_VERSION=0.40.1
RUN apk add --no-cache bash ca-certificates coreutils curl tar
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
ADD --checksum=sha256:982ca20490b45ed1ec8d99795974d3d874a358f75938c9c237305010e6b7e548 \
    https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-amd64 \
    /usr/local/libexec/docker/cli-plugins/docker-buildx
ADD --checksum=sha256:551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb \
    https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz \
    /tmp/gitleaks.tar.gz
ADD --checksum=sha256:2842de93e68713af9037e0bc0a398d7da78f3b96aa4804303a638db2bc69bd30 \
    https://github.com/railwayapp/railpack/releases/download/v${RAILPACK_VERSION}/railpack-v${RAILPACK_VERSION}-x86_64-unknown-linux-musl.tar.gz \
    /tmp/railpack.tar.gz
RUN tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && tar -xzf /tmp/railpack.tar.gz -C /usr/local/bin railpack \
    && rm /tmp/gitleaks.tar.gz /tmp/railpack.tar.gz \
    && chmod 0755 /usr/local/libexec/docker/cli-plugins/docker-buildx \
    && docker buildx version && gitleaks version && railpack --version
