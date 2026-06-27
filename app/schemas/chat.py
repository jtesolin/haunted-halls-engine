from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    campaign_id: Optional[str] = None
    character_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    campaign_id: str
    turn_id: str


class ChatMessage(BaseModel):
    message: str
    campaign_id: Optional[str] = None
    character_id: Optional[str] = None


class GameTurnResult(BaseModel):
    reply: str
    campaign_id: str
    turn_id: str
