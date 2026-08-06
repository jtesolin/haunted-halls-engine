---
name: Haunted Halls Engine FastAPI Rules
description: Transport-layer boundaries, auth dependencies, and HTTP contract rules for FastAPI endpoints.
applyTo: "app/main.py,app/api/**/*.py,app/schemas/**/*.py"
---

- Keep route handlers focused on transport, dependency injection, auth checks, validation, orchestration calls, response mapping, and HTTP error translation.
- Do not implement game rules, agent workflows, or direct low-level persistence in route handlers.
- Use existing request/response schema models under app/schemas.
- Validate input at HTTP boundaries and keep internal calls strongly typed.
- Reuse existing FastAPI dependency patterns in app/api/dependencies.py.
- Complete internal-service auth and trusted user-context resolution before user-scoped work.
- Do not trust user identity supplied in request body or query string.
- Map known application errors to deliberate HTTP responses.
- Do not leak stack traces, credentials, tokens, DB internals, or provider internals in responses.
- Preserve current status-code behavior unless intentionally changing the API contract.
- Use async handlers/dependencies correctly and avoid blocking operations.
- Treat response shape changes as API contract changes requiring tests and potential frontend coordination.
- Keep health endpoints lightweight and non-sensitive.
