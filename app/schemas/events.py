from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

GameEventType = Literal[
    "player_message_received",
    "narrator_response_created",
    "action_parsed",
    "action_parse_failed",
    "tool_executed",
    "tool_execution_failed",
    "game_state_updated",
]


class PlayerMessageReceivedPayload(BaseModel):
    message: str


class NarratorResponseCreatedPayload(BaseModel):
    reply: str


class ActionParsedPayload(BaseModel):
    action: str
    target: str | None = None
    confidence: float
    stealth: bool
    parse_status: Literal["ok", "ambiguous", "invalid"]
    parser_notes: str | None = None


class ActionParseFailedPayload(BaseModel):
    reason: str


class ToolExecutedPayload(BaseModel):
    applied_tools: list[str] = Field(default_factory=list)
    summary: str
    state_delta: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionFailedPayload(BaseModel):
    action: str
    reason: str


class GameStateUpdatedPayload(BaseModel):
    state: dict[str, Any]


GameEventPayload: TypeAlias = (
    PlayerMessageReceivedPayload
    | NarratorResponseCreatedPayload
    | ActionParsedPayload
    | ActionParseFailedPayload
    | ToolExecutedPayload
    | ToolExecutionFailedPayload
    | GameStateUpdatedPayload
)