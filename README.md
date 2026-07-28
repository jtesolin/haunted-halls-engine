# Haunted Halls Engine

## Local Service Authentication

- Generate the shared service token with `openssl rand -hex 32`.
- Set `INTERNAL_ENGINE_SERVICE_TOKEN` in this repo's `.env` file and in the Next.js BFF environment to the exact same value.
- Restart both services after changing the token.
- The FastAPI engine is intended to have no public ingress; Next.js is the only public application service.
- Network isolation and service authentication are complementary controls.
- A direct unauthenticated engine request should return `401`; the same request through Next.js should succeed.