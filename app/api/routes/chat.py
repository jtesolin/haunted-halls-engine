from fastapi import APIRouter, Depends

from app.api.dependencies import AuthenticatedUserContext, require_authenticated_user_context
from app.orchestration.orchestrator import orchestrator
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(
    payload: ChatRequest,
    _user_context: AuthenticatedUserContext = Depends(require_authenticated_user_context),
) -> ChatResponse:
    return await orchestrator.handle_chat(payload, owner_user_id=_user_context.internal_user_id)
