---
name: haunted-halls-dev-loop
description: Run the Haunted Halls implementation, independent review, director disposition, remediation, and re-review loop for a linked issue or current PR. Use for feature implementation and review remediation. Stop for human validation; never merge.
---

# Haunted Halls development loop

The main Claude Code session is the implementation owner. Keep implementation context in this session. Use isolated subagents for independent review and director judgment.

## 1. Establish authoritative scope

- Read `CLAUDE.md` / `AGENTS.md` and relevant repository instructions.
- Read the linked GitHub issue/specification and current code/tests.
- For engine architecture, status, and review policy, use the canonical engine docs referenced by `AGENTS.md`.
- If durable sources conflict or required product/architecture intent is undocumented, stop with an escalation instead of guessing.

## 2. Prepare isolated implementation work

- Use a dedicated branch/worktree for the feature.
- Follow the repository's existing branch-freshness, commit-message, scope, validation, and initial-PR safeguards.
- Never create a second PR for the same branch.
- Never merge autonomously.

## 3. Implement and validate

- Make the smallest coherent implementation satisfying the issue.
- Run the narrowest relevant validation first, then broader validation as appropriate.
- Review the final diff for scope creep.
- Commit validated work.
- Before the first push/PR, reconcile the unpublished branch with freshly fetched `origin/main` according to the repository workflow.
- Push and create exactly one PR with non-interactive `gh pr create` if the branch does not already have one.

## 4. Independent review

After the initial PR exists, and after every remediation push:

1. Gather the current PR number, head SHA, linked issue/specification, changed-file list, and current CI state when available.
2. Invoke the `haunted-halls-reviewer` subagent with fresh context.
3. Supply only durable scope/context plus the current change identity; do not pass implementation-conversation rationale as authority.
4. If the reviewer reports `No substantive review findings.`, proceed to the stop-condition check.

## 5. Director disposition

When candidate findings exist:

1. Invoke the `haunted-halls-director` subagent.
2. Provide the current PR/head, linked issue/specification, CI state, and complete reviewer findings.
3. The director independently validates and classifies every substantive finding as:
   - `FIX IN THIS PR`
   - `DEFER`
   - `REJECT`
   - `ESCALATE`
4. The implementation session must not override the director's disposition.
5. During this foundation phase, keep the disposition in the Claude session and report it clearly. A later automation phase will persist the same contract as a top-level GitHub PR comment.

## 6. Remediate

- If any finding is `ESCALATE`, stop the autonomous loop and surface the required human decision.
- Implement only `FIX IN THIS PR` items.
- Do not implement `DEFER` or `REJECT` items.
- Re-run relevant validation.
- Commit and push remediation to the existing PR branch without rebasing or rewriting published history unless explicitly directed.
- Return to independent review.

## 7. Stop condition

Declare `READY FOR HUMAN VALIDATION` only when all are true:

- the latest head has no `FIX IN THIS PR` findings;
- there are no unresolved `ESCALATE` findings;
- required validation is green;
- the PR is still scoped to its linked issue;
- no merge has occurred.

Report the PR number/head, validation status, review/remediation rounds, any deferred findings, and anything the human should specifically validate.

Never merge.
