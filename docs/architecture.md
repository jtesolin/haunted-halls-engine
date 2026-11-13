# Haunted Halls Architecture Invariants

This document records stable cross-repository architecture and authority
invariants. Current milestone progress and chronological status belong in
[project-status.md](project-status.md).

## Repository and trust boundaries

- [`haunted-halls`](https://github.com/jtesolin/haunted-halls) is the public
  Next.js browser application, BFF, and authentication boundary.
- `haunted-halls-engine` is the private FastAPI game engine and the authority
  for game-domain behavior, AI orchestration, persistence, and campaign
  ownership.
- A browser must not call the private engine directly or receive its
  credentials. The Next.js server authenticates the internal engine request
  and supplies trusted user context.
- The engine enforces campaign ownership and remains the authority for
  game-domain state. API-contract changes between the repositories require
  coordinated review.

## Preview environment invariants

- Preview application/runtime resources and the broad preview deployment
  control plane live in the dedicated GCP project `hh-preview-458395246135`.
  The fixed trusted preview DB provisioner is the narrow exception: it lives
  in `haunted-halls-development` alongside the shared Cloud SQL trust boundary
  and is the controlled bridge only for strictly generated preview database
  create/drop operations. This does not give the general preview deployer
  production/staging Cloud Run mutation authority. Production and staging
  remain hard trust boundaries, and preview runtime identities cannot read
  production/staging secrets.
- Previews reuse the existing Cloud SQL instance with isolated per-PR databases
  and one hardened shared preview app login. Only the fixed trusted DB
  provisioner may create/drop strictly generated preview database names.
  Neither the preview app login nor the provisioner login may `CONNECT` to
  production/staging databases.
- OpenAI access uses a separate preview-only credential. Preview frontend
  access uses direct Cloud Run IAP only, with no public fallback. Tester
  `roles/iap.httpsResourceAccessor` grants are scoped to each preview frontend
  resource, never project-wide.
- Workload Identity Federation (WIF) impersonation is bound to exact reviewed
  default-branch workflow refs. PR-controlled code never receives preview
  deployment credentials; privileged deployment runs from reviewed
  default-branch workflow definitions on a fresh job/runner.
- Durable foundation Terraform state is separate from per-PR preview state;
  per-PR state is isolated by repository/PR key.
- The preview lifecycle is an empty database, then an explicit Alembic migration,
  then a private engine and IAP frontend. PR updates preserve the database;
  PR close destroys preview runtime, secrets, per-PR state, and database.
- Frontend PR previews pin the exact stable, currently serving engine staging
  artifact. Engine PR previews pin the exact stable, currently serving frontend
  staging artifact. Cross-repository PR pairing is out of scope for v1.

## Game and agent authority

Player language is valuable input for AI interpretation, while authoritative
game-state changes remain deterministic. The Director may then propose at most
one privileged world action from the resulting authoritative player state,
executed only through the deterministic world-authority boundary, before the
Narrator grounds in the final authoritative state:

```text
player text
  -> Action Parser
  -> structured player action
  -> deterministic rule/tool execution (player ToolExecutor)
  -> authoritative player result/state
  -> deterministic story/rule progression
  -> deterministic authored progression rewards
  -> authoritative post-story/post-reward state
  -> Director bounded context
  -> zero or one typed WorldAction
  -> WorldAuthorityExecutor
  -> final authoritative state
  -> Narrator
```

The Narrator describes authoritative results, the authoritative scene context,
and narrow authoritative current-turn narrative effects when supplied. It must
not invent state changes. Player-action authority is separate from privileged
world/Director authority; a player-facing action cannot become a world-authority
operation through narration or agent choice. The Director receives bounded
read-only story, character, and revealable-clue projections after deterministic
story/rule progression and authored progression rewards; those projections do
not grant quest/objective mutation, progression-grant, ability-unlock,
ability-check, or arbitrary narrative-writing authority. The Director never
mutates campaign state directly; only `WorldAuthorityExecutor` may apply a
privileged world-state transition, and only after the player's own
authoritative result, deterministic story/rule progression, and authored
progression rewards are already final.

The current world model includes a room graph, items, NPCs, deterministic
interactions, authoritative narrator scene projection, campaign state, clock
and facts, persistence, and memory.

## Persistence and runtime integrity

Database migrations are explicit deployment steps. Application startup must not
silently create or repair schema or authoritative state. Persisted corruption
or invalid authoritative state must fail explicitly rather than silently
regenerating replacement state.

### Character detail API contract

`GET /api/character/{character_id}` (authenticated, owner-scoped; unknown or
foreign characters return 404) returns `character_id`, `name`, `inventory`, and
`abilities`. Inventory and abilities are read-only projections of the owning
campaign's persisted authoritative state; the endpoint never mutates state or
calls a model.

- `inventory`: list of `{id, name, description, quantity}` for items located in
  the player's inventory. Empty list when none.
- `abilities`: list of `{ability_id, display_name, description, available,
  availability_reason}` for owned built-in and generated abilities (same
  projection as `project_owned_abilities`). Empty list when none.
- A campaign with no persisted state yields empty lists; malformed persisted
  state (invalid JSON, or valid JSON with structurally invalid `items`,
  item fields, `player`, or progression such as out-of-range track points or a
  malformed, blank, untrimmed, or duplicate unlocked-ability list) returns 500 with a generic detail and
  no payload content. This read path validates strictly and never repairs or
  defaults state, unlike the normalizing loader used for gameplay. Missing
  optional namespaces (`items`, `player`, `progression`) read as empty, and
  unknown progression track keys (their values are not validated) and
  well-formed but unknown unlocked ability IDs are ignored and omitted.

`GET /api/characters` remains the lightweight `{character_id, name}` list.

### Campaign-to-character association API contract

The campaign-to-character association is the persisted `characters.campaign_id`
relationship. It is exposed as a nullable `character_id` on
`GET /api/campaign/{campaign_id}` (`CampaignDetail`) and `GET /api/campaigns`
(`CampaignSummary`), both authenticated and owner-scoped (foreign or unknown
campaigns return 404 on detail and are absent from the list). The frontend uses
it to call `GET /api/character/{character_id}` for the active campaign.

- The value is read only from persisted `characters` rows; it is never inferred,
  generated, or derived from model output or chat content.
- Every newly created campaign (`POST /api/campaign` and auto-created chat
  campaigns) persists exactly one starter character in the same transaction as
  the campaign, so a failure leaves neither row. The character ID is derived
  deterministically from the campaign ID (`character_<campaign_id>`), so retries
  converge on one row. The name is generated with the Faker library (`app/game/character_names.py`) when the row is created and never changes on retry;
  inventory and abilities are not copied but projected from the campaign's
  persisted game state by the character-detail API. `POST /api/campaign`
  returns this `character_id`.
- `character_id` is `null` only when the campaign has no character row.
  Migration `0005_backfill_characters` idempotently inserts a starter character
  for every pre-existing campaign that has none (`make db-upgrade`); campaigns
  that already have a character are untouched. If a campaign's deterministic
  character ID is already used by another row, the migration aborts before
  changing data and names the affected campaigns so the conflict is resolved
  manually rather than silently skipped. Existing campaign fields and
  behavior are otherwise unchanged.
- The schema does not enforce one character per campaign. If multiple character
  rows exist for a campaign, the earliest by `created_at`, tie-broken by
  ascending row id, is returned deterministically in both detail and list.

## Transactions, durable usage and concurrency

Issue #117. A chat turn or campaign creation never holds a database
transaction or pooled connection across a model call. It runs in three phases:

1. **Begin (one short transaction):** idempotency claim or replay, request
   validation, per-campaign turn lease, read snapshot (state, recent turns,
   memory, summary, user-turn count), then the global budget row lock (taken
   last on PostgreSQL so snapshot work never queues behind it; up front on
   SQLite), the request-cap check and one `usage_requests` row (the logical
   request).
2. **Model phases (no transaction open):** action parser, deterministic tool
   execution (offloaded with `asyncio.to_thread`; it may block on MCP), Director,
   Narrator and best-effort summary/reflection run over in-memory state, under a
   server-side deadline (`CHAT_MODEL_DEADLINE_SECONDS`=140,
   `CAMPAIGN_CREATE_MODEL_DEADLINE_SECONDS`=85) shorter than the BFF timeouts
   (150s / 90s). Memory layers are skipped when the deadline is spent.
3. **Commit (one short transaction):** fenced lease release (first statement),
   campaign/character creation (re-checking the campaign cap under a per-user
   lock), both turns, all events, state update, memory/summary writes and
   idempotency completion commit atomically or not at all. A failed turn
   persists no turns, events or state; the claim and lease are released.

**Durable usage.** Each model call inserts a `model_requests` row with
`status='reserved'` (counted at its estimate; actuals NULL) under the global
budget lock after checking the daily player and project token caps, then
finalizes it with actual usage or the failure in its own transaction. Usage
therefore survives any later failure. A failure with no provider usage keeps
NULL actuals and keeps counting at the estimate; counts are never fabricated. A
crash, cancellation or deadline leaves the row reserved; it keeps counting at its
estimate and there is no reaper (conservative, never under-counted).

**Units.** *Request* = one executed chat turn or campaign creation attempt,
recorded in `usage_requests` (not cascade-deleted). Player and project request
caps count it. A retry that re-executes after a failed or expired attempt is a
second paid run and counts again; replays of a completed idempotent request,
in-progress 503s and validation rejections do not. *Model call* = one `model_requests` row. *Tokens* = the sum of those rows
(estimate fallback). *Campaigns* and *turns* are gameplay limits.

**Hard vs soft.** Token and request caps are hard (checked and recorded under
the global lock). The campaign cap is exact in persistence (re-checked at commit
under a per-user lock) but concurrent over-limit creations may waste model spend
bounded by the token caps. The turn limit is exact: one turn per campaign is in
flight (`campaigns.turn_lease_*`, fenced by token, TTL `REQUEST_LEASE_SECONDS`).
A concurrent turn gets 503. Residual uncertainty: token estimates can differ from
provider billing (reconciled at finalization), and request caps under-count while
an old image still serves traffic during a rolling deploy. Idempotency claims
carry `owner_token`/`lease_expires_at`; an expired claim from a crashed worker
can be taken over and the stale worker's completion is fenced out.

Behavior notes: semantic-memory writes now commit inside the final transaction,
so a failure there fails the turn (after paid usage, which is durable) instead of
being swallowed. Accepted risks: a handler cancelled while its begin transaction is still running
leaves the claim and campaign lease held until `REQUEST_LEASE_SECONDS` (retries get
503 meanwhile). If an idempotency-claim takeover begins exactly as
a stalled previous owner commits, the two transactions lock the claim and
campaign rows in opposite order and PostgreSQL may abort one; the commit side is
retried and the begin side returns a retryable error, with no data loss.
Migration 0006's downgrade drops the request ledger and its backfill counts only
existing user turns (earlier campaign creations and failed requests are absent
from that day's totals).

The frontend contract is unchanged: status codes and bodies are as before. A
concurrent turn on the same campaign returns 503 (retryable) and the server
deadline can return 502 slightly earlier than before.

## World-authority boundary

Player `ParsedAction` values and the player `ToolExecutor` remain limited to
player-authorized deterministic actions. Typed privileged `WorldAction` values
execute separately through the deterministic `WorldAuthorityExecutor`.
World authority may execute canonical deterministic narrative actions, but
those actions operate only on authored IDs and static content rather than
arbitrary model-authored mutations. Authored narrative actions such as
`reveal_clue` remain deterministically validated and executed by
`WorldAuthorityExecutor`, using canonical definitions and current eligibility
rather than model-authored text. Quest and objective completion remains owned
exclusively by deterministic story progression.

The application runtime requires non-whitespace OpenAI provider configuration.
Model-backed agents interpret natural-language player intent and generate
bounded proposals; deterministic validation grounds interpreted identifiers
and targets in player text and authoritative context. Deterministic systems
retain exclusive authority for game-state validation and mutation. A missing
provider configuration is a startup error, and provider failures remain
explicit failures rather than selecting heuristic or stub gameplay.

The model-backed Director is integrated into every normal authoritative chat
turn, after the player's own result/state plus deterministic story/rule
progression and authored progression rewards, and before narrator scene
construction. It proposes at most one currently exposed typed `WorldAction`,
or no action, from a bounded, non-mutating projection of that authoritative
post-story/post-reward state. Only `WorldAuthorityExecutor` may execute a
proposed action; the Director itself never mutates campaign state. Tests remain
provider-free through explicit agent/model doubles and mocked HTTP, including
the real OpenAI SDK contract tests.
Executor support for a `WorldAction` does not automatically expose it to the
model-backed Director: the Director has an independently bounded proposal and
provider-facing action vocabulary.
When a Director-proposed authored clue reveal succeeds and changes state, the
Narrator receives only a narrow current-turn effect containing the canonical
clue ID and authored text. The Narrator does not receive raw narrative state,
the full Director proposal, or arbitrary world-action results as narrative
grounding.

Authored character-progression rewards are deterministic static content triggered
only by authoritative story outcomes. Persistent campaign-state reward claims
prevent duplicate grants; models may observe and narrate an earned reward but
never select or grant progression or abilities.

At campaign creation, AI may propose structured campaign-specific starter
abilities, but the engine validates them against its bounded mechanical
vocabulary before persisting their definitions and ownership. Those persisted,
validated definitions are authoritative; their vocabulary and execution
semantics remain engine-owned. The Action Parser may identify an explicit
player ability request in natural language only when its ability reference and
any required target are grounded in player text and authoritative parser
context; it cannot choose mechanics, difficulty, or outcome.
Player ability checks execute through the player ToolExecutor, never through
the Director or WorldAuthorityExecutor. The Narrator receives only the
authoritative ability definition/result projection and cannot invent an
unexecuted generated-ability effect.

Generated traversal uses a finite engine-owned method vocabulary (levitation,
spider climb, supernatural jump, water walking) and a shared deterministic
authored-route validator. Each player invocation is one atomic crossing between
valid authored rooms ending at a stable landing, not a persistent effect or
physics simulation. Jump reach is fixed at three metres of authored crossing
length; names and flavor cannot alter constraints. Special routes are separate
from ordinary exits and do not grant walking, NPC/Director movement, or sensing
adjacency. Parser/Narrator projections expose only bounded local routes.
Successful traversal emits exactly one ordinary room-entry story signal before
Director processing; failures leave location unchanged and emit no such signal.
Normal transactional persistence and completed-request replay remain the
authority for movement, story changes, and final-scene narration.

New generated starter pairs are exactly two mechanically distinct choices from
the supported pool, without a sensory/utility pairing requirement. Their
deterministic signature ignores cosmetic fields and progression-track labels.
Stricter new-generation validation is separate from compatible persisted-state
loading: existing definitions are never regenerated, backfilled, or assigned
guessed traversal semantics.
