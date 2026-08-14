from __future__ import annotations

import asyncio

from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.schemas.chat import ParsedAction, ToolExecutionResult, ActionType


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
        ),
    )

    result = asyncio.run(agent.generate(payload=payload))

    assert result.reply_text == "You step into the corridor."
    tool_message = next(message for message in captured_messages if message["content"].startswith("Tool execution result"))
    assert '"current_location": "grand_corridor"' in tool_message["content"]
    assert '"current_room_name": "Grand Corridor"' in tool_message["content"]