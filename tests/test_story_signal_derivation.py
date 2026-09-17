from __future__ import annotations

from app.game.story import apply_story_signal, derive_story_signal
from app.schemas.chat import ActionType, ParsedAction, ToolExecutionResult
from app.schemas.story import ItemAcquiredSignal, NpcSpokenToSignal, RoomEnteredSignal


def test_derive_story_signal_from_successful_movement() -> None:
    signal = derive_story_signal(
        ParsedAction(raw_text="go east", action=ActionType.MOVE, target="east", parse_status="ok"),
        ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="You move east.",
            current_location="library",
            resolved_exit="library",
        ),
    )

    assert signal == RoomEnteredSignal(room_id="library")


def test_derive_story_signal_from_successful_talk_and_take() -> None:
    talk_signal = derive_story_signal(
        ParsedAction(raw_text="talk to ghost", action=ActionType.TALK, target="library_ghost", parse_status="ok"),
        ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You address the ghost.",
            npc_id="library_ghost",
        ),
    )
    take_signal = derive_story_signal(
        ParsedAction(raw_text="take book", action=ActionType.TAKE, target="old_book", parse_status="ok"),
        ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            item_id="old_book",
        ),
    )

    assert talk_signal == NpcSpokenToSignal(npc_id="library_ghost")
    assert take_signal == ItemAcquiredSignal(item_id="old_book")


def test_derive_story_signal_ignores_failed_or_uncertain_results() -> None:
    assert derive_story_signal(None, ToolExecutionResult(success=False, summary="No move.")) is None
    assert (
        derive_story_signal(
            ParsedAction(raw_text="take book", action=ActionType.TAKE, target="old_book", parse_status="ok"),
            ToolExecutionResult(
                success=True,
                applied_tools=["take_item"],
                summary="You try to take the old book.",
                item_id=None,
            ),
        )
        is None
    )


def test_story_progression_is_idempotent_for_duplicate_matches() -> None:
    state = {
        "player": {"location": "entry_hall", "inventory": []},
        "items": {},
        "npcs": {},
        "clock": {"tick": 0},
        "facts": [],
    }
    first = apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    second = apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    assert first.changed is True
    assert second.changed is False
    assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
