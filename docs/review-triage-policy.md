# Review-Triage Policy

This is the canonical policy for AI-assisted review disposition across Haunted
Halls repositories.

## Decision sources

Evaluate review and planning decisions using this precedence:

1. Current repository code and tests for what is implemented.
2. The linked GitHub implementation issue for the current change's intended
   scope and acceptance criteria.
3. The canonical cross-repository
   [project status](project-status.md).
4. Stable [architecture and authority invariants](architecture.md).
5. Repository-specific `.github/copilot-instructions.md` for local
   engineering boundaries and conventions.
6. Older PR descriptions, historical issues, and conversational context only
   as supporting history when needed.

When durable sources conflict, do not silently reconcile them. Surface the
current code and explicit current issue intent for human escalation.

## Dispositions

Every substantive finding receives exactly one of these four dispositions:

### `FIX IN THIS PR`

Use when the finding is valid, materially relevant to the current change,
within the linked issue's coherent scope, and does not require unresolved
architectural or product judgment.

Examples include correctness bugs introduced or exposed by the PR, CI or test
gaps necessary to prove acceptance criteria, narrow portability or
error-handling mistakes, and documentation that misstates behavior the PR
claims to provide.

### `DEFER`

Use when the finding is valid and worthwhile but is pre-existing or outside
the current PR's coherent scope and is not required for the current change to
be safe and correct. Explain why it is safe to carry temporarily and identify
the desired follow-up. Do not silently expand the PR.

### `REJECT`

Use when the finding is technically incorrect, already addressed by current
code, based on a false assumption, or recommends behavior that conflicts with
explicit current requirements or architecture. Include a concise rationale.

### `ESCALATE`

Use when durable context cannot safely decide the finding or a human must make
an architectural, product, or risk decision. Escalate significant
architecture/responsibility changes; materially ambiguous or conflicting
requirements; undocumented game or product semantics; authentication,
authorization, or trust-boundary redesign; destructive persistence or
data-recovery behavior; operationally meaningful schema or migration strategy
changes; agent/tool privilege or world-authority boundary changes; substantial
scope expansion; and findings dependent on undocumented conversational intent.

## Automated Work triage

For each GitHub Copilot Code Review completion on either Haunted Halls
repository, the triage agent must:

1. Identify the PR and current head commit.
2. Read the PR description and linked implementation issue when one exists.
3. Read the latest unresolved GitHub Copilot review threads and findings.
4. Inspect the current PR diff and only relevant current code and tests needed
   to validate each finding.
5. Read the project status, architecture invariants, and repository-specific
   Copilot instructions as needed.
6. Check current CI status without waiting for or polling pending CI; report
   pending status when applicable.
7. Classify every substantive finding with one of the four dispositions.
8. Post or update exactly one top-level PR comment headed
   `## Review disposition`.
9. Treat duplicate, suppressed, or repeated Copilot findings as one
   substantive finding.
10. Do not edit code; create branches, PRs, or forks; delegate coding work;
    merge; or resolve inline review threads.
11. Do not automatically create deferred GitHub issues during the initial
    rollout. Describe the follow-up in the disposition and leave issue
    creation for human or ChatGPT confirmation.
12. Stop after posting or updating the disposition. One review-completion
    event produces one focused triage run.

Use context efficiently: do not resynchronize the whole project or read
unrelated historical conversations or PRs when the current issue, diff, and
relevant durable documentation are sufficient.

## `Review disposition` comment contract

Use this stable human- and machine-readable format. Empty disposition sections
may be omitted, but the heading and exact disposition names must remain stable.

```markdown
## Review disposition

PR head evaluated: `<sha>`
Copilot review evaluated: `<review id/timestamp if available>`
CI: `green | failed | pending | not found`

### FIX IN THIS PR
- <finding + concise rationale>

### DEFER
- <finding + reason + desired follow-up>

### REJECT
- <finding + rationale>

### ESCALATE
- <finding + required human decision>

### Implementation instruction
Implement only items under `FIX IN THIS PR`. Do not implement `DEFER`,
`REJECT`, or `ESCALATE` items without explicit user direction.
```

When a reliable Work-owned disposition comment already exists, a later triage
pass should update it. Otherwise it may create a new top-level disposition;
the most recent `Review disposition` remains authoritative for remediation.
