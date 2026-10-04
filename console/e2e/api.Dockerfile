# The SSC API behind a dev stack, for the console's Playwright smoke test only. Built from the
# repository root by compose.yaml; api.Dockerfile.dockerignore limits what is sent.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH
RUN useradd --create-home --uid 10001 ssc && mkdir -p /app /state && chown ssc:ssc /app /state
USER ssc
WORKDIR /app
COPY --chown=ssc:ssc pyproject.toml uv.lock ./
COPY --chown=ssc:ssc packages packages
COPY --chown=ssc:ssc conformance conformance
COPY --chown=ssc:ssc tools/dev_stack.py tools/dev_stack.py
# dev_stack.py imports the control-plane test helpers, which need the dev group (testcontainers)
# and ssc-agent.
RUN uv sync --locked --all-packages
EXPOSE 8000
HEALTHCHECK --interval=2s --timeout=3s --start-period=5s --retries=60 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# `up` prints a token; it goes nowhere. e2e/run.mjs mints its own with `token`. The worker, with
# the fake builder and runtime, runs the kill switch's steps.
# Every spec shares one token, so its rate limit is raised.
CMD ["sh", "-c", "python tools/dev_stack.py --dir /state up --dsn \"$SSC_E2E_DSN\" > /dev/null && exec python tools/dev_stack.py --dir /state serve --worker --rate-capacity 1000 --host 0.0.0.0 --port 8000"]
