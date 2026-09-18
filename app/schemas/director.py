from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import ActionType, ParseStatus
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.story import QuestStatus
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


class DirectorStoryObjectiveContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective_id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class DirectorStoryQuestContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quest_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: QuestStatus
    completed_objective_ids: list[str] = Field(default_factory=list)
    active_objective: DirectorStoryObjectiveContext | None = None


class DirectorStoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quests: list[DirectorStoryQuestContext] = Field(default_factory=list)


class DirectorProgressionTrackContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: ProgressionTrackId
    points: int = Field(ge=0)


class DirectorAbilityContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    short_description: str = Field(min_length=1)
    track_id: ProgressionTrackId


class DirectorCharacterContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    progression_tracks: list[DirectorProgressionTrackContext] = Field(default_factory=list)
    available_abilities: list[DirectorAbilityContext] = Field(default_factory=list)


class DirectorInput(BaseModel):
    """Bounded authoritative context supplied to a future Director."""

    model_config = ConfigDict(extra="forbid")

    current_player_room_id: str = Field(min_length=1)
    clock_tick: int = Field(ge=0)
    facts: list[str] = Field(default_factory=list)
    npcs: list[DirectorNPCContext] = Field(default_factory=list)
    player_action: DirectorPlayerActionContext
    story: DirectorStoryContext
    character: DirectorCharacterContext


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
