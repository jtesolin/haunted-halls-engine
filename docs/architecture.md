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

- Preview runtime and control-plane resources live in the dedicated GCP project
  `hh-preview-458395246135`. Production and staging remain in
  `haunted-halls-development` as hard trust boundaries: preview automation has
  no production/staging Cloud Run mutation authority, and preview runtime
  identities cannot read production/staging secrets.
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
