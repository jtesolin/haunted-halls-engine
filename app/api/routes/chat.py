from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


@router.post("")
async def chat_echo(payload: ChatRequest) -> dict[str, str]:
    return {"message": f"Did you say: {payload.message}?"}
