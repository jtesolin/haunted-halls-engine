from __future__ import annotations

import time
from uuid import uuid4

from fastapi import HTTPException

from app.agents.action_parser import ActionParseProviderError, ActionParserAgent
from app.agents.memory_reflection import MemoryReflectionAgent, MemoryReflectionInput
from app.agents.memory_summarizer import MemorySummarizerAgent, MemorySummarizerInput
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.core.config import settings
from app.db.session import session
from app.guardrails.input_validation import validate_chat_request
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.rate_limits import (
    validate_campaign_turn_limit,
    validate_daily_request_limit,
    validate_daily_token_limit,
)
from app.guardrails.token_budget import estimate_tokens, validate_request_budget
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


class ChatOrchestrator:
    def __init__(self) -> None:
        self.action_parser_agent = ActionParserAgent()
        self.narrator_agent = NarratorAgent()
        self.memory_summarizer_agent = MemorySummarizerAgent()
        self.memory_reflection_agent = MemoryReflectionAgent()
        self.tool_executor = ToolExecutor()

    async def create_campaign(self, request: CampaignCreateRequest) -> CampaignDetail:
        player_id = request.player_id.strip()
        campaign_id = f"campaign_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        agent_name = "Narrator"
        model = ModelPolicy.narrator_model()

        with session() as db:
            self._validate_campaign_creation(db, player_id)

            if not (settings.AI_ENABLED or bool(settings.OPENAI_API_KEY)):
                opening_prompt = self._stub_campaign_opening()
                campaign_name = self._stub_campaign_title()
            else:
                campaign_state = "No campaign state yet."
                recent_turns: list[dict[str, str]] = []

                opening_request = self._build_campaign_opening_request()
                opening_prompt = await self._generate_narrator_response(
                    db=db,
                    player_id=player_id,
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
                        player_id=player_id,
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
                player_id=player_id,
                name=campaign_name,
                description="AI-created campaign",
            )
            assistant_turn = db.create_turn(
                turn_id=assistant_turn_id,
                player_id=player_id,
                campaign_id=campaign_id,
                role="assistant",
                content=opening_prompt,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                player_id=player_id,
                campaign_id=campaign_id,
                turn_id=assistant_turn_id,
                type="narrator_response_created",
                payload=NarratorResponseCreatedPayload(reply=opening_prompt),
            )

        return CampaignDetail(
            campaign_id=campaign_id,
            name=campaign_name,
            description="AI-created campaign",
            player_id=player_id,
            messages=[
                CampaignTurn(
                    turn_id=assistant_turn.turn_id,
                    player_id=assistant_turn.player_id,
                    role=assistant_turn.role,
                    content=assistant_turn.content,
                    created_at=assistant_turn.created_at,
                )
            ],
            truncated=False,
        )

    async def handle_chat(self, request: ChatRequest) -> ChatResponse:
        player_id = request.player_id.strip()
        campaign_id = request.campaign_id or f"campaign_{uuid4().hex}"
        player_turn_id = f"turn_{uuid4().hex}"
        assistant_turn_id = f"turn_{uuid4().hex}"
        agent_name = "Narrator"
        model = ModelPolicy.narrator_model()

        with session() as db:
            validate_chat_request(db, request)
            validate_campaign_turn_limit(db, player_id, campaign_id)

            db.create_campaign(
                campaign_id=campaign_id,
                player_id=player_id,
                name=f"Campaign {campaign_id}",
                description="Auto-created campaign",
            )
            db.create_turn(
                turn_id=player_turn_id,
                player_id=player_id,
                campaign_id=campaign_id,
                role="user",
                content=request.message,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                player_id=player_id,
                campaign_id=campaign_id,
                turn_id=player_turn_id,
                type="player_message_received",
                payload=PlayerMessageReceivedPayload(message=request.message),
            )

            ai_enabled = settings.AI_ENABLED or bool(settings.OPENAI_API_KEY)
            memory_service = MemoryService(db)
            campaign_state = memory_service.build_campaign_state(player_id=player_id, campaign_id=campaign_id)
            recent_turns = memory_service.load_recent_turns(player_id=player_id, campaign_id=campaign_id)
            memory_context = memory_service.load_memory_context(
                player_id=player_id,
                campaign_id=campaign_id,
                query=request.message,
                campaign_state=campaign_state,
                recent_turns=recent_turns,
            )

            estimated_input_tokens = (
                estimate_tokens(request.message)
                + estimate_tokens("structured action parsing and tool execution context")
                + estimate_tokens(memory_service.format_memory_context(memory_context))
            )
            validate_request_budget(estimated_input_tokens, settings.MAX_OUTPUT_TOKENS)
            validate_daily_request_limit(db, player_id)
            validate_daily_token_limit(db, player_id, estimated_input_tokens)

            parser_start_time = time.perf_counter()
            try:
                parsed_action = await self.action_parser_agent.parse(
                    message=request.message,
                    campaign_state=campaign_state,
                    recent_turns=recent_turns,
                    memory_context=memory_context,
                    model=ModelPolicy.action_parser_model(),
                    deterministic_only=not ai_enabled,
                )
            except ActionParseProviderError as exc:
                parser_latency_ms = int((time.perf_counter() - parser_start_time) * 1000)
                db.log_model_request(
                    request_id=f"req_{uuid4().hex}",
                    player_id=player_id,
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
                raise HTTPException(status_code=502, detail="Action parser service failed.") from exc

            if ai_enabled:
                parser_latency_ms = int((time.perf_counter() - parser_start_time) * 1000)
                db.log_model_request(
                    request_id=f"req_{uuid4().hex}",
                    player_id=player_id,
                    campaign_id=campaign_id,
                    turn_id=player_turn_id,
                    agent_name="ActionParser",
                    model=ModelPolicy.action_parser_model(),
                    estimated_input_tokens=estimated_input_tokens,
                    actual_input_tokens=estimated_input_tokens,
                    actual_output_tokens=estimate_tokens(parsed_action.model_dump_json()),
                    latency_ms=parser_latency_ms,
                    success=True,
                    failure_reason=None,
                    cost_estimate=0.0,
                )

            if parsed_action.parse_status == "invalid":
                reason = parsed_action.parser_notes or "Action parser produced invalid output."
                db.add_event(
                    event_id=f"evt_{uuid4().hex}",
                    player_id=player_id,
                    campaign_id=campaign_id,
                    turn_id=player_turn_id,
                    type="action_parse_failed",
                    payload=ActionParseFailedPayload(reason=reason),
                )
                raise HTTPException(status_code=422, detail=f"Unprocessable action: {reason}")

            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                player_id=player_id,
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
                        player_id=player_id,
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
                        player_id=player_id,
                        campaign_id=campaign_id,
                        turn_id=player_turn_id,
                        type="tool_execution_failed",
                        payload=ToolExecutionFailedPayload(
                            action=parsed_action.action,
                            reason=tool_result.summary,
                        ),
                    )

                if tool_result.state_delta:
                    db.update_campaign_state(campaign_id, updated_state)
                    db.add_event(
                        event_id=f"evt_{uuid4().hex}",
                        player_id=player_id,
                        campaign_id=campaign_id,
                        turn_id=player_turn_id,
                        type="game_state_updated",
                        payload=GameStateUpdatedPayload(state=updated_state),
                    )
                    campaign_state = memory_service.build_campaign_state(player_id=player_id, campaign_id=campaign_id)

            if not ai_enabled:
                reply = self._stub_reply(request.message)
                db.log_model_request(
                    request_id=f"req_{uuid4().hex}",
                    player_id=player_id,
                    campaign_id=campaign_id,
                    turn_id=assistant_turn_id,
                    agent_name=agent_name,
                    model=model,
                    estimated_input_tokens=estimated_input_tokens,
                    actual_input_tokens=estimated_input_tokens,
                    actual_output_tokens=0,
                    latency_ms=0,
                    success=True,
                    failure_reason=None,
                    cost_estimate=0.0,
                )
            else:
                start_time = time.perf_counter()
                try:
                    narrator_output = await self.narrator_agent.generate(
                        payload=NarratorAgentInput(
                            player_message=request.message,
                            campaign_state=campaign_state,
                            recent_turns=recent_turns,
                            relevant_memories=memory_context,
                            parsed_action=parsed_action,
                            tool_result=tool_result,
                        ),
                        model=model,
                    )
                    reply = narrator_output.reply_text
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    db.log_model_request(
                        request_id=f"req_{uuid4().hex}",
                        player_id=player_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
                        agent_name=agent_name,
                        model=model,
                        estimated_input_tokens=estimated_input_tokens,
                        actual_input_tokens=estimated_input_tokens,
                        actual_output_tokens=len(reply) // 1,
                        latency_ms=latency_ms,
                        success=True,
                        failure_reason=None,
                        cost_estimate=0.0,
                    )
                except Exception as exc:
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    db.log_model_request(
                        request_id=f"req_{uuid4().hex}",
                        player_id=player_id,
                        campaign_id=campaign_id,
                        turn_id=assistant_turn_id,
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

            db.create_turn(
                turn_id=assistant_turn_id,
                player_id=player_id,
                campaign_id=campaign_id,
                role="assistant",
                content=reply,
            )
            db.add_event(
                event_id=f"evt_{uuid4().hex}",
                player_id=player_id,
                campaign_id=campaign_id,
                turn_id=assistant_turn_id,
                type="narrator_response_created",
                payload=NarratorResponseCreatedPayload(reply=reply),
            )

            await self._maybe_update_memory_layers(
                db=db,
                memory_service=memory_service,
                player_id=player_id,
                campaign_id=campaign_id,
                user_turn_id=player_turn_id,
                assistant_turn_id=assistant_turn_id,
                campaign_state=campaign_state,
                recent_turns=memory_service.load_recent_turns(player_id=player_id, campaign_id=campaign_id),
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
        player_id: str,
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
                player_id=player_id,
                campaign_id=campaign_id,
                user_turn_id=user_turn_id,
                parsed_action=parsed_action,
                tool_result=tool_result,
                request_message=request_message,
                campaign_state=campaign_state,
            )
            if memory_service.should_update_summary(player_id=player_id, campaign_id=campaign_id):
                summary_model = ModelPolicy.summarizer_model()
                previous_summary = memory_service.get_current_summary_text(player_id=player_id, campaign_id=campaign_id)
                summarizer_payload = MemorySummarizerInput(
                    previous_summary=previous_summary,
                    recent_turns=recent_turns,
                    campaign_state=campaign_state,
                    latest_reply=reply,
                )
                summary_start_time = time.perf_counter()
                try:
                    summary_output = await self.memory_summarizer_agent.summarize(
                        payload=summarizer_payload,
                        model=summary_model,
                        ai_enabled=ai_enabled,
                    )
                    if ai_enabled:
                        summary_latency_ms = int((time.perf_counter() - summary_start_time) * 1000)
                        estimated_tokens = estimate_tokens(summarizer_payload.model_dump_json())
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            player_id=player_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_summarizer_agent.name,
                            model=summary_model,
                            estimated_input_tokens=estimated_tokens,
                            actual_input_tokens=estimated_tokens,
                            actual_output_tokens=summary_output.token_usage or estimate_tokens(summary_output.summary_text),
                            latency_ms=summary_latency_ms,
                            success=True,
                            failure_reason=None,
                            cost_estimate=0.0,
                        )
                    memory_service.store_summary(
                        player_id=player_id,
                        campaign_id=campaign_id,
                        summary_text=summary_output.summary_text,
                    )
                except Exception as exc:
                    if ai_enabled:
                        summary_latency_ms = int((time.perf_counter() - summary_start_time) * 1000)
                        estimated_tokens = estimate_tokens(summarizer_payload.model_dump_json())
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            player_id=player_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_summarizer_agent.name,
                            model=summary_model,
                            estimated_input_tokens=estimated_tokens,
                            actual_input_tokens=estimated_tokens,
                            actual_output_tokens=0,
                            latency_ms=summary_latency_ms,
                            success=False,
                            failure_reason=str(exc),
                            cost_estimate=0.0,
                        )

            if memory_service.should_reflect_memory(player_id=player_id, campaign_id=campaign_id):
                reflection_model = ModelPolicy.memory_reflection_model()
                current_summary = memory_service.get_current_summary_text(player_id=player_id, campaign_id=campaign_id)
                reflection_payload = MemoryReflectionInput(
                    recent_turns=recent_turns,
                    campaign_state=campaign_state,
                    current_summary=current_summary,
                )
                reflection_start_time = time.perf_counter()
                try:
                    reflection_output = await self.memory_reflection_agent.reflect(
                        payload=reflection_payload,
                        model=reflection_model,
                        ai_enabled=ai_enabled,
                    )
                    if ai_enabled:
                        reflection_latency_ms = int((time.perf_counter() - reflection_start_time) * 1000)
                        estimated_tokens = estimate_tokens(reflection_payload.model_dump_json())
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            player_id=player_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_reflection_agent.name,
                            model=reflection_model,
                            estimated_input_tokens=estimated_tokens,
                            actual_input_tokens=estimated_tokens,
                            actual_output_tokens=reflection_output.token_usage
                            or estimate_tokens("\n".join(item.text for item in reflection_output.memories_to_store)),
                            latency_ms=reflection_latency_ms,
                            success=True,
                            failure_reason=None,
                            cost_estimate=0.0,
                        )
                    memory_service.store_reflection_memories(
                        player_id=player_id,
                        campaign_id=campaign_id,
                        source_event_id=assistant_turn_id,
                        memory_candidates=reflection_output.memories_to_store,
                    )
                except Exception as exc:
                    if ai_enabled:
                        reflection_latency_ms = int((time.perf_counter() - reflection_start_time) * 1000)
                        estimated_tokens = estimate_tokens(reflection_payload.model_dump_json())
                        db.log_model_request(
                            request_id=f"req_{uuid4().hex}",
                            player_id=player_id,
                            campaign_id=campaign_id,
                            turn_id=assistant_turn_id,
                            agent_name=self.memory_reflection_agent.name,
                            model=reflection_model,
                            estimated_input_tokens=estimated_tokens,
                            actual_input_tokens=estimated_tokens,
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
        player_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        message: str,
    ) -> str:
        estimated_input_tokens = estimate_tokens(message)
        validate_request_budget(estimated_input_tokens, settings.MAX_OUTPUT_TOKENS)
        validate_daily_request_limit(db, player_id)
        validate_daily_token_limit(db, player_id, estimated_input_tokens)

        start_time = time.perf_counter()
        try:
            narrator_output = await self.narrator_agent.generate(
                payload=NarratorAgentInput(
                    player_message=message,
                    campaign_state=campaign_state,
                    recent_turns=recent_turns,
                ),
                model=model,
            )
            reply = narrator_output.reply_text
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            db.log_model_request(
                request_id=f"req_{uuid4().hex}",
                player_id=player_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                agent_name=agent_name,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                actual_input_tokens=estimated_input_tokens,
                actual_output_tokens=len(reply),
                latency_ms=latency_ms,
                success=True,
                failure_reason=None,
                cost_estimate=0.0,
            )
            return reply
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            db.log_model_request(
                request_id=f"req_{uuid4().hex}",
                player_id=player_id,
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

    def _validate_campaign_creation(self, db, player_id: str) -> None:
        if not player_id:
            raise HTTPException(status_code=422, detail="player_id is required")
        if player_id.lower() == "anonymous":
            raise HTTPException(status_code=422, detail="player_id cannot be 'anonymous'")
        player_campaign_count = db.count_player_campaigns(player_id)
        if player_campaign_count >= UsageLimits.MAX_CAMPAIGNS_PER_PLAYER:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Player has reached the maximum number of campaigns "
                    f"({UsageLimits.MAX_CAMPAIGNS_PER_PLAYER})."
                ),
            )

    def _stub_reply(self, message: str) -> str:
        return f"AI narrator replies (stub): {message}"


orchestrator = ChatOrchestrator()