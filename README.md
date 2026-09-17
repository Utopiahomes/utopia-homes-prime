# Utopia Homes guest.answer Provider

An independently deployed Homes provider for **Business Contract `guest.answer@1.0`** (RC2, frozen
commit `daf99943abf177f2209a6efb00e03c087bc542c6`), built against the frozen Tier A conformance
bundle (digest `sha256:50492b998b393a25322cb9b76e4a8fbd7199905b8457450b8aa48ae00149ad4c`, 250
checks / 218 files, no remaining acknowledged gaps). It exposes one authenticated HTTP resource —
`POST /business/v1/guest/answer` — implementing the full RC2 protocol (EdDSA auth, idempotency,
RFC 8785 canonicalization, the 13-row error contract) around the **existing, already-working**
legacy FAQ-snapshot answer engine `utopia-homes-web` already calls — RC2 §20.2's strangler step 2:
wrap the accepted current behavior, don't rebuild it.

**Conformance status: preconformant compatibility stage only — not the conformant Homes Prime
provider, and not eligible to enter Tier B evaluation as-is.** Wrapping the legacy answer engine
proves the protocol boundary (auth, schemas, idempotency, retries, failure behavior, process
separation, consumer compatibility); it cannot prove RC2 §§13-18 (grounding, precedence, model/
knowledge requirements), because the legacy engine has no Homes-owned prompts, approved-knowledge
grounding, or answer policy behind it. Becoming the conformant provider requires relocating those
into this service and replacing the legacy delegation with a private, provider-neutral Shared
Model Execution call — see `docs/guest-answer-preview-rollout.md` for the full two-stage plan and
why the stages are distinct, not sequential phases of the same claim.

Preconformant status is signaled out of band only — every response carries an
`X-Utopia-Preview-Mode: legacy-bridge` diagnostic header (`api.py`), applied centrally so it can't
be missed on any response path. It is never in the customer-visible `limitations[]` field: RC2
§12.2 requires limitations to stay customer-relevant and explicitly prohibits revealing internal
provider, prompt, policy, security, or infrastructure details there — an earlier version of this
provider put it there, which was a real conformance bug, caught in Control-side review.

**Status: local conformance evidence only, not deployed.** This repo delivers source, tests, a
Dockerfile, and a review-only Render Blueprint example — nothing here has been deployed, and no
production credentials exist. See `docs/stoin-utopia-business-contract-guest-answer-rc2.md` for
the full normative text and `docs/implementation-notes.md` for the judgment calls made where the
contract intentionally leaves strangler-step answer sourcing, idempotency retention, and similar
architectural choices open.

This repo is intentionally standalone: it never imports from, or depends on, `cloud-hermes-lucy`,
`utopia-homes-web`, or any Stoin Control repository. It calls the *same* legacy upstream endpoint
`utopia-homes-web` already calls (same env-var-driven config, same wire shape), but as an
independent process — Stoin Control stays completely outside this guest request path. The vendored
conformance bundle under `contracts/` is the only shared artifact between this repo and the
Control-side and website-consumer implementations, pinned by digest.

**Stage 2 candidate (local only):** `GUEST_ANSWER_PROVIDER_ANSWER_ENGINE=homes-prime` switches this
service to the Homes Prime engine: Homes-owned prompts, a digest-pinned approved knowledge
projection, deterministic grounding checks, a support-review pass, and a private
`inference.execute@1.0` client for Shared Model Execution (RC1, pinned under
`contracts/stoin-shared-model-execution-v1-rc1/`). It is refused outside `preview`, the legacy
bridge stays the default, and nothing has been deployed or run against a real provider. See
`docs/homes-prime-stage2.md`.

## Layout

```
src/guest_answer_provider/    the service
  config.py                    env vars -> frozen Config, including the legacy-upstream config
  patterns.py                  format rules copied verbatim from the bundle's common.defs.json
  bundle_tools.py               imports the vendored bundle's own check_invariants_impl.py directly
  schema_validation.py         request/response/error validation against the vendored JSON Schema
  models.py                    Pydantic response/error models (second line of defense)
  errors.py                    the §17 error table as an exception hierarchy
  auth.py                      stoin-business-jwt-v1 (Ed25519/EdDSA) verification, per-key
                                environment/capability binding, jti replay
  jti_replay.py                 in-memory jti replay rejection
  canonicalization.py          RFC 8785 JCS + SHA-256 (via the same pinned `rfc8785` version
                                Tier A itself validated)
  idempotency.py                the I-B09 five-branch idempotency decision table
  legacy_upstream.py            Python port of utopia-homes-web's askPublicLucy()
  legacy_bridge.py              RC2 <-> legacy shape mapping (see implementation-notes.md)
  logging_utils.py              §16-allowlisted structured access logging
  api.py                        FastAPI app factory: the full request pipeline, error envelope
  runtime.py                    process entrypoint (python -m guest_answer_provider.runtime)
tests/
  unit/                        pure-function tests + full vendored-vector replay, no network
  integration/                 FastAPI TestClient, in-process, header/transport/exchange vectors
  contract/                    real OS-process + real HTTP + real fake-upstream HTTP server —
                                the two-process (three, counting the fake upstream) proof
contracts/stoin-business-guest-answer-v1-bundle/   vendored, digest-pinned conformance bundle
docs/stoin-utopia-business-contract-guest-answer-rc2.md   reference copy of the normative text
docs/implementation-notes.md   judgment-call log
docs/guest-answer-preview-rollout.md   proposed (not executed) isolated-preview plan
deploy/render/*.yaml.example   review-only Render Blueprint — never applied
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

All of the above pass locally as of this commit (191 tests total). CI runs the same sequence on
every push via `.github/workflows/ci.yml`.

## Running it locally (non-production)

The provider needs a JWT public-key allowlist and the legacy-upstream config, supplied entirely
through environment variables — see `.env.example` for the full list and
`src/guest_answer_provider/config.py` for validation rules.

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
   python -m guest_answer_provider.runtime
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
