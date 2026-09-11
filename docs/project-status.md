# Haunted Halls — Project Status

**Last synchronized (planning memory sync):** September 8, 2026
**Engine baseline:** `haunted-halls-engine` `main` at commit `70081498aaec87895969865333ea574eba4cd654`
**Web baseline:** `haunted-halls` `main` at commit `b836691839185ad9b115eda530fbe00df2411ebf`

Synchronization metadata meaning:

- **Last synchronized (planning memory sync)** is the date when ChatGPT intentionally inspected the repositories and reconciled this status document against the implemented code.
- **Engine baseline** is the engine commit inspected during that synchronization pass.
- **Web baseline** is the web/frontend commit inspected during that synchronization pass.
- Normal implementation changes, Copilot-generated updates, and ordinary documentation edits do **not** update these fields.
- Update these fields together only during an explicit synchronization/re-baselining pass.
- Expected workflow:
      1. Implementation proceeds normally.
      2. `Last synchronized (planning memory sync)`, `Engine baseline`, and `Web baseline` remain unchanged.
      3. During a future explicit synchronization, ChatGPT inspects commits after the recorded baselines.
      4. After reconciliation, all metadata fields are updated together to the new synchronized state.

## Project Goal

Haunted Halls is a long-running AI-driven MUD/chat game and learning project focused on agentic AI architecture.

The system combines deterministic game systems with LLM-based agents. AI is responsible for understanding player intent, memory synthesis, and narration, while authoritative game state and rules should increasingly remain deterministic.

## Current Architecture

```text
Browser
   |
   v
Next.js Web App / BFF
   |
   | Internal service authentication
   | Trusted internal user context
   v
FastAPI Engine
   |
   +-- Orchestrator
   |    |
   |    +-- Action Parser Agent
   |    +-- Tool Executor
   |    +-- Narrator Agent
   |    +-- Memory Services
   |
   +-- Game Rules / State
   +-- Tool Registry
   +-- MCP Client
   +-- Persistence
```

Next.js is the public application boundary. Google OIDC authentication occurs there, while the FastAPI engine is intended to remain private and trusts user identity only when it arrives through an authenticated internal-service request. Campaign ownership is enforced by the engine.

Stable cross-repository architecture and authority invariants are maintained in [architecture.md](architecture.md). The canonical AI-assisted review-disposition policy and comment contract are maintained in [review-triage-policy.md](review-triage-policy.md).

## Completed Work

### Phase 1 — Core Application Foundation

**Status: Complete**

* Next.js frontend established.
* FastAPI engine established.
* Campaign and chat APIs implemented.
* Persistent campaigns and conversation turns implemented.
* SQLite local-development persistence implemented through SQLAlchemy Core.
* Campaign creation, retrieval, and deletion supported.
* Web UI supports campaign selection and persistent conversations.

### Phase 2 — AI Narration and Guardrails

**Status: Complete**

* OpenAI model integration implemented.
* Responses API used for model execution.
* Narrator Agent separated from model transport.
* Model policies and token budgets implemented.
* Input validation, rate limiting, usage limits, and model guardrails implemented.
* Model request/usage information persisted.
* Daily usage accounting now counts authenticated user turns at the top level while preserving model-call telemetry for internal agent requests and provider usage when available.
* Guardrail checks now evaluate before each provider call and reject over-limit requests cleanly without mutating chat state when the daily budget is exhausted.

### Phase 3 — Production Infrastructure

**Status: Complete — D4C Cloud Run runtime and first production deployment verified**

The production infrastructure is now deployed through D4C and ready for the future D5 rollout:

* Local containerization and Compose orchestration remain in place for developer workflows.
* PostgreSQL compatibility and migration readiness were validated and formalized in D3B through D3D.
* D4A completed the shared GCP and Terraform foundation.
* D4B completed the Cloud SQL and Secret Manager foundation.
* D4C completed and verified the Cloud Run runtime and first production deployment.

The D2 baseline is no longer the current production-infrastructure status; it is superseded by the D3/D4 deployment-readiness work.

### D3B — PostgreSQL Compatibility

**Status: Complete — PostgreSQL compatibility validated**

The engine persistence layer now uses native SQLAlchemy Core statements instead of the legacy SQLite placeholder adapter, and the repository remains dialect-neutral across SQLite and PostgreSQL. Alembic upgrades are validated against both a fresh SQLite database and a fresh PostgreSQL database. SQLite remains the default lightweight local database, while PostgreSQL is supported for engine persistence and CI validation. Cloud SQL PostgreSQL was later deployed as managed infrastructure in D4B, and the current Alembic schema was applied there by the D4C migration job.

### D3C — Local PostgreSQL Integration

**Status: Complete — PostgreSQL integrated into Docker Compose stack**

The Docker Compose application stack now uses PostgreSQL 16 instead of SQLite. The Compose configuration includes:

* PostgreSQL 16 Alpine service with persistent volume and health check.
* One-shot migration service that runs `alembic upgrade head` before the engine starts.
* Explicit service dependency ordering: PostgreSQL → migration → engine → frontend.
* Engine DATABASE_URL configured to use PostgreSQL when running in Compose.
* Direct local development and pytest continue to use SQLite as the default database.
* Debug Compose stack shares the same PostgreSQL database as the normal stack.
* Sibling `haunted-halls` repository Makefile targets updated to reflect PostgreSQL persistence model.

At the time D3C was implemented, the following production infrastructure was deferred because development remained local. The GCP/Terraform foundation, Cloud SQL, production secrets management, and Cloud Run application runtime have since been established in D4A-D4C:

Still deferred:

* Production observability.
* Network/private-service deployment at scale.

Application hosting and GitHub Actions deployment automation are complete through D5.

### D3D — Deployment Migration Readiness

**Status: Complete — deployment migration contract formalized**

D3D formalizes the database migration contract and strengthens CI validation around migrations to support future deployment automation (D5).

Implemented as part of D3D:

* **Migration contract documentation** — The production deployment sequence: build image → run migration job → `alembic upgrade head` → migrate succeeds → deploy application. Application startup must remain independent of migrations.
* **Migration failure semantics** — Migration failure blocks application deployment; the new application revision must not start if migrations fail.
* **Rollback expectations** — Clarified distinction between application rollback and schema downgrade. Backward-compatible schema changes are preferred; destructive downgrade during application rollback is not automatic or assumed.
* **Direct SQLite workflow documentation** — Explicit guidance on `make db-upgrade` + `make dev` for local development without Compose.
* **Direct Compose PostgreSQL workflow documentation** — Clarified that `make docker-up` automatically applies migrations via the one-shot migration service; `make docker-migrate` is used only when pulling new migrations while the stack is already running.
* **Migration creation workflow** — Documented the flow: modify metadata → `alembic revision --autogenerate` → review generated migration → `make db-upgrade` → test → commit together. Migrations are production code requiring review.
* **Migration safety expectations** — Prefer additive changes, avoid destructive changes in the same deployment, test against both SQLite and PostgreSQL, review migrations carefully.
* **CI migration validation** — Enhanced CI checks: single-head validation (detects accidental multiple heads) and migration drift detection (`alembic check`).
* **Makefile improvements** — Engine Makefile now includes `db-heads` and `db-check` targets with clear documentation for each migration command. Frontend Makefile clarified with detailed explanations of Compose workflow including automatic migration ordering.
* **Documentation fixes** — Corrected stale references to SQLite persistence in Compose; documentation now correctly reflects PostgreSQL.

D3D provided the migration contract and validation consumed by the completed D4C deployment and future D5 automation.

### D4A — GCP + Terraform Foundation

**Status: Complete — shared GCP/Terraform control plane established**

D4A establishes the foundational Google Cloud and Terraform platform needed before creating billable database or application runtime resources. The frontend repository owns the shared deployment infrastructure and the Terraform foundation includes:

* GCS remote state bootstrap with a dedicated state bucket
* reusable `infra/terraform` configuration managed under the frontend repository
* GA Terraform provider configuration and lock file discipline
* GCP API enablement for Cloud Run, Artifact Registry, Cloud SQL, Secret Manager, IAM, IAM Credentials, Service Usage, Cloud Resource Manager, and billing budgets
* Artifact Registry repository for future Docker images
* dedicated runtime service accounts for frontend, engine, and migration workloads
* separate frontend and engine GitHub deployment service accounts
* GitHub OIDC Workload Identity Federation restricted to the trusted main-branch repository context
* project budget guardrail with a $20 monthly default
* credential-free Terraform validation in CI

D4A intentionally did not create Cloud SQL or Cloud Run; Cloud SQL was added in D4B, and Cloud Run runtime deployment was completed in D4C.

### D4B — Cloud SQL + Secret Manager

**Status: Complete — managed PostgreSQL and production secret foundation deployed**

D4B deploys the managed database and production secrets foundation on top of the D4A GCP/Terraform control plane. Implemented and verified in GCP:

* Cloud SQL PostgreSQL 16 instance in `us-east1`, edition `ENTERPRISE`, `db-f1-micro` shared-core development tier, ZONAL availability, 10 GB SSD storage with autoresize.
* Backups disabled and deletion protection disabled for the current development environment.
* Public IPv4 retained intentionally for Cloud SQL Auth Proxy/connector access, with `connector_enforcement = "REQUIRED"` and zero authorized networks, so only proxy/connector traffic is accepted.
* Application database `haunted_halls` and application user `haunted_halls_app` created; Alembic remains authoritative for the application schema, which was applied to this instance by the successful D4C migration job.
* Secret Manager secret containers created for the database URL, internal engine service token, NextAuth secret, OpenAI API key, and Google OAuth client secret.
* Terraform-generated secrets (database URL, internal engine service token, NextAuth secret) use ephemeral/write-only handling so values are not written to Terraform state; generated secret rotation is controlled through explicit version variables.
* Secret-level IAM grants `roles/secretmanager.secretAccessor` only to the runtime identities that require each secret; only the engine and migration runtime identities receive `roles/cloudsql.client`; the frontend runtime has no direct database access.
* OpenAI API key is stored in Secret Manager (value not recorded here).
* Production Google OAuth Web Application is configured, with its client secret stored in the `hh-google-client-secret` Secret Manager secret.
* Final post-apply Terraform plan reported no drift.

### D4C — Cloud Run runtime + first deployment

**Status: Complete — production runtime deployed and verified**

D4C deploys the public frontend BFF, private engine, and migration job in `us-east1`:

```text
Internet
   |
   v
Cloud Run — haunted-halls-frontend
   |
   | Cloud Run IAM:
   | X-Serverless-Authorization: Bearer <Google ID token>
   |
   | application service auth:
   | Authorization: Bearer <Haunted Halls internal service token>
   |
   v
Cloud Run — haunted-halls-engine
   |
   v
Cloud SQL PostgreSQL 16
```

The migration path uses the same engine image:

```text
engine image
      |
      v
Cloud Run Job — haunted-halls-migrate
      |
      v
alembic upgrade head
      |
      v
Cloud SQL
```

The deployed resources are `haunted-halls-frontend`, `haunted-halls-engine`, and `haunted-halls-migrate`. Runtime identities are `hh-frontend-runtime`, `hh-engine-runtime`, and `hh-migration-runtime`. Cloud Run uses minimum instances `0`, maximum instances `2`, and initial sizing of `1 CPU / 512 MiB`. The production engine uses `TOOL_REGISTRY_TRANSPORT=local`.

The first deployment used `application_services_enabled = false` to provision the migration job without application services. After `alembic upgrade head` completed successfully, `application_services_enabled = true` enabled Terraform management of the engine, frontend, and service IAM. The migration succeeds before application services are deployed; future D5 automation must preserve this contract.

Production authentication has two layers: `X-Serverless-Authorization` carries a Google-signed ID token for Cloud Run IAM, while `Authorization` carries the Haunted Halls internal service token for FastAPI application authentication. The browser does not receive or control either credential. The frontend runtime has `roles/run.invoker` on the engine, the engine has no unauthenticated Cloud Run invocation, and the frontend is the public application boundary. Local Compose continues to use only the application-level token without Cloud Run identity tokens.

The configured stable deterministic URLs are derived from service name, project number, and region. Cloud Run also reports valid hash-based `a.run.app` aliases. The deterministic frontend URL is used for `NEXTAUTH_URL` and the Google OAuth origin/callback; the deterministic engine URL is used for `ENGINE_BASE_URL` and `ENGINE_ID_TOKEN_AUDIENCE`.

Acceptance evidence includes a successful migration execution, Ready engine and frontend services, frontend health `200`, unauthenticated direct engine rejection `403`, correct IAM boundaries, and a final Terraform plan reporting `No changes`. Manual browser verification confirmed frontend loading, Google sign-in/callback, authenticated sessions, campaign workflow, chat, private engine invocation, Cloud SQL persistence, and OpenAI narration without blocking authentication or runtime errors.

## Phase 4 — Long-Term Memory

**Status: Complete — v1**

Implemented memory layers:

### Recent-Turn Memory

Recent conversation turns are included in model context for short-term continuity.

### Summary Memory

Older conversation history is periodically summarized and reused as compact context.

### Semantic Memory

Durable memory records can be retrieved by relevance.

The current semantic retrieval implementation is intentionally lightweight rather than using model-generated embeddings and a dedicated vector database. A future implementation may replace this with embedding vectors and PostgreSQL/pgvector or equivalent.

### Reflection Memory

The Memory Reflection Agent periodically converts conversation/game history into durable memory candidates.

### Memory Architecture

Memory logic is separated into dedicated agents/services rather than being embedded directly in the narrator or orchestrator.

Memory retrieval remains scoped to the authenticated campaign before entering model context.

## Phase 5 — Structured Agentic Game Pipeline

**Status: Complete**

Phase 5 established the core separation between:

```text
Player language
      |
      v
Action Parser Agent
      |
      v
Structured Action
      |
      v
Deterministic Tool / Rule Execution
      |
      v
Authoritative Result
      |
      v
Narrator Agent
      |
      v
Player-facing prose
```

### Action Parser Agent

The Action Parser converts natural-language player input into an explicit structured action.

As of commit `0219311`, the parser uses model-generated **typed structured output** rather than requesting arbitrary JSON and attempting to recover JSON blobs from generated text.

The parser now operates against a constrained context containing fields such as:

* Current location.
* Available exits.
* Nearby objects.
* Player inventory.
* Nearby NPCs.
* Relevant status flags.

This keeps intent interpretation focused on information needed to classify a player action rather than exposing an arbitrary serialization of the complete campaign state.

Player actions are represented by explicit action types rather than unrestricted strings.

Current parser-level concepts include actions such as:

* Observe.
* Move.
* Climb.
* Take.
* Drop.
* Wait.
* Talk.
* Use/interact.
* Attack.
* Unknown/ambiguous actions.

The deterministic fallback parser remains available when deterministic parsing is explicitly requested.

### Parser Safety Boundary

Player input is no longer allowed to directly request engine-level world manipulation and have that interpreted as a valid player action.

Requests corresponding to privileged operations such as spawning NPCs, directly recording world facts, or directly advancing world state are classified as unsupported/ambiguous rather than being exposed as player capabilities.

This establishes an important distinction:

```text
Player actions != engine/director actions
```

Future game-management or Director capabilities should use a separate authority/tool surface.

### Parser Diagnostics

Structured model-call failures now emit diagnostic logging containing relevant operational information such as:

* Model.
* Message size.
* Memory-context size.
* Exception type.
* Exception message.
* Stack information.

The user-facing/parser exception remains abstracted rather than exposing provider internals directly.

### Tool Executor

The deterministic Tool Executor receives parsed actions and performs game-state mutations.

The executor currently supports a v1 game-state model containing concepts including:

* Player location.
* Inventory.
* NPC state.
* Game clock/time.
* Persistent facts.

The tool layer is intentionally distinct from narration.

### Tool Registry

Tool registration and dispatch are abstracted through a registry.

The engine supports:

* Local tools.
* MCP-backed tools.
* Hybrid execution.

Hybrid execution permits MCP-backed behavior while retaining local implementations as fallback where configured.

### MCP Client

The engine contains an MCP client abstraction with support for multiple transports.

MCP infrastructure is therefore considered implemented.

A dedicated Haunted Halls domain MCP-server ecosystem remains future work and should not be confused with the MCP client/registry infrastructure already present.

### Phase 5 Completion Note

Commit `0219311` closes the remaining reliability gap in structured player-action parsing by replacing permissive free-form JSON parsing with a validated schema and by adding diagnostics for provider/schema failures. The commit changes 13 files across agents, model access, prompts, rules, orchestration, schemas, tool execution, and tests.

## Phase 7A — World Authority Foundation

**Status: Complete**

Phase 7A establishes a separate deterministic privileged world-authority boundary that is intentionally distinct from normal player action execution.

Implemented as part of this milestone:

* A dedicated typed world-action contract in `app/schemas/world.py` covering `move_npc`, `set_npc_status`, `advance_clock`, and `record_fact`.
* A deterministic `WorldAuthorityExecutor` in `app/services/world_authority.py` that validates canonical IDs and applies copy-on-write state transitions.
* Structural separation between player authority and privileged world authority: player `ActionType`/`ParsedAction` remain player-scoped, and the player `ToolExecutor` no longer exposes the legacy `spawn_npc` and `record_fact` hooks.
* Player `WAIT` semantics remain intact and continue to route through the deterministic clock tick path.
* State transitions include precise `state_delta` payloads, strict bounds checks, successful idempotent no-op handling, and semantic validation that leaves authoritative input state unchanged on failure.

This milestone intentionally deferred Director model behavior and normal chat
orchestration integration until the approved Phase 7B sequence.

## Phase 7B — Director rollout

**Status: Complete (7B1, 7B2, and 7B3)**

Engine issue #3 (idempotent chat turns) shipped ahead of 7B3 and provided the
stable `Idempotency-Key` claim/replay/completion boundary that 7B3 integrates
against without weakening its owner/fingerprint/concurrency semantics.

The rollout was delivered in sequence:

1. **7B1 — Director Contract & Proposal Boundary:** a bounded authoritative
   Director input projection and a strict zero-or-one proposal contract that
   reuses the Phase 7A `WorldAction` vocabulary. The projection is
   deterministic and non-mutating; malformed authoritative fields fail
   explicitly.
2. **7B2 — Model-backed Director Agent:** bounded structured proposal
   generation using the Director model policy, a compact prompt, a small
   output budget, deterministic request estimation, and provider usage
   returned to its caller.
3. **Issue #3 — Idempotent Chat Turns:** the engine-side idempotency boundary
   for valid `Idempotency-Key` chat requests. Keys are scoped to the
   authenticated user, claimed durably before turn execution, and completed
   responses are replayed without rerunning game or model work.
4. **7B3 — Director Orchestration Integration:** the Director is now invoked
   inside `ChatOrchestrator.handle_chat` immediately after the authoritative
   player result/state and before narrator scene construction. A validated
   `decision="act"` proposal executes exactly once through
   `WorldAuthorityExecutor`; `decision="none"` performs no world mutation.
   A successful world-state change persists a dedicated `world_action_executed`
   event (plus `game_state_updated` when state actually changed) and a
   semantic failure persists a dedicated `world_action_failed` event while
   leaving authoritative state unchanged. Narrator scene projection and memory
   maintenance both ground in the resulting final authoritative state, not the
   pre-Director state. A disabled provider path (no OpenAI key) skips the
   Director and privileged world execution entirely and preserves prior
   deterministic/stub chat behavior. Director provider/schema failures
   (`DirectorProviderError`, `DirectorProposalOutputError`) log sanitized
   operational context and fail the request safely; the existing chat
   transaction boundary rolls back player/world state, turns/events, model
   telemetry, and any in-progress idempotency claim from that failed attempt,
   so a retry with the same `Idempotency-Key` executes normally.

Web issue `jtesolin/haunted-halls#21` may run independently of this engine
work, and #22 follows that cleanup. The 7B rollout does not include combat,
doors/locks/keys, autonomous NPC simulation beyond the one Director proposal
per player turn, generic perception, narrator validation/retry,
semantic-memory redesign, content/engine separation, broad E2E expansion, or
D7 observability work. No new `WorldAction` types (for example `spawn_npc`)
were introduced in 7B3.

## Phase 8 — Narrative Progression & Character Systems

**Status: Active. 8A Story / Quest, 8B progression, 8C ability/check domain
foundation, and 8E1 evaluation-harness foundation are complete. With the
8A/8B/8C prerequisites complete, 8D Narrative Director Expansion is the next
main gameplay milestone; ongoing Phase 8 work is tracked by #52.**

### 8A — Story / Quest Model

A deterministic story/quest domain foundation lives in `app/game/story.py`
and `app/schemas/story.py`. The governing invariant: player prose is never
evidence of quest progression. Progression is driven only by a small typed
`StorySignal` vocabulary (`room_entered`, `npc_spoken_to`, `item_acquired`,
`fact_recorded`) derived from already-authoritative Phase 6/7 outcomes, never
from `ParsedAction.raw_text` or model output.

Static quest/objective definitions are immutable content data, kept separate
from mutable per-campaign progress. `ensure_story_state()` provides/normalizes
the `story` progress namespace in authoritative campaign state, safely
resetting any single quest whose persisted progress is malformed or
internally inconsistent to that quest's fresh starting progress without
touching other campaign-state namespaces or granting unearned completion.
`apply_story_signal()` deterministically advances at most one currently
active, in-order objective per matching signal, completes a quest once its
final objective completes, is idempotent for signals that match an
already-completed objective, and returns a typed `StoryProgressionResult`
(never a bare boolean) that also distinguishes not-applicable signals from
malformed/unrecognized ones. It does not persist events itself.

Each progression trigger condition (`signal_type`, `match_value`) is
unique across the static story definition collection, enabling payload-level
replay idempotence without event identity; richer repeated or shared triggers
require future authoritative event identity/consumption semantics.

One development quest, "The Library's Whisper", proves the model using only
existing canonical content: enter the `library`, speak to `library_ghost`,
then acquire the `old_book`.

8A intentionally does not wire story evaluation into
`ChatOrchestrator.handle_chat()` or expand the Director contract; that
integration is deferred to a later Phase 8 milestone.

## Phase 8B — Character Progression Model

**Status: Complete**

Phase 8B (`jtesolin/haunted-halls-engine#54`, tracked by the Phase 8 roadmap
`jtesolin/haunted-halls-engine#52`) adds a persistent, deterministic
character-progression domain foundation that can later power Phase 8C
ability/check resolution, without designing the system around combat.

* `app/game/character_progression.py` normalizes and mutates a
  `state["player"]["progression"]` namespace with four stable, exploration/
  narrative-oriented tracks (`investigation`, `resolve`, `rapport`,
  `occult`), each bounded `0`-`10` with a deterministic zero-point default and
  no random stats.
* `ensure_character_progression_state()` is a lazy normalization helper: it
  safely defaults legacy campaign state with no progression namespace,
  re-derives the persisted version marker, drops arbitrary/unknown track
  ids, resets out-of-range or malformed rank values to the safe zero-point
  default rather than granting elevated ranks, and
  dedupes/filters unlocked-ability ids to non-empty strings, all without
  regenerating unrelated `items`/`npcs`/`clock`/`facts` state.
* `grant_progress()` and `unlock_ability()` return structured
  `ProgressionGrantResult` / `AbilityUnlockResult` outcomes (see
  `app/schemas/character_progression.py`) reporting success/failure,
  whether a domain mutation occurred, prior/new points, cap status, and
  idempotent duplicate-unlock detection. Invalid grants, unknown track ids,
  and invalid ability ids are rejected without mutation; duplicate ability
  unlocks are a successful no-op.
* No database migration was required; progression persists as part of the
  existing authoritative campaign-state JSON document, consistent with
  current persistence architecture.
* This milestone intentionally does not implement ability/check resolution
  (Phase 8C), does not touch `ChatOrchestrator`, the Director, or
  `WorldAuthorityExecutor`, and preserves the existing `CharacterInfo` /
  `CharacterList` API DTOs in `app/schemas/character.py` unchanged.

## Phase 8C — Deterministic Ability and Check System

**Status: Complete — domain foundation only (issue #59; roadmap #52)**

Phase 8C adds the engine-owned deterministic ability-availability and
check-resolution foundation on top of Phase 8B progression. Its immutable,
read-only static domain definitions are separate from campaign state and
define four canonical abilities, each requiring two progression points in its
backing track:

* `keen_eye` → `investigation`
* `steady_nerves` → `resolve`
* `read_the_room` → `rapport`
* `occult_insight` → `occult`

Ability ownership persists in the player character's progression state.
Availability requires all three authoritative conditions: a known ability
definition, persisted ownership, and sufficient normalized progression in the
ability's backing track. The shared progression scale remains bounded from
`0` through `10`; checks resolve deterministically as
`track_points >= difficulty`, so equality succeeds. Structured availability
and check results expose their status and, for resolved checks, the margin.

Malformed persisted progression or ownership data is normalized safely and
cannot escalate ownership or ability availability. Evaluation and resolution
are pure reads with no campaign mutation, RNG/dice, model judgment, combat,
or database migration.

This is **8C DOMAIN FOUNDATION only**. It is **not** integrated into the
Action Parser, player `ToolExecutor`, Director, Narrator, `ChatOrchestrator`,
or story progression.

## Authentication and Authorization

**Status: Complete for the deployed public custom-domain BFF and private engine boundary**

The following are implemented:

* Google OIDC login through Next.js/Auth.js.
* Internal FastAPI service authentication.
* Internal user resolution.
* Stable internal user identity based on OIDC issuer + subject.
* Authenticated user-context propagation from the BFF.
* Campaign ownership persistence.
* Campaign-level authorization.
* Child-resource authorization inherited through campaign ownership.
* Protection against browser-controlled identity fields.
* Authorization before chat/model/memory persistence.
* Same behavior for missing and unauthorized campaign resources.
* Legacy unowned campaigns excluded from normal authenticated APIs.

FastAPI is the domain-authorization boundary. The browser does not provide authoritative ownership or user identity. The deployed path has been verified through the public custom-domain BFF and the private engine boundary.

## Status Maintenance Checklist

When a repository change affects architecture, behavior, roadmap, or phase progress, update this file in the same PR.

Use this checklist:

- Update **Last synchronized (planning memory sync)** date only when the user explicitly confirms that planning memory was synchronized from git.
- Update **Engine baseline** and **Web baseline** commits only during the same intentional synchronization/re-baselining pass that updates **Last synchronized (planning memory sync)**.
- Update phase sections whose completion state changed.
- Add or revise bullet points under **Completed Work** to reflect shipped behavior.
- Update architecture diagrams or flow descriptions when boundaries or data flow changed.
- Record major guardrail, auth, memory, parser, tool, or orchestration changes.
- Keep completed historical phases concise summaries of what is implemented.
- Keep the current active phase detailed when implementation guidance is needed.
- Keep future phases relatively high-level until they become active.
- When an active phase is completed, collapse detailed planning into a concise completed-state summary before expanding the next active phase.
- Do not let this file become a chronological development diary or changelog.
- Remove stale planning detail when it no longer helps active implementation.
- Keep entries factual and implementation-grounded; avoid speculative roadmap details unless clearly marked as future work.
- Remove or correct outdated statements so this document remains the single source of truth.

## Current Agents

### Narrator Agent

**Implemented**

Produces player-facing narrative using authoritative context and game results.

### Action Parser Agent

**Implemented**

Converts player language into validated structured intent.

### Memory Summarizer Agent

**Implemented**

Produces compact historical summaries.

### Memory Reflection Agent

**Implemented**

Extracts durable facts/memories from longer-running play.

### Director Agent

**Implemented (7B1, 7B2, 7B3)**

The engine exposes a bounded, typed authoritative context and a strict proposal
contract representing no action or one existing typed `WorldAction`. The
model-backed Director consumes only that projection and returns a validated
advisory proposal plus provider usage metadata; it never mutates or repairs
authoritative state itself. The Director is invoked inside the normal
authoritative chat turn after the player's own result/state and before
narrator scene construction. A `decision="act"` proposal executes exactly once
through `WorldAuthorityExecutor`, which remains the sole deterministic
execution boundary for privileged world actions; `decision="none"` performs no
world mutation. A disabled provider path skips the Director and privileged
world execution entirely.

## Current Persistence

**Persistence Architecture:**

* **SQLite** — Default lightweight database for direct local development and pytest.
* **PostgreSQL via Docker Compose** — Used by the local containerized Compose application stack; persists across normal stack restarts; removed only by explicit `docker compose down -v` or `make docker-reset-db` from `../haunted-halls`.
* **Cloud SQL PostgreSQL 16** — Deployed managed production database. The instance, `haunted_halls` database, and `haunted_halls_app` user exist in GCP; the current Alembic schema has been applied, and the Cloud Run engine is connected to it.

Persisted concepts include:

* Internal users.
* Campaigns.
* Characters.
* Turns.
* Game events.
* Model requests.
* Summaries.
* Memories.

All three databases share the same Alembic migration contract. SQLite and Compose PostgreSQL are validated to support current migrations and application behavior; Cloud SQL is the deployed managed production database with the current schema applied.

## Testing

Testing is part of normal feature implementation rather than a future standalone phase.

Existing test coverage includes areas such as:

* Chat behavior and orchestrator flow.
* Action parsing (typed structured outputs, deterministic fallback, diagnostics).
* Model client behavior and guardrails.
* Tool execution and deterministic player actions (MOVE, TAKE, DROP, OBSERVE, WAIT, TALK, USE/INTERACT).
* Tool registry behavior and MCP client abstraction.
* Narrator scene projection and grounded campaign initialization.
* Campaign state integrity and malformed-state failure handling.
* Internal service authentication and user-context validation.
* Campaign ownership and domain authorization boundaries.
* Database migrations (SQLite and PostgreSQL compatibility).
* Full-stack browser E2E (E2E-1): Chromium Playwright suite in `haunted-halls` exercising the real frontend/BFF → engine → PostgreSQL path with a guarded loopback-only NextAuth test seam and deterministic engine stub replies.

# Active Development

## Phase 6 — Deterministic World Model and Game Rules

**Status: Complete**

The infrastructure necessary to build the game now exists.

The main limitation is no longer agent architecture. It is the richness of the deterministic game world.

The goal of Phase 6 was to transition from:

> AI interprets actions and modifies a small generic state object.

to:

> The engine owns a modeled world with explicit entities, relationships, rules, and valid state transitions; AI interprets intent and narrates authoritative outcomes.

## Phase 6A — Rooms and World Graph

**Status: Complete**

Phase 6A introduced explicit room entities, named exits, and room-id based player location. Movement is now validated only through the deterministic world graph, and narration is grounded in authoritative movement outcomes.

## Phase 6B — Item Model

**Status: Complete**

Phase 6B replaced string-only inventory mutations with an explicit item entity model and deterministic ownership transfer rules.

Implemented behavior includes:

* Explicit item entities with stable IDs, names, descriptions, portability, quantity, tags/aliases, and lightweight properties metadata.
* Canonical development items distributed across deterministic rooms, including portable and non-portable examples.
* Authoritative item location/ownership tracking (`room:<room_id>` or player inventory location) as the source of truth.
* Player inventory represented as item-ID references derived from authoritative item ownership.
* Deterministic TAKE validation (existence, unambiguous resolution, room presence, portability, already-owned rejection).
* Deterministic DROP validation (existence, unambiguous resolution, player ownership, valid current room).
* Structured success/failure result payloads for narration grounding, including machine-readable error codes and item transfer metadata.
* Persistence compatibility through campaign state serialization/reload of explicit item entities and ownership.
* Registry/MCP boundary protection for player TAKE/DROP so item validation cannot be bypassed by transport selection.
* New campaigns start the player with 3 randomly selected items from a fixed exploration-gear pool, assigned once at first-state creation and persisted from then on.
* OBSERVE is a deterministic, non-mutating tool action that returns current room/exit/item/inventory context for narration instead of being treated as an unmatched-tool failure.
* TAKE/DROP resolve name/alias/tag matches scoped to the actionable context first (current room for TAKE, inventory for DROP), falling back to a global lookup only to produce a precise not-found/wrong-location error — preventing false `ambiguous_item` results from same-tag items elsewhere in the world.

## UI-1 — Mobile chat layout cleanup

**Status: Complete — merged**

Responsive/mobile polish only: compact mobile header, no mobile horizontal or viewport overflow, reduced mobile layout chrome and padding, preserved sliding sidebar, and preserved desktop behavior. This did not change engine or gameplay architecture.

## E2E-1 — Authenticated Playwright Full-Stack Test Foundation

**Status: Complete (frontend `haunted-halls` issue #25 / PR #26)**

Authenticated full-stack end-to-end browser test foundation using Playwright Chromium across the real integrated application path:

```text
Chromium UI
    ↓
NextAuth session
    ↓
Next.js BFF
    ↓ internal service auth + user context
FastAPI Engine
    ↓
PostgreSQL
```

Key architectural properties:

* **Real vs. controlled boundaries**: Exercises the real frontend, Next.js BFF, internal service authentication, trusted internal user resolution/propagation, FastAPI engine, and PostgreSQL database. External dependencies are controlled: Google OAuth UI is replaced by a guarded test seam, and OpenAI is disabled (`AI_ENABLED=false`) for deterministic engine stub responses.
* **Guarded loopback-only NextAuth test seam**: `lib/e2e-auth.ts` requires both `E2E_AUTH_ENABLED=true` and a loopback `NEXTAUTH_URL` (`http://localhost:*` or `http://127.0.0.1:*`). It fails closed (`E2EAuthConfigurationError`) if enabled on any non-loopback origin, preventing activation in production.
* **Synthetic identity with engine-owned resolution**: The seam uses a fixed synthetic user (`playwright-e2e@example.com`) and never accepts browser-supplied identities. It resolves a real engine-owned internal user through `POST /internal/auth/users/resolve`, identical to the Google OAuth flow.
* **Stale session rejection**: E2E-issued JWTs are tagged, and the NextAuth JWT callback re-checks that the guarded seam remains active before reusing them.
* **Test suite coverage**: Chromium Playwright specs cover unauthenticated auth shell, authenticated initial campaign creation with deterministic Entry Hall opening, chat round trip with input refocus, message/narrator persistence across page reload, multi-campaign lifecycle (creation, isolated switching, deletion), and a mobile viewport smoke test.
* **CI status check**: Runs in frontend CI as `Frontend / E2E` against an isolated Compose stack (`docker-compose.e2e.yml`).

## Phase 6C — NPC Model

**Status: Complete — Phase 6C1 entity foundation complete**

Phase 6C1 establishes authoritative persistent NPC entities and scene grounding:

* Stable ID.
* Player-facing name and concise authoritative description.
* Current location.
* Lightweight status and disposition.
* Aliases and tags for deterministic resolution.
* Fixed development NPC content in `entry_hall`, `library`, and `crypt` for fresh campaign state only.
* Legacy `room` records normalized to `location` without backfilling canonical NPCs into established campaigns.
* Structured nearby-NPC context supplied to the action parser and authoritative nearby-NPC projections supplied by OBSERVE and MOVE results.
* Narrator instructions that prohibit invented NPC presence, movement, or state changes.

Future NPC movement, interaction rules, and state changes remain deterministic gameplay work. NPC behavior must remain independent of narrator prose.

## Phase 6D — Rule-Based Player Actions

**Status: Complete — deterministic player actions and item interactions implemented**

Phase 6D1 introduces deterministically-authoritative TALK handling and explicit rejection semantics for deferred actions:

* Deterministic TALK is now a local player-authority action with room-scoped NPC presence checks.
* TALK resolves to the authoritative nearby NPC using stable ID, player-facing name, aliases, and tags already established by the Phase 6C resolver.
* Successful TALK returns the chosen NPC identity and authorizes narration without mutating NPC state or world state.
* Rejections are explicit and structured: `invalid_current_location`, `npc_not_found`, `npc_not_present`, and `ambiguous_npc`.
* ATTACK is explicitly rejected with `combat_not_supported` and no mutation or damage state.
* USE/INTERACT deterministically resolve accessible items by ID, name, aliases, and tags; OPEN/CLOSE old books and LIGHT/EXTINGUISH candles are authoritative local state transitions.
* Ignition requires an inventory-held canonical ignition source (`box_of_matches` or `tinderbox`); no consumable counts, durability, or burn time is modeled.
* Existing campaign items receive new canonical capabilities while retaining persisted dynamic property values; unselected starter items are not backfilled.
* Existing deterministic MOVE / TAKE / DROP / OBSERVE / WAIT behavior remains in place and remains the authoritative baseline.

Combat remains explicitly deferred until the necessary mechanics are designed.

## Phase 6E — Narrator Grounding

**Status: Complete — Phase 6E1 authoritative scene projection, Phase 6E2 grounded campaign initialization, and malformed persisted-state integrity hardening complete**

Strengthen the narrator contract so narration reflects authoritative results rather than creating state changes.

Conceptually:

```text
Action:
  TAKE brass_key

Game result:
  success
  item_id: brass_key
  moved_from: library_table
  moved_to: player_inventory

Narrator:
  "You lift the tarnished brass key from the dusty table..."
```

If the engine rejects an action, narration must describe the rejection rather than silently overriding it.

Phase 6E1 replaces raw campaign-state exposure to the narrator with a deterministic scene projection:

* `app/game/narrator_scene.py` builds a `NarratorSceneContext` from authoritative campaign state: current room (id/name/description), authoritative world-graph exits, room-scoped nearby items, inventory items (projected separately), and room-scoped nearby NPCs (excluding absent/off-room NPCs), all in deterministic order.
* Narrator-facing item projection (`NarratorItem`) exposes only player-observable identity/description and observable dynamic state (`is_open`, `lit`); internal capability/hook properties (`openable`, `lightable`, `ignition_source`, `opens`, and other arbitrary internal properties, including the brass key's latent `opens: cellar_door` hook) are never exposed.
* Normal chat narration now sends `NarratorSceneContext` instead of raw `campaign_state` JSON; the existing structured `ToolExecutionResult` continues to be sent unchanged.
* The narrator prompt now states explicit authority precedence: current tool result, current scene context, parsed intent, then memory/summary/recent turns as historical-only context, then player wording (intent only, never proof of success).
* Regression tests cover scene projection (room/exits/items/inventory/NPCs, deterministic ordering, off-room/absent exclusion), item-state redaction (observable state exposed, capability/hook/unknown properties never exposed), the updated narrator contract (scene context replaces raw state, tool result still authoritative for success/failure), and conflicting historical context (stale prior narration/memory cannot override current NPC/room presence, failed tool results remain authoritative despite player wording).
* Phase 6E2 grounds campaign creation against the same authoritative narrator projection used for normal turns:
  * `app/game/campaign_state.py` is the single shared `build_fresh_campaign_state()` initializer; both new-campaign creation and `ToolExecutor`'s legacy missing-state fallback reuse it, with no duplicated starter-inventory/world-initialization logic.
  * Campaign creation now builds the authoritative `initial_state` once, projects it into a `NarratorSceneContext`, generates the opening and title narration from that exact projection, and persists that same `initial_state` via `create_campaign(..., state=...)` — no reroll of starter inventory occurs between opening generation and persistence.
  * The AI-disabled stub path builds and persists the same authoritative fresh state and derives its deterministic opening from the authoritative Entry Hall projection instead of inventing unrelated scene details; it never calls a model.
  * `NarratorAgentInput.campaign_state` (the transitional raw-state compatibility path) is removed; the narrator now only ever receives `NarratorSceneContext`, including for campaign-opening/title generation.
  * The first player action after campaign creation loads the persisted initial state; `random_starting_inventory_items()` is not called again for an already-initialized campaign.
  * Regression tests cover the shared initializer, authoritative persisted state for both AI-enabled and AI-disabled campaign creation, grounded opening/title narrator contracts (scene context present, no raw `Campaign state:` message), and first-action inventory equivalence (no reroll).
* Malformed persisted campaign state is now a hard failure rather than a silent reroll: `ToolExecutor._state_from_text()` still calls `build_fresh_campaign_state()` for the legitimate legacy/missing-state sentinel (empty/absent state, or the literal string "No campaign state yet."), but persisted state that is non-empty/non-sentinel and either fails to decode as JSON or decodes to a non-dict top-level value raises `InvalidCampaignStateError` (`app/game/campaign_state.py`) instead of silently regenerating a fresh campaign.
  * `Orchestrator.handle_chat()` validates persisted campaign state immediately after load (before parser/tool/narrator processing) via shared `validate_persisted_campaign_state_json()`, catches `InvalidCampaignStateError`, logs a sanitized operational error (`owner_user_id`, `campaign_id`, `turn_id`, error type — never the raw persisted payload), and raises an HTTP 500 without invoking deterministic tools against replacement state, persisting replacement state, or continuing to narrator generation. The existing DB session transaction rollback-on-exception behavior ensures the failed request leaves no partial player turns/events/state changes committed.
  * Regression tests cover: fresh-state initializer still used only for legitimate missing/sentinel state; valid dict JSON still loads/normalizes; malformed JSON and valid non-object JSON (`[]`, string, number, `null`) raise the focused error without calling `build_fresh_campaign_state()`; pre-parser integrity validation across all parse statuses (including ambiguous/invalid parses); and an end-to-end orchestrator/persistence test proving a corrupted campaign fails the chat request with a sanitized 500, does not invoke the narrator, and leaves no new turns/events/state-replacement committed.

## Pre-Phase 7 Context Hardening

**Status: Complete**

Durable repository context was hardened before Phase 7 through the stable architecture/invariants companion and canonical review-triage policy. Automated review triage can rely on these repository artifacts and the current implementation issue rather than conversational history. This documentation-only work does not start or implement Phase 7A.
# Future Work

## Director Agent

**7B1, 7B2, and 7B3 implemented (Phase 7B complete)**

The Director has a bounded authoritative input projection, a strict
zero-or-one proposal boundary over existing typed world actions, a
model-backed advisory proposal generator, and normal-chat orchestration
integration that executes at most one validated proposal per player turn
through `WorldAuthorityExecutor`.

Potential future Director capabilities beyond the current one-proposal-per-turn
boundary:

```text
unlock_exit
lock_exit
spawn_npc
move_npc
set_npc_goal
start_event
complete_event
reveal_clue
set_world_flag
advance_story_beat
```

These capabilities must remain separate from normal player-action authority.

## Content / Engine Separation

Eventually separate game content from generic engine implementation.

Possible structure:

```text
content/
  haunted_halls/
    world.yaml
    rooms.yaml
    items.yaml
    npcs.yaml
    lore.yaml
```

or equivalent database-backed definitions.

Long term, this would allow Haunted Halls to function as one game/content package running on a reusable AI MUD engine.

## Semantic Memory Upgrade

Potential future replacement for the current lightweight relevance mechanism:

```text
OpenAI embedding model
        |
        v
embedding vectors
        |
        v
PostgreSQL + pgvector
        |
        v
semantic memory retrieval
```

This should be driven by an observed retrieval-quality or scaling need rather than implemented preemptively.

## Production Deployment

Production runtime and deployment automation are complete through D6. Terraform owns non-image Cloud Run configuration, while GitHub Actions CD owns application image revisions. The build → migration → deploy safety contract remains intact: a successful migration is required before the application rollout.

### D5 — GitHub Actions CD

**Status: Complete — engine and frontend CD production verified**

* **D5A — Complete.** Terraform and CD ownership are separated: Terraform owns Cloud Run non-image configuration, while CD owns deployed image revisions. Image fields are ignored by Terraform to prevent image drift. `hh-frontend-deployer` and `hh-engine-deployer` have resource-scoped deployment IAM; Artifact Registry writer access is repository-scoped; runtime `serviceAccountUser` relationships are narrowly scoped; WIF is restricted to each repository's `deploy.yml` on `main`; and no long-lived service-account keys are used.
* **D5B — Complete — production verified.** `.github/workflows/deploy.yml` deploys after a successful `Engine CI` push run on `main`, or manually through `workflow_dispatch` from `main`. The deployment pipeline is:

  ```
  successful Engine CI on main
      ↓
  Engine Deploy
      ↓
  GitHub OIDC / WIF
      ↓
  cached Buildx image build
      ↓
  immutable SHA tag + digest
      ↓
  migration job image update/verification
      ↓
  migration execute --wait
      ↓
  engine image rollout
      ↓
  Ready + digest + private 403 verification
  ```

  The workflow resolves an explicit deployment Git SHA, authenticates with GitHub OIDC/WIF as `hh-engine-deployer` (no long-lived keys), builds with cached BuildKit (`scope=haunted-halls-engine`), publishes a SHA tag and immutable digest, updates and verifies the migration job image, executes the migration with `--wait`, then updates the engine service image only after the migration succeeds. Post-deployment verification derives Ready status, the deployed image digest, and the service URL from a single `gcloud run services describe --format=json` snapshot, and confirms the unauthenticated private boundary returns HTTP 403. Production deployments are serialized with `cancel-in-progress: false`; Terraform owns non-image configuration; rollback is manual to a previous known-good image, and schema downgrade is not automatic.

  Earlier rollout testing exposed two workflow-verification defects (a wrong migration-job image field path and a fragile Ready-condition `gcloud` format filter). Both were corrected; neither produced incorrect production state.

  **Production acceptance evidence — Engine Deploy run `33786120964` (commit `e72f54d`):** migration execution `haunted-halls-migrate-p7wld` succeeded; engine revision `haunted-halls-engine-00003-f24` was deployed with 100% of traffic routed to it; Ready `== True`; the private unauthenticated boundary returned HTTP 403.
* **D5C — Complete — production verified.** The frontend deployment workflow uses `successful Frontend CI on main` → `Frontend Deploy` → GitHub OIDC/WIF → cached Buildx image → immutable digest → frontend Cloud Run image-only rollout → Ready + digest + public health verification.

### D6 — Custom Domain

**Status: Complete — canonical frontend domain production verified**

Canonical frontend: `https://haunted-halls.tesolin.us`

* Network Solutions remains the registrar; Google Cloud DNS is authoritative for `tesolin.us`.
* The Google ownership-verification TXT record is Terraform-managed and retained.
* A Cloud Run domain mapping exists for `haunted-halls.tesolin.us`, and Terraform manages `haunted-halls.tesolin.us. CNAME ghs.googlehosted.com.`
* Google-managed TLS is provisioned and healthy.
* `NEXTAUTH_URL` uses the custom domain, and Google OAuth is configured for its custom-domain origin and callback.
* `ENGINE_BASE_URL` and `ENGINE_ID_TOKEN_AUDIENCE` remain on the private deterministic engine `run.app` URL.
* Browser acceptance verified custom-domain loading, Google sign-in, existing-conversation loading, narrator response, and campaign create/delete.
* The old frontend `run.app` hostname is no longer the canonical user entry point. Redirecting the legacy hostname and eventually removing legacy OAuth entries remain deferred cleanup outside D6.

### D7 — Operability/observability

**Status: Future infrastructure roadmap item**

D7 remains planned. Detailed subphases will be defined when this work becomes active.

## Explicit Deferrals

The following remain explicitly deferred and are kept out of the completed Phase 7A/7B milestones unless a later dedicated issue explicitly promotes them:

* Combat mechanics and damage modeling.
* Doors, locks, key mechanics, and cellar progression.
* Autonomous NPC simulation beyond the explicit one-proposal-per-player-turn Director actions.
* Semantic memory redesign and vector database / pgvector integration.
* Content / engine separation.
* Generic perception framework.
* Narrator output validation and retry framework.
* Cross-browser and visual-regression E2E expansion.
* Production E2E testing against live Google OAuth or live OpenAI endpoints.
* Unrelated infrastructure and observability work (D7).

# Architectural Principles

The following should guide subsequent implementation.

### Code is authoritative

The repositories represent what is actually implemented.

Planning documents and conversations should be reconciled to the code rather than assuming that a previously generated Copilot prompt was implemented exactly as written.

### AI interprets; game systems decide

Use LLMs where ambiguity and language understanding are valuable.

Use deterministic systems where correctness and authoritative state matter.

### Narration does not own game state

The Narrator explains results.

It should not independently decide that an authoritative state transition occurred.

### Player authority and world authority are separate

Player input should map only to actions a player is permitted to attempt.

Administrative, world-building, and future Director capabilities must be exposed through a separate authority boundary.

### Persist meaningful state

Important world changes should be explicit and durable rather than recoverable only from narrative prose.

### Avoid infrastructure without a current need

PostgreSQL, vector databases, deployment infrastructure, additional agents, and MCP servers should be introduced when game requirements justify them.

# Current Milestone Summary

| Area                          | Status            |
| ----------------------------- | ----------------- |
| Next.js frontend/BFF          | Complete baseline |
| FastAPI engine                | Complete baseline |
| Persistent campaigns/chat     | Complete          |
| OpenAI Responses integration  | Complete          |
| Guardrails                    | Complete baseline |
| Narrator Agent                | Complete          |
| Action Parser Agent           | Complete          |
| Structured parser schema      | Complete          |
| Parser diagnostics            | Complete          |
| Tool Executor                 | Complete v1       |
| Tool Registry                 | Complete          |
| MCP client infrastructure     | Complete          |
| MCP/local hybrid execution    | Complete          |
| Recent-turn memory            | Complete          |
| Summary memory                | Complete          |
| Semantic memory               | Complete v1       |
| Reflection memory             | Complete          |
| Google OIDC                   | Complete          |
| Internal service auth         | Complete          |
| Internal user resolution      | Complete          |
| Campaign ownership/authz      | Complete          |
| SQLite local persistence      | Complete          |
| Explicit rooms/world graph    | Complete (Phase 6A) |
| Item entity model             | Complete (Phase 6B) |
| Rich NPC model                | Complete (Phase 6C1 foundation) |
| Rule-based world interactions | Complete (Phase 6D) |
| Narrator scene projection     | Complete (Phase 6E1) |
| Grounded campaign initialization | Complete (Phase 6E2) |
| Malformed campaign state hardening | Complete (issue #2 / PR #33) |
| Playwright E2E foundation     | Complete (E2E-1)  |
| World Authority Foundation    | Complete (Phase 7A) |
| Director Agent                | Complete (Phase 7B: 7B1, 7B2, issue #3, 7B3) |
| Domain MCP servers            | Future            |
| PostgreSQL local/CI compatibility | Complete       |
| Cloud SQL PostgreSQL foundation | Complete         |
| Vector database / pgvector    | Deferred          |
| GCP/Terraform foundation       | Complete          |
| Cloud SQL/Secret Manager       | Complete          |
| Cloud Run application deployment | Complete        |
| CI/CD deployment automation   | Complete — D5 engine and frontend CD production verified |
| Custom domain (tesolin.us)    | Complete (D6)     |

### Phase 8E1 — AI evaluation harness foundation

**Status: Complete (issue #55)**

* A provider-free eval harness lives under `evals/` and validates deterministic
  Director/Narrator agent behavior without modifying production gameplay
  authority (Director/Narrator agents, Director/WorldAction schemas,
  `ChatOrchestrator`, persistence, story/quest, or character-progression code
  are all untouched).
* Offline, zero-provider-call scenario evaluation is the default and the mode
  used in CI: it loads a small, version-controlled synthetic JSON scenario
  corpus, validates each fixture against the real production
  `DirectorInput`/`NarratorAgentInput` contracts, runs deterministic graders,
  and produces a stable aggregate report (JSON and human-readable) with raw
  provider/actual output excluded from serialization.
* An explicit opt-in `--live` path also exists for local developer use. It
  requires `AI_ENABLED=true` and a non-empty, non-whitespace
  `OPENAI_API_KEY`; credentials alone never enable it. Live runs reuse the
  same `DirectorAgent`/`NarratorAgent` contracts and the same `ModelPolicy`
  the production agents use, and populate `model_metadata` (target agent,
  selected model, token usage) without persisting raw provider content.
* Model-judged/subjective scoring remains explicitly deferred: a
  `SubjectiveGrader` protocol establishes the future extension boundary, but
  no LLM-judge grading is implemented in this phase.

# Next Step

Phase 7A — World Authority Foundation and Phase 7B — Director rollout (7B1,
7B2, issue #3, and 7B3) are complete. The Director now participates in the
normal authoritative chat turn end-to-end: it is invoked after the player's
own authoritative result/state, may execute at most one validated proposal
through the deterministic `WorldAuthorityExecutor`, and both narrator scene
projection and memory maintenance ground in the resulting final authoritative
state.

Phase 8A Story / Quest, 8B progression, 8C ability/check domain foundation,
and 8E1 AI evaluation-harness foundation (issue #55) are complete. With the
8A/8B/8C prerequisites complete, 8D Narrative Director Expansion is the next
main gameplay milestone. Ongoing Phase 8 work is tracked under roadmap issue
#52; consult that issue for the current sequence of Phase 8 milestones before
starting further Phase 8 work.
Tracking issue #43 (Phase 7B rollout planning) is closed and is not the
active source for future-work candidates. See **Explicit Deferrals** above
for the separate backlog of pre-Phase-8 candidate areas (for example
combat/damage, doors/locks/keys/cellar progression, further Director
capabilities, semantic-memory redesign, or D7 observability).

Phase 5 should be considered closed as of engine commit:

```text
0219311
fix: harden structured action parsing schema and add parser failure diagnostics
```
