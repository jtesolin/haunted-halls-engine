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
game-state changes increasingly remain deterministic:

```text
player text
  -> Action Parser
  -> structured action
  -> deterministic rule/tool execution
  -> authoritative result/state
  -> Narrator
```

The Narrator describes authoritative results and the authoritative scene
context. It must not invent state changes. Player-action authority is separate
from privileged world/Director authority; a player-facing action cannot become
a world-authority operation through narration or agent choice.

The current world model includes a room graph, items, NPCs, deterministic
interactions, authoritative narrator scene projection, campaign state, clock
and facts, persistence, and memory.

## Persistence and runtime integrity

Database migrations are explicit deployment steps. Application startup must not
silently create or repair schema or authoritative state. Persisted corruption
or invalid authoritative state must fail explicitly rather than silently
regenerating replacement state.

## Planned authority boundary

Phase 7A is planned, not started. It is expected to establish typed,
privileged world-authority actions before any Director autonomy is introduced.
