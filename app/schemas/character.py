from __future__ import annotations

from pydantic import BaseModel


class CharacterInfo(BaseModel):
    character_id: str
    name: str


class CharacterInventoryItem(BaseModel):
    id: str
    name: str
    description: str
    quantity: int


class CharacterAbility(BaseModel):
    ability_id: str
    display_name: str
    description: str
    available: bool
    availability_reason: str | None = None


class CharacterDetail(CharacterInfo):
    """Single-character response with persisted inventory and owned abilities."""

    inventory: list[CharacterInventoryItem]
    abilities: list[CharacterAbility]


class CharacterList(BaseModel):
    characters: list[CharacterInfo]
