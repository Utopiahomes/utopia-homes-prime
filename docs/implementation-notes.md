# Implementation notes — judgment calls

RC2 (guest.answer@1.0) pins the wire contract exactly; it does not (and by design should not)
pin how a strangler-step provider sources its answers, or several small architectural choices
Tier A itself leaves open. This is the log of every place this implementation had to decide
something the contract doesn't, so the decisions are visible and revisitable rather than buried.
None of these are blocking — they're flagged the same way the Management Contract adapter's own
`implementation-notes.md` flagged its analogous list.

## Strangler-step answer sourcing (legacy_bridge.py)

The provider's only answer engine right now is the existing, already-working legacy FAQ-snapshot
lookup `utopia-homes-web` already calls (`lib/lucy/server.ts`'s `askPublicLucy`) — per RC2 §20.2's
strangler pattern, step 2 wraps the *accepted current behavior*, it doesn't invent a new one.
Five real mapping decisions fell out of that:

1. **`message.content` (1–2000 chars, RC2-valid) outside legacy's narrower `question` bound
   (2–500 chars).** Not truncated (would silently change the guest's question), not
   `invalid_request` (the request *is* schema-valid). Mapped to `answer_validation_failed` (503,
   non-retryable) — a provider-capability limitation, not a client mistake, and correctly
   non-retryable since retrying identical over-length content can never succeed.
2. **Legacy's oversized answer (up to 8000 chars) vs. RC2's tighter `answerText` bound (≤4000
   chars).** Same treatment: `answer_validation_failed`, never truncated.
3. **`sources: []`, always.** Legacy's `source` field is unstructured free text with no
   relationship to any I-B08 approved-destination allowlist. Fabricating a `sources[]` entry from
   it would either fail the exact-destination invariant or misrepresent unvetted text as a vetted
   source. Surfaced instead as a fixed `limitations[]` note.
4. **`actions: []`, always** — legacy has no action/link concept at all.
5. **Every successful legacy call → `outcome: "answered"`.** `askPublicLucy` has no "no match
   found" signal distinguishable from a real answer (that logic lives inside `cloud-hermes-lucy`,
   out of this repo's scope); any legacy failure (transport, timeout, malformed body, snapshot
   digest mismatch — all collapsed into `PublicLucyUnavailable` by the original TypeScript, ported
   as `LegacyUpstreamUnavailable` here) maps to `temporarily_unavailable` (503, retryable) —
   distinct from `answer_validation_failed` because it's an opaque, not-distinguishable-in-advance
   failure, not a structural content-length limitation.

## Idempotency store (idempotency.py)

RC2 §11 pins the five-branch decision table (I-B09) exactly but explicitly leaves retention
policy architectural ("single-coordinator v1.0 replica profile", no TTL number given). This
provider's own conservative choice:

- **900s (15 min) TTL** for a completed record. Long enough to cover the consumer's one permitted
  retry plus a human resubmitting after a page reload; short enough to bound unbounded memory
  growth in a process with no persistence layer.
- **15s ceiling** for a record stuck `in_progress` (configurable via
  `GUEST_ANSWER_PROVIDER_IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS`, defaulting to a bit more than
  the legacy upstream's own 10s timeout) before it's treated as `unresolved` — covers a process
  crash mid-request without holding a slot open forever.
- **`still_eligible`** is mapped to "the record's stored `response_snapshot_digest` still equals
  the currently configured `LUCY_PUBLIC_SNAPSHOT_DIGEST`" — the only real public state this
  transitional provider has. Rotating the FAQ snapshot while a record is cached correctly
  invalidates it (`response_invalidated`) rather than replaying a stale answer forever.
- A record only reaches `durable_status="completed"` for well-formed outcomes (success,
  `answer_validation_failed`, `temporarily_unavailable`) — all of these are recorded (RC2's I-B09
  table has no path back to `new_execution` once *any* record exists for a key, so caching a
  transient `temporarily_unavailable` is correct: it lets the consumer's automatic retry get a
  clean `replay` of the same definitive-for-now answer instead of hammering the upstream again).
  Only a genuinely unhandled exception (a real bug, not a modeled failure) is left uncompleted, so
  it naturally times out into `unresolved` via the in-progress ceiling above.

## `unsupported_version` is effectively unreachable in v1.0

`request.schema.json` pins `contract_version` to `const: "1.0"`, so a request with any other
version value fails schema validation and maps to `invalid_request`, not `unsupported_version`.
The `unsupported_version` error class exists (per the full §17 table) but has no live trigger path
until this provider ever needs to reject an *unsupported but otherwise well-formed* version
string distinctly from a malformed one — a decision for whenever v1.1+ exists.

## `Accept: application/json` — no wildcard leniency

Unlike the Management Contract adapter (GET-only, tolerant of a bare `Accept: */*` from curl/
browsers as a convenience), this contract's own `headers.008` vector tests only
`application/json`/absent/`text/html`/`application/xml` — no `*/*` case — so `_accept_ok()`
requires the literal media type exactly. This was caught by the vector-driven integration tests
during implementation (httpx's default client always sends *some* Accept header, which surfaced
the leniency as a real behavioral gap rather than a hypothetical one).

## `nbf == iat`, found by vector replay

RC2 §7.2 requires `nbf` to equal `iat` exactly (not merely "not yet valid within skew", which is
all the Management Contract's looser profile checks). This was missing in the first draft of
`auth.py` — copied too literally from the Management Contract's auth skeleton — and was caught by
replaying `auth.014.json` (`violated_rule: "nbf==iat"`) during implementation, not found by
inspection. Left here as a concrete example of why every vector category got a real replay test
rather than a hand-picked subset.

## Session-id logging digest key

RC2 §16 requires the durable-telemetry `session_id_keyed_digest` field to use an
"environment-specific key, unavailable to browser code" — this provider reads it from
`GUEST_ANSWER_PROVIDER_SESSION_DIGEST_KEY` if set, and otherwise falls back to a random key
generated once per process start. That fallback is acceptable for a preview instance (digests
just won't be stable across restarts, which nothing here depends on) but a real deployment should
set the env var explicitly.
