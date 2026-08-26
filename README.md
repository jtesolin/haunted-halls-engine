# Haunted Halls Engine

## Project Status

Canonical project status for the two-repo system lives in:

- `docs/project-status.md`

Keep this document updated as engine changes affect architecture, behavior, roadmap, or phase completion.

## Local Service Authentication

- Generate the shared service token with `openssl rand -hex 32`.
- Set `INTERNAL_ENGINE_SERVICE_TOKEN` in this repo's `.env` file and in the Next.js BFF environment to the exact same value.
- Restart both services after changing the token.
- The FastAPI engine is intended to have no public ingress; Next.js is the only public application service.
- Network isolation and service authentication are complementary controls.
- A direct unauthenticated engine request should return `401`; the same request through Next.js should succeed.

## Internal User Resolution

- Google OIDC authentication is handled by Next.js/Auth.js; the engine does not authenticate browser users directly.
- Next.js resolves authenticated identities through the private endpoint `POST /internal/auth/users/resolve`.
- This endpoint is protected by the same internal service bearer credential used for other private BFF-to-engine calls.
- Internal users are keyed by canonical OIDC issuer + provider subject.
- Email is mutable profile data and is not used as an identity key.
- Resolution occurs during initial sign-in, updates profile fields + `last_login_at`, and returns only `user_id`.
- Development sessions created before Phase 1C may require signing out and signing back in.
- Email remains mutable profile data only; it never becomes an authorization key or fallback identity path.

## Campaign Ownership

- Campaigns now persist an internal ownership relationship through `campaigns.owner_user_id -> internal_users.user_id`.
- Campaign ownership is derived from the trusted authenticated user context propagated by the BFF and is persisted at campaign creation time.
- The browser never supplies campaign ownership; request bodies and query parameters cannot override it.
- FastAPI is the domain authorization boundary. Campaign ownership is enforced using the Phase 1D authenticated internal user. All user-facing campaign operations require `campaigns.owner_user_id == authenticated_user.id`.
- Child resources (turns, events, memories, summaries) inherit authorization through campaign ownership — no redundant owner columns are added to child tables.
- Cross-user access and nonexistent resources both return `404`. The response does not reveal whether a resource exists, who owns it, or whether it is unowned.
- Legacy campaigns with null `owner_user_id` are excluded from all normal user-facing APIs (list, get, update, delete, chat). They remain in the database and are not automatically claimed.
- Local developers may delete and recreate legacy campaigns to associate them with their authenticated account.
- Browser-facing contracts use authenticated session context only.

## Service And User Context

- FastAPI uses a two-step trust model for user-scoped endpoints: authenticate the calling service, then validate propagated user context.
- Next.js sends user context through `X-Haunted-Halls-User-Id` on user-scoped requests while continuing to use `Authorization: Bearer <internal-service-token>` for service authentication.
- The user ID header is trusted only when service authentication succeeds; missing, empty, malformed, unknown, or conflicting values are rejected with a generic auth error.
- User-scoped endpoints require both service auth and validated internal user context.
- Service-only endpoints, including `POST /internal/auth/users/resolve`, require service authentication but do not require the user-context header.
- Public liveness endpoints remain intentionally unauthenticated.
- Browser-controlled identity values such as `user_id`, `owner_user_id`, `player_id`, email, provider claims, or user-context headers never override the authenticated internal user.
- Campaign ownership is the domain authorization boundary for campaigns and all child resources. Missing, cross-user, and legacy unowned resources return the same `404`.
- Chat authorization and quota checks complete before turns, events, memories, summaries, state changes, or model-usage records are persisted.
- Memory retrieval and semantic search remain campaign-scoped before results enter model context.

## CI

GitHub Actions runs the engine validation workflow on pull requests targeting `main`, on pushes to `main`, and manually via `workflow_dispatch`.

The workflow validates the repository's existing Python checks:

- `python -m ruff check .`
- `python -m pyright`
- `python -m pytest`

This mirrors the repo-defined Python 3.14 development setup and runs on the standard GitHub-hosted Linux runner.

## Local Security Verification

- `.venv/bin/python -m pytest tests/test_internal_service_auth.py tests/test_authorization.py tests/test_chat.py`
- `.venv/bin/python -m pytest`
- `make lint`
- `.venv/bin/python -c "import app.main"`