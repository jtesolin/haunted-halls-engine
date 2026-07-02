from __future__ import annotations

from app.agents.base import BaseAgent


class ActionParserAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "ActionParser"
