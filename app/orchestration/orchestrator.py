from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.agents.action_parser import ActionParseProviderError, ActionParserAgent
from app.agents.starter_abilities import StarterAbilityGenerator
from app.ai.model_client import ModelCallResult
from app.agents.director import (
    DirectorAgent,
    DirectorProposalOutputError,
    DirectorProviderError,
)
from app.agents.memory_reflection import MemoryReflectionAgent, MemoryReflectionInput
from app.agents.memory_summarizer import MemorySummarizerAgent, MemorySummarizerInput
from app.agents.narrator import (
    NarratorAgent,
    NarratorAgentInput,
    NarratorNarrativeReveal,
)
from app.core.config import settings
from app.db.models import TurnDBModel
from app.db.repositories import ChatRequestIdempotencyOwnershipError, Repository
from app.db.session import session
from app.game.character_names import generate_character_name
from app.game.campaign_state import (
    InvalidCampaignStateError,
    build_fresh_campaign_state,
    load_authoritative_campaign_state,
    validate_persisted_campaign_state_json,
)
from app.game.abilities import (
    generated_ability_definitions,
    validate_starter_ability_definitions,
)
from app.game.character_progression import ensure_character_progression_state, unlock_ability
from app.game.narrator_scene import build_narrator_scene_context
from app.game.narrative import NARRATIVE_CLUES
from app.game.progression_rewards import (
    ProgressionRewardOutcome,
    apply_quest_completion_rewards,
)
from app.game.story import apply_story_signal, derive_story_signal
from app.guardrails.input_validation import validate_chat_request
from app.guardrails.limit_errors import usage_limit_error
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.rate_limits import (
    validate_campaign_turn_limit,
    validate_daily_request_limit,
    validate_daily_token_limit,
    validate_project_request_limit,
    validate_project_token_limit,
)
from app.guardrails.token_budget import (
    TokenBudget,
    estimate_tokens,
    validate_request_budget,
)
from app.guardrails.usage_limits import UsageLimits
from app.memory.services import MemoryService
from app.schemas.campaign import CampaignCreateRequest, CampaignDetail, CampaignTurn
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    NarratorSceneContext,
    ParsedAction,
    ToolExecutionResult,
)
from app.schemas.character_progression import NarratorProgressionReward
from app.schemas.director import WorldActionProposal
from app.schemas.events import (
    ActionParsedPayload,
    GameEventType,
    GameStateUpdatedPayload,
    NarratorResponseCreatedPayload,
    PlayerMessageReceivedPayload,
    ToolExecutedPayload,
    ToolExecutionFailedPayload,
    WorldActionExecutedPayload,
    WorldActionFailedPayload,
)
from app.schemas.generated_abilities import StarterAbilityGeneration
from app.services.director_context import InvalidDirectorContextError, build_director_input
from app.services.tool_executor import ToolExecutor
from app.services.world_authority import WorldAuthorityExecutor
from app.schemas.world import RevealClueWorldAction


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectorStepResult:
    campaign_state: str
    current_turn_reveal: NarratorNarrativeReveal | None = None
    current_turn_reward: NarratorProgressionReward | None = None


_COMMIT_ATTEMPTS = 3
DEFAULT_CAMPAIGN_TITLE = "Haunted Halls"


@dataclass(frozen=True)
class _ChatSnapshot:
    """Read-only state captured in the first short transaction."""

    campaign_state: str
    recent_turns: list[dict[str, str]]
    memory_context: list[dict[str, str]]
    summary_text: str | None
    prior_user_turns: int


@dataclass
class _TurnOutcome:
    """Everything a successful turn will persist, collected in memory."""

    campaign_state: str
    events: list[tuple[str, GameEventType, str]] = field(default_factory=list)
    final_state: dict[str, Any] | None = None
    initial_state: dict[str, Any] | None = None
    reply: str = ""
    parsed_action: Any = None
    tool_result: Any = None

    def add_event(self, turn_id: str, event_type: GameEventType, payload: Any) -> None:
        # Serialize now: the payload may reference state that is mutated later.
        self.events.append((turn_id, event_type, payload.model_dump_json()))

    def set_state(self, turn_id: str, state: dict[str, Any]) -> None:
        self.final_state = copy.deepcopy(state)
        self.add_event(turn_id, "game_state_updated", GameStateUpdatedPayload(state=state))


@dataclass
class _MemoryWrites:
    summary_text: str | None = None
    reflection_candidates: list[Any] = field(default_factory=list)


class ChatOrchestrator:
    # -- durable usage ledger -------------------------------------------------
    # Every model call is reserved (counted at its estimate, under the global
    # budget lock) and finalized in its own short transaction, so paid usage
    # survives any later failure and no transaction spans a model await.

    def _reserve_model_call_sync(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
    ) -> str:
        validate_request_budget(estimated_input_tokens, max_output_tokens)
        request_id = f"req_{uuid4().hex}"
        with session(lock_budget=True) as db:
            validate_daily_token_limit(
                db, owner_user_id, estimated_input_tokens, max_output_tokens
            )
            validate_project_token_limit(db, estimated_input_tokens, max_output_tokens)
            db.reserve_model_request(
                request_id=request_id,
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                agent_name=agent_name,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=max_output_tokens,
            )
        return request_id

    async def _reserve_model_call(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(self._reserve_model_call_sync, **kwargs)

    def _finalize_model_call_sync(self, request_id: str, **fields: Any) -> None:
        with session() as db:
            db.finalize_model_request(request_id, **fields)

    async def _finalize_model_call(
        self,
        request_id: str,
        *,
        started_at: float,
        success: bool,
        failure_reason: str | None = None,
        usage: Any = None,
    ) -> None:
        fields: dict[str, Any] = {
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "success": success,
            "failure_reason": failure_reason,
        }
        if usage is not None:
            fields.update(
                actual_input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_input_tokens=usage.cache_write_input_tokens,
                actual_output_tokens=usage.output_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                actual_total_tokens=usage.total_tokens,
            )
        try:
            await asyncio.to_thread(self._finalize_model_call_sync, request_id, **fields)
        except Exception:
            # The reserved row keeps counting at its estimate, which is conservative.
            logger.error("model_usage_finalize_failed request_id=%s", request_id, exc_info=True)


    def _estimate_payload_tokens(self, payload: object) -> int:
        payload_obj = cast(Any, payload)
        payload_json = (
            payload_obj.model_dump_json(exclude_none=True)
            if hasattr(payload_obj, "model_dump_json")
            else str(payload_obj)
        )
        return estimate_tokens(payload_json)

    def __init__(self) -> None:
        self.action_parser_agent = ActionParserAgent()
        self.narrator_agent = NarratorAgent()
        self.memory_summarizer_agent = MemorySummarizerAgent()
        self.memory_reflection_agent = MemoryReflectionAgent()
        self.tool_executor = ToolExecutor()
        self.director_agent = DirectorAgent()
        self.world_authority_executor = WorldAuthorityExecutor()
        self.starter_ability_generator = StarterAbilityGenerator()

    # -- campaign creation ------------------------------------------------------

    def _begin_campaign_creation(self, owner_user_id: str) -> None:
        """Short transaction: validate, check request caps, count the request."""
        with session(lock_budget=True) as db:
            self._validate_campaign_creation(db, owner_user_id)
            validate_daily_request_limit(db, owner_user_id)
            validate_project_request_limit(db)
            db.add_usage_request(owner_user_id, "campaign_create")

    def _commit_campaign_creation(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        campaign_name: str,
        initial_state: dict[str, Any],
        assistant_turn_id: str,
        opening: str,
    ) -> tuple[str, TurnDBModel]:
        for attempt in range(_COMMIT_ATTEMPTS):
            try:
                with session() as db:
                    db.lock_internal_user(owner_user_id)
                    # Authoritative campaign-count check, atomic with the insert.
                    self._validate_campaign_creation(db, owner_user_id)
                    db.create_campaign(
                        campaign_id=campaign_id,
                        owner_user_id=owner_user_id,
                        name=campaign_name,
                        description="AI-created campaign",
                        state=initial_state,
                    )
                    character_id = db.ensure_campaign_character(
                        campaign_id, generate_character_name()
                    )
                    assistant_turn = db.create_turn(
                        turn_id=assistant_turn_id,
                        campaign_id=campaign_id,
                        role="assistant",
                        content=opening,
                    )
                    db.add_event(
                        event_id=f"evt_{uuid4().hex}",
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        type="narrator_response_created",
                        payload=NarratorResponseCreatedPayload(reply=opening),
                    )
                return character_id, assistant_turn
            except OperationalError:
                if attempt == _COMMIT_ATTEMPTS - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise AssertionError("unreachable")

    async def create_campaign(
        self, _request: CampaignCreateRequest, owner_user_id: str
    ) -> CampaignDetail:
        campaign_id = f"campaign_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        agent_name = "Narrator"
        model = ModelPolicy.narrator_model()

        await asyncio.to_thread(self._begin_campaign_creation, owner_user_id)

        try:
            async with asyncio.timeout(settings.CAMPAIGN_CREATE_MODEL_DEADLINE_SECONDS):
                initial_state = await self._build_initial_campaign_state(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                )
                scene_context = build_narrator_scene_context(json.dumps(initial_state))
                recent_turns: list[dict[str, str]] = []

                opening_prompt = await self._generate_narrator_response(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    agent_name=agent_name,
                    model=model,
                    scene_context=scene_context,
                    recent_turns=recent_turns,
                    message=self._build_campaign_opening_request(),
                )
                campaign_name = self._normalize_campaign_title(
                    await self._generate_narrator_response(
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        agent_name=agent_name,
                        model=model,
                        scene_context=scene_context,
                        recent_turns=recent_turns,
                        message=self._build_campaign_title_request(opening_prompt),
                    )
                )
        except TimeoutError as exc:
            raise HTTPException(status_code=502, detail="AI service timed out.") from exc

        character_id, assistant_turn = await asyncio.to_thread(
            self._commit_campaign_creation,
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            initial_state=initial_state,
            assistant_turn_id=assistant_turn_id,
            opening=opening_prompt,
        )

        return CampaignDetail(
            campaign_id=campaign_id,
            name=campaign_name,
            description="AI-created campaign",
            messages=[
                CampaignTurn(
                    turn_id=assistant_turn.turn_id,
                    role=assistant_turn.role,
                    content=assistant_turn.content,
                    created_at=assistant_turn.created_at,
                )
            ],
            truncated=False,
            character_id=character_id,
        )

    # -- chat turn --------------------------------------------------------------

    def _begin_chat(
        self,
        *,
        request: ChatRequest,
        owner_user_id: str,
        campaign_id: str,
        auto_create_campaign: bool,
        idempotency_key: str | None,
        request_fingerprint: str,
        token: str,
    ) -> "ChatResponse | _ChatSnapshot":
        """Phase 1: one short transaction. Claims, leases, snapshot and request count."""
        with session(lock_budget=True) as db:
            if idempotency_key is not None:
                existing_request = db.get_chat_request_idempotency(
                    owner_user_id, idempotency_key
                )
                if existing_request is not None:
                    if existing_request["request_fingerprint"] != request_fingerprint:
                        raise HTTPException(
                            status_code=409,
                            detail="Idempotency-Key was already used for a different request.",
                        )
                    if existing_request["status"] == "completed":
                        return ChatResponse(
                            reply=existing_request["reply"],
                            campaign_id=existing_request["resolved_campaign_id"],
                            turn_id=existing_request["turn_id"],
                        )

            validate_chat_request(db, request, owner_user_id)

            if idempotency_key is not None:
                replay = self._claim_chat_request(
                    db,
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    request=request,
                    token=token,
                )
                if replay is not None:
                    return replay

            if not auto_create_campaign:
                if not db.acquire_turn_lease(
                    campaign_id, owner_user_id, token, settings.REQUEST_LEASE_SECONDS
                ):
                    raise HTTPException(
                        status_code=503,
                        detail="Another turn for this campaign is already in progress; retry.",
                    )
                # Exact under the lease: no other turn can be in flight.
                validate_campaign_turn_limit(db, owner_user_id, campaign_id)

            memory_service = MemoryService(db)
            campaign_state = memory_service.build_campaign_state(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            )
            try:
                validate_persisted_campaign_state_json(campaign_state)
                generated_ability_definitions(
                    load_authoritative_campaign_state(campaign_state)
                )
            except InvalidCampaignStateError as exc:
                logger.error(
                    "campaign_state_integrity_failure owner_user_id=%s campaign_id=%s error_type=%s",
                    owner_user_id,
                    campaign_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Campaign state could not be processed.",
                ) from exc
            recent_turns = memory_service.load_recent_turns(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            )
            memory_context = memory_service.load_memory_context(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                query=request.message,
                campaign_state=campaign_state,
                recent_turns=recent_turns,
            )
            summary_text = memory_service.get_current_summary_text(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            )
            prior_user_turns = db.count_campaign_turns(campaign_id, owner_user_id)

            validate_daily_request_limit(db, owner_user_id)
            validate_project_request_limit(db)
            db.add_usage_request(owner_user_id, "chat")

            return _ChatSnapshot(
                campaign_state=campaign_state,
                recent_turns=recent_turns,
                memory_context=memory_context,
                summary_text=summary_text,
                prior_user_turns=prior_user_turns,
            )

    def _release_chat_claims(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        lease_held: bool,
        idempotency_key: str | None,
        request_fingerprint: str,
        token: str,
    ) -> None:
        with session() as db:
            if idempotency_key is not None:
                db.release_chat_request_idempotency(
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    owner_token=token,
                )
            if lease_held:
                db.release_turn_lease(campaign_id, token)

    def _commit_chat_turn(self, **kwargs: Any) -> None:
        for attempt in range(_COMMIT_ATTEMPTS):
            try:
                with session() as db:
                    self._write_chat_turn(db, **kwargs)
                return
            except OperationalError:
                if attempt == _COMMIT_ATTEMPTS - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _write_chat_turn(
        self,
        db: Repository,
        *,
        owner_user_id: str,
        campaign_id: str,
        auto_create_campaign: bool,
        initial_state: dict[str, Any] | None,
        token: str,
        idempotency_key: str | None,
        request_fingerprint: str,
        player_turn_id: str,
        assistant_turn_id: str,
        received_at: str,
        message: str,
        reply: str,
        outcome: "_TurnOutcome",
        memory_writes: "_MemoryWrites",
        parsed_action,
        tool_result,
    ) -> None:
        """Phase 3: the single atomic write of every durable game effect."""
        if not auto_create_campaign:
            # Fenced, and the first statement (a write) takes the SQLite write lock.
            if not db.release_turn_lease(campaign_id, token):
                if db.get_campaign_for_owner(campaign_id, owner_user_id) is None:
                    raise HTTPException(status_code=404, detail="Campaign not found")
                raise HTTPException(
                    status_code=503, detail="Turn lease was lost; retry."
                )
        else:
            assert initial_state is not None
            db.lock_internal_user(owner_user_id)
            if db.count_owner_campaigns(owner_user_id) >= UsageLimits.MAX_CAMPAIGNS_PER_PLAYER:
                raise usage_limit_error(
                    code="max_campaigns",
                    detail="Maximum number of campaigns reached.",
                )
            db.create_campaign(
                campaign_id=campaign_id,
                owner_user_id=owner_user_id,
                name=f"Campaign {campaign_id}",
                description="Auto-created campaign",
                state=initial_state,
            )
            db.ensure_campaign_character(campaign_id, generate_character_name())

        db.create_turn(
            turn_id=player_turn_id,
            campaign_id=campaign_id,
            role="user",
            content=message,
            created_at=received_at,
        )
        db.create_turn(
            turn_id=assistant_turn_id,
            campaign_id=campaign_id,
            role="assistant",
            content=reply,
        )
        for turn_id, event_type, payload_json in outcome.events:
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                campaign_id=campaign_id,
                turn_id=turn_id,
                type=event_type,
                payload_json=payload_json,
            )
        if outcome.final_state is not None:
            db.update_campaign_state(campaign_id, outcome.final_state)

        memory_service = MemoryService(db)
        memory_service.maybe_store_semantic_memories(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            user_turn_id=player_turn_id,
            parsed_action=parsed_action,
            tool_result=tool_result,
            request_message=message,
            campaign_state=outcome.campaign_state,
        )
        if memory_writes.summary_text is not None:
            memory_service.store_summary(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                summary_text=memory_writes.summary_text,
            )
        if memory_writes.reflection_candidates:
            memory_service.store_reflection_memories(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                source_event_id=assistant_turn_id,
                memory_candidates=memory_writes.reflection_candidates,
            )

        if idempotency_key is not None:
            try:
                db.complete_chat_request_idempotency(
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    reply=reply,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    owner_token=token,
                )
            except ChatRequestIdempotencyOwnershipError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Idempotent request was superseded; retry.",
                ) from exc

    async def handle_chat(
        self, request: ChatRequest, owner_user_id: str, idempotency_key: str | None = None
    ) -> ChatResponse:
        request_fingerprint = self._chat_request_fingerprint(request)
        auto_create_campaign = not request.campaign_id
        campaign_id = request.campaign_id or f"campaign_{uuid4().hex}"
        player_turn_id = f"turn_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        model = ModelPolicy.narrator_model()
        received_at = datetime.utcnow().isoformat()
        token = uuid4().hex

        begun = await asyncio.to_thread(
            self._begin_chat,
            request=request,
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            auto_create_campaign=auto_create_campaign,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            token=token,
        )
        if isinstance(begun, ChatResponse):
            return begun
        snapshot = begun

        logger.info(
            "chat_request_received owner_user_id=%s campaign_id=%s turn_id=%s",
            owner_user_id,
            campaign_id,
            player_turn_id,
        )

        try:
            deadline = asyncio.get_running_loop().time() + settings.CHAT_MODEL_DEADLINE_SECONDS
            try:
                async with asyncio.timeout_at(deadline):
                    outcome = await self._run_chat_models(
                        request=request,
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        auto_create_campaign=auto_create_campaign,
                        snapshot=snapshot,
                        player_turn_id=player_turn_id,
                        assistant_turn_id=assistant_turn_id,
                        model=model,
                    )
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=502, detail="AI service timed out."
                ) from exc

            # Best effort: skipped when the deadline is already spent.
            memory_writes = _MemoryWrites()
            try:
                async with asyncio.timeout_at(deadline):
                    memory_writes = await self._run_memory_layers(
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        player_turn_id=player_turn_id,
                        assistant_turn_id=assistant_turn_id,
                        snapshot=snapshot,
                        outcome=outcome,
                        request_message=request.message,
                    )
            except TimeoutError:
                logger.warning(
                    "memory_layers_skipped_deadline campaign_id=%s turn_id=%s",
                    campaign_id,
                    assistant_turn_id,
                )

            await asyncio.to_thread(
                self._commit_chat_turn,
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                auto_create_campaign=auto_create_campaign,
                initial_state=outcome.initial_state,
                token=token,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                player_turn_id=player_turn_id,
                assistant_turn_id=assistant_turn_id,
                received_at=received_at,
                message=request.message,
                reply=outcome.reply,
                outcome=outcome,
                memory_writes=memory_writes,
                parsed_action=outcome.parsed_action,
                tool_result=outcome.tool_result,
            )
        except BaseException:
            # Usage is already durable; drop the claim/lease so the client may retry
            # (BaseException also covers cancellation; TTL expiry is the backstop).
            try:
                await asyncio.to_thread(
                    self._release_chat_claims,
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    lease_held=not auto_create_campaign,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    token=token,
                )
            except Exception:
                logger.error("chat_claim_release_failed campaign_id=%s", campaign_id, exc_info=True)
            raise

        return ChatResponse(
            reply=outcome.reply,
            campaign_id=campaign_id,
            turn_id=assistant_turn_id,
        )

    async def _run_chat_models(
        self,
        *,
        request: ChatRequest,
        owner_user_id: str,
        campaign_id: str,
        auto_create_campaign: bool,
        snapshot: "_ChatSnapshot",
        player_turn_id: str,
        assistant_turn_id: str,
        model: str,
    ) -> "_TurnOutcome":
        """Model phases. Pure in-memory: no DB transaction is open while awaiting."""
        agent_name = "Narrator"
        campaign_state = snapshot.campaign_state
        recent_turns = snapshot.recent_turns
        memory_context = snapshot.memory_context
        writes = _TurnOutcome(campaign_state=campaign_state)
        initial_state: dict[str, Any] | None = None

        writes.add_event(
            player_turn_id,
            "player_message_received",
            PlayerMessageReceivedPayload(message=request.message),
        )

        if auto_create_campaign:
            initial_state = await self._build_initial_campaign_state(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                turn_id=player_turn_id,
            )
            campaign_state = json.dumps(initial_state)
            writes.initial_state = initial_state

        parser_model = ModelPolicy.action_parser_model()
        parser_estimated_input_tokens = (
            self.action_parser_agent.estimate_provider_input_tokens(
                message=request.message,
                campaign_state=campaign_state,
                recent_turns=recent_turns,
                memory_context=memory_context,
            )
        )
        parser_request_id = await self._reserve_model_call(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=player_turn_id,
            agent_name="ActionParser",
            model=parser_model,
            estimated_input_tokens=parser_estimated_input_tokens,
            max_output_tokens=TokenBudget.action_parser_max_output_tokens(),
        )

        parser_start_time = time.perf_counter()
        try:
            parsed_action = await self.action_parser_agent.parse(
                message=request.message,
                campaign_state=campaign_state,
                recent_turns=recent_turns,
                memory_context=memory_context,
                model=parser_model,
            )
            logger.info(
                "action_parser_completed owner_user_id=%s campaign_id=%s turn_id=%s parse_status=%s action=%s confidence=%.3f",
                owner_user_id,
                campaign_id,
                player_turn_id,
                parsed_action.parse_status,
                parsed_action.action,
                parsed_action.confidence,
            )
        except ActionParseProviderError as exc:
            cause = exc.__cause__
            logger.error(
                "action_parser_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s cause_type=%s cause_message=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                parser_model,
                int((time.perf_counter() - parser_start_time) * 1000),
                type(cause).__name__ if cause is not None else "None",
                str(cause) if cause is not None else "",
                exc_info=True,
            )
            await self._finalize_model_call(
                parser_request_id,
                started_at=parser_start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise HTTPException(
                status_code=502, detail="Action parser service failed."
            ) from exc
        except Exception as exc:
            await self._finalize_model_call(
                parser_request_id,
                started_at=parser_start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise

        await self._finalize_model_call(
            parser_request_id,
            started_at=parser_start_time,
            success=True,
            usage=parsed_action,
        )

        if parsed_action.parse_status == "invalid":
            reason = (
                parsed_action.parser_notes or "Action parser produced invalid output."
            )
            raise HTTPException(
                status_code=422, detail=f"Unprocessable action: {reason}"
            )

        writes.add_event(
            player_turn_id,
            "action_parsed",
            ActionParsedPayload(
                action=parsed_action.action,
                target=parsed_action.target,
                confidence=parsed_action.confidence,
                stealth=parsed_action.stealth,
                parse_status=parsed_action.parse_status,
                parser_notes=parsed_action.parser_notes,
            ),
        )

        tool_result = ToolExecutionResult(
            success=False,
            summary="Action parse status was not executable.",
        )
        try:
            if parsed_action.parse_status == "ok":
                # Sync (may block on MCP): keep it off the event loop.
                updated_state, tool_result = await asyncio.to_thread(
                    self.tool_executor.execute,
                    parsed_action=parsed_action,
                    campaign_state=campaign_state,
                )
                if tool_result.success:
                    writes.add_event(
                        player_turn_id,
                        "tool_executed",
                        ToolExecutedPayload(
                            applied_tools=tool_result.applied_tools,
                            summary=tool_result.summary,
                            state_delta=tool_result.state_delta,
                        ),
                    )
                else:
                    writes.add_event(
                        player_turn_id,
                        "tool_execution_failed",
                        ToolExecutionFailedPayload(
                            action=parsed_action.action,
                            reason=tool_result.summary,
                        ),
                    )
            else:
                updated_state = load_authoritative_campaign_state(campaign_state)
        except InvalidCampaignStateError as exc:
            logger.error(
                "campaign_state_integrity_failure owner_user_id=%s campaign_id=%s turn_id=%s error_type=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Campaign state could not be processed.",
            ) from exc

        story_signal = derive_story_signal(tool_result)
        story_result = None
        story_state_changed = False
        if story_signal is not None:
            story_result = apply_story_signal(updated_state, story_signal)
            story_state_changed = bool(story_result.changed)
        reward_result = apply_quest_completion_rewards(updated_state, story_result)
        if reward_result.outcome == ProgressionRewardOutcome.FAILED:
            logger.error(
                "progression_reward_failure owner_user_id=%s campaign_id=%s turn_id=%s reward_id=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                reward_result.reward_id,
            )
            raise HTTPException(
                status_code=500,
                detail="Campaign state could not be processed.",
            )
        current_turn_reward = reward_result.narrator_reward
        reward_state_changed = reward_result.changed

        authoritative_state_changed = (
            bool(tool_result.state_delta)
            or initial_state is not None
            or campaign_state == "No campaign state yet."
            or story_state_changed
            or reward_state_changed
        )
        if authoritative_state_changed:
            writes.set_state(player_turn_id, updated_state)
            campaign_state = json.dumps(updated_state)

        director_step_result = await self._run_director_step(
            writes=writes,
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            player_turn_id=player_turn_id,
            campaign_state=campaign_state,
            parsed_action=parsed_action,
            tool_result=tool_result,
            current_turn_reward=current_turn_reward,
        )
        campaign_state = director_step_result.campaign_state
        current_turn_reveal = director_step_result.current_turn_reveal
        current_turn_reward = director_step_result.current_turn_reward

        scene_context = build_narrator_scene_context(campaign_state)
        narrator_payload = NarratorAgentInput(
            player_message=request.message,
            scene_context=scene_context,
            recent_turns=recent_turns,
            relevant_memories=memory_context,
            parsed_action=parsed_action,
            tool_result=tool_result,
            current_turn_reveal=current_turn_reveal,
            current_turn_reward=current_turn_reward,
        )
        narrator_estimated_input_tokens = self._estimate_payload_tokens(narrator_payload)
        narrator_request_id = await self._reserve_model_call(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=assistant_turn_id,
            agent_name=agent_name,
            model=model,
            estimated_input_tokens=narrator_estimated_input_tokens,
            max_output_tokens=TokenBudget.narrator_max_output_tokens(),
        )
        start_time = time.perf_counter()
        try:
            narrator_output = await self.narrator_agent.generate(
                payload=narrator_payload,
                model=model,
            )
        except Exception as exc:
            logger.error(
                "narrator_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s error_message=%s",
                owner_user_id,
                campaign_id,
                assistant_turn_id,
                model,
                int((time.perf_counter() - start_time) * 1000),
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            await self._finalize_model_call(
                narrator_request_id,
                started_at=start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise HTTPException(status_code=502, detail="AI service failed.") from exc
        await self._finalize_model_call(
            narrator_request_id,
            started_at=start_time,
            success=True,
            usage=narrator_output,
        )

        writes.reply = narrator_output.reply_text
        writes.campaign_state = campaign_state
        writes.parsed_action = parsed_action
        writes.tool_result = tool_result
        writes.add_event(
            assistant_turn_id,
            "narrator_response_created",
            NarratorResponseCreatedPayload(reply=writes.reply),
        )
        return writes

    async def _run_memory_layers(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        player_turn_id: str,
        assistant_turn_id: str,
        snapshot: "_ChatSnapshot",
        outcome: "_TurnOutcome",
        request_message: str,
    ) -> "_MemoryWrites":
        """Best-effort summary/reflection model calls over in-memory turn data."""
        memory_writes = _MemoryWrites()
        # The new user turn will be the (prior + 1)th user turn once committed.
        turn_count = snapshot.prior_user_turns + 1
        do_summary = turn_count % settings.MEMORY_SUMMARY_EVERY_TURNS == 0
        do_reflection = turn_count % settings.MEMORY_REFLECTION_EVERY_TURNS == 0
        if not (do_summary or do_reflection):
            return memory_writes

        recent_turns = (
            snapshot.recent_turns
            + [
                {"role": "user", "content": request_message},
                {"role": "assistant", "content": outcome.reply},
            ]
        )[-settings.MAX_RECENT_MESSAGES :]
        current_summary = snapshot.summary_text

        try:
            if do_summary:
                summary_model = ModelPolicy.summarizer_model()
                summarizer_payload = MemorySummarizerInput(
                    previous_summary=current_summary,
                    recent_turns=recent_turns,
                    campaign_state=outcome.campaign_state,
                    latest_reply=outcome.reply,
                )
                summary_estimated_tokens = self._estimate_payload_tokens(summarizer_payload)
                summary_request_id = await self._reserve_model_call(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    agent_name=self.memory_summarizer_agent.name,
                    model=summary_model,
                    estimated_input_tokens=summary_estimated_tokens,
                    max_output_tokens=TokenBudget.summarizer_max_output_tokens(),
                )
                started = time.perf_counter()
                try:
                    summary_output = await self.memory_summarizer_agent.summarize(
                        payload=summarizer_payload,
                        model=summary_model,
                    )
                except Exception as exc:
                    await self._finalize_model_call(
                        summary_request_id,
                        started_at=started,
                        success=False,
                        failure_reason=str(exc),
                    )
                else:
                    await self._finalize_model_call(
                        summary_request_id,
                        started_at=started,
                        success=True,
                        usage=summary_output,
                    )
                    if summary_output.summary_text.strip():
                        memory_writes.summary_text = summary_output.summary_text
                        current_summary = summary_output.summary_text

            if do_reflection:
                reflection_model = ModelPolicy.memory_reflection_model()
                reflection_payload = MemoryReflectionInput(
                    recent_turns=recent_turns,
                    campaign_state=outcome.campaign_state,
                    current_summary=current_summary,
                )
                reflection_estimated_tokens = self._estimate_payload_tokens(reflection_payload)
                reflection_request_id = await self._reserve_model_call(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    agent_name=self.memory_reflection_agent.name,
                    model=reflection_model,
                    estimated_input_tokens=reflection_estimated_tokens,
                    max_output_tokens=TokenBudget.memory_reflection_max_output_tokens(),
                )
                started = time.perf_counter()
                try:
                    reflection_output = await self.memory_reflection_agent.reflect(
                        payload=reflection_payload,
                        model=reflection_model,
                    )
                except Exception as exc:
                    await self._finalize_model_call(
                        reflection_request_id,
                        started_at=started,
                        success=False,
                        failure_reason=str(exc),
                    )
                else:
                    await self._finalize_model_call(
                        reflection_request_id,
                        started_at=started,
                        success=True,
                        usage=reflection_output,
                    )
                    memory_writes.reflection_candidates = list(
                        reflection_output.memories_to_store
                    )
        except HTTPException:
            # A denied budget reservation only skips the optional memory layer.
            pass
        return memory_writes

    def _authoritative_state_for_director(self, campaign_state: str) -> dict[str, Any]:
        """Project persisted campaign state text into an authoritative dict.

        Delegates to the same canonical state-loading contract used by
        `ToolExecutor` (`load_authoritative_campaign_state`), so the Director
        always receives the exact same validated/normalized authoritative
        state -- including item/NPC normalization -- rather than a
        subtly different parallel representation.
        """
        return load_authoritative_campaign_state(campaign_state)

    async def _run_director_step(
        self,
        *,
        writes: "_TurnOutcome",
        owner_user_id: str,
        campaign_id: str,
        player_turn_id: str,
        campaign_state: str,
        parsed_action: ParsedAction,
        tool_result: ToolExecutionResult,
        current_turn_reward: NarratorProgressionReward | None,
    ) -> DirectorStepResult:
        """Invoke the Director after the authoritative player result and, for
        `decision="act"`, execute at most one validated `WorldAction` through
        `WorldAuthorityExecutor`.

        Returns the authoritative campaign-state text to use for final
        narrator grounding and memory maintenance, plus the optional
        same-turn authored reveal that resulted from a changed reveal_clue
        action. The Director never mutates campaign state directly; only
        `WorldAuthorityExecutor` may apply a privileged world-state
        transition.
        """
        # A legitimate missing/sentinel campaign state is the *only* case in
        # which fresh starter state may be materialized here (never for
        # malformed persisted state, which raises below instead). Track this
        # up front so the exact state Director evaluates can be made
        # authoritative for the rest of the turn -- Narrator, memory
        # maintenance, and any later turn must never observe a different,
        # independently rerolled starter state.
        is_uninitialized_state = not campaign_state or campaign_state == "No campaign state yet."
        try:
            director_state = self._authoritative_state_for_director(campaign_state)
            director_input = build_director_input(
                director_state,
                parsed_action=parsed_action,
                tool_result=tool_result,
            )
        except (InvalidCampaignStateError, InvalidDirectorContextError) as exc:
            logger.error(
                "director_context_invalid owner_user_id=%s campaign_id=%s turn_id=%s error_type=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Campaign state could not be processed.",
            ) from exc

        if is_uninitialized_state:
            # The Director just reasoned over freshly materialized starter
            # state for a first-time/auto-created campaign. Persist that
            # exact state as authoritative immediately -- via the existing
            # `game_state_updated` event contract, not a new mechanism -- so
            # it (not the original sentinel) grounds Narrator and memory for
            # this turn and is reloaded, rather than rerolled, on any later
            # turn. This is initialization, not a privileged world action, so
            # it is never attributed to `world_action_executed`.
            writes.set_state(player_turn_id, director_state)
            campaign_state = json.dumps(director_state)

        director_model = ModelPolicy.director_model()
        director_estimated_input_tokens = (
            self.director_agent.estimate_provider_input_tokens(director_input=director_input)
        )
        director_request_id = await self._reserve_model_call(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=player_turn_id,
            agent_name=self.director_agent.name,
            model=director_model,
            estimated_input_tokens=director_estimated_input_tokens,
            max_output_tokens=TokenBudget.director_max_output_tokens(),
        )

        director_start_time = time.perf_counter()
        try:
            director_result = await self.director_agent.propose(
                director_input=director_input,
                model=director_model,
            )
        except (DirectorProviderError, DirectorProposalOutputError) as exc:
            logger.error(
                "director_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                director_model,
                int((time.perf_counter() - director_start_time) * 1000),
                type(exc).__name__,
                exc_info=True,
            )
            await self._finalize_model_call(
                director_request_id,
                started_at=director_start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise HTTPException(status_code=502, detail="Director service failed.") from exc
        except Exception as exc:
            await self._finalize_model_call(
                director_request_id,
                started_at=director_start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise

        await self._finalize_model_call(
            director_request_id,
            started_at=director_start_time,
            success=True,
            usage=director_result.usage,
        )

        proposal = director_result.proposal
        if not isinstance(proposal, WorldActionProposal):
            return DirectorStepResult(
                campaign_state=campaign_state,
                current_turn_reward=current_turn_reward,
            )

        updated_world_state, world_action_result = self.world_authority_executor.execute(
            proposal.world_action, director_state
        )
        current_turn_reveal = None

        if world_action_result.success:
            writes.add_event(
                player_turn_id,
                "world_action_executed",
                WorldActionExecutedPayload(
                    action=world_action_result.action,
                    summary=world_action_result.summary,
                    changed=world_action_result.changed,
                    state_delta=world_action_result.state_delta,
                ),
            )
            if world_action_result.changed:
                writes.set_state(player_turn_id, updated_world_state)
                campaign_state = json.dumps(updated_world_state)
                if isinstance(proposal.world_action, RevealClueWorldAction):
                    clue = NARRATIVE_CLUES[proposal.world_action.clue_id]
                    current_turn_reveal = NarratorNarrativeReveal(
                        clue_id=clue.clue_id,
                        text=clue.text,
                    )
        else:
            writes.add_event(
                player_turn_id,
                "world_action_failed",
                WorldActionFailedPayload(
                    action=world_action_result.action,
                    summary=world_action_result.summary,
                    error_code=world_action_result.error_code,
                ),
            )

        return DirectorStepResult(
            campaign_state=campaign_state,
            current_turn_reveal=current_turn_reveal,
            current_turn_reward=current_turn_reward,
        )

    def _chat_request_fingerprint(self, request: ChatRequest) -> str:
        canonical_request = json.dumps(
            {
                "message": request.message,
                "campaign_id": request.campaign_id,
                "character_id": request.character_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical_request).hexdigest()

    def _claim_chat_request(
        self,
        db,
        *,
        owner_user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        request: ChatRequest,
        token: str,
    ) -> ChatResponse | None:
        claim = db.claim_chat_request_idempotency(
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            requested_campaign_id=request.campaign_id,
            requested_character_id=request.character_id,
            owner_token=token,
            lease_seconds=settings.REQUEST_LEASE_SECONDS,
        )
        if claim.row is None:
            raise HTTPException(
                status_code=503,
                detail="Idempotent request could not be claimed; retry.",
            )
        if claim.row["request_fingerprint"] != request_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for a different request.",
            )
        if claim.row["status"] == "completed":
            return ChatResponse(
                reply=claim.row["reply"],
                campaign_id=claim.row["resolved_campaign_id"],
                turn_id=claim.row["turn_id"],
            )
        if not claim.acquired:
            raise HTTPException(
                status_code=503,
                detail="Idempotent request is already in progress; retry.",
            )
        return None

    def _build_campaign_opening_request(self) -> str:
        return (
            "Using the authoritative current scene provided, write the opening scene for this "
            "haunted halls campaign. Establish immediate tension and end with a clear invitation "
            "for the player's first action. Do not invent contradictory rooms, exits, NPC "
            "presence, or item state."
        )

    def _build_campaign_title_request(self, opening_prompt: str) -> str:
        return (
            "Based on the campaign opening below, provide only a short haunted campaign title with no quotes "
            f"and no extra commentary.\n\n{opening_prompt}"
        )

    async def _build_initial_campaign_state(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        generated_abilities = await self._generate_starter_abilities(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=turn_id,
        )
        initial_state = build_fresh_campaign_state()
        ensure_character_progression_state(initial_state)
        initial_state["player"]["generated_abilities"] = [
            ability.model_dump(mode="json") for ability in generated_abilities.abilities
        ]
        for ability in generated_abilities.abilities:
            unlock_result = unlock_ability(initial_state, ability.ability_id)
            if not unlock_result.success:
                raise HTTPException(
                    status_code=502, detail="Starter ability service failed."
                )
        return initial_state

    async def _generate_starter_abilities(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
    ) -> StarterAbilityGeneration:
        model = ModelPolicy.narrator_model()
        estimated_input_tokens = (
            self.starter_ability_generator.estimate_provider_input_tokens()
        )
        request_id = await self._reserve_model_call(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=turn_id,
            agent_name=self.starter_ability_generator.name,
            model=model,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=TokenBudget.starter_ability_max_output_tokens(),
        )

        start_time = time.perf_counter()
        usage = None
        response_status = None
        incomplete_details_reason = None
        try:
            generated = await self.starter_ability_generator.generate(
                return_usage=True,
            )
            if not isinstance(generated, ModelCallResult):
                raise ValueError("Starter ability generator did not return valid structured output.")
            usage = generated.usage
            response_status = generated.status
            incomplete_details_reason = generated.incomplete_details_reason
            if generated.output is None:
                response_details = []
                if response_status is not None:
                    response_details.append(f"status={response_status}")
                if incomplete_details_reason is not None:
                    response_details.append(
                        f"incomplete_details.reason={incomplete_details_reason}"
                    )
                details = f" ({', '.join(response_details)})" if response_details else ""
                raise ValueError(
                    "Starter ability provider returned no structured output"
                    f"{details}."
                )
            starter_abilities = validate_starter_ability_definitions(
                generated.output.abilities
            )
        except Exception as exc:
            logger.error(
                "starter_ability_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s error_message=%s",
                owner_user_id,
                campaign_id,
                turn_id,
                model,
                int((time.perf_counter() - start_time) * 1000),
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            await self._finalize_model_call(
                request_id,
                started_at=start_time,
                success=False,
                failure_reason=str(exc),
                usage=usage,
            )
            raise HTTPException(status_code=502, detail="Starter ability service failed.") from exc
        await self._finalize_model_call(
            request_id, started_at=start_time, success=True, usage=usage
        )
        return StarterAbilityGeneration(abilities=list(starter_abilities))

    async def _generate_narrator_response(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        scene_context: NarratorSceneContext,
        recent_turns: list[dict[str, str]],
        message: str,
    ) -> str:
        narrator_payload = NarratorAgentInput(
            player_message=message,
            scene_context=scene_context,
            recent_turns=recent_turns,
        )
        estimated_input_tokens = self._estimate_payload_tokens(narrator_payload)
        request_id = await self._reserve_model_call(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            turn_id=turn_id,
            agent_name=agent_name,
            model=model,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=TokenBudget.narrator_max_output_tokens(),
        )

        start_time = time.perf_counter()
        try:
            narrator_output = await self.narrator_agent.generate(
                payload=narrator_payload,
                model=model,
            )
        except Exception as exc:
            logger.error(
                "campaign_narrator_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s error_message=%s",
                owner_user_id,
                campaign_id,
                turn_id,
                model,
                int((time.perf_counter() - start_time) * 1000),
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            await self._finalize_model_call(
                request_id,
                started_at=start_time,
                success=False,
                failure_reason=str(exc),
            )
            raise HTTPException(status_code=502, detail="AI service failed.") from exc
        await self._finalize_model_call(
            request_id, started_at=start_time, success=True, usage=narrator_output
        )
        return narrator_output.reply_text

    def _normalize_campaign_title(self, value: str) -> str:
        candidate = value.strip().strip('"').strip("'").strip()
        first_line = candidate.splitlines()[0][:80].strip() if candidate else ""
        return first_line or DEFAULT_CAMPAIGN_TITLE

    def _validate_campaign_creation(self, db, owner_user_id: str) -> None:
        owner_campaign_count = db.count_owner_campaigns(owner_user_id)
        if owner_campaign_count >= UsageLimits.MAX_CAMPAIGNS_PER_PLAYER:
            raise usage_limit_error(
                code="max_campaigns",
                detail="Maximum number of campaigns reached.",
            )

orchestrator = ChatOrchestrator()
