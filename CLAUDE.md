@AGENTS.md

## Claude Code role separation

- The main Claude Code session is the implementation owner for the current issue/PR/worktree.
- Use the `haunted-halls-dev-loop` skill for the implementation -> independent review -> director disposition -> remediation workflow.
- Use the `haunted-halls-reviewer` subagent for independent code review. It must start from fresh context and must not edit files.
- Use the `haunted-halls-director` subagent to classify reviewer findings against durable project context. It must not edit files or implement fixes.
- The implementer must not decide whether its own review findings should be accepted; the director owns that disposition step.
- `.github/agents/local-developer.agent.md` is the VS Code Copilot implementation-agent prompt. Reuse its branch freshness, commit, PR-creation, and scope safeguards where applicable, but the Claude-specific loop and role boundaries live under `.claude/`.
- Durable repository/issue context outranks conversational assumptions. Escalate when durable sources are insufficient or conflicting.
- The user retains final merge authority. Never merge autonomously.
