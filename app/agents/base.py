from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent name for logging and orchestration."""
