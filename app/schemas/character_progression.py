"""Structured result contracts for the character-progression domain.

These models describe the *shape* of deterministic progression operation
results (see `app/game/character_progression.py`). They intentionally do not
represent the persisted campaign-state progression namespace itself, which is
a plain JSON-serializable dict so it can live inside the existing campaign
state document alongside items/npcs/clock/facts.

This module is separate from `app/schemas/character.py`, which contains the
lightweight `CharacterInfo` / `CharacterList` API list/info DTOs. Those DTOs
are unrelated to game-domain progression and must not be repurposed here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProgressionTrackId(StrEnum):
    """Stable identifiers for the supported progression tracks.

    Deliberately small and exploration/narrative oriented rather than a
    combat-oriented attribute set.
    """

    INVESTIGATION = "investigation"
    RESOLVE = "resolve"
    RAPPORT = "rapport"
    OCCULT = "occult"


class ProgressionGrantResult(BaseModel):
    """Structured outcome of a `grant_progress` call."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    changed: bool = False
    track_id: str
    prior_points: int
    new_points: int
    capped: bool = False
    error_code: str | None = None
    reason: str | None = None


class AbilityUnlockResult(BaseModel):
    """Structured outcome of an `unlock_ability` call."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    changed: bool = False
    ability_id: str
    already_unlocked: bool = False
    error_code: str | None = None
    reason: str | None = None


class NarratorProgressionGrant(BaseModel):
    """Narrow authoritative projection of points earned on the current turn."""

    model_config = ConfigDict(extra="forbid")

    track_id: ProgressionTrackId
    prior_points: int
    new_points: int


class NarratorUnlockedAbility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_id: str
    display_name: str


class NarratorProgressionReward(BaseModel):
    """Narrow authoritative projection of one earned authored reward."""

    model_config = ConfigDict(extra="forbid")

    reward_id: str
    progression_grants: list[NarratorProgressionGrant] = Field(default_factory=list)
    unlocked_abilities: list[NarratorUnlockedAbility] = Field(default_factory=list)
