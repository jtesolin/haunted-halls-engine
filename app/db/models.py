from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Optional


INTERNAL_USER_ID_REGEX = re.compile(r"^user_[0-9a-f]{32}$")


def is_valid_internal_user_id(value: str) -> bool:
    return bool(INTERNAL_USER_ID_REGEX.fullmatch(value))


@dataclass
class CampaignDBModel:
    campaign_id: str
    name: str
    description: Optional[str]
    state: Optional[str]
    created_at: datetime
    owner_user_id: Optional[str] = None


@dataclass
class CharacterDBModel:
    character_id: str
    campaign_id: str
    name: str
    description: Optional[str]
    created_at: datetime


@dataclass
class TurnDBModel:
    turn_id: str
    campaign_id: str
    role: str
    content: str
    created_at: datetime


@dataclass
class GameEventDBModel:
    event_id: str
    campaign_id: str
    turn_id: Optional[str]
    type: str
    payload_json: Optional[str]
    created_at: datetime


@dataclass
class SummaryDBModel:
    campaign_id: str
    summary: str
    created_at: datetime


@dataclass
class MemoryDBModel:
    memory_id: str
    campaign_id: str
    kind: str
    content: str
    embedding_json: str
    importance: float
    source_event_id: Optional[str]
    created_at: datetime


@dataclass
class InternalUserDBModel:
    id: str
    identity_provider: str
    provider_issuer: str
    provider_subject: str
    email: str
    email_verified: bool
    display_name: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime
