from __future__ import annotations

from app.ai.model_client import model_client
from app.ai.prompts import narrator_prompt
from app.schemas.chat import ChatRequest, ChatResponse


class ChatOrchestrator:
    async def handle_chat(self, request: ChatRequest) -> ChatResponse:
        prompt = self._build_prompt(request)
        reply = await model_client.generate_text(prompt)
        return ChatResponse(
            reply=reply,
            campaign_id=request.campaign_id or "",
            turn_id="turn_0001",
        )

    def _build_prompt(self, request: ChatRequest) -> str:
        return f"{narrator_prompt}\n\nPlayer says: {request.message}"


orchestrator = ChatOrchestrator()
