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
    "world_action_executed",
    "world_action_failed",
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


class WorldActionExecutedPayload(BaseModel):
    """A privileged Director-proposed WorldAction executed successfully.

    Distinct from player `tool_executed` events: this records Director/world
    authority, not player-authorized deterministic action execution.
    """

    action: str
    summary: str
    changed: bool = False
    state_delta: dict[str, Any] = Field(default_factory=dict)


class WorldActionFailedPayload(BaseModel):
    """A privileged Director-proposed WorldAction that failed semantically.

    Distinct from player `tool_execution_failed` events.
    """

    action: str
    summary: str
    error_code: str | None = None


GameEventPayload: TypeAlias = (
    PlayerMessageReceivedPayload
    | NarratorResponseCreatedPayload
    | ActionParsedPayload
    | ActionParseFailedPayload
    | ToolExecutedPayload
    | ToolExecutionFailedPayload
    | GameStateUpdatedPayload
    | WorldActionExecutedPayload
    | WorldActionFailedPayload
)