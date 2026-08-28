from __future__ import annotations

import logging
import time
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException

from app.agents.action_parser import ActionParseProviderError, ActionParserAgent
from app.agents.memory_reflection import MemoryReflectionAgent, MemoryReflectionInput
from app.agents.memory_summarizer import MemorySummarizerAgent, MemorySummarizerInput
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.core.config import settings
from app.db.session import session
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
from app.schemas.chat import ChatRequest, ChatResponse, ToolExecutionResult
from app.schemas.events import (
    ActionParseFailedPayload,
    ActionParsedPayload,
    GameStateUpdatedPayload,
    NarratorResponseCreatedPayload,
    PlayerMessageReceivedPayload,
    ToolExecutedPayload,
    ToolExecutionFailedPayload,
)
from app.services.tool_executor import ToolExecutor


logger = logging.getLogger(__name__)


class ChatOrchestrator:
    def _check_model_call_budget(
        self,
        db,
        owner_user_id: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
    ) -> None:
        validate_daily_token_limit(db, owner_user_id, estimated_input_tokens, max_output_tokens)
        validate_project_request_limit(db)
        validate_project_token_limit(db, estimated_input_tokens, max_output_tokens)

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

    async def create_campaign(
        self, _request: CampaignCreateRequest, owner_user_id: str
    ) -> CampaignDetail:
        campaign_id = f"campaign_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        agent_name = "Narrator"
        model = ModelPolicy.narrator_model()

        with session() as db:
            self._validate_campaign_creation(db, owner_user_id)

            if not (settings.AI_ENABLED or bool(settings.OPENAI_API_KEY)):
                opening_prompt = self._stub_campaign_opening()
                campaign_name = self._stub_campaign_title()
            else:
                campaign_state = "No campaign state yet."
                recent_turns: list[dict[str, str]] = []

                opening_request = self._build_campaign_opening_request()
                opening_prompt = await self._generate_narrator_response(
                    db=db,
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    agent_name=agent_name,
                    model=model,
                    campaign_state=campaign_state,
                    recent_turns=recent_turns,
                    message=opening_request,
                )

                title_request = self._build_campaign_title_request(opening_prompt)
                campaign_name = self._normalize_campaign_title(
                    await self._generate_narrator_response(
                        db=db,
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        agent_name=agent_name,
                        model=model,
                        campaign_state=campaign_state,
                        recent_turns=recent_turns,
                        message=title_request,
                    )
                )

            db.create_campaign(
                campaign_id=campaign_id,
                owner_user_id=owner_user_id,
                name=campaign_name,
                description="AI-created campaign",
            )
            assistant_turn = db.create_turn(
                turn_id=assistant_turn_id,
                campaign_id=campaign_id,
                role="assistant",
                content=opening_prompt,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                campaign_id=campaign_id,
                turn_id=assistant_turn_id,
                type="narrator_response_created",
                payload=NarratorResponseCreatedPayload(reply=opening_prompt),
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
        )

    async def handle_chat(
        self, request: ChatRequest, owner_user_id: str
    ) -> ChatResponse:
        campaign_id = request.campaign_id or f"campaign_{uuid4().hex}"
        player_turn_id = f"turn_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        agent_name = "Narrator"
        model = ModelPolicy.narrator_model()

        with session() as db:
            validate_chat_request(db, request, owner_user_id)
            validate_campaign_turn_limit(db, owner_user_id, campaign_id)

            has_openai_key = bool((settings.OPENAI_API_KEY or "").strip())
            ai_enabled = settings.AI_ENABLED or has_openai_key
            parser_model_enabled = has_openai_key
            logger.info(
                "chat_request_received owner_user_id=%s campaign_id=%s turn_id=%s ai_enabled=%s parser_model_enabled=%s has_openai_key=%s",
                owner_user_id,
                campaign_id,
                player_turn_id,
                ai_enabled,
                parser_model_enabled,
                has_openai_key,
            )
            memory_service = MemoryService(db)
            campaign_state = memory_service.build_campaign_state(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            )
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

            estimated_input_tokens = (
                estimate_tokens(request.message)
                + estimate_tokens(
                    "structured action parsing and tool execution context"
                )
                + estimate_tokens(memory_service.format_memory_context(memory_context))
            )
            validate_request_budget(
                estimated_input_tokens, TokenBudget.narrator_max_output_tokens()
            )
            validate_daily_request_limit(db, owner_user_id)
            validate_daily_token_limit(
                db,
                owner_user_id,
                estimated_input_tokens,
                TokenBudget.narrator_max_output_tokens(),
            )

            db.create_campaign(
                campaign_id=campaign_id,
                owner_user_id=owner_user_id,
                name=f"Campaign {campaign_id}",
                description="Auto-created campaign",
            )
            db.create_turn(
                turn_id=player_turn_id,
                campaign_id=campaign_id,
                role="user",
                content=request.message,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                campaign_id=campaign_id,
                turn_id=player_turn_id,
                type="player_message_received",
                payload=PlayerMessageReceivedPayload(message=request.message),
            )

            parser_start_time = time.perf_counter()
            try:
                if parser_model_enabled:
                    parser_prompt = self.action_parser_agent.build_provider_request(
                        message=request.message,
                        campaign_state=campaign_state,
                        recent_turns=recent_turns,
                        memory_context=memory_context,
                    )
                    parser_estimated_input_tokens = sum(
                        estimate_tokens(str(message.get("content", "")))
                        for message in parser_prompt
                        if isinstance(message, dict)
                        and isinstance(message.get("content"), str)
                    )
                    self._check_model_call_budget(
                        db,
                        owner_user_id,
                        parser_estimated_input_tokens,
                        TokenBudget.action_parser_max_output_tokens(),
                    )
                    parsed_action = await self.action_parser_agent.parse(
                        message=request.message,
                        campaign_state=campaign_state,
                        recent_turns=recent_turns,
                        memory_context=memory_context,
                        model=ModelPolicy.action_parser_model(),
                        deterministic_only=False,
                    )
                else:
                    parsed_action = await self.action_parser_agent.parse(
                        message=request.message,
                        campaign_state=campaign_state,
                        recent_turns=recent_turns,
                        memory_context=memory_context,
                        model=ModelPolicy.action_parser_model(),
                        deterministic_only=True,
                    )
                logger.info(
                    "action_parser_completed owner_user_id=%s campaign_id=%s turn_id=%s parse_status=%s action=%s confidence=%.3f deterministic_only=%s",
                    owner_user_id,
                    campaign_id,
                    player_turn_id,
                    parsed_action.parse_status,
                    parsed_action.action,
                    parsed_action.confidence,
                    not parser_model_enabled,
                )
            except ActionParseProviderError as exc:
                parser_latency_ms = int(
                    (time.perf_counter() - parser_start_time) * 1000
                )
                cause = exc.__cause__
                cause_type = type(cause).__name__ if cause is not None else "None"
                cause_message = str(cause) if cause is not None else ""
                logger.error(
                    "action_parser_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s cause_type=%s cause_message=%s",
                    owner_user_id,
                    campaign_id,
                    player_turn_id,
                    ModelPolicy.action_parser_model(),
                    parser_latency_ms,
                    cause_type,
                    cause_message,
                    exc_info=True,
                )
                db.log_model_request(
                    request_id=f"req_{uuid4().hex}",
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=player_turn_id,
                    agent_name="ActionParser",
                    model=ModelPolicy.action_parser_model(),
                    estimated_input_tokens=estimated_input_tokens,
                    actual_input_tokens=estimated_input_tokens,
                    actual_output_tokens=0,
                    latency_ms=parser_latency_ms,
                    success=False,
                    failure_reason=str(exc),
                    cost_estimate=0.0,
                )
                raise HTTPException(
                    status_code=502, detail="Action parser service failed."
                ) from exc

            if parser_model_enabled:
                parser_latency_ms = int(
                    (time.perf_counter() - parser_start_time) * 1000
                )
                parser_input_tokens = getattr(parsed_action, "input_tokens", None)
                parser_output_tokens = getattr(parsed_action, "output_tokens", None)
                actual_input_tokens = (
                    int(parser_input_tokens)
                    if parser_input_tokens is not None
                    else parser_estimated_input_tokens
                )
                actual_output_tokens = (
                    int(parser_output_tokens)
                    if parser_output_tokens is not None
                    else estimate_tokens(parsed_action.model_dump_json())
                )
                db.log_model_request(
                    request_id=f"req_{uuid4().hex}",
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    turn_id=player_turn_id,
                    agent_name="ActionParser",
                    model=ModelPolicy.action_parser_model(),
                    estimated_input_tokens=parser_estimated_input_tokens,
                    actual_input_tokens=actual_input_tokens,
                    actual_output_tokens=actual_output_tokens,
                    latency_ms=parser_latency_ms,
                    success=True,
                    failure_reason=None,
                    cost_estimate=0.0,
                )

            if parsed_action.parse_status == "invalid":
                reason = (
                    parsed_action.parser_notes
                    or "Action parser produced invalid output."
                )
                db.add_event(
                    event_id=f"evt_{uuid4().hex}",
                    campaign_id=campaign_id,
                    turn_id=player_turn_id,
                    type="action_parse_failed",
                    payload=ActionParseFailedPayload(reason=reason),
                )
                raise HTTPException(
                    status_code=422, detail=f"Unprocessable action: {reason}"
                )

            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                campaign_id=campaign_id,
                turn_id=player_turn_id,
                type="action_parsed",
                payload=ActionParsedPayload(
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
            if parsed_action.parse_status == "ok":
                updated_state, tool_result = self.tool_executor.execute(
                    parsed_action=parsed_action,
                    campaign_state=campaign_state,
                )

                if tool_result.success:
                    db.add_event(
                        event_id=f"evt_{uuid4().hex}",
                        campaign_id=campaign_id,
                        turn_id=player_turn_id,
                        type="tool_executed",
                        payload=ToolExecutedPayload(
                            applied_tools=tool_result.applied_tools,
                            summary=tool_result.summary,
                            state_delta=tool_result.state_delta,
                        ),
                    )
                else:
                    db.add_event(
                        event_id=f"evt_{uuid4().hex}",
                        campaign_id=campaign_id,
                        turn_id=player_turn_id,
                        type="tool_execution_failed",
                        payload=ToolExecutionFailedPayload(
                            action=parsed_action.action,
                            reason=tool_result.summary,
                        ),
                    )

                if tool_result.state_delta or campaign_state == "No campaign state yet.":
                    db.update_campaign_state(campaign_id, updated_state)
                    db.add_event(
                        event_id=f"evt_{uuid4().hex}",
                        campaign_id=campaign_id,
                        turn_id=player_turn_id,
                        type="game_state_updated",
                        payload=GameStateUpdatedPayload(state=updated_state),
                    )
                    campaign_state = memory_service.build_campaign_state(
                        owner_user_id=owner_user_id, campaign_id=campaign_id
                    )

            if not ai_enabled:
                reply = self._stub_reply(request.message)
            else:
                start_time = time.perf_counter()
                narrator_payload = NarratorAgentInput(
                    player_message=request.message,
                    campaign_state=campaign_state,
                    recent_turns=recent_turns,
                    relevant_memories=memory_context,
                    parsed_action=parsed_action,
                    tool_result=tool_result,
                )
                narrator_estimated_input_tokens = self._estimate_payload_tokens(narrator_payload)
                self._check_model_call_budget(
                    db,
                    owner_user_id,
                    narrator_estimated_input_tokens,
                    TokenBudget.narrator_max_output_tokens(),
                )
                try:
                    narrator_output = await self.narrator_agent.generate(
                        payload=narrator_payload,
                        model=model,
                    )
                    reply = narrator_output.reply_text
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    actual_input_tokens = (
                        narrator_output.input_tokens
                        if narrator_output.input_tokens is not None
                        else narrator_estimated_input_tokens
                    )
                    actual_output_tokens = (
                        narrator_output.output_tokens
                        if narrator_output.output_tokens is not None
                        else estimate_tokens(reply)
                    )
                    db.log_model_request(
                        request_id=f"req_{uuid4().hex}",
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        agent_name=agent_name,
                        model=model,
                        estimated_input_tokens=narrator_estimated_input_tokens,
                        actual_input_tokens=actual_input_tokens,
                        actual_output_tokens=actual_output_tokens,
                        latency_ms=latency_ms,
                        success=True,
                        failure_reason=None,
                        cost_estimate=0.0,
                    )
                except Exception as exc:
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    logger.error(
                        "narrator_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s error_message=%s",
                        owner_user_id,
                        campaign_id,
                        assistant_turn_id,
                        model,
                        latency_ms,
                        type(exc).__name__,
                        str(exc),
                        exc_info=True,
                    )
                    db.log_model_request(
                        request_id=f"req_{uuid4().hex}",
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        agent_name=agent_name,
                        model=model,
                        estimated_input_tokens=narrator_estimated_input_tokens,
                        actual_input_tokens=narrator_estimated_input_tokens,
                        actual_output_tokens=0,
                        latency_ms=latency_ms,
                        success=False,
                        failure_reason=str(exc),
                        cost_estimate=0.0,
                    )
                    raise HTTPException(status_code=502, detail="AI service failed.")

            db.create_turn(
                turn_id=assistant_turn_id,
                campaign_id=campaign_id,
                role="assistant",
                content=reply,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                campaign_id=campaign_id,
                turn_id=assistant_turn_id,
                type="narrator_response_created",
                payload=NarratorResponseCreatedPayload(reply=reply),
            )

            await self._maybe_update_memory_layers(
                db=db,
                memory_service=memory_service,
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                user_turn_id=player_turn_id,
                assistant_turn_id=assistant_turn_id,
                campaign_state=campaign_state,
                recent_turns=memory_service.load_recent_turns(
                    owner_user_id=owner_user_id, campaign_id=campaign_id
                ),
                parsed_action=parsed_action,
                tool_result=tool_result,
                reply=reply,
                ai_enabled=ai_enabled,
                request_message=request.message,
            )

        return ChatResponse(
            reply=reply,
            campaign_id=campaign_id,
            turn_id=assistant_turn_id,
        )

    def _build_campaign_opening_request(self) -> str:
        return (
            "Start a new haunted halls campaign. Write the opening scene for the player, "
            "establish the immediate tension, and end with a clear invitation for the player's first action."
        )

    def _build_campaign_title_request(self, opening_prompt: str) -> str:
        return (
            "Based on the campaign opening below, provide only a short haunted campaign title with no quotes "
            f"and no extra commentary.\n\n{opening_prompt}"
        )

    async def _maybe_update_memory_layers(
        self,
        *,
        db,
        memory_service: MemoryService,
        owner_user_id: str,
        campaign_id: str,
        user_turn_id: str,
        assistant_turn_id: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        parsed_action,
        tool_result,
        reply: str,
        ai_enabled: bool,
        request_message: str,
    ) -> None:
        try:
            memory_service.maybe_store_semantic_memories(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                user_turn_id=user_turn_id,
                parsed_action=parsed_action,
                tool_result=tool_result,
                request_message=request_message,
                campaign_state=campaign_state,
            )
            if memory_service.should_update_summary(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            ):
                summary_model = ModelPolicy.summarizer_model()
                previous_summary = memory_service.get_current_summary_text(
                    owner_user_id=owner_user_id, campaign_id=campaign_id
                )
                summarizer_payload = MemorySummarizerInput(
                    previous_summary=previous_summary,
                    recent_turns=recent_turns,
                    campaign_state=campaign_state,
                    latest_reply=reply,
                )
                summary_start_time = time.perf_counter()
                summary_estimated_tokens = self._estimate_payload_tokens(summarizer_payload)
                if ai_enabled:
                    self._check_model_call_budget(
                        db,
                        owner_user_id,
                        summary_estimated_tokens,
                        TokenBudget.summarizer_max_output_tokens(),
                    )
                try:
                    summary_output = await self.memory_summarizer_agent.summarize(
                        payload=summarizer_payload,
                        model=summary_model,
                        ai_enabled=ai_enabled,
                    )
                    if ai_enabled:
                        summary_latency_ms = int(
                            (time.perf_counter() - summary_start_time) * 1000
                        )
                        actual_input_tokens = summary_output.input_tokens if summary_output.input_tokens is not None else summary_estimated_tokens
                        actual_output_tokens = summary_output.output_tokens if summary_output.output_tokens is not None else summary_output.token_usage or estimate_tokens(summary_output.summary_text)
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            owner_user_id=owner_user_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_summarizer_agent.name,
                            model=summary_model,
                            estimated_input_tokens=summary_estimated_tokens,
                            actual_input_tokens=actual_input_tokens,
                            actual_output_tokens=actual_output_tokens,
                            latency_ms=summary_latency_ms,
                            success=True,
                            failure_reason=None,
                            cost_estimate=0.0,
                        )
                    memory_service.store_summary(
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        summary_text=summary_output.summary_text,
                    )
                except Exception as exc:
                    if ai_enabled:
                        summary_latency_ms = int(
                            (time.perf_counter() - summary_start_time) * 1000
                        )
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            owner_user_id=owner_user_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_summarizer_agent.name,
                            model=summary_model,
                            estimated_input_tokens=summary_estimated_tokens,
                            actual_input_tokens=summary_estimated_tokens,
                            actual_output_tokens=0,
                            latency_ms=summary_latency_ms,
                            success=False,
                            failure_reason=str(exc),
                            cost_estimate=0.0,
                        )

            if memory_service.should_reflect_memory(
                owner_user_id=owner_user_id, campaign_id=campaign_id
            ):
                reflection_model = ModelPolicy.memory_reflection_model()
                current_summary = memory_service.get_current_summary_text(
                    owner_user_id=owner_user_id, campaign_id=campaign_id
                )
                reflection_payload = MemoryReflectionInput(
                    recent_turns=recent_turns,
                    campaign_state=campaign_state,
                    current_summary=current_summary,
                )
                reflection_start_time = time.perf_counter()
                reflection_estimated_tokens = self._estimate_payload_tokens(reflection_payload)
                if ai_enabled:
                    self._check_model_call_budget(
                        db,
                        owner_user_id,
                        reflection_estimated_tokens,
                        TokenBudget.memory_reflection_max_output_tokens(),
                    )
                try:
                    reflection_output = await self.memory_reflection_agent.reflect(
                        payload=reflection_payload,
                        model=reflection_model,
                        ai_enabled=ai_enabled,
                    )
                    if ai_enabled:
                        reflection_latency_ms = int(
                            (time.perf_counter() - reflection_start_time) * 1000
                        )
                        actual_input_tokens = reflection_output.input_tokens if reflection_output.input_tokens is not None else reflection_estimated_tokens
                        actual_output_tokens = reflection_output.output_tokens if reflection_output.output_tokens is not None else reflection_output.token_usage or estimate_tokens("\n".join(item.text for item in reflection_output.memories_to_store))
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            owner_user_id=owner_user_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_reflection_agent.name,
                            model=reflection_model,
                            estimated_input_tokens=reflection_estimated_tokens,
                            actual_input_tokens=actual_input_tokens,
                            actual_output_tokens=actual_output_tokens,
                            latency_ms=reflection_latency_ms,
                            success=True,
                            failure_reason=None,
                            cost_estimate=0.0,
                        )
                    memory_service.store_reflection_memories(
                        owner_user_id=owner_user_id,
                        campaign_id=campaign_id,
                        source_event_id=assistant_turn_id,
                        memory_candidates=reflection_output.memories_to_store,
                    )
                except Exception as exc:
                    if ai_enabled:
                        reflection_latency_ms = int(
                            (time.perf_counter() - reflection_start_time) * 1000
                        )
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            owner_user_id=owner_user_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_reflection_agent.name,
                            model=reflection_model,
                            estimated_input_tokens=reflection_estimated_tokens,
                            actual_input_tokens=reflection_estimated_tokens,
                            actual_output_tokens=0,
                            latency_ms=reflection_latency_ms,
                            success=False,
                            failure_reason=str(exc),
                            cost_estimate=0.0,
                        )
        except Exception:
            return

    async def _generate_narrator_response(
        self,
        *,
        db,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        message: str,
    ) -> str:
        narrator_payload = NarratorAgentInput(
            player_message=message,
            campaign_state=campaign_state,
            recent_turns=recent_turns,
        )
        estimated_input_tokens = self._estimate_payload_tokens(narrator_payload)
        validate_request_budget(
            estimated_input_tokens, TokenBudget.narrator_max_output_tokens()
        )
        self._check_model_call_budget(
            db,
            owner_user_id,
            estimated_input_tokens,
            TokenBudget.narrator_max_output_tokens(),
        )

        start_time = time.perf_counter()
        try:
            narrator_output = await self.narrator_agent.generate(
                payload=narrator_payload,
                model=model,
            )
            reply = narrator_output.reply_text
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            actual_input_tokens = (
                narrator_output.input_tokens if narrator_output.input_tokens is not None else estimated_input_tokens
            )
            actual_output_tokens = (
                narrator_output.output_tokens if narrator_output.output_tokens is not None else estimate_tokens(reply)
            )
            db.log_model_request(
                request_id=f"req_{uuid4().hex}",
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                agent_name=agent_name,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                actual_input_tokens=actual_input_tokens,
                actual_output_tokens=actual_output_tokens,
                latency_ms=latency_ms,
                success=True,
                failure_reason=None,
                cost_estimate=0.0,
            )
            return reply
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(
                "campaign_narrator_provider_failed owner_user_id=%s campaign_id=%s turn_id=%s model=%s latency_ms=%s error_type=%s error_message=%s",
                owner_user_id,
                campaign_id,
                turn_id,
                model,
                latency_ms,
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            db.log_model_request(
                request_id=f"req_{uuid4().hex}",
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                agent_name=agent_name,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                actual_input_tokens=estimated_input_tokens,
                actual_output_tokens=0,
                latency_ms=latency_ms,
                success=False,
                failure_reason=str(exc),
                cost_estimate=0.0,
            )
            raise HTTPException(status_code=502, detail="AI service failed.")

    def _normalize_campaign_title(self, value: str) -> str:
        candidate = value.strip().strip('"').strip("'")
        if not candidate:
            return self._stub_campaign_title()
        return candidate.splitlines()[0][:80]

    def _stub_campaign_opening(self) -> str:
        return (
            "A cold draft slips through the cracked archway as the lanterns wake one by one. "
            "Somewhere deeper in the halls, a bell tolls once and then goes silent. What do you do first?"
        )

    def _stub_campaign_title(self) -> str:
        return "The Bell Beneath the Hall"

    def _validate_campaign_creation(self, db, owner_user_id: str) -> None:
        owner_campaign_count = db.count_owner_campaigns(owner_user_id)
        if owner_campaign_count >= UsageLimits.MAX_CAMPAIGNS_PER_PLAYER:
            raise usage_limit_error(
                code="max_campaigns",
                detail="Maximum number of campaigns reached.",
            )

    def _stub_reply(self, message: str) -> str:
        return f"AI narrator replies (stub): {message}"


orchestrator = ChatOrchestrator()
