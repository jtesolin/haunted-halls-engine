# Haunted Halls Engine Agent Context

Use this as a short navigation map; repository-specific instructions remain
authoritative for local engineering practice.

## Primary references

- [.github/copilot-instructions.md](.github/copilot-instructions.md) — engine
  role, boundaries, and working practices.
- [.github/agents/local-developer.agent.md](.github/agents/local-developer.agent.md)
  — local implementation and review-remediation workflow.
- [docs/project-status.md](docs/project-status.md) — canonical
  cross-repository roadmap and current status.
- [docs/architecture.md](docs/architecture.md) — stable architecture and
  product/authority invariants.
- [docs/review-triage-policy.md](docs/review-triage-policy.md) — canonical
  review-disposition policy and comment contract.

## Review and planning source hierarchy

When evaluating a current change, use sources in this order:

1. Current repository code and tests for implemented behavior.
2. The linked GitHub implementation issue for the change's intended scope and
   acceptance criteria.
3. [docs/project-status.md](docs/project-status.md).
4. [docs/architecture.md](docs/architecture.md).
5. Repository-specific `.github/copilot-instructions.md`.
6. Older PR descriptions, historical issues, and conversational context only
   as supporting history when needed.

If durable sources conflict, surface the current code and explicit issue intent
for human escalation rather than silently reconciling the conflict.
