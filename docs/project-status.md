# Haunted Halls — Project Status

**Last synchronized (planning memory sync):** September 8, 2026
**Engine baseline:** `haunted-halls-engine` `main` at commit `70081498aaec87895969865333ea574eba4cd654`
**Web baseline:** `haunted-halls` `main` at commit `b836691839185ad9b115eda530fbe00df2411ebf`

Synchronization metadata is planning memory, not an implementation version:
ordinary code, documentation, and Copilot changes do not update these fields.
They are updated together only during an explicit synchronization pass.

## Project goal

Haunted Halls is a long-running AI-driven MUD/chat game and learning project
focused on reliable agentic AI architecture. Models interpret player intent,
synthesize memory, and narrate; deterministic game rules and authoritative
state increasingly remain outside model control.

## Current architecture

The public Next.js application/BFF authenticates requests and calls the private
FastAPI engine with trusted user context. The engine owns campaign ownership,
game state, deterministic rules, persistence, orchestration, model guardrails,
memory, and narration.

Normal turn flow is:

```text
player text -> Action Parser -> ToolExecutor -> Story/rule progression
  -> authored progression rewards -> Director -> WorldAuthorityExecutor
  -> Narrator grounded in final authoritative state
```

Stable cross-repository authority and security invariants belong in
[architecture.md](architecture.md). Review disposition policy belongs in
[review-triage-policy.md](review-triage-policy.md). The broader roadmap is
[issue #52](https://github.com/jtesolin/haunted-halls-engine/issues/52).

## Completed phases

- **Phases 1–2:** FastAPI campaign/chat foundation, persistent conversations,
  model integration, narration, and request/model guardrails.
- **Phases 3–5:** PostgreSQL-compatible persistence, explicit Alembic
  deployment migrations, Cloud Run infrastructure/deployment, and production
  operational foundations.
- **Phase 6:** Authoritative deterministic world state, player tools, scene
  projections, ownership boundaries, and narrator grounding.
- **Phase 7:** Deterministic story/quest progression for typed signals derived
  from authoritative player outcomes.
- **Phase 8A–8C:** Story definitions, bounded character progression, and
  deterministic ability availability/check domain foundations.
- **Phase 8D:** Director integration with bounded read-only context and
  separately executed typed world authority actions.
- **Phase 8E:** Provider-free AI evaluation harness foundation.
- **Phase 8F1:** Authored quest-completion rewards. The completed Library's
  Whisper quest grants +2 investigation and unlocks `keen_eye`; a persistent
  campaign-state claim prevents duplicate grants, and only a narrow
  authoritative effect is exposed to the Narrator.

## Current Phase 8 status

**8F1 is implemented. The next intended slice is 8F2 — Ability / Check
Gameplay Integration.**

8F2 will connect existing deterministic ability/check resolution to gameplay
interactions. It is not part of 8F1: abilities are not parser actions, the
Director cannot grant or invoke them, and models do not choose rewards or
progression.

## Stable invariants and explicit deferrals

- Story completion, progression rewards, ability ownership, checks, and world
  actions are deterministic authoritative operations.
- Authored reward definitions are static; reward claims are persisted in
  campaign state without a database migration. Models may observe and narrate
  an earned reward but never grant one.
- The engine enforces internal authentication, trusted user resolution,
  campaign ownership, transaction rollback, and explicit migration execution.
- Production observability work (D7), generic reward/achievement/rules DSLs,
  combat progression, levels/classes, model-selected rewards, frontend
  progression UI, and broad evaluation expansion remain deferred.

See [architecture.md](architecture.md) for durable architecture decisions and
the repository instructions for implementation boundaries.
