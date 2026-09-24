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
| C5 | Deploy Homes Prime on Render with its own service, secrets, and provider key. No cloud-hermes-lucy image or database, no Tiamat URL or grant, no legacy endpoint | Ready; needs Ray (paid resource). Blueprint: `deploy/render/utopia-homes-prime.preview.yaml.example`. The image now builds: the lock is regenerated on Linux with `uvloop`, `jsonschema` and `referencing` are runtime dependencies, and knowledge and materials ship in the image. It was verified locally: pinned R1, no Tiamat setting, and a real guest answer over HTTP |
| C6 | Redeploy the management adapter from Homes Prime; archive the standalone repository | Needs Ray |
| C7 | Point `/api/lucy` at Homes Prime in a preview deployment, with a manual rollback switch to legacy (operator-controlled, never an automatic fallback) | Code ready: `utopia-homes-web` `claude/homes-prime-cutover` (`32edfd9`), with `LUCY_ANSWER_BACKEND=homes-prime` and legacy as the default; 128 website tests pass. Exercising it waits on C5, a reachable Homes Prime. In a Vercel preview, `LUCY_PUBLIC_SITE_HOSTNAME` must be the preview's own hostname, because the route only answers for its configured site |
| C8 | Run the acceptance set against the preview; verify Vercel's production branch (expected `main`, not yet confirmed); Ray approves the production switch. The switch includes lifting Homes Prime's Stage 2 guard (the homes-prime engine is refused outside `preview`) and the website's `homes-prime-candidate` marker check with it, so a production Homes serves its own engine deliberately | Needs Ray |
| C9 | After a stable period, retire legacy: the `lucy-public` services, the Cloud Lucy database in the guest path, `LUCY_PUBLIC_*` configuration, and Homes Prime's `legacy_bridge.py` / `legacy_upstream.py` | Needs Ray |

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
