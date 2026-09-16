# Proposed isolated-preview rollout (not executed)

This is a plan, not a completed action. Nothing described here has been deployed, and this
document does not authorize deployment — it exists so the next, separately-approved step has a
concrete sequence to follow, per RC2 §20.2's staged strangler pattern:

> Step 2: wrap the accepted current behavior behind the new contract, retaining existing
> production behavior. Step 3: run an explicitly preconformant compatibility preview using
> synthetic and staff-only traffic — it must not advertise conformance or receive public guest
> traffic while carrying forward behavior that does not yet satisfy §§13-18.

This repo delivers step 2 (the wrapper) with local conformance evidence (191 tests, including a
real two-process network proof — see README.md "Verification"). What follows is the proposed
sequence for step 3.

## 1. Provision a preview-only environment

- Deploy this provider using `deploy/render/utopia-homes-guest-answer-provider.preview.yaml.example`
  (renamed to `render.yaml`), with `GUEST_ANSWER_PROVIDER_ENVIRONMENT=preview` and a JWT key
  allowlist containing **only** `environment: "preview"` keys — never a production key, so
  auth.024/025's environment-binding rejection is a second, redundant layer of protection on top
  of the operational discipline of not issuing one.
- Point `LUCY_PUBLIC_API_URL` at the real legacy upstream (read-only FAQ lookup, no guest-specific
  state, safe to share with a preview deployment) or a dedicated preview-only mirror if the
  operator prefers full isolation.
- Deploy the website-consumer preview route (see `utopia-homes-web`'s
  `claude/guest-answer-preview` branch, `app/api/lucy-preview/route.ts`) to a preview/staging
  environment of the website, with `PREVIEW_GUEST_ANSWER_PROVIDER_URL` pointing at the provider
  deployed above and a matching preview-environment JWT keypair.

## 2. Synthetic traffic first

- Before any staff-only traffic, run a scripted synthetic suite against the live preview
  deployment: replay a representative sample of the vendored Tier A vectors as real HTTP requests
  (the pattern `tests/contract/test_live.py` already establishes locally, pointed at the preview
  URL instead of a spawned local subprocess).
- Confirm: schema-valid responses, correct release headers, idempotency behavior holds under
  real network latency (not just the fast local fake upstream), and the `answer_validation_failed`
  non-retry rule holds against the real legacy upstream's real response shapes.

## 3. Staff-only traffic

- Once synthetic traffic passes, open the preview website route to staff only (existing
  same-origin + session-cookie mechanisms in `utopia-homes-web` already gate `/api/lucy`; the
  preview route should get an equivalent or stricter gate — e.g. an internal-only header or an
  allowlisted staff session claim — before any human traffic reaches it).
- Collect real interaction data: does the legacy FAQ-snapshot engine's answers, wrapped in RC2's
  richer envelope (session/turn tracking, idempotency, structured errors), actually serve staff
  well? This is where the gap between "protocol-conformant" (what this repo proves) and
  "semantically good" (Tier B) becomes visible.

## 4. Tier B — semantic and grounding evaluation

- RC2 §§13-18 (grounding, precedence, model requirements) are explicitly **not** satisfied by this
  strangler-step wrapper, and Tier A's own scope excludes them by design (the same boundary RC3's
  Management Contract bundle drew around evidentiary/content requirements). Tier B evaluation —
  does the answer actually ground correctly, cite real sources, handle out-of-scope questions —
  requires a runnable candidate with real traffic data, which steps 1-3 above produce. This
  provider is explicitly not advertised as passing Tier B; that's the next phase, not this one.

## 5. Only after 1-4: consider production

Nothing above authorizes swapping the live `/api/lucy` route's traffic, generating production
credentials, or announcing conformance. Each of those is a separate, explicit decision for a
later task once steps 1-4 have actually run and been reviewed.
