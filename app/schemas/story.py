"""Typed contracts for deterministic story/quest progression.

These schemas exist to guarantee that quest progression is driven by typed
signals derived from authoritative gameplay outcomes, never by free-text
player prose or model narration. See `app/game/story.py` for the domain
service that consumes these types.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestStatus(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    COMPLETED = "completed"


class ObjectiveStatus(StrEnum):
    LOCKED = "locked"
    ACTIVE = "active"
    COMPLETED = "completed"


class StorySignalType(StrEnum):
    ROOM_ENTERED = "room_entered"
    NPC_SPOKEN_TO = "npc_spoken_to"
    ITEM_ACQUIRED = "item_acquired"
    FACT_RECORDED = "fact_recorded"


class RoomEnteredSignal(BaseModel):
    """The player entered `room_id`, per authoritative player-movement state."""

    model_config = ConfigDict(extra="forbid")

    signal_type: Literal[StorySignalType.ROOM_ENTERED] = StorySignalType.ROOM_ENTERED
    room_id: str = Field(min_length=1)


class NpcSpokenToSignal(BaseModel):
    """The player spoke to `npc_id`, per authoritative talk-action state."""

    model_config = ConfigDict(extra="forbid")

    signal_type: Literal[StorySignalType.NPC_SPOKEN_TO] = StorySignalType.NPC_SPOKEN_TO
    npc_id: str = Field(min_length=1)


class ItemAcquiredSignal(BaseModel):
    """The player acquired `item_id`, per authoritative inventory state."""

    model_config = ConfigDict(extra="forbid")

    signal_type: Literal[StorySignalType.ITEM_ACQUIRED] = StorySignalType.ITEM_ACQUIRED
    item_id: str = Field(min_length=1)


class FactRecordedSignal(BaseModel):
    """An authoritative fact was recorded, per existing `facts` state semantics."""

    model_config = ConfigDict(extra="forbid")

    signal_type: Literal[StorySignalType.FACT_RECORDED] = StorySignalType.FACT_RECORDED
    fact: str = Field(min_length=1)


StorySignal = (
    RoomEnteredSignal | NpcSpokenToSignal | ItemAcquiredSignal | FactRecordedSignal
)


class StoryProgressionOutcome(StrEnum):
    OBJECTIVE_ADVANCED = "objective_advanced"
    QUEST_COMPLETED = "quest_completed"
    ALREADY_SATISFIED = "already_satisfied"
    NOT_APPLICABLE = "not_applicable"
    INVALID_SIGNAL = "invalid_signal"


class StoryProgressionResult(BaseModel):
    """Structured outcome of `apply_story_signal`.

    Callers must treat `changed` as the only authoritative indicator of a
    state mutation; the remaining fields are diagnostic context for future
    persistence/eventing integration and are not evidence of progression by
    themselves.
    """

    model_config = ConfigDict(extra="forbid")

    changed: bool
    outcome: StoryProgressionOutcome
    reason: str
    quest_id: str | None = None
    objective_id: str | None = None
    previous_quest_status: QuestStatus | None = None
    new_quest_status: QuestStatus | None = None
    previous_objective_status: ObjectiveStatus | None = None
    new_objective_status: ObjectiveStatus | None = None
