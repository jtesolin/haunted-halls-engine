# Phase 7B — Director rollout and parallel execution plan

This document records the near-term Phase 7B execution split so multiple implementation agents can work safely without creating avoidable merge or architecture conflicts.

## Current baseline

- Engine `main`: `b280af61322f9a134c7fa0fef433bd5a4a597060`
- Web `main`: `abd62bc7ccee23debe93b4d2102c5b39becb3589`
- Phase 7A — World Authority Foundation: complete.

`docs/project-status.md` remains the canonical cross-repository status document. The first Phase 7B implementation PR should reconcile its current broad/contradictory Phase 7B text with this approved split without changing planning-sync metadata.

## Phase 7B split

### 7B1 — Director Contract & Proposal Boundary

Engine-only foundation.

Define a bounded Director input/context contract and a strict proposal contract that represents exactly zero or one existing typed `WorldAction`. Establish the proposal boundary without adding a model call, prompt, normal-chat orchestration integration, persistence side effects, or narrator/frontend changes.

The Director may propose privileged action; it must never mutate authoritative campaign state directly. `WorldAuthorityExecutor` remains the only execution boundary for the existing privileged world actions.

### 7B2 — Model-backed Director Agent

Depends on 7B1.

Add the actual Director agent/model call, structured output, prompt, model-policy/token-budget integration, diagnostics, and focused tests. Keep it disconnected from normal chat execution so this phase only proves model-backed proposal generation.

### Issue #3 — Idempotent chat turns

Cross-repository reliability work may run in parallel with 7B2 once 7B1 is merged, provided it uses a separate engine worktree/clone from the Director agent work.

Issue #3 owns request identity, retry, persistence/concurrency, BFF propagation, and frontend retry semantics. Avoid Director/orchestration changes in this lane.

### 7B3 — Director Orchestration Integration

Depends on 7B2 and issue #3.

Integrate the Director into the normal turn pipeline only after both the model-backed proposal behavior and idempotent request/persistence semantics are stable.

Target ordering:

```text
player text
  -> Action Parser
  -> player ToolExecutor
  -> authoritative player result/state
  -> Director
  -> zero or one typed WorldAction
  -> WorldAuthorityExecutor
  -> final authoritative state
  -> narrator scene projection
  -> Narrator
```

7B3 owns world-action execution ordering, persistence/events, Director failure semantics in the turn transaction, final scene projection, and narrator grounding after world actions.

## Safe parallel lanes

- 7B1 may run in parallel with web issue #21 (legacy `run.app` redirect).
- Web issue #22 follows #21 after custom-domain sign-in is reverified.
- Web issue #6 (VS Code browser debugging) is independent and optional/low priority.
- 7B2 may run in parallel with issue #3 if separate engine working trees are used and both scopes avoid the other's owned integration surfaces.
- Do not run 7B3 in parallel with issue #3 because both change the chat-processing/persistence boundary.

## Deferred until after 7B3

Keep these out of the Director rollout unless promoted by a later dedicated issue:

- combat/damage;
- doors, locks, keys, and cellar progression;
- autonomous NPC simulation beyond explicit Director world actions;
- generic perception;
- narrator validation/retry;
- pgvector redesign;
- content/engine separation;
- broad E2E expansion;
- D7 observability implementation until its subphases are explicitly designed.

## Working-tree rule for parallel engine agents

Multiple agents must not switch branches in the same checkout. Concurrent engine lanes require separate Git worktrees or clones so branch state, uncommitted changes, validation, and PR remediation remain isolated.
