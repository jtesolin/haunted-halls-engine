from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Campaign:
    campaign_id: str
    name: str
    description: str
    state: dict[str, Any]
