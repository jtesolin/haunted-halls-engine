from enum import StrEnum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class ActionType(StrEnum):
    OBSERVE = "observe"
    MOVE = "move"
    CLIMB = "climb"
    TAKE = "take"
    DROP = "drop"
    USE = "use"
    TALK = "talk"
    ATTACK = "attack"
    WAIT = "wait"
    INTERACT = "interact"
    UNKNOWN = "unknown"


ParseStatus = Literal["ok", "ambiguous", "invalid"]


class ActionParserParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: Optional[str] = None
    amount: Optional[int] = None
    duration: Optional[str] = None
    with_item: Optional[str] = None
    interaction_mode: Optional[str] = None


class ActionParserOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionType
    target: Optional[str] = None
    parameters: ActionParserParameters = Field(default_factory=ActionParserParameters)
    stealth: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    parse_status: ParseStatus
    parser_notes: Optional[str] = None


class ParsedAction(BaseModel):
    raw_text: str
    action: ActionType = ActionType.UNKNOWN
    target: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    stealth: bool = False
    confidence: float = 0.0
    parse_status: ParseStatus = "invalid"
    parser_notes: Optional[str] = None


class ToolExecutionResult(BaseModel):
    success: bool
    applied_tools: list[str] = Field(default_factory=list)
    summary: str
    state_delta: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    previous_location: str | None = None
    current_location: str | None = None
    requested_target: str | None = None
    resolved_exit: str | None = None
    error_code: str | None = None
    previous_room_name: str | None = None
    current_room_name: str | None = None
    current_room_description: str | None = None
    available_exits: list[dict[str, str]] = Field(default_factory=list)
