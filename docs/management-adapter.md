# Utopia Homes Management Adapter

> **Folded into Utopia Homes Prime on 2026-09-24** from the standalone
> `utopia-homes-management-adapter` repository (`a292321`). It is still a separately deployed
> Homes component: package `utopia_homes_prime.management_adapter`, tests under
> `tests/management/`, image built from `deploy/management-adapter/Dockerfile`. Paths below are
> updated; the text is otherwise the original README.

The transitional Homes-side provider for **Management Contract v1.0** (RC3, frozen 2026-09-15,
§17). It exposes four authenticated, read-only HTTP resources — `/management/v1/identity`,
`/health`, `/version`, `/capabilities` — so Stoin Control can observe Utopia Homes Prime without
sharing a database, runtime, or release artifact with it.

**Status: transitional scaffolding, not deployed.** This repo delivers source, tests, a
Dockerfile, and a review-only Render Blueprint example — nothing here has been deployed, and no
production credentials exist. See `docs/management-contract-v1-rc3.md` §17 and §18 for the exact
scope and acceptance criteria this implementation targets, and `docs/implementation-notes.md` for
the judgment calls made where the contract text doesn't pin an exact answer.

This repo is intentionally standalone: it never imports from, or depends on, the `cloud-hermes-lucy`
monolith or `utopia-homes-web`. The vendored conformance bundle under `contracts/` is the only
shared artifact between this repo and the Stoin Control implementation, pinned by digest.

## Layout

```
src/utopia_homes_prime/management_adapter/   the service
  config.py                deployment-owned static config (env vars -> frozen Config)
  models.py                Pydantic response models mirroring the bundle's JSON schemas
  errors.py                the §13 error table as an exception hierarchy
  auth.py                  stoin-service-jwt-v1 (Ed25519/EdDSA) verification
  health.py                transitional health assembly (§10, §17)
  logging_utils.py         §15-allowlisted structured access logging
  api.py                   FastAPI app factory: routes, preflight checks, error envelope
  runtime.py               process entrypoint (python -m utopia_homes_prime.management_adapter.runtime)
tests/
  unit/                    pure-function tests, no network
  integration/             FastAPI TestClient, in-process
  contract/                real OS-process + real HTTP, the two-process proof (§18 criterion 16)
contracts/stoin-management-v1-bundle/   vendored, digest-pinned conformance bundle
docs/management-contract-v1-rc3.md      reference copy of the normative contract text
deploy/render/*.yaml.example             review-only Render Blueprints: eventual private-service
                                          shape, and a staging-only public HTTPS variant for the
                                          non-production commissioning proof
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
mypy --strict src

# Confirms the vendored bundle hasn't drifted from its pinned digest.
python contracts/stoin-management-v1-bundle/tools/compute_digest.py --check
python contracts/stoin-management-v1-bundle/tools/verify_bundle.py

pytest -m "not contract" tests/management   # fast
pytest -m contract tests/management/contract   # spawns real subprocesses, ~2 min
```

All of the above are run in `.github/workflows/ci.yml` on every push.

## Running it locally (non-production)

The adapter needs a JWT public-key allowlist and static identity/capability config, supplied
entirely through environment variables — see `.env.example` for the full list and
`src/utopia_homes_prime/management_adapter/config.py` for validation rules. Nothing here talks to a database or an
external service.

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
   print("MANAGEMENT_ADAPTER_JWT_PUBLIC_KEYS_JSON=" + json.dumps(json.dumps(
       [{"kid": "local-dev-1", "public_key_pem": public_pem, "status": "active"}]
   )))

   now = int(time.time())
   token = jwt.encode(
       {
           "iss": "stoin:control",
           "sub": "stoin:service:control-management-reader",
           "aud": "stoin:management:utopia-homes-prime",
           "scope": "management.read",
           "iat": now, "nbf": now, "exp": now + 300,
           "jti": str(uuid.uuid4()),
       },
       private_key,
       algorithm="EdDSA",
       headers={"kid": "local-dev-1"},
   )
   print("TEST_JWT=" + token)
   PY
   ```

2. Copy `.env.example` to `.env`, fill in `MANAGEMENT_ADAPTER_JWT_PUBLIC_KEYS_JSON` with the value
   printed above (it's already JSON-escaped), and export it:

   ```bash
   set -a && source .env && set +a   # bash/zsh; on Windows PowerShell, set each $env: var instead
   ```

3. Run the adapter:

   ```bash
   python -m utopia_homes_prime.management_adapter.runtime
   ```

4. In another shell, using the `TEST_JWT` printed in step 1:

   ```bash
   curl -s http://127.0.0.1:8080/management/v1/identity \
     -H "Authorization: Bearer $TEST_JWT" \
     -H "Accept: application/json" \
     -H "X-Request-ID: $(python -c 'import uuid; print(uuid.uuid4())')" | python -m json.tool

   # A bad/missing token gets the same generic 401 envelope every time:
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/management/v1/identity \
     -H "Accept: application/json" -H "X-Request-ID: $(python -c 'import uuid; print(uuid.uuid4())')"
   ```

The keypair and token above are for local testing only — never reuse them anywhere real.

## Building the image (not deployed)

```bash
docker build -f deploy/management-adapter/Dockerfile -t utopia-homes-management-adapter:local .
```

`requirements.lock` was generated for the Linux container target with
`uv pip compile pyproject.toml --python-platform x86_64-unknown-linux-gnu --python-version 3.12 --generate-hashes`
(run on a Windows host — `uvicorn[standard]`'s Linux-only extras like `uvloop` need a
cross-platform-aware resolver rather than a same-host `pip-compile`). The image build itself was
not verified in the environment this repo was built in (the local Docker daemon wasn't available);
verify it before relying on it.
