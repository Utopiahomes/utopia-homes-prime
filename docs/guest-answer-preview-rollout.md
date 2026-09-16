# Proposed rollout — preconformant preview now, conformant provider later (not executed)

This is a plan, not a completed action. Nothing described here has been deployed, and this
document does not authorize deployment.

**Correction from Control-side review (2026-09-16, relayed via Lyra), recorded verbatim in
substance because it changes what this repo is allowed to claim:** wrapping the existing
`cloud-hermes-lucy` legacy answer endpoint is acceptable **only** as RC2 §20.2's explicitly
preconformant compatibility stage. It cannot become the final Homes Prime provider, because that
would preserve the exact production coupling this whole effort exists to remove — guest answering
would still depend on Cloud Lucy / Control-owned runtime behavior. The two stages below are
therefore distinct, not sequential phases of the same claim, and only the second is eligible for
full conformance and Tier B evaluation.

## Stage 1 — preconformant compatibility preview (this repo, current state)

What this repo actually is today: an independently deployed Homes provider that exposes the RC2
`guest.answer@1.0` wire protocol and **temporarily delegates the actual answer to the legacy
`cloud-hermes-lucy` FAQ-snapshot engine** (`legacy_upstream.py`/`legacy_bridge.py`). This is
useful and legitimate for exactly one purpose: proving the protocol boundary. It is not, and must
not be represented as, a conformant `guest.answer@1.0` provider.

- **Label**: preconformant, out of band only. Every deployment of this stage must say so wherever
  it's described — README, deploy configs, internal comms —
  and, on the wire, via the `X-Utopia-Preview-Mode: legacy-bridge` diagnostic response header
  (`api.py`, applied on every response regardless of outcome). `deploy/render/utopia-homes-
  guest-answer-provider.preview.yaml.example` already names itself `-preview`; keep that
  discipline everywhere else too. This status must **never** go in the customer-visible
  `limitations[]` field — RC2 §12.2 explicitly prohibits limitations from revealing internal
  provider, prompt, policy, security, or infrastructure details, and an earlier version of this
  provider violated exactly that rule (caught in Control-side review; see
  `legacy_bridge.py`'s `build_limitations()` for the corrected, customer-relevant-only content).
  The diagnostic header must never be forwarded into a public widget or analytics — the website
  consumer's own code only reads the response body, never its headers, and should stay that way.
- **Traffic**: synthetic and staff-only only. Never public guest traffic. Never advertise
  `guest.answer@1.0` conformance while this stage is live, in any form — a status page, a
  changelog, a stakeholder update, or a response field, should any of RC2's optional extension
  points ever tempt one.
- **What Stage 1 proves** (and only this): authentication (stoin-business-jwt-v1, environment/
  capability binding, jti replay), schema validation, the idempotency five-branch table, consumer
  retry behavior, failure-mode error codes, process separation from Stoin Control, and
  website-consumer wire compatibility. All of this is already demonstrated locally — 193 tests,
  including a real two-process network proof (README.md "Verification") — and is what a synthetic/
  staff-only preview deployment would additionally prove under real network conditions.
- **What Stage 1 explicitly does not and cannot prove**: anything in RC2 §§13-18 (grounding,
  precedence, model/knowledge requirements). The legacy engine has no Homes-owned prompts,
  approved-knowledge grounding, or answer policy behind it — it's a static FAQ lookup — so no
  amount of traffic against this stage moves the needle on semantic quality. Don't collect staff
  feedback here expecting it to double as Tier B signal; it can't.

Provision and traffic sequence for Stage 1 (still not executed — proposed only):

1. Deploy using `deploy/render/utopia-homes-guest-answer-provider.preview.yaml.example` (renamed
   to `render.yaml`), `GUEST_ANSWER_PROVIDER_ENVIRONMENT=preview`, and a JWT key allowlist
   containing only `environment: "preview"` keys — never a production key.
2. Deploy the website-consumer preview route (`utopia-homes-web`'s `claude/guest-answer-preview`
   branch, `app/api/lucy-preview/route.ts`) to a preview/staging environment, pointed at the
   provider above.
3. Synthetic traffic first: replay a representative sample of the vendored Tier A vectors as real
   HTTP requests against the live deployment (the pattern `tests/contract/test_live.py` already
   establishes locally). Confirm schema-valid responses, correct release headers, and idempotency
   behavior under real network latency, not just the local fake upstream.
4. Staff-only traffic only after synthetic traffic passes, gated at least as strictly as the
   existing `/api/lucy` route's same-origin/session mechanism — ideally an explicit staff-session
   claim, since this route has no guest-facing UI wired to it at all yet.

## Stage 2 — the conformant Homes Prime provider (not started)

Stage 2 is a different architecture, not an extension of Stage 1's traffic ramp. It requires:

1. Relocate Homes-owned prompts, approved knowledge, answer policy, and validation logic into
   this provider (or its successor), replacing `legacy_bridge.py`'s delegation entirely.
2. Replace the legacy HTTP call with a **private, provider-neutral Shared Model Execution call**
   — the exact interface/contract for this does not exist yet in this repo and has not been
   specified to this implementation; it is Control/Homes-side design work, not something inferred
   here. Stoin Control must remain absent from the guest request path regardless of how this is
   implemented.
3. Only once both of the above are real can this provider enter the full RC2 conformance preview
   and Tier B (semantic/grounding) evaluation. Nothing in Stage 1 — no amount of staff traffic
   against the legacy-wrapped path — substitutes for this.

**A note on recoverable prior work relevant to Stage 2**: a separate, orphaned git worktree at
`utopia-homes-web-claude` (unusable via git in this environment — its `.git` metadata points at a
different session's filesystem path — but still readable on disk) contains a real, business-
owner-approved candidate knowledge corpus: `content/lucy-public-knowledge.r1.approved.json` (25
entries, schema `lucy-public-knowledge-v1`, SHA-256-pinned, approved by Ray DeLuca per
`docs/public-lucy-r1-corpus-approval.md` dated 2026-09-12), plus supporting code
(`lib/lucy/knowledge.ts`, `lib/lucy/page-context.ts`) and tests. Its own approval note is explicit
that the scope is **R1 testing only** — it does not authorize production publication, deployment,
provider use, or a future corpus with a different digest. This is flagged here as a pointer for
whoever scopes Stage 2, not as material already folded into this repo; using it for anything
beyond its stated R1-testing scope needs its own, separate authorization.
