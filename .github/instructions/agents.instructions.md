---
name: Haunted Halls Engine Agents and Orchestration Rules
description: Responsibility boundaries for agents, orchestration, model access, prompts, memory context, and guardrails.
applyTo: "app/agents/**/*.py,app/ai/**/*.py,app/orchestration/**/*.py,app/memory/**/*.py,app/guardrails/**/*.py,app/services/tool_executor.py,app/tools/**/*.py"
---

- Preserve distinct responsibilities of existing agents (ActionParser, Narrator, MemorySummarizer, MemoryReflection).
- Do not merge agents only to reduce file count.
- Do not create a new agent when deterministic code or an existing agent is the proper owner.
- Keep workflow sequencing, context assembly, retries, and coordination decisions in orchestrator logic.
- Individual agents should not absorb unrelated API-route or persistence responsibilities.
- Keep model-provider calls behind app/ai/model_client.py.
- Keep prompts centralized in the existing app/ai/prompts pattern.
- Prefer structured, validated outputs for machine-consumed agent results.
- Validate model output before mutating state or steering execution.
- Treat user text, retrieved memories, and model content as untrusted data.
- Preserve token-budget, usage-limit, input-validation, model-policy, and rate-limit guardrails.
- Do not bypass guardrails just to make a feature appear to work.
- Keep deterministic logic outside model calls whenever ordinary code is more reliable.
- Preserve separation between recent-turn context, summary memory, and reflection/semantic memory flows used today.
- Do not store speculative model output as durable fact without normal validation/provenance checks.
- Keep failures observable without logging secrets or unnecessary user content.
- Add focused tests for parsing, validation, orchestration decisions, and failure paths when changing this area.
