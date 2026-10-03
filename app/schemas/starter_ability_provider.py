"""Provider-only contract for generating starter abilities."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.character_progression import ProgressionTrackId
from app.schemas.generated_abilities import AbilitySenseFilter


class StarterUtilityOperation(StrEnum):
    RETRIEVE = "retrieve"
    TOGGLE_OPEN = "toggle_open"
    TOGGLE_LIT = "toggle_lit"


class StarterAbilitySensoryProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    description: str
    track: ProgressionTrackId
    sense_filter: AbilitySenseFilter
    range: Literal[0, 1]


class StarterAbilityUtilityProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    description: str
    track: ProgressionTrackId
    operation: StarterUtilityOperation


class StarterAbilityProviderGeneration(BaseModel):
    """The model's bounded choices, without engine-owned mechanics."""

    model_config = ConfigDict(extra="forbid")

    sensory_ability: StarterAbilitySensoryProviderOutput
    utility_ability: StarterAbilityUtilityProviderOutput
