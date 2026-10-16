# Homes feedback to Tiamat

This first Phase 1B connection gives the existing **Utopia Lucy business operator** an explicit
`utopia_record_feedback` tool for Homes behavior and Stoin product feedback. It is independent of
meeting participation (1A), property records, maintenance tasks, and the public guest-answer
pipeline. It does not add a feedback button to the guest website or collect guest conversations.

The profile enables the separate `tiamat_feedback` plugin in the existing `utopia_business` toolset.
Its tool requires both dedicated settings below; without them, feedback is not connected. The
existing Dockerfile and startup script already copy profile plugins. No Hermes fork, extra Python
dependency, shared source package, model route change, or business-core token is needed.

## Connection and activation

Do not merge to the live branch or change the production runtime without Ray's approval, as
required by [the production cutover agreement](production-cutover.md). This source change does not
provide that approval or provision a Tiamat service.

1. Run Tiamat's **intake-only** service (`python -m tiamat.feedback_intake`) with durable storage,
   HTTPS at its ingress, and an explicit `TIAMAT_INTAKE_HOSTS` hostname. Do not publish or tunnel
   the Workbench operator app on port 8787. The receiver only exposes `/healthz` and authenticated
   `POST /api/field/intake/homes`; it has no private read or operator mutation routes.
2. Generate a new dedicated random token and place it in the normal secret settings on both
   sides, never in source, chat, feedback, or a URL. Set `TIAMAT_HOMES_FEEDBACK_TOKEN` in the receiver
   and `UTOPIA_TIAMAT_FEEDBACK_TOKEN` in Homes to the same value (at least 32 printable ASCII
   characters). Do not reuse `UTOPIA_BUSINESS_LUCY_TOKEN`, guest credentials, or provider keys.
3. Set `UTOPIA_TIAMAT_FEEDBACK_ORIGIN` in the Homes operator to the receiver's HTTPS origin, with
   no path, query, credentials, or fragment. Plain HTTP is accepted only on loopback for a local
   check. `localhost` inside the deployed operator is not Ray's computer.
4. After approval, deploy the profile and restart the operator with these dedicated settings.
   Leave the public guest-answer service, model settings, database, and 1A unchanged.
5. Ask Lucy to record a clearly synthetic private Homes report and a separately approved synthetic
   Stoin summary. Check the actual tool receipts, confirm both in Tiamat's private operator view,
   and verify the ordinary Stoin digest still excludes them. Then record one explicitly requested
   real report. A local handler test does not replace this deployed-runtime check.

Use a persistent `TIAMAT_STATE_DIR` on the receiver. The current storage is a single SQLite database,
not a multi-host service. The operator must use that same database through a suitable private
local arrangement; two machines with separate copies do not synchronize automatically. A public
deployment and operator access arrangement remain to be selected. No hosting or paid resource was
created for this change.

## Recording and sharing

Required tool arguments are `target` (`homes_dragon` or `stoin`), private `topic`, and explicit
feedback `text`. The tool sends only these bounded fields and optional feedback metadata. Extra
fields such as chat history are rejected. It never automatically harvests a conversation.

- Sharing defaults to `private`, evidence to `operator_observation`. Use `customer_report` only
  for explicitly supplied customer feedback and `dragon_inference` for the agent's inference.
- Summary sharing requires the separately approved `approved_summary`. Quotation sharing requires
  permission to share the submitted wording. Permission also covers category, evidence kind, and
  the fixed public grouping label. Private free-text topic, expected behavior, and observed version
  do not enter product digests. Do not submit names, secrets, transcripts, or unrelated context.
- Use `synthetic: true` for tests or practice. An optional shared `experience_id` UUID can connect
  separate Homes and Stoin reports without merging their audiences or permissions.
- `recorded` means Tiamat saved the report; `product_owner_delivery: not_sent` explicitly means no
  owner delivery. It does not mean an improvement was deployed or a problem fixed.

A new call generates a report UUID. Retain the returned UUID. After an `unknown` transport or server
outcome, retry only the identical fields with that `report_id`; there is no automatic retry or
durable sender outbox. Conflicting reuse is rejected. Do not invent another ID to bypass a refusal
or uncertain result. Receiver idempotency does not restore sharing withdrawn later in Tiamat.

## Verification and remaining work

On October 16, 2026, the four new plugin checks and six existing business-plugin checks passed.
Ruff passed for the new plugin and tests. A disposable local check invoked the registered Homes
tool over real HTTP against Tiamat's separate intake process and private operator process. It
verified a Homes draft brief, permitted Stoin digest, private-field filtering, synthetic separation,
duplicate/conflicting ID behavior, wrong-token rejection, private-route absence, and persistence
after both receiver and operator restart. No model, Telegram, production service, or real customer
was involved. Tiamat's 27 affected checks and strict type checks also passed separately.

The deployed Hermes loader and a real agent-selected tool call remain unverified. There is no live
Tiamat endpoint or Stoin owner recipient configured by this work. Automatic owner delivery,
Vibehard, public guest feedback, and autonomous code changes are outside this increment. Stoin
digests remain reviewable exports; Homes briefs remain drafts until an authorized maintainer
implements, checks, and releases a candidate under the existing owner controls.
