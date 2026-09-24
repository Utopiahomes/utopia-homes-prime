# Utopia Homes Prime — source-split provenance

- **Canonical project:** `C:\Users\Forti\Projects\utopia-homes-prime` (package `utopia_homes_prime`)
- **Own history:** this repository keeps its full history (formerly `utopia-homes-guest-answer-provider`,
  branch `claude/homes-prime-stage2`). Before its first push (2026-09-24), every commit's author
  and committer email was rewritten to the GitHub noreply address, so that no personal email is
  on GitHub (Ray's decision). Content, messages, and dates are unchanged; hashes differ from the
  old checkout. See "Commit hash mapping" below.
- **Homes business source extracted from:** `Utopiahomes/cloud-hermes-lucy`, commit
  `ba461b7b131ca7ee3c42177c2826cb0a177daf6d` (`codex/management-contract-v1`). Its Public Lucy files
  are identical to `codex/public-model-milestone-a` (`a233bff`), the branch the live `lucy-public`
  service deploys.
- **Split date:** 2026-09-24
- **Historical repository:** `https://github.com/Utopiahomes/cloud-hermes-lucy` (kept for provenance;
  do not rewrite it)

## Independence rule

Homes Prime is provisionable, deployable, startable, recoverable, releasable, and able to answer
`guest.answer` with no Tiamat account, Control service, Shared Model Execution service, grant,
credential, import, database, or network connection. Tiamat Shared Model Execution is one optional
inference backend (`inference/tiamat.py`). Homes selects the backend in its own configuration, and
its own direct route (`inference/direct_openrouter.py`) needs nothing from Tiamat.

This is checked by `tests/integration/test_homes_without_tiamat.py`:
- guest.answer and the meeting operations succeed with no Tiamat setting of any kind;
- no source imports another system's package;
- no build file references `cloud-hermes-lucy` or a sibling path.

## Ownership of the former Public Lucy (Homes) family in cloud-hermes-lucy

| cloud-hermes-lucy source (at `ba461b7`) | Classification | Where it lives now |
| --- | --- | --- |
| `src/lucy/public_openrouter.py` | HOMES PRIME, adapted | `src/utopia_homes_prime/inference/direct_openrouter.py`: the same routing guarantees (exact model, ZDR, data collection denied, no fallbacks, price ceilings, strict schema, charge bound), now async behind the backend seam |
| `src/lucy/public_model.py` | HOMES PRIME, adapted earlier (Stage 2) | `guest_answer/homes_prime.py`: answer and support-review policies, context assembly, draft and verdict validation, adapted to RC2 |
| `src/lucy/publication.py` | HOMES PRIME, superseded | `knowledge/projection.py`: a digest-pinned knowledge release with effective windows and withdrawal replaces the database publisher and reader |
| `src/lucy/public_retrieval.py` | HOMES PRIME, superseded in part | Whole-packet admission (≤ 64 IDs) in `knowledge/projection.py`, and grounding checks in `homes_prime.validate_draft`. **Not yet ported:** its deterministic `restricted_topic` gate (see "Open items") |
| `src/lucy/public_contracts.py` | HOMES PRIME, superseded | RC2 Business Contract types and schemas (`guest_answer/models.py`, the vendored bundle) |
| `deploy/render/utopia-public-knowledge.r1.json` | HOMES PRIME, moved byte-exact | `knowledge/r1/` (sha256 `0e61afca…`, canonical corpus digest `95e2e20a…`). Still local-testing only; a real-provider preview still needs an explicitly authorized knowledge release |
| `deploy/render/utopia-public-projection.v0.json` | HOMES PRIME, moved byte-exact | `knowledge/legacy/`: the FAQ snapshot behind the legacy bridge |
| `deploy/render/public_knowledge_acceptance.v1.json`, `public_model_acceptance.v1.json` | HOMES PRIME, moved byte-exact | `knowledge/evaluation/`: the Homes question sets for later evaluation |
| `docs/stoin-utopia-business-contract-guest-answer-rc2.md`, `contracts/stoin-business-guest-answer-v1-bundle/` | HOMES PRIME, authoritative here | Already byte-identical in this repository (bundle digest `50492b99…`). Homes owns guest.answer; the website consumes it |
| `src/lucy/public_api.py`, `public_runtime.py`, `public_model_api.py`, `public_model_runtime.py`, `public_model_service.py`, `public_model_admission.py`, `public_model_activation.py`, `public_inference.py`, `public_diagnostics.py` | LEGACY: live service, delete after cutover | Not moved. This is the running `lucy-public` / `lucy-public-model` service, bound to Cloud Lucy's database, tenancy, readiness, cost admission and recovery journal. It stays in cloud-hermes-lucy until the website calls Homes Prime, then it is deleted |
| `deploy/postgres/{commission_public_model_staging,commission_public_projection,inspect_public_conversation,migrate_public_conversation,release_public_knowledge,reopen_public_conversation}_v1.py`; `deploy/render/{evaluate_public_knowledge,evaluate_public_model,validate_public_knowledge,validate_public_model_activation_v2}.py`; `utopia-public-model-activation-manifest.v2.json.example`; the `lucy-public` entries in `security-baseline-v1.*.yaml.example`; `migrations/versions/{0022,0052,0057}_*` | LEGACY: live service operations | Same as above: they operate the live service and its shared database lineage. Delete after cutover |
| `tests/**/test_public_*`, `test_r1_*`, `test_evaluate_public_knowledge.py`, `test_validate_public_knowledge.py` | LEGACY | They test the legacy service; delete with it |
| `docs/public-lucy-*.md`, `docs/evidence/utopia-public-*` | ARCHIVED | Historical evidence cited by commit; it stays in the historical repository |

## Utopia Homes Management Contract provider (folded in)

The standalone `utopia-homes-management-adapter` repository (`master`, `a292321`) is folded in as a
separately deployable Homes component. Every file was copied byte-exact from that commit, with
only import and path rewrites:

| Standalone path | Here |
| --- | --- |
| `src/management_adapter/` | `src/utopia_homes_prime/management_adapter/` |
| `tests/{unit,integration,contract}/`, `tests/conftest.py` | `tests/management/` (the same 205 tests, all passing) |
| `tests/fixtures/{bundle,keys,tokens}.py` | `tests/fixtures/management/` |
| `contracts/stoin-management-v1-bundle/` | unchanged path (digest `c3bc25e4…`, verified) |
| `docs/management-contract-v1-rc3.md` | unchanged path |
| `docs/implementation-notes.md`, `README.md` | `docs/management-adapter-implementation-notes.md`, `docs/management-adapter.md` |
| `Dockerfile`, `.env.example` | `deploy/management-adapter/` (build context: repository root) |
| `deploy/render/*.yaml.example` | unchanged paths; `dockerfilePath` updated |

The Utopia-specific provider side of the Management Contract now lives with Homes Prime. Tiamat
keeps its consumer side and a pinned compatibility reference. Any service already deployed from
the standalone repository is unaffected until it is redeployed from here. The standalone
repository was archived on 2026-09-24; its staging service stays suspended.

## Removed from this repository during the split

- `tools/generate_canonical_identity_fixtures.py` imported Tiamat source through `sys.path`. The
  vectors it produced (`tests/fixtures/sme_canonical_identity_vectors.json`) stay as pinned
  compatibility data for the optional Tiamat backend. Regenerating them is a Tiamat-side step.
  The script remains in this repository's history at `5090749` (formerly `6267bbf`).

## Commit hash mapping

The old checkout (`C:\Users\Forti\Projects\utopia-homes-guest-answer-provider`, and its `uhgap-stage2` worktree) still holds the original
commits. Anything cited by an old hash (for example the Workspaces acceptance ledger) resolves
there, and to this repository through this table.

| Old | Here | Subject |
| --- | --- | --- |
| `251ec3b` | `1fef7ba` | Implement Homes Prime provider for guest.answer@1.0 (RC2, strangler step 2) |
| `4e3e8b5` | `1e354b8` | Correct conformance framing per Control-side review: preconformant only |
| `1c696ac` | `552c59a` | Move preconformant disclosure out of limitations[] into a diagnostic header |
| `172045a` | `6a62519` | Add Homes Prime Stage 2 candidate against Shared Model Execution RC1 |
| `d08d526` | `95b41f2` | Bound admitted evidence packet at 64 IDs instead of dropping enums |
| `e52790c` | `e7ba34d` | Add caller-side RFC 8785 canonical-identity parity with Tiamat (RC1 §11) |
| `e55d834` | `023ca29` | Replay the vendored RC1 bundle's vectors against the caller's own parsing |
| `f54edb1` | `251049d` | Prove named rejection rules, not just categorical mode fail-closed |
| `909f677` | `18c0e29` | Pin canonical-identity fixtures to the Tiamat revision that produced them |
| `e9560c2` | `9faa9a8` | Prove the RC1 client over a real loopback socket, not just an in-process fake |
| `47aa71d` | `e961ee3` | Add the Homes Dragon meeting operations: identity, respond, and draft |
| `6267bbf` | `5090749` | Take the Ray-approved Demo v1 meeting materials |
| `1787d07` | `59854e9` | Rename the package to utopia_homes_prime and group it by responsibility |
| `8446982` | `adb6289` | Make inference a Homes-selected backend; Homes answers with no Tiamat configured |
| `4c9653b` | `aecfab5` | Move Homes knowledge data in; record the split provenance and ownership |
| `1f31825` | `0ab2d18` | Fold the Utopia Homes Management Contract provider into Homes Prime |

## Open items (not part of the source split)

The production cutover plan and its gates are in `docs/production-cutover.md`.

1. **Website cutover.** `utopia-homes-web` still calls the legacy `/api/lucy` → Public Lucy path
   (`LUCY_PUBLIC_API_URL`). Once the website calls this service's Business API, the legacy service
   files above can be deleted and cloud-hermes-lucy archived.
2. **Real direct-route proof.** The no-Tiamat proof uses a fake provider. Answering a real guest
   question needs a Homes-owned OpenRouter key and price ceilings: a paid resource and a credential,
   so Ray decides.
3. **`restricted_topic` gate: resolved (C3), not ported.** Legacy `public_retrieval.restricted_topic`
   ran only in the retrieval-only mode. The live model path never used it. Live pricing,
   availability, and reservation-access requests are acceptance cases instead, and Homes Prime
   declines them (see `docs/production-cutover.md`).
4. **Contract text.** The agreed corrections (Tiamat execution optional in the Business Contract,
   caller independence in the SME contract, the Homes independence invariant in the Management
   Contract) follow the split as new revisions. RC2 is pinned by digest and is not edited in place.
