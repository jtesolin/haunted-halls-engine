from fastapi import APIRouter

from app.ai.orchestrator import orchestrator
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(payload: ChatRequest) -> ChatResponse:
    return await orchestrator.handle_chat(payload)
