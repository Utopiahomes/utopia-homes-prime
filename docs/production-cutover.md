# Homes production cutover

**Goal:** move live guest answering from the legacy Public Lucy path to Utopia Homes Prime,
without adding architecture.

The legacy path runs utopiahomes.com `/api/lucy` → `lucy-public` and `lucy-public-model` on Render
(built from cloud-hermes-lucy) → the Cloud Lucy Postgres → OpenRouter. The target is `/api/lucy` →
Homes Prime `POST /business/v1/guest/answer` → Homes' own OpenRouter route.

Owner: Claude (Homes). Lyra reviews the independence boundary once. Ray approves every production
switch, every merge to the live branch, and every new paid resource.

## Settled decisions

- **Conversation memory stays in the browser.** The website widget already keeps recent turns in
  browser memory (30-minute expiry, cleared on refresh) and sends a bounded history with each
  request, exactly as RC2 describes. Homes Prime stays stateless for guest content: no transcript
  store, and no Supabase chat history.
- **Separation stops here.** Homes Web, Homes Prime, Tiamat, Workspaces, and Personal Lucy are the
  separate systems. Inside Homes Prime, knowledge, retrieval, prompts, validation, inference
  adapters, the management adapter, and the meeting operations stay together in one source tree.
  Separate deployments are fine; separate repositories, microservices, or a shared "common" repo
  are not.
- **Knowledge stays as files.** It uses a validated, approved, digest-pinned release in this
  repository. There is no CMS or knowledge service.
- **Spending is capped at the provider.** The limit is set on the OpenRouter key, plus Homes'
  per-call price and cost ceilings. No Tiamat-style accounting is copied in.

## Gates

| Gate | Work | Status |
| --- | --- | --- |
| C1 | Rescue the website-side guest.answer client onto a clean branch from `main` | Done: `utopia-homes-web` `claude/homes-prime-cutover` (`28d782e`); the 5 preview commits were re-authored to the noreply address; typecheck, lint, and 116 tests pass |
| C2 | Knowledge release workflow: candidate → validation → Ray approves → release ID + digest → the deployment pins that digest | Done: `tools/knowledge_release.py` (`check` admits a candidate exactly as Homes does at startup and shows added, removed, and changed entries; `record` adds a Ray-approved file to `knowledge/releases.json`; `pin` prints the deployment's three knowledge settings). R1 is recorded. A test keeps the register matched to the files |
| C3 | Behavior parity with legacy Public Lucy: restricted topics, private data, business policy (move the rules, not the old architecture) | Done: Homes Prime has every check the live legacy model path applies, plus stricter ones. The legacy deterministic `restricted_topic` gate (live pricing, availability, reservation access) guarded only the older retrieval-only mode, not the live model path, so it was not ported; those requests are acceptance cases instead, and they pass |
| C4 | Acceptance set: 40–60 cases with multi-turn memory (facts, comparisons, page context, follow-ups, corrections, ambiguity, unknowns, Design, local recommendations, private data, restricted topics, sources and links, provider failures, duplicate requests, knowledge changes) | Done: 54 conversations, 64 turns (see below). Provider failures, duplicate requests, and knowledge changes are protocol behavior and stay covered by the automated tests |
| C5 | Deploy Homes Prime on Render with its own service, secrets, and provider key. No cloud-hermes-lucy image or database, no Tiamat URL or grant, no legacy endpoint | Done 2026-09-24 (Ray approved): `utopia-homes-prime-preview` (`srv-daqn5au7bikc7382f8hg`, virginia, starter, auto-deploy off) at `https://utopia-homes-prime-preview.onrender.com`, built from `main` at `9fbac34`, `environment=preview`, homes-prime engine on the direct OpenRouter route, R1 pinned, website adapter key `utopia-homes-web-preview-2026-09-24` (public half only). Live checks: health 200; a bad token gets 401; real answers of 3.2–3.5 s for a capacity-and-pool question, page-context parking, history-based dogs, and a refused door-code request |
| C6 | Redeploy the management adapter from Homes Prime; archive the standalone repository | Done 2026-09-24 (Ray: no redeploy needed while unused, as long as it costs nothing). The staging service `utopia-homes-management-adapter-staging` stays suspended on the free plan. `Utopiahomes/utopia-homes-management-adapter` is archived (read-only); its content is identical to what was folded in here. Redeploy from `deploy/management-adapter/Dockerfile` only if Control needs the provider again |
| C7 | Point `/api/lucy` at Homes Prime in a preview deployment, with a manual rollback switch to legacy (operator-controlled, never an automatic fallback) | Done 2026-09-24: branch-scoped Vercel Preview settings for `claude/homes-prime-cutover` (the backend switch, Homes URL, website key, and site hostname `utopia-homes-web-git-claude-homes-prime-cutover-stoincock.vercel.app`); production settings are untouched. The full chain passed: preview `/api/lucy` → Homes Prime on Render → OpenRouter, with no Tiamat (capacity and pool; page-context parking; a history-based dogs follow-up; a refused door-code request; 2.9–3.6 s). The first live call exposed a website bug (public-site links were rejected on preview hostnames), fixed in `0633ab3`. Note: the Vercel CLI created a deployment-protection bypass secret for the project so it could call protected previews |
| C8 | Acceptance against the preview; production Homes; Vercel Production settings; merge to `main` | Done 2026-09-24 (Ray approved; Ray pushed `main` to `0633ab3`). www.utopiahomes.com `/api/lucy` now answers through production Homes Prime (`utopia-homes-prime`, production key `utopia-homes-web-production-2026-09-24`) on Homes' own OpenRouter route, with no Tiamat and no Cloud Lucy. Live checks: capacity and pool, a history-based dogs follow-up, page-context parking, and a declined door-code request, all 200 in 2.5–3.1 s. Before the switch, the legacy path was already returning 503. Rollback: `vercel rollback utopia-homes-csjas8o5t-stoincock.vercel.app` (which restores that legacy state) or unset `LUCY_ANSWER_BACKEND` and redeploy |
| C9 | Retire legacy so it costs nothing (Ray: suspend, do not delete code) | Done 2026-09-24. Suspended (reversible) the legacy cloud-hermes-lucy cluster: `lucy-public`, `lucy-public-model`, `lucy-authority-writer`, `lucy-cost-writer`, `lucy-recovery-coordinator`, `lucy-routine`, `lucy-workspaces-internal-web-probe`, the crons `lucy-finality-utility` and `lucy-v13-migration-0042`, and the database `lucy-postgres` (data kept). A dependency check of every running service's settings first showed that nothing outside this cluster (Personal Lucy `raymond-*`, Workspaces `utopia-studio-*`, Tiamat, Homes Prime) referenced it. The live site kept answering afterwards. Not removed: code, the website's `LUCY_PUBLIC_*` Production settings, and Homes Prime's `legacy_bridge.py` / `legacy_upstream.py` |

## Acceptance results (C4, 2026-09-24)

The set is `knowledge/evaluation/homes-guest-answer-acceptance.v1.json`, generated by
`tools/build_acceptance_set.py` and checked offline by `tests/unit/test_acceptance_set.py`. It holds
55 conversations and 65 turns, including 12 multi-turn or history-carrying conversations. Run it with
`tools/evaluate_guest_answer.py`, which uses real provider calls. Each conversation is played the
way the website does it: one session, with the bounded history of earlier questions and Homes'
actual answers sent with every request.

These runs used Homes' own OpenRouter route with `google/gemini-3.1-flash-lite` through
`google-vertex` and the R1 knowledge. There was no Tiamat.

| Run | Passed | p50 / p95 latency | Cost |
| --- | --- | --- | --- |
| First (as extracted) | 46 / 59 | 2.9 s / 3.7 s | $0.14 |
| After the Homes fixes below | 63 / 64, 62 / 64, 63 / 64 | 2.6–3.0 s / 3.4–3.7 s | $0.16 per run |

The first run exposed Homes-side problems, not model problems. Fixes:

- **Too-literal deterministic checks.** A plain limitation ("I do not have access… so I cannot
  confirm whether The Shamrock…") may now name a home when it carries no number. A claim that cites
  only generally applicable entries (booking handoff, collection overview, contact) may name a home.
  Uncited property facts, numbers smuggled into limitations, and another home's facts are still
  rejected, and tests pin both sides.
- **One bounded repair.** RC2 §11 allows one. When a draft breaks a deterministic Homes rule, Homes
  asks once more with a fixed, content-free note naming the rule, if enough budget remains.
  Support-review rejections are never repaired.
- **Review policy.** Text restating cited evidence, limitation statements, and refusals are
  supported. A question's premise does not make a supported fact unsupported.
- **Answer policy.** A sentence naming a home must cite that home's evidence unless it only states
  a limitation, and no count or number may appear that the evidence does not state.

The remaining failures are single, non-repeating fail-closed 503s: none of them reached a guest as
an answer. Passing turns met their written expectations; they were not otherwise fact-checked
line by line.


- The support reviewer (Flash-Lite) occasionally rejects text copied from the evidence.
- The reviewer correctly caught "two options for four cars" (Central Ave has parking for 3).

Whether a stronger model for the short review step is worth its cost is a question for the model
comparison.

## Future gate (deferred by Ray, 2026-09-24)

**F1: durable coordination store.** RC2 §11 requires a duplicate request after a restart to return
`idempotency_recovery_unavailable` instead of starting a second paid pipeline. Homes Prime keeps
idempotency and `jti` records in process memory, so a restart forgets them. The fix is a tiny
Homes-owned store: scoped key digest, canonical request digest, status, expiry, and `jti` digests.
It never holds content. The initial single-coordinator profile fits SQLite on a persistent disk;
Postgres is the alternative. Until F1 lands, a restart during the ten-minute replay window can
re-run a duplicate. Record this as a known limitation of any deployment before F1.
