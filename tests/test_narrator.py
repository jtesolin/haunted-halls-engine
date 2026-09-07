from __future__ import annotations

import asyncio

from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.schemas.chat import ActionType, NearbyNPC, ParsedAction, ToolExecutionResult


def test_narrator_receives_authoritative_tool_result(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You step into the corridor."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="I go north.",
        campaign_state='{"player": {"location": "grand_corridor", "inventory": []}}',
        recent_turns=[],
        parsed_action=ParsedAction(
            raw_text="I go north.",
            action=ActionType.MOVE,
            target="north",
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="Moved from Entry Hall to Grand Corridor.",
            state_delta={"player": {"location": {"from": "entry_hall", "to": "grand_corridor"}}},
            previous_location="entry_hall",
            current_location="grand_corridor",
            requested_target="north",
            resolved_exit="grand_corridor",
            previous_room_name="Entry Hall",
            current_room_name="Grand Corridor",
            current_room_description="A long corridor lined with faded portraits and creaking floorboards.",
            available_exits=[{"direction": "east", "room_id": "library", "room_name": "Library"}],
            nearby_npcs=[
                NearbyNPC(
                    id="library_ghost",
                    name="Library Ghost",
                    description="A pale figure drifts between the shelves.",
                    status="active",
                    disposition="neutral",
                )
            ],
        ),
    )

    result = asyncio.run(agent.generate(payload=payload))

    assert result.reply_text == "You step into the corridor."
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"current_location": "grand_corridor"' in tool_message["content"]
    assert '"current_room_name": "Grand Corridor"' in tool_message["content"]
    assert '"id": "library_ghost"' in tool_message["content"]
    assert "NPC presence, location, status, and disposition are authoritative game state." in captured_messages[0]["content"]


def test_narrator_receives_authoritative_talk_target(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You address the Old Caretaker."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="talk to old caretaker",
        campaign_state='{"player": {"location": "entry_hall", "inventory": []}}',
        recent_turns=[],
        parsed_action=ParsedAction(
            raw_text="talk to old caretaker",
            action=ActionType.TALK,
            target="old caretaker",
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You address the Old Caretaker.",
            state_delta={},
            npc_id="old_caretaker",
            npc_name="Old Caretaker",
            npc_status="active",
            npc_disposition="neutral",
        ),
    )

    result = asyncio.run(agent.generate(payload=payload))

    assert result.reply_text == "You address the Old Caretaker."
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"npc_id": "old_caretaker"' in tool_message["content"]
    assert '"npc_name": "Old Caretaker"' in tool_message["content"]


def test_narrator_receives_authoritative_item_interaction(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "The old book opens."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)
    payload = NarratorAgentInput(
        player_message="open the old book",
        campaign_state='{"player": {"location": "library"}}',
        parsed_action=ParsedAction(raw_text="open the old book", action=ActionType.INTERACT, target="old book", parameters={"interaction_mode": "open"}, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["interact_item"], summary="Opened Old Book.", state_delta={"items": {"old_book": {"properties": {"is_open": {"from": False, "to": True}}}}}, item_id="old_book", item_name="Old Book", interaction_mode="open"),
    )

    asyncio.run(agent.generate(payload=payload))

    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"item_id": "old_book"' in tool_message["content"]
    assert '"interaction_mode": "open"' in tool_message["content"]
    assert '"is_open"' in tool_message["content"]