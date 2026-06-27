from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GameEvent:
    event_id: str
    type: str
    payload: dict[str, Any]
