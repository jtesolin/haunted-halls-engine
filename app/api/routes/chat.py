from fastapi import APIRouter

from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(payload: ChatRequest) -> ChatResponse:
    return ChatResponse(
        reply=f"Did you say: {payload.message}?",
        campaign_id=payload.campaign_id or "",
        turn_id="turn_0001",
    )
