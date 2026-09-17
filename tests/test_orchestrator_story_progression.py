from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import session
from app.game.campaign_state import build_fresh_campaign_state
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.chat import ActionType, ChatRequest, ParsedAction, ToolExecutionResult


def _enable_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INTERNAL_ENGINE_SERVICE_TOKEN", "test-token")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")


def _disable_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)


def _resolve_user(client: TestClient, provider_subject: str) -> str:
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": "https://accounts.google.com",
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Test Player",
            "avatar_url": None,
        },
        headers={"Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN}"},
    )
    assert response.status_code == 200
    return response.json()["user_id"]


def _create_campaign(*, user_id: str, campaign_id: str, state: dict | None = None) -> str:
    with session() as db:
        db.create_campaign(
            campaign_id=campaign_id,
            owner_user_id=user_id,
            name=f"Campaign {campaign_id}",
            description="Story progression regression test",
            state=state or build_fresh_campaign_state(),
        )
    return campaign_id


def _load_campaign_state(campaign) -> dict:
    state_raw = campaign.state
    assert state_raw is not None
    return json.loads(state_raw)


def _story_state(*, objective_1: str = "active", objective_2: str = "locked", objective_3: str = "locked") -> dict:
    state = build_fresh_campaign_state()
    state["story"] = {
        "quests": {
            "librarys_whisper": {
                "status": "active",
                "objectives": {
                    "enter_library": objective_1,
                    "speak_to_library_ghost": objective_2,
                    "acquire_old_book": objective_3,
                },
            }
        }
    }
    return state


def test_story_progression_move_into_library_advances_objective_1(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="go to the library",
            action=ActionType.MOVE,
            target="library",
            confidence=0.94,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The library stirs.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        assert parsed_action.action == ActionType.MOVE
        state = build_fresh_campaign_state()
        state["player"]["location"] = "library"
        state["story"] = _story_state()["story"]
        state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] = "completed"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="You enter the library.",
            state_delta={"player": {"location": {"from": "entry_hall", "to": "library"}}},
            current_location="library",
            resolved_exit="library",
        )
        return state, tool_result

    original_build_director_input = orchestrator_module.build_director_input

    def capture_build_director_input(state, *, parsed_action, tool_result, world=None):
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
        return original_build_director_input(
            state,
            parsed_action=parsed_action,
            tool_result=tool_result,
            world=world,
        )

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        assert director_input.current_player_room_id == "library"
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module, "build_director_input", capture_build_director_input)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-move-objective-1")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go to the library"), owner_user_id=user_id
        )
    )

    assert response.reply == "The library stirs."
    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["player"]["location"] == "library"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"


def test_failed_move_does_not_progress(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="go to the library",
            action=ActionType.MOVE,
            target="library",
            confidence=0.9,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The library remains still.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state()
        state["player"]["location"] = "entry_hall"
        tool_result = ToolExecutionResult(
            success=False,
            applied_tools=[],
            summary="The door bars the way.",
            state_delta={},
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-failed-move")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go to the library"), owner_user_id=user_id
        )
    )

    assert response.reply == "The library remains still."
    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "active"


def test_talk_to_library_ghost_before_objective_1_cannot_skip_ordering(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The ghost waits.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state()
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-talk-before-objective")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost"), owner_user_id=user_id
        )
    )

    assert response.reply == "The ghost waits."
    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "locked"


def test_talk_after_objective_1_advances_objective_2(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The whisper deepens.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-talk-after-objective-1")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost"), owner_user_id=user_id
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "active"


def test_failed_talk_does_not_progress(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The ghost stays silent.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
        tool_result = ToolExecutionResult(
            success=False,
            applied_tools=[],
            summary="The ghost ignores you.",
            state_delta={},
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-failed-talk")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost"), owner_user_id=user_id
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "active"


def test_take_after_objectives_1_and_2_completes_quest(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="take the old book",
            action=ActionType.TAKE,
            target="old_book",
            confidence=1.0,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The book settles into your hands.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            state_delta={},
            item_id="old_book",
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-take-final-quest")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="take the old book"), owner_user_id=user_id
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["status"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "completed"


def test_failed_take_or_mentioning_old_book_does_not_progress(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    cases = (
        (
            "take the old book",
            ActionType.TAKE,
            ToolExecutionResult(
                success=False,
                applied_tools=[],
                summary="The book slips away.",
                state_delta={},
                item_id="old_book",
            ),
        ),
        (
            "old book",
            ActionType.OBSERVE,
            ToolExecutionResult(
                success=True,
                applied_tools=["observe"],
                summary="You glance at the old book.",
                state_delta={},
                item_id=None,
            ),
        ),
    )

    for message, action, tool_result in cases:
        async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
            return ParsedAction(
                raw_text=message,
                action=action,
                target="old_book" if "old" in message else None,
                confidence=1.0,
                parse_status="ok",
            )

        async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
            return type("NarratorOutput", (), {"reply_text": "Nothing changes.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

        def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
            state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
            return state, tool_result

        async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
            return type("DirectorResult", (), {"proposal": None, "usage": None})()

        monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
        monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
        monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
        monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

        client = TestClient(app)
        user_id = _resolve_user(client, f"story-take-no-progress-{message.replace(' ', '-')}")
        response = asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message=message), owner_user_id=user_id
            )
        )
        with session() as db:
            campaign = db.get_campaign(response.campaign_id)
            state = _load_campaign_state(campaign)
            assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "active"


def test_provider_disabled_mode_still_advances_story(monkeypatch) -> None:
    _disable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-provider-disabled")
    campaign_id = "campaign_story_provider_disabled"
    initial_state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
    initial_state["player"]["location"] = "library"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=initial_state,
    )
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
            owner_user_id=user_id,
        )
    )

    assert response.reply == "AI narrator replies (stub): talk to the ghost"
    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "completed"


def test_story_progress_with_empty_state_delta_persists_and_reloads_on_next_turn(monkeypatch) -> None:
    _enable_provider(monkeypatch)
    seen_states = []

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = json.loads(campaign_state) if isinstance(campaign_state, str) else dict(campaign_state)
        if "story" not in state:
            state["story"] = _story_state(objective_1="completed", objective_2="active", objective_3="locked")["story"]
        seen_states.append(json.dumps(state["story"]))
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "The whisper deepens.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-empty-delta-persistence")
    campaign_id = "campaign_story_persisted_turn"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=_story_state(objective_1="completed", objective_2="active", objective_3="locked"),
    )
    asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
            owner_user_id=user_id,
        )
    )
    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "completed"

    second_response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
            owner_user_id=user_id,
        )
    )
    assert second_response.campaign_id == campaign_id
    assert '"speak_to_library_ghost": "completed"' in seen_states[-1]


def test_non_advancing_signal_does_not_write_story_only_state(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="observe the room",
            action=ActionType.OBSERVE,
            confidence=1.0,
            parse_status="ok",
        )

    async def fake_narrator(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("NarratorOutput", (), {"reply_text": "You survey the room.", "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0})()

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state()
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["observe"],
            summary="You survey the room.",
            state_delta={},
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return type("DirectorResult", (), {"proposal": None, "usage": None})()

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-non-advancing-signal")
    campaign_id = "campaign_story_non_advancing_signal"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=_story_state(),
    )
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="observe the room", campaign_id=campaign_id),
            owner_user_id=user_id,
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "active"
        events = db.list_campaign_events(response.campaign_id)
        assert not any(event.type == "game_state_updated" for event in events)


def test_director_failure_rolls_story_progression_back_with_transaction(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):  # noqa: ANN001, ANN202
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):  # noqa: ANN001, ANN002, ANN202
        state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        raise RuntimeError("director failed")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-director-failure-rollback")
    campaign_id = "campaign_story_director_rollback"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=_story_state(objective_1="completed", objective_2="active", objective_3="locked"),
    )

    try:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
                owner_user_id=user_id,
            )
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected director failure to propagate")

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        if campaign is not None:
            state = _load_campaign_state(campaign)
            assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "active"
