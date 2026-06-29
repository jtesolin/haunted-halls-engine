from __future__ import annotations

import json
import time
from uuid import uuid4

from fastapi import HTTPException

from app.ai.model_client import model_client
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
from app.schemas.chat import ChatRequest, ChatResponse


class ChatOrchestrator:
    async def handle_chat(self, request: ChatRequest) -> ChatResponse:
        player_id = request.player_id.strip()
        campaign_id = request.campaign_id or f"auto_{uuid4().hex}"
        turn_id = f"turn_{uuid4().hex}"
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

            if not settings.AI_ENABLED:
                reply = self._stub_reply(request.message)
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
                    latency_ms=0,
                    success=True,
                    failure_reason=None,
                    cost_estimate=0.0,
                )
            else:
                prompt = self._build_prompt(request)
                start_time = time.perf_counter()
                try:
                    reply = await model_client.generate_text(
                        prompt,
                        model=model,
                        max_output_tokens=settings.MAX_OUTPUT_TOKENS,
                        temperature=0.7,
                        timeout=20,
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

            db.save_turn(
                campaign_id=campaign_id,
                turn_id=turn_id,
                player_message=request.message,
                ai_reply=reply,
            )

        return ChatResponse(
            reply=reply,
            campaign_id=campaign_id,
            turn_id=turn_id,
        )

    def _build_prompt(self, request: ChatRequest) -> str:
        return f"{narrator_prompt}\n\nPlayer says: {request.message}"

    def _stub_reply(self, message: str) -> str:
        return f"AI narrator replies (stub): {message}"


orchestrator = ChatOrchestrator()
