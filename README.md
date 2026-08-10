# Haunted Halls Engine

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

## Campaign Ownership

- Campaigns now persist an internal ownership relationship through `campaigns.owner_user_id -> internal_users.user_id`.
- Campaign ownership is derived from the trusted authenticated user context propagated by the BFF and is persisted at campaign creation time.
- The browser never supplies campaign ownership; request bodies and query parameters cannot override it.
- Campaigns are the ownership aggregate for this phase; turns, events, and memories continue to inherit authorization through their campaign later in Phase 2B.
- Legacy `player_id` remains a temporary gameplay field and is not treated as the ownership identity.
- Existing legacy campaigns may temporarily remain unowned when no safe mapping exists; they are preserved without fabricated ownership assignments.
- Phase 2B will enforce owner-based reads and mutations, while Phase 2C will remove the legacy `player_id` identity behavior.

## Service And User Context

- FastAPI uses a two-step trust model for user-scoped endpoints: authenticate the calling service, then validate propagated user context.
- Next.js sends user context through `X-Haunted-Halls-User-Id` on user-scoped requests while continuing to use `Authorization: Bearer <internal-service-token>` for service authentication.
- The user ID header is trusted only when service authentication succeeds; missing, empty, malformed, unknown, or conflicting values are rejected with a generic auth error.
- User-scoped endpoints require both service auth and validated internal user context.
- Service-only endpoints, including `POST /internal/auth/users/resolve`, require service authentication but do not require the user-context header.
- Public liveness endpoints remain intentionally unauthenticated.
- Existing `player_id` remains temporary legacy gameplay input and is not trusted authentication identity.