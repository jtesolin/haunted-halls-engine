from __future__ import annotations


def validate_action(action: str) -> bool:
    allowed_actions = {"look", "move", "take", "use", "talk"}
    return action.lower() in allowed_actions
