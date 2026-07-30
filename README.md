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
- Campaign ownership and `player_id` migration remain future work.