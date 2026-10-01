# The runtime probe image (SSC-017): the probe app, and the in-cell runner job (`python runner.py`).
# Base pinned by digest: python:3.14-slim, looked up 2026-09-29.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
WORKDIR /app
COPY app.py checks.py runner.py /app/
USER 10001
CMD ["python", "/app/app.py"]
