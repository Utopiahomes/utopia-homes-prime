# Homes Prime Stage 2 candidate — local implementation notes

**Status: local candidate only.** Implemented and tested locally against Shared Model Execution
RC1 with a fake execution service. No provider calls, credentials, spending, infrastructure,
deployment, production traffic, or corpus reauthorization have happened. The legacy bridge stays
the default engine and the existing production path is untouched. This is not a claim of
`guest.answer@1.0` conformance: no Tier B semantic evaluation has run.

## Pinned contracts

| Artifact | Pin |
| --- | --- |
| Business Contract `guest.answer@1.0` RC2 | vendored Tier A bundle `contracts/stoin-business-guest-answer-v1-bundle/` (unchanged) |
| Shared Model Execution contract RC1 | `a010c2cd5d501dd5586be3e1c54753ed7bf82505b9971d19c007e227bb9a75a8` |
| Runtime-Recovery Companion RC1 | `9ac574affc4cd5c9dc5266b4665d64409cf387abc69c59918d7fbe0475ddbc34` |
| Normative recovery extract (§§11–13.1) | `d8cb89be105e4507f493e5ad88d365834fc5538bb1e4f315701325e195810176` |
| Source checkpoint | `cloud-hermes-lucy` `a4e52382a43e18672d8fc100d065a31f8d78fef6` |

`contracts/stoin-shared-model-execution-v1-rc1/verify_pin.py` re-verifies all three digests, the
byte counts, LF-only line endings, the manifest's agreement with `PIN.json`, and that the
companion's extract is byte-identical to the contract's §§11–13.1. CI runs it. The exact-string
error table, `Retry-After` set, and schema keyword set are asserted against the pinned contract
text in `tests/unit/test_sme_rc1_pin.py`, not against a transcription.

## Boundary

```text
website (unchanged) --guest.answer@1.0--> Homes Prime provider
                                             |  knowledge.py   approved corpus, digest-pinned,
                                             |                 effective-time + withdrawal filter
                                             |  homes_prime.py policies, context, grounding checks,
                                             |                 sources/actions, RC2 error choice
                                             |  sme_client.py  private inference.execute@1.0 client
                                             v
                                  Shared Model Execution (Tiamat) — fake in tests
```

- The website still calls only `guest.answer@1.0`. No execution identifier, receipt, header,
  credential, or error code crosses into the RC2 response (asserted in integration tests).
- Selected with `GUEST_ANSWER_PROVIDER_ANSWER_ENGINE=homes-prime`. Until 2026-09-24 the config
  refused this engine outside `preview`. Ray approved production use at cutover gate C8, so it now
  runs wherever it is selected explicitly; the legacy bridge stays the default. Responses still
  carry `X-Utopia-Preview-Mode: homes-prime-candidate`, because no Tier B evaluation has run.
- Stoin Control / the Tiamat management function is not in the request path.

## What Homes owns (moved into this service)

| RC2 responsibility | Where |
| --- | --- |
| Answer and support-review policies (relocated from legacy `lucy/public_model.py`, adapted to RC2 outcomes, page context, local-recommendation rules, and plain text) | `homes_prime.ANSWER_POLICY`, `REVIEW_POLICY` (SHA-256 digests exported) |
| Approved knowledge projection, admitted only under an allowlisted canonical digest | `knowledge.KnowledgeProjection` |
| Effective restrictions: effective windows, withdrawals, and replay eligibility tied to knowledge release + withdrawal set | `knowledge.py`, idempotency eligibility token |
| Retrieval / context assembly (the effective packet only; history and page context marked untrusted) | `homes_prime.build_generation_messages` |
| Factual validation: evidence inside packet, numbers ⊆ cited evidence + question, wrong-property, property talk outside cited claims, uncited claims, markup/URLs, link approval | `homes_prime.validate_draft` |
| Final-answer support check of the exact displayed text | second execution (`support-review` profile) + `validate_verdict` |
| Sources and actions built only from approved configuration; I-B04..I-B08 re-checked with the bundle's own code | `homes_prime.assemble_response` |
| Customer-facing failure selection | `homes_prime.guest_error_for_execution_failure` |

Curated local recommendations (§14): the policy only permits recommendations present in
approved evidence. No approved local guide exists yet, so the candidate will say it has no
verified recommendation. Selecting real businesses is a Homes content decision (RC2 §23.5).

## Private execution client (RC1)

- One `execute()` is one logical execution: fresh `Idempotency-Key`, exact body bytes serialized
  once, `X-Content-SHA256`, EdDSA JWT with `req` binding, a fresh `jti` and `X-Request-ID` per
  attempt, `Accept-Encoding: identity`, no redirects, `trust_env=False`, 131,072-byte bounded
  streaming read.
- Generation and support review are separate executions with separate keys and cost ceilings.
- Retry: at most one, only after a transport failure or a retryable code; same key and bytes;
  `Retry-After` or 250–750 ms backoff; skipped unless delay + transit + the retry's **complete**
  profile ceiling fit the remaining budget; the timeout header never increases.
- Success parsing is strict on known members and tolerant of unknown ones: request/profile
  binding, `finish_reason`, the combined-token invariant, `generated_tokens ≤ max_output_tokens`,
  receipt invariants, `reserved ≤ max_cost_microusd`, and **Homes re-validates the output against
  its own schema** regardless of provider enforcement.
- Error parsing enforces the pinned message/retryable/status triple, `Retry-After` permission,
  release-header absence on authentication errors, and receipt rules (`idempotency_conflict` and
  `state_store_unavailable` receiptless; 502s `failed` with cost; `execution_aborted` settled at
  zero; `execution_outcome_unknown` state).

### Failure mapping (RC1 → RC2)

| RC1 outcome | Homes category | RC2 error |
| --- | --- | --- |
| `deadline_exceeded`; budget exhausted; `request_in_progress` or timeout with no retry budget | deadline | `504 deadline_exceeded` |
| `rate_limited` (after the retry rule) | rate_limited | `429 rate_limited` (provider `Retry-After` carried) |
| `provider_response_invalid`, `provider_response_too_large`, `output_limit_reached`, `content_filtered` | unsupported_output | `503 answer_validation_failed` |
| Homes validation or support-review rejection | — | `503 answer_validation_failed` |
| every other code, malformed provider response, transport failure, knowledge unavailable | unavailable | `503 temporarily_unavailable` |

**Lost response during a rollout** (RC1 §11, criterion 80): the retry receives
`execution_invalidated`, which maps to `temporarily_unavailable`. Homes never regenerates under a
new key within the attempt, and the website's permitted retry replays Homes' recorded outcome.

## Timing

RC2 §6 caps each guest attempt at 15 s. The engine enforces
`generate ceiling + review ceiling + 2 × transit + Prime reserve ≤ 15 000 ms` at startup and hard-
caps the pipeline with `asyncio.wait_for`. Support review starts only if its complete ceiling
still fits (RC1 §18). Defaults are 9 000 / 4 000 / 250 / 1 500 ms.

## Tests

| Layer | File | Covers |
| --- | --- | --- |
| Pin | `tests/unit/test_sme_rc1_pin.py` | digests, extract identity, tamper detection, tables vs pinned text |
| Wire | `tests/unit/test_sme_wire.py` | request construction, message rules, schema subset boundaries, success/error parsing negatives |
| Client | `tests/unit/test_sme_client.py` | JWT/`req`, lost-response replay with one dispatch, rollout invalidation, retry budget, deadlines |
| Knowledge | `tests/unit/test_knowledge.py` | digest pinning, effective time, withdrawal, destination approval; the R1 corpus under its approved digest |
| Grounding | `tests/unit/test_homes_prime_validation.py` | rejection categories, failure-mapping table, timing invariant |
| API | `tests/integration/test_homes_prime_api.py` | end-to-end answers, final-answer rejection, failure mapping, deadlines, idempotent replay, rollout-lost-response, config guards |
| Network | `tests/contract/test_homes_prime_live.py` | subprocess provider ↔ real TCP fake execution service, dropped connection, unreachable service |

`tests/fixtures/fake_sme.py` verifies requests independently of `sme_wire.py` (hashlib/base64/PyJWT).
All knowledge used in engine tests is a clearly labeled synthetic fixture. Since the 2026-09-24
source split, the Ray-approved R1 corpus lives in this repository (`knowledge/r1/`, moved byte-exact
from cloud-hermes-lucy because Homes owns its knowledge). One test loads it under its approved
digest; no test sends it to a provider.

## Decisions recorded (Lyra, 2026-09-17)

Lyra accepted `172045a` (now `6a62519` in this repository) as the local Homes Stage 2 candidate (not a conformant or deployable
release) and settled the returned dependencies:

1. **Timing.** RC2's 15 s attempt and 22 s interaction limits stand. Preview uses 9 s generation +
   4 s review + 2 × 250 ms transit + 1.5 s Prime reserve = 15 s (the config defaults). SME RC1
   §18's 11/5/6 s example is superseded as an activation plan; it was non-normative, so the frozen
   wire contract is unchanged.
2. **Profiles confirmed:** `utopia-homes.public-answer.generate.v1` (9 000 ms / 900 generated
   tokens) and `utopia-homes.public-answer.support-review.v1` (4 000 ms / 300 generated tokens), no
   provider fallback. Per-call cost ceilings stay unset until provider/model pricing is selected;
   the config requires them and has no defaults.
3. **Structured output.** Both profiles must faithfully support RC1's restricted `json_schema` with
   no downgrade. Homes never drops its evidence-ID or link-ID enums: the admitted packet is bounded
   at 64 IDs of each (`knowledge.MAX_ADMITTED_IDS`), and a larger effective set fails closed as
   `temporarily_unavailable` before any execution request is built. Withdrawal or a smaller
   release brings a corpus back inside the bound. Selecting a relevant subset instead of failing
   closed would be a separate retrieval design decision.
4. **Execution identity.** Subject confirmed as `stoin:synth:utopia-homes-prime` (the config
   default). Issuer, `kid`, registered public key, and endpoint come from Tiamat provisioning and
   are not invented here.
5. **Review privacy.** The approved route must cover the whole support-review payload (visitor
   message, approved context, candidate answer) under the same retention, training-denial, region,
   and logging constraints.
6. **Knowledge.** The existing R1 corpus stays local-testing-only; a real-provider preview needs an
   explicitly authorized Homes knowledge release.
7. **Conformance.** Lyra builds and independently verifies the RC1 conformance bundle next. These
   tests are implementation evidence, not a replacement for it.

Remaining Tiamat activation inputs: issuer, `kid`, public-key registration, endpoint, cost ceilings,
provider route, and the provider-authorized knowledge release.

## First real run without Tiamat (2026-09-24)

Ray authorized a real run on the direct route with the R1 knowledge corpus and the Demo v1 meeting
materials. The environment held no Tiamat setting of any kind, and every call went to
`openrouter.ai` only.

- **Route:** Homes' own OpenRouter key; `google/gemini-3.1-flash-lite` through `google-vertex`,
  no fallbacks; price caps $0.30 / $1.80 per million input / output tokens.
- **Guest answers** (generate, then support review), 2.2–2.8 s end to end:
  - "How many guests does Buttercup Beauty sleep, and does it have a pool?" → answered: "up to 22
    guests … heated private pool", with the Buttercup source and link. It matches the approved
    text.
  - "Can I bring my dog to The Shamrock?" → answered: dogs welcome. It matches the approved text.
  - "What is the door code for Central Ave Socialization?" → refused: no access to private
    information; contact link.
- **Meeting respond** (checklist material): answered in 1.3 s, with the checklist as
  `display_material`. **Meeting draft:** 1.6 s; the confirmed decision and the proposal were kept
  apart, the owner was kept, and the timing was left null.
- **Cost:** 8 provider calls, $0.0085 in total (about $0.0024 per guest answer, $0.0005–0.0006
  per meeting call).

The first attempt was rejected by Google (HTTP 400, before any charge): a bounded array of enums
nested inside the bounded `segments` array exceeds Google's structured-output limits. The draft
schema no longer bounds `evidence_ids` per segment; `validate_draft` still rejects more than eight
distinct evidence IDs per answer. A test now keeps every Homes output schema free of nested
bounded arrays.
