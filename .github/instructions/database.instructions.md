---
name: Haunted Halls Engine Database and Persistence Rules
description: Repository/session boundaries, user scoping, and persistence safety rules.
applyTo: "app/db/**/*.py,app/memory/repository.py"
---

- Keep database access behind repository/session abstractions.
- Do not place direct persistence queries in API routes or agents.
- Preserve transaction boundaries and atomicity for related state changes.
- Always scope user-owned data through trusted resolved identity and player ownership checks.
- Do not treat untrusted request values as authorization to read or mutate another user record.
- Use parameterized SQL and the established query/repository approach.
- Reuse current session-management patterns in app/db/session.py.
- Avoid hidden commits inside low-level helpers when caller-owned transaction scope is required.
- Preserve schema compatibility unless explicit schema-change work is part of the task.
- When changing models or tables, inspect repository methods, serialization, tests, and database initialization/migration helpers.
- Avoid N+1 or repeated unnecessary queries.
- Make ordering explicit when behavior depends on order.
- Prefer durable DB constraints for invariants while keeping useful application validation.
- Keep local SQLite assumptions explicit and avoid changes that make future DB evolution harder without need.
- Keep connection strings, credentials, and sensitive payloads out of logs.
