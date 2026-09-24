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
| C2 | Knowledge release workflow: candidate → validation → Ray approves → release ID + digest → the deployment pins that digest | Open |
| C3 | Behavior parity with legacy Public Lucy: restricted topics, private data, business policy (move the rules, not the old architecture) | Open |
| C4 | Acceptance set: 40–60 cases with multi-turn memory (facts, comparisons, page context, follow-ups, corrections, ambiguity, unknowns, Design, local recommendations, private data, restricted topics, sources and links, provider failures, duplicate requests, knowledge changes) | Open |
| C5 | Deploy Homes Prime on Render with its own service, secrets, and provider key. No cloud-hermes-lucy image or database, no Tiamat URL or grant, no legacy endpoint | Needs Ray (paid resource) |
| C6 | Redeploy the management adapter from Homes Prime; archive the standalone repository | Needs Ray |
| C7 | Point `/api/lucy` at Homes Prime in a preview deployment, with a manual rollback switch to legacy (operator-controlled, never an automatic fallback) | Open |
| C8 | Run the acceptance set against the preview; verify Vercel's production branch (expected `main`, not yet confirmed); Ray approves the production switch | Needs Ray |
| C9 | After a stable period, retire legacy: the `lucy-public` services, the Cloud Lucy database in the guest path, `LUCY_PUBLIC_*` configuration, and Homes Prime's `legacy_bridge.py` / `legacy_upstream.py` | Needs Ray |

## Future gate (deferred by Ray, 2026-09-24)

**F1: durable coordination store.** RC2 §11 requires a duplicate request after a restart to return
`idempotency_recovery_unavailable` instead of starting a second paid pipeline. Homes Prime keeps
idempotency and `jti` records in process memory, so a restart forgets them. The fix is a tiny
Homes-owned store: scoped key digest, canonical request digest, status, expiry, and `jti` digests.
It never holds content. The initial single-coordinator profile fits SQLite on a persistent disk;
Postgres is the alternative. Until F1 lands, a restart during the ten-minute replay window can
re-run a duplicate. Record this as a known limitation of any deployment before F1.
