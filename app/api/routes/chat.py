from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.dependencies import AuthenticatedUserContext, require_authenticated_user_context
from app.orchestration.orchestrator import orchestrator
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat_echo(
    payload: ChatRequest,
    _user_context: AuthenticatedUserContext = Depends(require_authenticated_user_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ChatResponse:
    normalized_key: str | None = None
    if idempotency_key is not None:
        if len(idempotency_key) > 64:
            raise HTTPException(status_code=400, detail="Invalid Idempotency-Key.")
        try:
            normalized_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Idempotency-Key.") from exc

    return await orchestrator.handle_chat(
        payload,
        owner_user_id=_user_context.internal_user_id,
        idempotency_key=normalized_key,
    )
