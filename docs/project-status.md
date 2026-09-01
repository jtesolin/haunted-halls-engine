# Haunted Halls — Project Status

**Last synchronized (planning memory sync):** August 13, 2026
**Engine baseline:** `haunted-halls-engine` `main` at commit `0219311`
**Web baseline:** `haunted-halls` `main`

Synchronization metadata meaning:

- **Last synchronized (planning memory sync)** is the date when ChatGPT intentionally inspected the repositories and reconciled this status document against the implemented code.
- **Engine baseline** is the engine commit inspected during that synchronization pass.
- Normal implementation changes, Copilot-generated updates, and ordinary documentation edits do **not** update either field.
- Update these two fields together only during an explicit synchronization/re-baselining pass.
- Expected workflow:
      1. Implementation proceeds normally.
      2. `Last synchronized (planning memory sync)` and `Engine baseline` remain unchanged.
      3. During a future explicit synchronization, ChatGPT inspects commits after the recorded baseline.
      4. After reconciliation, both metadata fields are updated together to the new synchronized state.

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

**Status: Complete — D3D deployment-readiness contract implemented (production deployment deferred)**

The production-infrastructure foundation is now in the correct state for the planned D4/D5 rollout:

* Local containerization and Compose orchestration remain in place for developer workflows.
* PostgreSQL compatibility and migration readiness were validated and formalized in D3B through D3D.
* D4A completes the shared GCP and Terraform foundation for future Cloud Run and Cloud SQL deployment work.

The D2 baseline is no longer the current production-infrastructure status; it is superseded by the D3/D4 deployment-readiness work.

### D3B — PostgreSQL Compatibility

**Status: Complete — PostgreSQL compatibility validated**

The engine persistence layer now uses native SQLAlchemy Core statements instead of the legacy SQLite placeholder adapter, and the repository remains dialect-neutral across SQLite and PostgreSQL. Alembic upgrades are validated against both a fresh SQLite database and a fresh PostgreSQL database. SQLite remains the default lightweight local database, while PostgreSQL is supported for engine persistence and CI validation. Cloud SQL PostgreSQL was later deployed as managed infrastructure in D4B; the application schema has not yet been migrated onto it.

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

At the time D3C was implemented, the following production infrastructure was deferred because development remained local. The GCP/Terraform foundation, Cloud SQL, and production secrets management have since been established in D4A/D4B; application hosting/runtime deployment remains deferred to D4C:

Still deferred:

* Production hosting (Cloud Run application runtime).
* Deployment automation (D5).
* Production observability.
* Network/private-service deployment at scale.

Application hosting should proceed through the planned D4C/D5 phases.

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

D3D does not implement production deployment (D4/D5) or GCP infrastructure; it provides the contract and validation that those future phases will consume.

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

D4A intentionally did not create Cloud SQL or Cloud Run; Cloud SQL was added in D4B, while Cloud Run remains D4C work.

### D4B — Cloud SQL + Secret Manager

**Status: Complete — managed PostgreSQL and production secret foundation deployed**

D4B deploys the managed database and production secrets foundation on top of the D4A GCP/Terraform control plane. Implemented and verified in GCP:

* Cloud SQL PostgreSQL 16 instance in `us-east1`, edition `ENTERPRISE`, `db-f1-micro` shared-core development tier, ZONAL availability, 10 GB SSD storage with autoresize.
* Backups disabled and deletion protection disabled for the current development environment.
* Public IPv4 retained intentionally for Cloud SQL Auth Proxy/connector access, with `connector_enforcement = "REQUIRED"` and zero authorized networks, so only proxy/connector traffic is accepted.
* Application database `haunted_halls` and application user `haunted_halls_app` created; Alembic remains authoritative for application schema, which has not yet been migrated onto this instance (that migration job is D4C runtime work).
* Secret Manager containers created for `hh-database-url`, `hh-internal-engine-service-token`, `hh-nextauth-secret`, `hh-openai-api-key`, and `hh-google-client-secret`.
* Terraform-generated secrets (`hh-database-url`, `hh-internal-engine-service-token`, `hh-nextauth-secret`) use ephemeral/write-only handling so values are never stored in Terraform state; generated secret rotation is controlled through explicit version variables.
* Secret-level IAM grants `roles/secretmanager.secretAccessor` only to the runtime identities that require each secret; only the engine and migration runtime identities receive `roles/cloudsql.client`; the frontend runtime has no direct database access.
* `hh-openai-api-key` has been populated with the production OpenAI service-account key, stored as an enabled Secret Manager version (value not recorded here).
* `hh-google-client-secret` container and IAM foundation exist, but the production OAuth client secret is intentionally not populated yet; the production Google OAuth client will be created/configured during D4C once the production frontend URL exists.
* Final post-apply Terraform plan reported no drift.

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

## Authentication and Authorization

**Status: Complete for local development**

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

FastAPI is the domain-authorization boundary. The browser does not provide authoritative ownership or user identity.

## Status Maintenance Checklist

When a repository change affects architecture, behavior, roadmap, or phase progress, update this file in the same PR.

Use this checklist:

- Update **Last synchronized (planning memory sync)** date only when the user explicitly confirms that planning memory was synchronized from git.
- Update **Engine baseline** commit only during the same intentional synchronization/re-baselining pass that updates **Last synchronized (planning memory sync)**.
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

**Not currently implemented**

Earlier planning included a Director Agent, but the current engine architecture does not require one yet.

The Director should remain deferred until the deterministic world model contains enough meaningful game systems for a Director to control through explicit tools.

## Current Persistence

**Persistence Architecture:**

* **SQLite** — Default lightweight database for direct local development and pytest.
* **PostgreSQL via Docker Compose** — Used by the local containerized Compose application stack; persists across normal stack restarts; removed only by explicit `docker compose down -v` or `make docker-reset-db` from `../haunted-halls`.
* **Cloud SQL PostgreSQL 16** — Deployed managed remote database foundation (D4B). The instance, `haunted_halls` database, and `haunted_halls_app` user exist in GCP, but the application schema has not yet been migrated onto it; that migration job is D4C runtime work, so production application traffic is not using Cloud SQL yet.

Persisted concepts include:

* Internal users.
* Campaigns.
* Characters.
* Turns.
* Game events.
* Model requests.
* Summaries.
* Memories.

All three databases share the same Alembic migration contract. SQLite and Compose PostgreSQL are validated to support current migrations and application behavior; Cloud SQL is the deployed managed target that D4C's migration job will bring up to date.

## Testing

Testing is part of normal feature implementation rather than a future standalone phase.

Existing test coverage includes areas such as:

* Chat behavior.
* Action parsing.
* Model client behavior.
* Tool execution.
* Tool registry behavior.
* Internal service authentication.
* User-context validation.
* Campaign authorization.
* Ownership boundaries.

The Phase 5 parser-hardening commit includes corresponding test changes for the new structured-output and action semantics.

# Active Development

## Phase 6 — Deterministic World Model and Game Rules

**Status: In progress**

The infrastructure necessary to build the game now exists.

The main limitation is no longer agent architecture. It is the richness of the deterministic game world.

The goal of Phase 6 is to transition from:

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

## Phase 6C — NPC Model

**Next active subphase**

Expand NPCs into persistent entities with concepts such as:

* Stable ID.
* Name.
* Current location.
* State/status.
* Disposition.
* Relevant relationships.
* Goals or behavioral state where needed.

NPCs should eventually be able to move and change state independently of narrator prose.

## Phase 6D — Rule-Based Player Actions

Introduce deterministic validation for core actions such as:

* Movement.
* Take/drop.
* Use/interact.
* Open/close.
* Talk.
* Wait.
* Basic environmental interaction.

Combat should remain minimal or deferred until its required mechanics are understood.

## Phase 6E — Narrator Grounding

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

# Future Work

## Director Agent

**Deferred until after the world model**

A Director Agent becomes valuable when it can manipulate the world through constrained, authoritative tools.

Potential future Director capabilities:

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

Production deployment infrastructure is in progress.

Completed:

* D4A — GCP/Terraform foundation.
* D4B — Cloud SQL + Secret Manager.

Remaining:

* D4C — Cloud Run frontend/engine/migration runtime and first deployment.
* D5 — CI/CD deployment automation.
* D6 — Custom domain.
* D7 — Operability/observability.

The application is not currently hosted; no Cloud Run services, production URLs, or automated deployments exist yet.

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
| Explicit rooms/world graph    | Complete          |
| Item entity model             | Planned           |
| Rich NPC model                | Planned           |
| Rule-based world interactions | Planned           |
| Director Agent                | Deferred          |
| Domain MCP servers             | Future            |
| PostgreSQL local/CI compatibility | Complete       |
| Cloud SQL PostgreSQL foundation | Complete         |
| Vector database / pgvector    | Deferred          |
| GCP/Terraform foundation       | Complete          |
| Cloud SQL/Secret Manager       | Complete          |
| Cloud Run application deployment | Planned / Next infra phase |
| CI/CD deployment automation   | Planned           |

# Next Step

**Phase 6C — NPC Model**

This is the active gameplay implementation phase. Phase 6B is complete; see the Phase 6C section above for scope.

Separately, the next active infrastructure phase is **D4C — Cloud Run runtime + first deployment**.

Phase 5 should be considered closed as of engine commit:

```text
0219311
fix: harden structured action parsing schema and add parser failure diagnostics
```
