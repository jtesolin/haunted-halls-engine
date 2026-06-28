from typing import Annotated, Optional

from pydantic import BaseModel, StringConstraints, field_validator

PlayerId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ChatRequest(BaseModel):
    message: str
    campaign_id: Optional[str] = None
    character_id: Optional[str] = None
    player_id: PlayerId

    @field_validator("player_id")
    @classmethod
    def validate_player_id(cls, value: str) -> str:
        if value.lower() == "anonymous":
            raise ValueError("player_id cannot be 'anonymous'")
        return value


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
