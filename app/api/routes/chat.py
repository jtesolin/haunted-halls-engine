from fastapi import APIRouter, Depends

from app.api.dependencies import require_internal_service_auth
from app.orchestration.orchestrator import orchestrator
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(
    payload: ChatRequest,
    _token: None = Depends(require_internal_service_auth),
) -> ChatResponse:
    return await orchestrator.handle_chat(payload)
