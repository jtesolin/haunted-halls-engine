"""Bounded persisted contracts for campaign-specific generated abilities."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.abilities import AbilityCheckResult
from app.schemas.character_progression import ProgressionTrackId


class GeneratedAbilityKind(StrEnum):
    SENSORY = "sensory"
    UTILITY = "utility"


class AbilityEffect(StrEnum):
    SENSE = "sense"
    MINOR_UTILITY = "minor_utility"
    MOVE = "move"
    TOGGLE = "toggle"


class AbilityDomain(StrEnum):
    SURROUNDINGS = "surroundings"
    OBJECT = "object"


class AbilityChannel(StrEnum):
    SUPERNATURAL = "supernatural"


class AbilityDetail(StrEnum):
    LIMITED = "limited"
    PRACTICAL = "practical"


class AbilitySenseFilter(StrEnum):
    PRESENCE = "presence"
    SUPERNATURAL_PRESENCE = "supernatural_presence"


class AbilityObjectMotion(StrEnum):
    TOWARD_PLAYER = "toward_player"


class AbilityObjectState(StrEnum):
    OPEN = "open"
    LIT = "lit"


class AbilityObjectEffectOperation(StrEnum):
    RETRIEVE = "retrieve"
    TOGGLE_OPEN = "toggle_open"
    TOGGLE_LIT = "toggle_lit"


class AbilityPresenceEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sense_filter: AbilitySenseFilter
    found: bool
    current_room_count: int = Field(ge=0)
    adjacent_room_count: int = Field(ge=0)


class AbilityObjectEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AbilityObjectEffectOperation
    item_id: str
    item_name: str
    previous_value: bool | None = None
    new_value: bool | None = None


class GeneratedAbilityMechanics(BaseModel):
    """Engine-owned primitives; no model-authored executable expressions."""

    model_config = ConfigDict(extra="forbid")

    effect: AbilityEffect
    domain: AbilityDomain
    channel: AbilityChannel
    detail: AbilityDetail
    range: int = Field(ge=0, le=1)
    requires: tuple[str, ...] = Field(default=(), max_length=2)
    bypasses: tuple[str, ...] = Field(default=(), max_length=2)
    sense_filter: AbilitySenseFilter | None = None
    object_motion: AbilityObjectMotion | None = None
    object_state: AbilityObjectState | None = None

    @field_validator("requires", "bypasses")
    @classmethod
    def collection_values_must_be_distinct(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Generated ability mechanic values must not contain duplicates.")
        return values

    @model_validator(mode="after")
    def validate_primitive_combinations(self) -> "GeneratedAbilityMechanics":
        if self.effect == AbilityEffect.SENSE:
            valid_detail = (
                self.sense_filter is None
                or self.detail == AbilityDetail.LIMITED
            )
            valid = (
                self.domain == AbilityDomain.SURROUNDINGS
                and self.object_motion is None
                and self.object_state is None
                and valid_detail
            )
        elif self.effect == AbilityEffect.MINOR_UTILITY:
            valid = (
                self.domain == AbilityDomain.OBJECT
                and self.sense_filter is None
                and self.object_motion is None
                and self.object_state is None
            )
        elif self.effect == AbilityEffect.MOVE:
            valid = (
                self.domain == AbilityDomain.OBJECT
                and self.sense_filter is None
                and self.object_motion == AbilityObjectMotion.TOWARD_PLAYER
                and self.object_state is None
                and self.detail == AbilityDetail.PRACTICAL
                and self.range == 0
            )
        else:
            valid = (
                self.domain == AbilityDomain.OBJECT
                and self.sense_filter is None
                and self.object_motion is None
                and self.object_state is not None
                and self.detail == AbilityDetail.PRACTICAL
                and self.range == 0
            )
        if not valid:
            raise ValueError("Generated ability mechanic primitives are contradictory.")
        return self


class GeneratedAbilityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    kind: GeneratedAbilityKind
    mechanics: GeneratedAbilityMechanics
    track: ProgressionTrackId
    minimum_points: int = Field(ge=0, le=10)


class StarterAbilityGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abilities: list[GeneratedAbilityDefinition] = Field(min_length=2, max_length=2)


class AbilityGameplayStatus(StrEnum):
    RESOLVED = "resolved"
    UNKNOWN_ABILITY = "unknown_ability"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported_mechanic"
    INELIGIBLE_CONTEXT = "ineligible_context"


class AbilityGameplayResult(BaseModel):
    """Narrow authoritative result of a player ability request."""

    model_config = ConfigDict(extra="forbid")

    ability_id: str
    display_name: str | None = None
    description: str | None = None
    owned: bool = False
    available: bool = False
    status: AbilityGameplayStatus
    check_id: str | None = None
    check_result: AbilityCheckResult | None = None
    presence_effect: AbilityPresenceEffect | None = None
    object_effect: AbilityObjectEffect | None = None
    error_code: str | None = None
    reason: str | None = None
