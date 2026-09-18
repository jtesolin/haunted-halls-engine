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
  -> authoritative post-player/post-story state
  -> Director bounded context
  -> zero or one typed WorldAction
  -> WorldAuthorityExecutor
  -> final authoritative state
  -> Narrator
```

The Narrator describes authoritative results and the authoritative scene
context. It must not invent state changes. Player-action authority is separate
from privileged world/Director authority; a player-facing action cannot become
a world-authority operation through narration or agent choice. The Director
receives bounded read-only story and character projections after deterministic
story/rule progression; those projections do not grant quest/objective
mutation, progression-grant, ability-unlock, or ability-check authority. The
Director never mutates campaign state directly; only `WorldAuthorityExecutor`
may apply a privileged world-state transition, and only after the player's own
authoritative result and deterministic story/rule progression are already
final.

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
arbitrary model-authored mutations. Quest and objective completion remains
owned exclusively by deterministic story progression.

The model-backed Director is integrated into the normal authoritative chat
turn, after the player's own result/state plus deterministic story/rule
progression and before narrator scene construction. It proposes at most one
typed `WorldAction`, or no action, from a bounded, non-mutating projection of
that authoritative post-player/post-story state.
Only `WorldAuthorityExecutor` may execute a proposed action; the Director
itself never mutates campaign state. Director invocation, and any resulting
privileged world-state transition, requires the same enabled provider model
path as other model-backed agents; a disabled provider path skips the
Director and privileged world execution entirely and preserves prior
deterministic/stub chat behavior.
Executor support for a `WorldAction` does not automatically expose it to the
model-backed Director: the Director has an independently bounded proposal and
provider-facing action vocabulary.
