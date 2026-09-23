# Homes Dragon meeting operations — local implementation notes

**Status: local candidate only.** Implemented and tested locally against the Workspaces draft
contract (`utopia-wordspaces` `docs/homes-dragon-meeting-contract-v1-draft.md`, at `22b8384`) with
the fake Shared Model Execution service. Nothing is deployed. No provider call, credential, key
registration, or spending has happened. Real inference waits on the Tiamat gates (Gate 2 closure,
then Gate 3) and on a signed Homes meeting execution profile.

This is a capability separate from `guest.answer@1.0`. It shares the service, the Homes Prime
execution identity toward Tiamat, and the JWT verifier; it does not share scopes, keys, schemas,
or error bodies.

## Operations

| Operation | Notes |
| --- | --- |
| `GET /business/v1/meeting/identity` | `synth_id` `stoin:synth:utopia-homes-prime`, `display_name` `Homes Dragon`, `software_version` = the deployment's business release ID, and the approved materials with the SHA-256 of their exact bytes |
| `POST /business/v1/meeting/respond` | One execution. Outcomes `answered`, `declined`, `unavailable`; execution or validation failure is an `unavailable` turn (HTTP 200), not an error |
| `POST /business/v1/meeting/draft` | One execution. Failure is an error (`503 temporarily_unavailable` or `504 deadline_exceeded`) |

Enabled with `GUEST_ANSWER_PROVIDER_MEETING_ENABLED=true`, only alongside the preview-only
`homes-prime` engine. Otherwise the routes do not exist.

## Authentication

The existing verifier (`auth.py`), with the required scope as a parameter:

- `alg` EdDSA, a registered active `kid`, per-key `iss`/`sub`/`aud`/environment binding;
- `nbf == iat`, lifetime ≤ 300 s, ±30 s skew, `jti` a UUID v4 rejected on reuse;
- `scope` exactly `meeting.assist` (one capability per token);
- the key's own configured capabilities must include `meeting.assist`, or `403 capability_forbidden`.

Registering the Workspaces adapter is one `JWT_PUBLIC_KEYS_JSON` entry holding its **public** key:

```json
{"kid": "<adapter kid>", "public_key_pem": "<PEM>", "environment": "preview",
 "issuer": "<adapter iss>", "subject": "<adapter sub>",
 "audience": "stoin:business:utopia-homes-prime", "capabilities": ["meeting.assist"]}
```

The website's `guest.answer` key cannot call the meeting operations, and the adapter's key cannot
call `guest.answer` (tested both ways).

## Materials

`materials/homes-dragon/manifest.json` lists each material's id, version, kind, title, file, and
SHA-256. At startup the manifest's own SHA-256 must be in
`GUEST_ANSWER_PROVIDER_MEETING_MATERIALS_ALLOWED_DIGESTS`, and each file's bytes must match its
recorded digest, or the service refuses to start. A request's materials resolve by exact
(id, version); anything else is `403 material_not_permitted` before any execution. Only the
materials a request names enter the prompt, and `display_material` is constrained to those.

The vendored pair is the Workspaces Demo v1 material, byte-identical to its copies at
`utopia-wordspaces` `ccfe930` (digests `dd5fcd22…` and `33407208…`), approved by Ray on
2026-09-23. The earlier pending-approval bytes (`54c522fa…`, `02e8cc06…`) are no longer held, so
they are no longer reported or accepted. Allowlisting the manifest's digest remains the deployment
act that puts a manifest into service.

## Homes rules on model output

- Respond: plain speakable text (no Markdown, HTML, or URLs); every number in an answer must appear
  in the named materials or in what was said; a decline carries no answer or material and a fixed
  limitation line. Breaking a rule makes the turn `unavailable`.
- Draft: every number must appear in the notes or materials, or the draft fails closed. An
  `owner` or `timing` the notes do not state verbatim is returned as `null`.

## Retention

The meeting operations write nothing to disk and have no memory write path. Access logs carry only
content-free fields (tested with sentinel strings). The idempotency record keeps a request digest
and the response in process memory for `MEETING_IDEMPOTENCY_TTL_SECONDS` (default 300, max 900);
expired records are swept on every admission, not only when the same key returns.

## Timing

Homes caps respond at 10 s and draft at 20 s. Defaults are 6 s / 15 s execution ceilings plus
transit and the Prime reserve. The per-call cost ceilings are required and have no defaults.
Everything runs under one meeting profile, `utopia-homes.meeting-assist.v1`.

## Contract changes proposed to Workspaces

1. `meeting_notes` ≤ 48,000 characters (not 100,000). One Tiamat execution accepts at most 65,536
   characters per message and 196,608 bytes in total, and the notes must fit the meeting profile's
   input-token limit together with the policy and the materials.
2. `meeting_id` is opaque (`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`), not a UUID, matching the adapter.
3. `scope` is exactly `meeting.assist`.
4. Further bounds: speaker ≤ 120 characters, line text ≤ 4,000, ≤ 8 materials without duplicates;
   draft lists ≤ 20 items of ≤ 500 characters; owner and timing ≤ 120.
5. Error codes: `invalid_request`, `unsupported_version`, `authentication_failed`,
   `capability_forbidden`, `material_not_permitted`, `not_found`, `idempotency_conflict`,
   `request_in_progress`, `idempotency_recovery_unavailable`, `request_too_large`, `rate_limited`,
   `temporarily_unavailable`, `deadline_exceeded`. Retryable errors carry `Retry-After`.
