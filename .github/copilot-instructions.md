# Haunted Halls Engine Instructions

## Repository Role

- This repository is the internal Haunted Halls FastAPI game engine.
- It owns game-domain behavior, AI-agent orchestration, narration, game state, memory processing, guardrails, persistence, and internal APIs.
- It is an internal service, not a browser-facing application.
- Frontend presentation and React behavior belong in the haunted-halls repository.

## Architectural Boundaries

- Keep HTTP transport concerns in app/api routes and dependencies.
- Keep orchestration decisions in app/orchestration.
- Keep agent behavior in app/agents, with model access centralized through app/ai/model_client.py.
- Keep game-domain behavior in app/game and typed schemas in app/schemas.
- Keep memory retrieval/storage flows in app/memory.
- Keep safety constraints in app/guardrails.
- Keep persistence behind app/db repositories and session management.
- Route handlers must stay thin; do not move game rules or persistence logic into route functions.
- Do not add frontend or browser concerns to this service.
- Treat API contract changes as cross-repository changes that may require updates in haunted-halls.

## Working Practices

- Inspect adjacent implementations, schemas, and tests before changing behavior.
- Prefer focused changes that preserve current boundaries.
- Preserve compatibility unless a breaking change is explicit.
- Do not invent fallback behavior that hides failures or corrupts state.
- Do not add dependencies without clear need.
- Never commit secrets or real credentials.
- After making repository changes, update `docs/project-status.md` when architecture, behavior, roadmap, or phase-progress status changes.
- Do not change `Last synchronized (planning memory sync)` unless the user explicitly confirms they performed that planning-memory synchronization.
- Treat project-status maintenance as part of normal completion for substantive repository updates.
- Use verified commands:
  - `make venv`
  - `make install`
  - `make dev`
  - `make start`
  - `make lint`
  - `make test`
  - `.venv/bin/python -m pytest`
  - `tox`
- Run focused tests first, then broader validation.
