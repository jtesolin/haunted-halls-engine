from __future__ import annotations

from app.orchestration.orchestrator import ChatOrchestrator
from app.schemas.chat import NarratorRoom, NarratorSceneContext


def test_stub_campaign_opening_fallback_avoids_duplicate_article() -> None:
    orchestrator = ChatOrchestrator()
    scene_context = NarratorSceneContext(current_room=NarratorRoom())

    opening = orchestrator._stub_campaign_opening(scene_context)

    assert "the the" not in opening.lower()
    assert opening.startswith("You stand in the Entry Hall.")


def test_stub_campaign_opening_uses_real_room_name_when_available() -> None:
    orchestrator = ChatOrchestrator()
    scene_context = NarratorSceneContext(
        current_room=NarratorRoom(id="library", name="Library", description="Tall shelves crowd the walls."),
    )

    opening = orchestrator._stub_campaign_opening(scene_context)

    assert opening.startswith("You stand in the Library.")
