"""Bounded persisted contracts for campaign-specific generated abilities."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.abilities import AbilityCheckResult
from app.schemas.character_progression import ProgressionTrackId


class GeneratedAbilityKind(StrEnum):
    SENSORY = "sensory"
    UTILITY = "utility"


class AbilityEffect(StrEnum):
    SENSE = "sense"
    MINOR_UTILITY = "minor_utility"


class AbilityDomain(StrEnum):
    SURROUNDINGS = "surroundings"
    OBJECT = "object"


class AbilityChannel(StrEnum):
    SUPERNATURAL = "supernatural"


class AbilityDetail(StrEnum):
    LIMITED = "limited"
    PRACTICAL = "practical"


class GeneratedAbilityMechanics(BaseModel):
    """Engine-owned primitives; no model-authored executable expressions."""

    model_config = ConfigDict(extra="forbid")

    effect: AbilityEffect
    domain: AbilityDomain
    channel: AbilityChannel
    detail: AbilityDetail
    range: int = Field(ge=0, le=1)
    requires: tuple[str, ...] = ()
    bypasses: tuple[str, ...] = ()


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

    abilities: tuple[GeneratedAbilityDefinition, GeneratedAbilityDefinition]


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
    available: bool = False
    status: AbilityGameplayStatus
    check_id: str | None = None
    check_result: AbilityCheckResult | None = None
    error_code: str | None = None
    reason: str | None = None
