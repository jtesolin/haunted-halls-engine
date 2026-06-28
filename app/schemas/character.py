from __future__ import annotations

from pydantic import BaseModel


class CharacterInfo(BaseModel):
    character_id: str
    name: str


class CharacterList(BaseModel):
    characters: list[CharacterInfo]
