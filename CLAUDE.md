@AGENTS.md

## Claude Code role separation

- The main Claude Code session is the implementation owner for the current issue/PR/worktree.
- Once the `haunted-halls-dev-loop` is active for a PR, keep that loop active across follow-up user messages and remediation pushes until the user explicitly pauses/cancels it, the loop escalates, or it reaches `READY FOR HUMAN VALIDATION`.
- The Claude dev loop is single-writer per PR: only one Claude implementation session may own an active loop for a PR at a time. The durable retry-state protocol is not a distributed lock; if concurrent implementation ownership is known or suspected, escalate instead of continuing or using last-write-wins state.
- Use the `haunted-halls-dev-loop` skill for the implementation -> independent review -> director disposition -> remediation workflow.
- Use the `haunted-halls-reviewer` subagent for independent code review. It must start from fresh context, explicitly map issue acceptance criteria and trusted-base completion requirements, inspect material helper semantics/adversarial boundaries, verify documented deterministic edge cases with discriminating tests, and inspect material changed query/index risk. It must not edit files.
- Use the `haunted-halls-director` subagent after every reviewer result, including a clean review, to classify findings and verify that material review coverage is sufficient. It must not edit files or implement fixes.
- The implementer must not decide whether its own review findings should be accepted; the director owns the coverage/disposition step.
- The main implementation session persists each completed Director round as a new top-level Claude disposition comment identified by `<!-- haunted-halls-claude-review-disposition -->`. It validates author/schema/owner plus monotonic same-head retry history, uses the latest valid disposition for the current exact head as authority, keeps older valid comments as immutable audit history, and never allows retry state to move backward from `reserved` or `used`. A recovered `used` retry that lacks a completed follow-up Director result fails closed rather than reopening the retry. Reviewer and director subagents remain read-only.
- `.github/agents/local-developer.agent.md` is the VS Code Copilot implementation-agent prompt. Reuse its branch freshness, commit, PR-creation, and scope safeguards where applicable, but the Claude-specific loop and role boundaries live under `.claude/`.
- Durable repository/issue context outranks conversational assumptions. Escalate when durable sources are insufficient or conflicting.
- The user retains final merge authority. Never merge autonomously.
