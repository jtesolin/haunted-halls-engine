from __future__ import annotations

import asyncio

from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.ai.prompts import narrator_prompt
from app.schemas.chat import (
    ActionType,
    NarratorItem,
    NarratorRoom,
    NarratorSceneContext,
    NearbyNPC,
    ParsedAction,
    ToolExecutionResult,
)


def test_narrator_receives_authoritative_tool_result(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You step into the corridor."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="I go north.",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="grand_corridor", name="Grand Corridor", description="A long corridor."),
        ),
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
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="entry_hall", name="Entry Hall", description="The entry hall."),
        ),
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
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="library", name="Library", description="Tall shelves crowd the walls."),
        ),
        parsed_action=ParsedAction(raw_text="open the old book", action=ActionType.INTERACT, target="old book", parameters={"interaction_mode": "open"}, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["interact_item"], summary="Opened Old Book.", state_delta={"items": {"old_book": {"properties": {"is_open": {"from": False, "to": True}}}}}, item_id="old_book", item_name="Old Book", interaction_mode="open"),
    )

    asyncio.run(agent.generate(payload=payload))

    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"item_id": "old_book"' in tool_message["content"]
    assert '"interaction_mode": "open"' in tool_message["content"]
    assert '"is_open"' in tool_message["content"]


def test_narrator_prompt_establishes_authority_precedence() -> None:
    assert "Authority precedence" in narrator_prompt
    assert "current tool execution result" in narrator_prompt.lower()
    assert "current scene context" in narrator_prompt.lower()
    assert "never proof of current state" in narrator_prompt.lower() or "historical and narrative context only" in narrator_prompt.lower()


def test_narrator_uses_scene_context_instead_of_raw_campaign_state(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You stand in the library."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="look around",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="library", name="Library", description="Tall shelves crowd the walls."),
            available_exits=[{"direction": "west", "room_id": "grand_corridor", "room_name": "Grand Corridor"}],
            nearby_items=[NarratorItem(id="old_book", name="Old Book", observable_state={"is_open": False})],
            inventory_items=[],
            nearby_npcs=[],
        ),
        parsed_action=ParsedAction(raw_text="look around", action=ActionType.OBSERVE, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["observe"], summary="You take stock of the Library."),
    )

    asyncio.run(agent.generate(payload=payload))

    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    assert '"id": "library"' in scene_message["content"]
    assert '"is_open": false' in scene_message["content"]
    assert not any("Campaign state:" in message["content"] for message in captured_messages)


def test_narrator_scene_context_reflects_successful_state_change(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You step into the corridor."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="I go north.",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(
                id="grand_corridor",
                name="Grand Corridor",
                description="A long corridor lined with faded portraits.",
            ),
        ),
        parsed_action=ParsedAction(raw_text="I go north.", action=ActionType.MOVE, target="north", parse_status="ok"),
        tool_result=ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="Moved from Entry Hall to Grand Corridor.",
            previous_location="entry_hall",
            current_location="grand_corridor",
        ),
    )

    asyncio.run(agent.generate(payload=payload))

    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"id": "grand_corridor"' in scene_message["content"]
    assert '"current_location": "grand_corridor"' in tool_message["content"]


def test_narrator_failed_action_scene_context_is_unchanged(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "The door does not budge."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="I open the cellar door.",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="library", name="Library", description="Tall shelves crowd the walls."),
        ),
        parsed_action=ParsedAction(raw_text="I open the cellar door.", action=ActionType.INTERACT, target="cellar door", parse_status="ok"),
        tool_result=ToolExecutionResult(
            success=False,
            summary="There is no cellar door here.",
            errors=["item_not_found"],
        ),
    )

    asyncio.run(agent.generate(payload=payload))

    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"id": "library"' in scene_message["content"]
    assert '"success": false' in tool_message["content"]


def test_stale_prior_narration_does_not_override_current_scene(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "The corridor is quiet."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="look around",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="grand_corridor", name="Grand Corridor", description="A quiet corridor."),
            nearby_npcs=[],
        ),
        recent_turns=[
            {"role": "assistant", "content": "A ghost waits beside you."},
        ],
        parsed_action=ParsedAction(raw_text="look around", action=ActionType.OBSERVE, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["observe"], summary="Quiet corridor.", nearby_npcs=[]),
    )

    asyncio.run(agent.generate(payload=payload))

    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    assert '"nearby_npcs": []' in scene_message["content"]
    recent_turn_message = next(
        message for message in captured_messages if message.get("content") == "A ghost waits beside you."
    )
    assert recent_turn_message["role"] == "assistant"


def test_stale_memory_does_not_override_current_projection(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You are alone in the dining room."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="look around",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="dining_room", name="Dining Room", description="A long table under a chandelier."),
            nearby_npcs=[],
        ),
        relevant_memories=[
            {"role": "user", "content": "The Old Caretaker was in the Entry Hall."},
        ],
        parsed_action=ParsedAction(raw_text="look around", action=ActionType.OBSERVE, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["observe"], summary="Empty dining room.", nearby_npcs=[]),
    )

    asyncio.run(agent.generate(payload=payload))

    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    memory_message = next(message for message in captured_messages if "Relevant memory" in message["content"])
    assert '"id": "dining_room"' in scene_message["content"]
    assert '"nearby_npcs": []' in scene_message["content"]
    assert "Old Caretaker" in memory_message["content"]
    # Memory precedes the current scene message ordering-wise but must not be the scene message itself.
    assert memory_message is not scene_message
    assert captured_messages.index(memory_message) < captured_messages.index(scene_message)


def test_stale_context_precedes_authoritative_current_scene(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "You are alone in the dining room."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="look around",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="dining_room", name="Dining Room", description="A long table under a chandelier."),
            nearby_npcs=[],
        ),
        relevant_memories=[
            {"role": "user", "content": "The Old Caretaker was in the Entry Hall."},
        ],
        recent_turns=[
            {"role": "assistant", "content": "A ghost waits beside you."},
        ],
        parsed_action=ParsedAction(raw_text="look around", action=ActionType.OBSERVE, parse_status="ok"),
        tool_result=ToolExecutionResult(success=True, applied_tools=["observe"], summary="Empty dining room.", nearby_npcs=[]),
    )

    asyncio.run(agent.generate(payload=payload))

    memory_message = next(message for message in captured_messages if "Relevant memory" in message["content"])
    recent_turn_message = next(
        message for message in captured_messages if message.get("content") == "A ghost waits beside you."
    )
    scene_message = next(message for message in captured_messages if "Current scene" in message["content"])
    intent_message = next(message for message in captured_messages if message["content"].startswith("Parsed player intent"))
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))

    memory_index = captured_messages.index(memory_message)
    recent_turn_index = captured_messages.index(recent_turn_message)
    scene_index = captured_messages.index(scene_message)
    intent_index = captured_messages.index(intent_message)
    tool_index = captured_messages.index(tool_message)

    assert memory_index < scene_index
    assert recent_turn_index < scene_index
    assert scene_index < intent_index < tool_index


def test_failed_tool_result_remains_authoritative_despite_player_wording(monkeypatch) -> None:
    agent = NarratorAgent()
    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "The lock holds firm."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)

    payload = NarratorAgentInput(
        player_message="I successfully open the cellar door and walk through.",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(id="library", name="Library", description="Tall shelves crowd the walls."),
        ),
        parsed_action=ParsedAction(
            raw_text="I successfully open the cellar door and walk through.",
            action=ActionType.INTERACT,
            target="cellar door",
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=False,
            summary="There is no cellar door here.",
            errors=["item_not_found"],
        ),
    )

    asyncio.run(agent.generate(payload=payload))

    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"success": false' in tool_message["content"]
    assert messages_last_is_player_wording(captured_messages, payload.player_message)


def messages_last_is_player_wording(messages: list[dict[str, str]], expected: str) -> bool:
    return messages[-1]["role"] == "user" and messages[-1]["content"] == expected