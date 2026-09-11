from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.character_progression import ProgressionTrackId


class AbilityAvailabilityStatus(StrEnum):
    """Deterministic ability-availability states."""

    AVAILABLE = "available"
    LOCKED = "locked"
    INSUFFICIENT_PROGRESSION = "insufficient_progression"
    UNKNOWN_ABILITY = "unknown_ability"


class AbilityCheckOutcome(StrEnum):
    """Deterministic outcomes for a bounded ability check."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNAVAILABLE = "unavailable"
    UNKNOWN_ABILITY = "unknown_ability"
    INVALID_DIFFICULTY = "invalid_difficulty"


class AbilityAvailabilityResult(BaseModel):
    """Structured result of evaluating whether an ability is available."""

    model_config = ConfigDict(extra="forbid")

    ability_id: str
    available: bool
    owned: bool
    track_id: ProgressionTrackId | None = None
    track_points: int | None = None
    minimum_points: int | None = None
    status: AbilityAvailabilityStatus
    error_code: str | None = None
    reason: str | None = None


class AbilityCheckResult(BaseModel):
    """Structured result of an authoritative deterministic ability check."""

    model_config = ConfigDict(extra="forbid")

    ability_id: str
    outcome: AbilityCheckOutcome
    resolved: bool
    success: bool | None = None
    track_id: ProgressionTrackId | None = None
    track_points: int | None = None
    difficulty: int | None = None
    margin: int | None = None
    error_code: str | None = None
    reason: str | None = None
