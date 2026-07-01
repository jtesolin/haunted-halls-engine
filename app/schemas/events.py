from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel

GameEventType = Literal["player_message_received", "narrator_response_created"]


class PlayerMessageReceivedPayload(BaseModel):
    message: str


class NarratorResponseCreatedPayload(BaseModel):
    reply: str


GameEventPayload: TypeAlias = PlayerMessageReceivedPayload | NarratorResponseCreatedPayload