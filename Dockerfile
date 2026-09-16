# syntax=docker/dockerfile:1
# Digest-pinned base (python:3.12-slim). Re-resolve deliberately, not by retagging, if the base
# image needs to move.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 guest-answer-provider \
    && useradd --system --uid 10001 --gid guest-answer-provider --home-dir /nonexistent guest-answer-provider

WORKDIR /app

COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

# The vendored, digest-pinned conformance bundle is a runtime dependency (bundle_tools.py imports
# its check_invariants_impl.py directly, and schema_validation.py loads its JSON schemas), not
# just a dev-time artifact — it must ship in the image.
COPY contracts ./contracts

USER 10001:10001
EXPOSE 8081

# §6: /healthz is deliberately outside /business/v1 and unauthenticated (the same
# process-liveness convention the Management Contract adapter uses).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8081\")}/healthz', timeout=2)" || exit 1

ENTRYPOINT ["python", "-m", "guest_answer_provider.runtime"]
