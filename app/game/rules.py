from __future__ import annotations

from app.schemas.chat import ActionType


def validate_action(action: str | ActionType) -> bool:
    try:
        normalized = ActionType(str(action).lower())
    except ValueError:
        return False
    return normalized != ActionType.UNKNOWN
