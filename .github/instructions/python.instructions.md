---
name: Haunted Halls Engine Python Rules
description: Python typing, exception, and module-boundary guidance for engine code.
applyTo: "**/*.py"
---

- Follow the current repository Python configuration and style (pytest, ruff, and project package boundaries).
- Add type annotations to new and modified functions consistent with existing code.
- Prefer specific types over Any.
- Prefer existing Pydantic models, dataclasses, and typed schema objects over unstructured dict plumbing.
- Follow current sync/async design; avoid blocking operations in async request paths.
- Raise specific domain or boundary exceptions instead of generic Exception.
- Avoid broad exception catches unless translating at boundaries, adding context, or handling cleanup.
- Preserve exception chaining when translating failures.
- Keep functions and modules focused.
- Avoid mutable default arguments.
- Preserve current import style and package layout.
- Do not suppress lint checks just to force a pass.
- Do not add dependencies when stdlib or existing packages are sufficient.
- Keep tokens, credentials, and sensitive values out of logs and error messages.
- Add docstrings where existing public abstractions need non-obvious behavioral clarification.
