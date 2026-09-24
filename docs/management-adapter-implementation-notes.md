# Implementation notes — judgment calls not pinned by the contract text

None of these are normative; they're recorded so they can be revisited without re-deriving them
from scratch. "The contract" below means `docs/management-contract-v1-rc3.md`.

1. **Preflight check order.** `api.py`'s `_preflight()` validates `X-Request-ID` *first*, before
   anything else, specifically so that a later failure (bad Accept header, auth failure, rate
   limit) still echoes a valid request ID if the caller sent one. After that: query-string
   rejection, body rejection, header-size, `Accept`, authentication, then rate limit. The
   contract doesn't say what happens when multiple things are wrong simultaneously; this order is
   a reasonable default, not a normative sequence.

2. **`Accept` header handling.** A missing or incompatible `Accept` header returns `400
   invalid_request`. This is inferred from §7 listing `Accept` among the headers "every request
   MUST include," not from an explicit vector — 406 `contract_not_supported` would also have been
   defensible, but 400 was chosen to keep every "malformed common request" case under one code.
   `httpx`'s `TestClient` always attaches a default `Accept: */*` header, so the test for this
   (`tests/integration/test_api_errors.py::test_accept_header_not_json_returns_400`) sends an
   explicit incompatible value rather than testing a truly absent header.

3. **Env var naming.** The `MANAGEMENT_ADAPTER_*` prefix and every specific variable name in
   `config.py` are invented; nothing in the contract names them.

4. **`/healthz` liveness endpoint.** Added outside `/management/v1`, unauthenticated, for
   Render's `healthCheckPath` and the contract-test harness's readiness poll. §5 explicitly
   permits this ("a separate process-liveness endpoint... is not part of this contract and does
   not represent Homes business health"), but its exact path and shape are invented.

5. **JWT key material format.** The allowlist stores PEM-encoded SPKI public keys
   (`public_key_pem`), not raw base64 Ed25519 bytes. Either works with `cryptography`/PyJWT; PEM
   is the more operationally familiar format for a deployment-config value.

6. **413 scope.** Mapped only to headers exceeding a bounded size (16 KiB, in `api.py`). A GET
   request that carries any body at all is rejected as `400 invalid_request` ("bodies aren't
   accepted at all"), not `413`. Genuinely oversized wire-level requests are expected to be
   rejected by the transport (uvicorn/h11) or a front proxy before reaching the application.

7. **Rate limiting.** `_RateLimiter` in `api.py` is a simple in-process fixed-one-minute-window
   counter per authenticated `kid`. The contract only requires the provider to *permit* at least
   12 requests/minute (§13); it doesn't require active enforcement. The configured default (120
   req/min, `config.MINIMUM_RATE_LIMIT_PER_MINUTE = 12` as the enforced floor) exists mainly so
   the `429 rate_limited` path is real and testable, not to police normal Control polling
   cadence (§13's own guidance: `/health` no more than once per 30s, `/version` every 5 minutes).

8. **Time-based JWT validation is fully manual, not PyJWT's built-in `exp`/`nbf` checking.**
   `auth.py` passes `verify_exp: False, verify_nbf: False` to `jwt.decode()` and checks `iat`,
   `nbf`, `exp`, and the 300-second lifetime cap itself against an injectable `now` parameter.
   This was a correctness fix, not just a testing convenience: PyJWT's built-in time validation
   always uses the real wall clock, which made the exact `exp+30`-accept / `exp+31`-reject
   boundary (§18 criterion 19) impossible to test deterministically. Centralizing all time logic
   in one place, driven by one clock value, is also simply more auditable for a security path.

9. **Auth-failure internal logging granularity.** `authenticate()`'s internal failure reason
   (e.g. "wrong scope" vs. "expired") never crosses the API boundary and currently isn't logged
   at all beyond the generic `http_class: "4xx"` access-log line — §15's logging allowlist has no
   field for "which check failed." If finer-grained internal diagnosis is wanted later, it should
   be a metrics counter/label, not a free-text log field.

10. **Unmatched-path 404 behavior.** Paths outside the four defined resources (and `/healthz`)
    fall through to Starlette's default 404 response rather than the §13 error envelope, since
    §13's table has no 404 entry and no vector exercises it.

11. **`runtime_id` uniqueness.** The contract requires it "unique" per deployment/runtime, but
    doesn't say whether it must change on every process restart or only on every release. This
    implementation treats it as deployment-config (stable across restarts of the same release,
    per `.env.example`/the Render Blueprint), consistent with identity needing to survive
    restarts (§9.2, criterion 7 discusses `synth_id`/`realm_id` specifically, not `runtime_id`,
    so this is an extension of that principle rather than a directly stated requirement).

12. **Known gap.** The two scripts that originally generated the vendored bundle's vectors were
    not available in the session that produced this repo (they lived in a separate AI assistant's
    session). This implementation treats the vendored vectors as a fixed, trusted input and does
    not attempt to regenerate or extend them.

13. **Two Render Blueprint examples, not one.** `deploy/render/utopia-homes-management-adapter.yaml.example`
    describes the eventual shape (a private service, reachable only inside Render's private
    network) once Homes and Control are co-located there. Stoin Control's non-production
    commissioning proof isn't (yet) on that private network, so it needs a public HTTPS `web`
    service instead — added as `utopia-homes-management-adapter.staging.yaml.example`, relying on
    the adapter's own JWT layer as the access boundary rather than network isolation. This isn't a
    security downgrade: the contract's actual boundary was always JWT + Ed25519, not network
    placement (per Lyra's review, 2026-09-16).

14. **Docker image build not verified.** The Dockerfile and `requirements.lock` (generated via
    `uv pip compile --python-platform x86_64-unknown-linux-gnu`, since `uvicorn[standard]`'s
    Linux-only extras can't be hash-locked from a same-host Windows `pip-compile`) were written
    carefully but the actual `docker build` was not run in the environment this repo was built in
    — the local Docker daemon wasn't available. Verify the build before relying on the image.
