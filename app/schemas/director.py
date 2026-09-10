from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import ActionType, ParseStatus
from app.schemas.world import WorldAction


class DirectorNPCContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    npc_id: str = Field(min_length=1)
    location_id: str = Field(min_length=1)
    status: Literal["active", "absent"]
    one_hop_destination_room_ids: list[str] = Field(default_factory=list)


class DirectorPlayerActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionType
    target: str | None = None
    parse_status: ParseStatus
    succeeded: bool
    result_summary: str
    applied_tools: list[str] = Field(default_factory=list)
    error_code: str | None = None


class DirectorInput(BaseModel):
    """Bounded authoritative context supplied to a future Director."""

    model_config = ConfigDict(extra="forbid")

    current_player_room_id: str = Field(min_length=1)
    clock_tick: int = Field(ge=0)
    facts: list[str] = Field(default_factory=list)
    npcs: list[DirectorNPCContext] = Field(default_factory=list)
    player_action: DirectorPlayerActionContext


class NoActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["none"] = "none"


class WorldActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["act"] = "act"
    world_action: WorldAction


DirectorProposal = Annotated[
    NoActionProposal | WorldActionProposal,
    Field(discriminator="decision"),
]
