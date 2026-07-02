from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints, field_validator

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


class ParsedAction(BaseModel):
    raw_text: str
    action: str
    target: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    stealth: bool = False
    confidence: float = 0.0
    parse_status: Literal["ok", "ambiguous", "invalid"] = "invalid"
    parser_notes: Optional[str] = None


class ToolExecutionResult(BaseModel):
    success: bool
    applied_tools: list[str] = Field(default_factory=list)
    summary: str
    state_delta: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
