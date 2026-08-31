# Haunted Halls Engine

## Project Status

Canonical project status for the two-repo system lives in:

- `docs/project-status.md`

Keep this document updated as engine changes affect architecture, behavior, roadmap, or phase completion.

## Local Runtime Configuration

- Copy `.env.example` to `.env` and fill in local values; `.env` is git-ignored and excluded from the Docker image.
- `.env` is read from the host at runtime, including by the sibling `haunted-halls` Docker Compose stack via `env_file`.
- AI is treated as enabled when `AI_ENABLED` is true or a non-empty `OPENAI_API_KEY` is present; otherwise the engine returns stub narration.
- Under Compose, `DATABASE_URL` and `INTERNAL_ENGINE_SERVICE_TOKEN` are set explicitly by the Compose file and override values from this `.env`.

## Database Migrations

SQLAlchemy Core is the engine persistence layer. SQLite remains the default lightweight local database for direct development and testing. PostgreSQL is now a supported backend and is the database used by the Docker Compose stack.

For a fresh local database, run:

```bash
make db-upgrade
```

For the Docker Compose stack, migrations are applied automatically by the one-shot `migrate` service before the engine starts. PostgreSQL data persists across normal `docker-compose down` / `docker-compose up` cycles and is removed only by `docker-compose down -v` (or `make docker-reset-db` from `../haunted-halls`).

The underlying commands are `alembic upgrade head`, `alembic current`, and `alembic history`. To use another local database, set `DATABASE_URL` before invoking them. For a PostgreSQL-backed test run, set `TEST_DATABASE_URL` before invoking pytest.

**Compose stack startup order:**
1. PostgreSQL service starts and becomes healthy (`pg_isready`)
2. Migration service runs `alembic upgrade head` and exits successfully
3. FastAPI engine starts and depends on migration success
4. Frontend starts and depends on engine health

## Container Debugging

- The sibling `haunted-halls` repository owns the Compose stack; start the debug stack there with `make debug-up`.
- The Dockerfile `debug` stage installs `requirements-dev.txt` (adds `debugpy`) and starts Uvicorn under `debugpy --listen 0.0.0.0:5678`; the production `runner` stage is unchanged and remains the default build target.
- `debugpy` does not wait for a client, so the container starts and passes its health check whether or not a debugger is attached.
- Attach with the `Attach: Haunted Halls Engine (Docker)` configuration in `.vscode/launch.json`; the existing `Python Debugger: FastAPI (dev)` launch configuration for non-container debugging still applies.
- Uvicorn reload is intentionally disabled in the debug container because subprocess reloading breaks breakpoint attachment; restart the engine container after Python changes.
- For local (non-container) development with debug tooling, use `make install-dev`.

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

CI also builds the production Docker image as `Engine / Docker Build`; it does not push the image.

## Local Docker Stack

Run the full local stack from the sibling frontend repository:

```bash
cd ../haunted-halls
docker compose build
docker compose up -d
```

The engine is reachable from the frontend as `http://engine:8000` and is not published directly to the host. View logs with `docker compose logs -f engine`, stop with `docker compose down`, and rebuild after changes with `docker compose up -d --build engine`.

The SQLite database is mounted at `/app/data` from the Compose-managed `engine-data` volume, so stopping, recreating, or rebuilding the engine preserves local data. To intentionally reset it, run `docker compose down -v` from `../haunted-halls`.

This mirrors the repo-defined Python 3.14 development setup and runs on the standard GitHub-hosted Linux runner.

## Local Security Verification

- `.venv/bin/python -m pytest tests/test_internal_service_auth.py tests/test_authorization.py tests/test_chat.py`
- `.venv/bin/python -m pytest`
- `make lint`
- `.venv/bin/python -c "import app.main"`