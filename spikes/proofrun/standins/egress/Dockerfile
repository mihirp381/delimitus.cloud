# T6's stand-in job (SSC-086). Not an SSC app: it runs as a Cloud Run job with a chosen account,
# subnet and network tag, which `ssc deploy` does not offer (README, T6).
# Base pinned by digest: python:3.14-slim, the same as the runtime probe image.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
WORKDIR /app
COPY probe.py /app/
USER 10001
ENTRYPOINT ["python", "/app/probe.py"]
CMD ["nat"]
