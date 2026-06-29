from fastapi import APIRouter, Depends

from app.ai.orchestrator import orchestrator
from app.api.dependencies import require_internal_api_token
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(
    payload: ChatRequest,
    _token: None = Depends(require_internal_api_token),
) -> ChatResponse:
    return await orchestrator.handle_chat(payload)
