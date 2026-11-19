# Haunted Halls Engine dev-loop policy

This file extends the project-agnostic `jt-dev-loop` plugin with requirements specific to the Haunted Halls engine. Generic review, Director disposition, retry, durable PR-state, external-evidence reconciliation, and readiness mechanics belong to the plugin and are intentionally not duplicated here.

## Architecture and ownership

- This repository, `jtesolin/haunted-halls-engine`, is the internal FastAPI game engine.
- The related frontend repository is `jtesolin/haunted-halls`.
- The engine owns authoritative game/domain state, deterministic game behavior, AI-agent orchestration, narration, memory, guardrails, persistence, and internal APIs.
- The frontend owns presentation, browser interaction, BFF behavior, and authentication integration.
- Preserve the detailed authority and component boundaries in trusted-base `docs/architecture.md` and `.github/copilot-instructions.md`.
- Treat API contract changes as potentially cross-repository changes; do not assume a locally correct engine change is complete when a supported frontend flow depends on additional work.

## Runtime lifecycle invariants

For every material user-visible or cross-repository capability, trace the supported production lifecycle from a real entry point through prerequisite state producers, persistence/service/API boundaries, and the final consumer.

A consumed state/value must have a normal application producer/update path, or the current scope must coherently add that producer. Fixtures, manually seeded rows, direct database edits, mocks, and read APIs without a normal producer do not prove production reachability.

When frontend behavior depends on engine state, inspect enough evidence from `jtesolin/haunted-halls` to establish the consumer and contract when that dependency is material.

Historical example:

`create campaign -> create/associate character -> campaign exposes character_id -> frontend enables Character Sheet -> character API returns character data`

## Validation requirements

Use the current trusted repository instructions to select validation appropriate to the change. Available verified commands include:

- `make lint`
- `make test`
- `.venv/bin/python -m pytest`
- `tox`

Run focused tests first, then broader validation. Migration/database changes must include the relevant migration and PostgreSQL validation. Required CI checks for the PR head must pass before readiness under the generic loop.

## Documentation and status

- Update `docs/project-status.md` when architecture, behavior, roadmap, or phase-progress status changes.
- Update `docs/architecture.md` when stable architecture, authority, persistence, API, or cross-repository invariants change.
- Do not change `Last synchronized (planning memory sync)` unless the user explicitly confirms a planning-memory synchronization.
- Keep implementation details in the owning repository; cross-repository durable invariants belong in the canonical engine architecture/status documents.

## Cross-repository dependencies

- Validate each repository's implementation and CI independently.
- A change spanning both repositories requires separate branches/PRs and explicit evidence for the prerequisite/consumer contract in each repository.
- Do not make mirrored or speculative frontend changes merely because the frontend repository is available.
- If the current engine scope cannot make a claimed cross-repository capability reachable without expanding into unresolved frontend/product/architecture work, escalate rather than silently defer the missing prerequisite.

## Human validation

Reserve `human validation required` for checks that genuinely require a person or external environment, such as deployed preview UX/narrative coherence or other behavior that cannot be established by repository tests/inspection. Do not use human validation to defer implementable code, tests, migrations, or documentation.

## Project-specific escalation

Escalate rather than invent or autonomously choose:

- undocumented game/product semantics that require an authoritative product value;
- significant architecture or repository-responsibility changes;
- authentication/trust-boundary redesign;
- destructive persistence, data-recovery, or materially risky migration behavior;
- public/cross-repository compatibility breaks without explicit scope;
- model/tool privilege or world-authority boundary changes;
- substantial cross-repository scope expansion.

The user retains final merge authority. Never merge autonomously.
