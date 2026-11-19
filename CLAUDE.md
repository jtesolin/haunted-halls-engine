@AGENTS.md

## Claude Code development loop

- Use the globally installed `jt-dev-loop:dev-loop` skill for scoped implementation and PR review work.
- Haunted Halls-specific review requirements live in `.claude/dev-loop-policy.md`; generic review, Director disposition, durable PR state, retry, external-evidence reconciliation, and readiness mechanics belong to the `jt-dev-loop` plugin.
- The main Claude Code session is the implementation owner for the current issue/PR/worktree. Keep an active dev loop continuous across follow-up remediation until the user pauses/cancels it, it escalates, or it reaches `READY FOR HUMAN VALIDATION`.
- Durable repository/issue context outranks conversational assumptions. Escalate when durable sources are insufficient or conflicting.
- The user retains final merge authority. Never merge autonomously.
