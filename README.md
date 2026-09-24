# Utopia Homes Prime

Utopia Homes Prime is the Homes business synth. Homes owns its knowledge, prompts, answer policy,
validation, approved sources and links, fallback behavior, and the choice of how inference
happens. It can be provisioned, run, and released with no Tiamat account, Control service, Shared
Model Execution service, grant, credential, or network connection. Tiamat is one optional
inference backend Homes may select. See `MIGRATION_PROVENANCE.md` for the 2026-09-24 source split
from cloud-hermes-lucy.

It also carries the Homes Management Contract provider, `utopia_homes_prime.management_adapter`,
deployed as its own service (`docs/management-adapter.md`). It serves two Homes business APIs:

- **`POST /business/v1/guest/answer`**, Business Contract `guest.answer@1.0` (RC2). Homes owns this
  contract: `docs/stoin-utopia-business-contract-guest-answer-rc2.md` and the Tier A conformance
  bundle `contracts/stoin-business-guest-answer-v1-bundle/` (digest
  `sha256:50492b998b393a25322cb9b76e4a8fbd7199905b8457450b8aa48ae00149ad4c`) are authoritative
  here.
- **Homes Dragon meeting operations**: `GET /business/v1/meeting/identity` and
  `POST /business/v1/meeting/{respond,draft}`. This is a separate `meeting.assist` capability for
  Workspaces meetings, using Homes-approved, digest-pinned materials. See
  `docs/homes-dragon-meeting-operations.md`.

Two answer engines exist, selected by `GUEST_ANSWER_PROVIDER_ANSWER_ENGINE`:

- **`legacy-bridge`** (default) wraps the legacy FAQ-snapshot endpoint `utopia-homes-web` already
  calls. That is RC2 §20.2's strangler step: **preconformant only**, not eligible for Tier B
  evaluation. It is signaled out of band by `X-Utopia-Preview-Mode: legacy-bridge`, never in
  customer-visible `limitations[]`. See `docs/guest-answer-preview-rollout.md`.
- **`homes-prime`** (approved for production on 2026-09-24; selected explicitly) is the Homes-owned engine: prompts, a digest-pinned knowledge
  projection, deterministic grounding checks, and a support-review pass. Inference goes through the
  backend Homes selects with `GUEST_ANSWER_PROVIDER_HOMES_PRIME_INFERENCE_BACKEND`:
  - `direct-openrouter`: Homes' own route (credential, model, price ceilings), needing nothing
    from Tiamat;
  - `tiamat`: optional, the private `inference.execute@1.0` client for Shared Model Execution (RC1,
    pinned under `contracts/stoin-shared-model-execution-v1-rc1/`).

  See `docs/homes-prime-stage2.md`.

**Status: local only, not deployed.** No production credentials exist. No real provider call has
run: the direct route is proven against a fake provider, and a real call needs a Homes-owned key and
price ceilings (Ray's decision). `docs/implementation-notes.md` records judgment calls.

This repository imports no other system's code. `tests/integration/test_homes_without_tiamat.py`
enforces that, and proves guest.answer and the meeting operations with no Tiamat configuration.

## Layout

```
src/utopia_homes_prime/
  config.py, runtime.py          env vars -> frozen Config; python -m utopia_homes_prime.runtime
  business_api/                  FastAPI app factory and request pipeline, EdDSA JWT auth, jti
                                 replay, idempotency, RFC 8785 canonicalization, access logging
  guest_answer/                  RC2 types, schemas and errors; the Homes Prime engine
                                 (homes_prime.py); the legacy bridge
  meeting_assist/                Homes Dragon meeting operations, HTTP routes, materials registry
  management_adapter/            Management Contract v1 provider (a separate deployable:
                                 deploy/management-adapter/Dockerfile, tests/management/)
  knowledge/                     digest-pinned approved knowledge projection
  inference/                     backend seam (backend.py), direct route (direct_openrouter.py),
                                 optional Tiamat backend (tiamat.py, sme_client.py, sme_wire.py),
                                 backend-neutral structured output
knowledge/                       Homes knowledge data: R1 corpus, legacy FAQ projection,
                                 evaluation question sets
materials/homes-dragon/          approved meeting materials and their pinned manifest
contracts/                       guest.answer bundle (authoritative); pinned SME RC1 client data
tests/unit, tests/integration, tests/contract
docs/                            contracts, implementation notes, rollout and Stage 2 records
deploy/render/*.yaml.example     review-only Render Blueprint, never applied
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on Linux/macOS
pip install -e ".[dev]"
```

## Verification

```bash
ruff check .
mypy src

# Confirms the vendored bundle hasn't drifted from its pinned digest.
python contracts/stoin-business-guest-answer-v1-bundle/tools/compute_digest.py --check
python contracts/stoin-business-guest-answer-v1-bundle/tools/verify_bundle.py
python contracts/stoin-shared-model-execution-v1-rc1/verify_pin.py   # RC1 digests

pytest -m "not contract"   # fast — unit + integration, ~20s, includes full vector replay
pytest -m contract         # spawns real subprocesses + a real fake-upstream HTTP server, ~20s
```

All of the above pass locally. CI runs the same sequence via `.github/workflows/ci.yml`.

## Running it locally (non-production)

The provider needs a JWT public-key allowlist and the legacy-upstream config, supplied entirely
through environment variables — see `.env.example` for the full list and
`src/utopia_homes_prime/config.py` for validation rules.

1. Generate a throwaway Ed25519 keypair and a matching signed JWT for testing:

   ```bash
   python - <<'PY'
   from cryptography.hazmat.primitives import serialization
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   import jwt, time, uuid, json

   private_key = Ed25519PrivateKey.generate()
   public_pem = private_key.public_key().public_bytes(
       encoding=serialization.Encoding.PEM,
       format=serialization.PublicFormat.SubjectPublicKeyInfo,
   ).decode()
   print("GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON=" + json.dumps(json.dumps([{
       "kid": "local-dev-1",
       "public_key_pem": public_pem,
       "environment": "preview",
       "issuer": "stoin:application:utopia-homes-web",
       "subject": "stoin:service:utopia-homes-web-guest-adapter",
       "audience": "stoin:business:utopia-homes-prime",
       "capabilities": ["guest.answer"],
       "status": "active",
   }])))

   now = int(time.time())
   token = jwt.encode(
       {
           "iss": "stoin:application:utopia-homes-web",
           "sub": "stoin:service:utopia-homes-web-guest-adapter",
           "aud": "stoin:business:utopia-homes-prime",
           "scope": "guest.answer",
           "iat": now, "nbf": now, "exp": now + 280,
           "jti": str(uuid.uuid4()),
       },
       private_key,
       algorithm="EdDSA",
       headers={"kid": "local-dev-1"},
   )
   print("TEST_JWT=" + token)
   PY
   ```

2. Copy `.env.example` to `.env`, fill in `GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON` with the
   value printed above, and either run a real (or fake) legacy upstream at `LUCY_PUBLIC_API_URL`
   or leave the placeholder to see `temporarily_unavailable` on every real answer attempt. Export
   the env:

   ```bash
   set -a && source .env && set +a   # bash/zsh; on Windows PowerShell, set each $env: var instead
   ```

3. Run the provider:

   ```bash
   python -m utopia_homes_prime.runtime
   ```

4. In another shell, using the `TEST_JWT` printed in step 1:

   ```bash
   curl -s http://127.0.0.1:8081/business/v1/guest/answer \
     -X POST \
     -H "Authorization: Bearer $TEST_JWT" \
     -H "Accept: application/json" \
     -H "Content-Type: application/json" \
     -H "X-Request-ID: $(python -c 'import uuid; print(uuid.uuid4())')" \
     -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
     -d '{
       "contract_version": "1.0",
       "session_id": "'"$(python -c 'import uuid; print(uuid.uuid4())')"'",
       "message": {
         "turn_id": "'"$(python -c 'import uuid; print(uuid.uuid4())')"'",
         "content": "What are your check-in times?"
       },
       "locale": "en-US"
     }' | python -m json.tool

   # A bad/missing token gets the same generic 401 envelope every time:
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/business/v1/guest/answer \
     -X POST -H "Accept: application/json" -H "Content-Type: application/json" \
     -H "X-Request-ID: $(python -c 'import uuid; print(uuid.uuid4())')" \
     -H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" -d '{}'
   ```

The keypair and token above are for local testing only — never reuse them anywhere real.

## Building the image (not deployed)

```bash
docker build -t utopia-homes-guest-answer-provider:local .
```

`requirements.lock` was generated with `pip-compile --generate-hashes` from `pyproject.toml`'s
production dependencies. The image build itself was not verified in the environment this repo was
built in (no local Docker daemon available); verify it before relying on it.
