from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorldActionType(StrEnum):
    MOVE_NPC = "move_npc"
    SET_NPC_STATUS = "set_npc_status"
    ADVANCE_CLOCK = "advance_clock"
    RECORD_FACT = "record_fact"


class MoveNpcWorldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[WorldActionType.MOVE_NPC] = WorldActionType.MOVE_NPC
    npc_id: str = Field(min_length=1)
    destination_room_id: str = Field(min_length=1)


class SetNpcStatusWorldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[WorldActionType.SET_NPC_STATUS] = WorldActionType.SET_NPC_STATUS
    npc_id: str = Field(min_length=1)
    status: Literal["active", "absent"]


class AdvanceClockWorldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[WorldActionType.ADVANCE_CLOCK] = WorldActionType.ADVANCE_CLOCK
    ticks: int = Field(ge=1, le=10)


class RecordFactWorldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[WorldActionType.RECORD_FACT] = WorldActionType.RECORD_FACT
    fact: str


WorldAction = MoveNpcWorldAction | SetNpcStatusWorldAction | AdvanceClockWorldAction | RecordFactWorldAction


class WorldActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    changed: bool = False
    action: str
    summary: str
    state_delta: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    errors: list[str] = Field(default_factory=list)
