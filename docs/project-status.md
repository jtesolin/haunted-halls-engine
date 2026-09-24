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

- **Phase 1:** FastAPI campaign/chat foundation, persistent campaigns and
  conversations, and the Next.js player experience.
- **Phase 2:** Model-backed narration with centralized model policy, usage
  accounting, token budgets, and request/model guardrails.
- **Phase 3:** PostgreSQL-compatible persistence, explicit Alembic deployment
  migrations, and deployed Cloud Run/Cloud SQL infrastructure with automated
  delivery foundations.
- **Phase 4:** Campaign-scoped recent-turn, summary, semantic, and reflection
  memory behind dedicated agents and services. Semantic retrieval remains a
  deliberately lightweight implementation rather than a vector database.
- **Phase 5:** A structured agentic pipeline separates natural-language action
  parsing, typed player intent, deterministic tool/rule execution, and
  grounded narration, with local/MCP tool-registry support.
- **Phase 6:** Authoritative deterministic world state, player tools, scene
  projections, ownership boundaries, and narrator grounding.
- **Phase 7:** A separate deterministic world-authority boundary and
  model-backed Director proposal pipeline, with privileged actions executed
  only through `WorldAuthorityExecutor`.
- **Phase 8A–8C:** Authored story definitions and deterministic quest
  progression, bounded character progression, and deterministic ability
  availability/check domain foundations.
- **Phase 8D:** Story progression was integrated into normal turns before
  Director context; Director gained bounded story/character context and
  authored narrative actions without direct state-mutation authority.
- **Phase 8E:** Provider-free AI evaluation harness foundation.
- **Phase 8F1:** Authored quest-completion rewards. The completed Library's
  Whisper quest grants +2 investigation and unlocks `keen_eye`; a persistent
  campaign-state claim prevents duplicate grants, and only a narrow
  authoritative effect is exposed to the Narrator.
- **Phase 8F2:** New campaigns receive two validated AI-generated, persisted
  starter abilities, represented with a bounded engine-owned mechanical
  vocabulary. `keen_eye` is the first player-invokable deterministic gameplay
  check, with an authored Library inspection at difficulty 2.

## Current Phase 8 status

**8F2 is implemented. The next intended slice generalizes deterministic
execution of validated generated-ability mechanics.**

The future character-details frontend may expose owned built-in/generated
abilities alongside inventory and player-facing mechanical summaries. It
remains out of scope for this engine slice.

## Stable invariants and explicit deferrals

- Story completion, progression rewards, ability ownership, checks, and world
  actions are deterministic authoritative operations.
- Authored reward definitions are static; reward claims are persisted in
  campaign state without a database migration. Models may observe and narrate
  an earned reward but never grant one.
- The engine enforces internal authentication, trusted user resolution,
  campaign ownership, transaction rollback, and explicit migration execution.
- D7A's observability foundation (OpenTelemetry tracing) is implemented;
  remaining D7 slices and production observability activation beyond D7A,
  generic reward/achievement/rules DSLs, combat progression, levels/classes,
  model-selected rewards, frontend progression UI, and broad evaluation
  expansion remain deferred.
- General deterministic execution of generated ability mechanics remains the
  next Phase 8 slice; generated starter definitions are persisted now but
  unsupported mechanics never receive model-adjudicated effects.

See [architecture.md](architecture.md) for durable architecture decisions and
the repository instructions for implementation boundaries.
