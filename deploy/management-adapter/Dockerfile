# syntax=docker/dockerfile:1
# The Utopia Homes Management Contract provider: a separately deployed Homes Prime component.
# Build from the repository root: docker build -f deploy/management-adapter/Dockerfile .
# Digest-pinned base (python:3.12-slim, resolved 2026-09-16). Re-resolve deliberately, not by
# retagging, if the base image needs to move.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 management-adapter \
    && useradd --system --uid 10001 --gid management-adapter --home-dir /nonexistent management-adapter

WORKDIR /app

COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

USER 10001:10001
EXPOSE 8080

# §5: the provider MUST complete each request within two seconds; a slow health probe here
# would misrepresent that. /healthz is deliberately outside /management/v1 and unauthenticated
# (permitted by §5 as "a separate process-liveness endpoint").
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=2)" || exit 1

ENTRYPOINT ["python", "-m", "utopia_homes_prime.management_adapter.runtime"]
