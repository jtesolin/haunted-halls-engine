from __future__ import annotations

from uuid import uuid4

from app.ai.model_client import model_client
from app.ai.prompts import narrator_prompt
from app.db.session import session
from app.schemas.chat import ChatRequest, ChatResponse


class ChatOrchestrator:
    async def handle_chat(self, request: ChatRequest) -> ChatResponse:
        prompt = self._build_prompt(request)
        reply = await model_client.generate_text(prompt)
        campaign_id = request.campaign_id or f"auto_{uuid4().hex}"
        turn_id = f"turn_{uuid4().hex}"

        player_id = request.player_id

        with session() as repo:
            repo.create_campaign(
                campaign_id=campaign_id,
                player_id=player_id,
                name=f"Campaign {campaign_id}",
                description="Auto-created campaign",
            )
            repo.save_turn(
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


orchestrator = ChatOrchestrator()
