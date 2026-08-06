---
name: Haunted Halls Engine Test Rules
description: Pytest-focused rules for API, auth, orchestration, tools, and repository behavior.
applyTo: "tests/**/*.py"
---

- Use the existing pytest setup and fixtures from tests/conftest.py.
- Write focused tests at the narrowest useful layer.
- Add a regression test for corrected defects.
- Keep tests deterministic and isolated.
- Use isolated DB state through the existing fixture pattern; do not mutate shared data files.
- Do not call real model providers, OAuth providers, or external services.
- Mock external boundaries, not the internal behavior under test.
- Test both success and important rejection/failure paths.
- For API tests, assert status codes and response contracts.
- For auth tests, cover missing, malformed, invalid, and untrusted identity contexts relevant to current implementation.
- For agent/orchestration tests, validate structured output handling, malformed model responses, and guardrail behavior.
- For repository tests, verify user scoping, ordering/boundaries, transaction behavior, and not-found paths.
- Reuse existing fixtures and keep test inputs minimal and explicit.
- Avoid assertions on incidental internals.
- Do not weaken production validation just to simplify tests.
- Run narrow tests first, then broader suites:
  - `.venv/bin/python -m pytest tests/test_<target>.py`
  - `.venv/bin/python -m pytest`
  - `make test`
