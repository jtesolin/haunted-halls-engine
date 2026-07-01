from __future__ import annotations

import time
from uuid import uuid4

from fastapi import HTTPException

from app.ai.model_client import model_client
from app.ai.narrator_agent import NarratorAgent2
from app.ai.prompts import narrator_prompt
from app.core.config import settings
from app.db.session import session
from app.guardrails.input_validation import validate_chat_request
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import estimate_tokens, validate_request_budget
from app.guardrails.rate_limits import (
    validate_campaign_turn_limit,
    validate_daily_request_limit,
    validate_daily_token_limit,
)
from app.guardrails.usage_limits import UsageLimits
from app.schemas.campaign import CampaignCreateRequest, CampaignDetail, CampaignTurn
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.events import NarratorResponseCreatedPayload, PlayerMessageReceivedPayload


class ChatOrchestrator:
    def __init__(self) -> None:
        self.narrator_agent = NarratorAgent2()

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

            estimated_input_tokens = estimate_tokens(request.message)
            validate_request_budget(estimated_input_tokens, settings.MAX_OUTPUT_TOKENS)
            validate_daily_request_limit(db, player_id)
            validate_daily_token_limit(db, player_id, estimated_input_tokens)

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

            if not (settings.AI_ENABLED or bool(settings.OPENAI_API_KEY)):
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
                campaign_state = self._build_campaign_state(db, campaign_id)
                recent_turns = self._load_recent_turns(db, campaign_id)
                start_time = time.perf_counter()
                try:
                    reply = await self.narrator_agent.generate(
                        campaign_state=campaign_state,
                        recent_turns=recent_turns,
                        message=request.message,
                        model=model,
                    )
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

        return ChatResponse(
            reply=reply,
            campaign_id=campaign_id,
            turn_id=assistant_turn_id,
        )

    def _build_prompt(self, request: ChatRequest) -> str:
        return f"{narrator_prompt}\n\nPlayer says: {request.message}"

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

    def _build_campaign_state(self, db, campaign_id: str) -> str:
        campaign = db.get_campaign(campaign_id)
        if campaign is None:
            return "No campaign state yet."
        return campaign.state or "No campaign state yet."

    def _load_recent_turns(self, db, campaign_id: str) -> list[dict[str, str]]:
        _, turns, _ = db.get_campaign_with_turns(campaign_id, limit=settings.MAX_RECENT_MESSAGES)
        return [
            {
                "role": turn.role,
                "content": turn.content,
            }
            for turn in turns
        ]

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
            reply = await self.narrator_agent.generate(
                campaign_state=campaign_state,
                recent_turns=recent_turns,
                message=message,
                model=model,
            )
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
