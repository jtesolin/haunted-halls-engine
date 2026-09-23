from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents.director import DirectorProviderError
from app.api.routes import chat as chat_routes
from app.core.config import settings
from app.db.session import session
from app.game.campaign_state import build_fresh_campaign_state
from app.game.world import DEFAULT_WORLD
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.orchestration.orchestrator import ChatOrchestrator
from app.schemas.chat import ActionType, ChatRequest, ParsedAction, ToolExecutionResult
from app.schemas.director import NoActionProposal
from app.schemas.story import NpcSpokenToSignal


@pytest.fixture(autouse=True)
def story_progression_isolation(monkeypatch):
    original_ai_enabled = settings.AI_ENABLED
    original_openai_key = settings.OPENAI_API_KEY
    original_orchestrator = orchestrator_module.orchestrator
    original_chat_orchestrator = chat_routes.orchestrator

    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"
    orchestrator_module.orchestrator = ChatOrchestrator()
    chat_routes.orchestrator = orchestrator_module.orchestrator

    try:
        yield
    finally:
        settings.AI_ENABLED = original_ai_enabled
        settings.OPENAI_API_KEY = original_openai_key
        orchestrator_module.orchestrator = original_orchestrator
        chat_routes.orchestrator = original_chat_orchestrator


def _enable_provider(monkeypatch) -> None:
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
    raw = campaign.state
    assert raw is not None
    return json.loads(raw)


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


def _stub_narrator_reply(reply_text: str):
    class _NarratorResult:
        def __init__(self, reply_text: str):
            self.reply_text = reply_text
            self.input_tokens = 0
            self.cached_input_tokens = 0
            self.cache_write_input_tokens = 0
            self.output_tokens = 0
            self.reasoning_output_tokens = 0
            self.total_tokens = 0

    return _NarratorResult(reply_text)


async def _async_stub_narrator_reply(reply_text: str):
    return _stub_narrator_reply(reply_text)


async def _stub_director_response(*, director_input, model=None):
    class _DirectorResult:
        proposal = NoActionProposal()
        usage = None

    return _DirectorResult()


def test_story_progression_move_into_library_advances_objective_1(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="go to the library",
            action=ActionType.MOVE,
            target="library",
            confidence=0.94,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        assert parsed_action.action == ActionType.MOVE
        state = _story_state(objective_1="active")
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="You enter the library.",
            state_delta={"player": {"location": {"from": "entry_hall", "to": "library"}}},
            previous_location="entry_hall",
            current_location="library",
            resolved_exit="library",
        )
        return state, tool_result

    original_build_director_input = orchestrator_module.build_director_input

    def capture_build_director_input(state, *, parsed_action, tool_result, world=DEFAULT_WORLD):
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
        assert world == DEFAULT_WORLD
        return original_build_director_input(
            state,
            parsed_action=parsed_action,
            tool_result=tool_result,
            world=world,
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The library stirs."))
    monkeypatch.setattr(orchestrator_module, "build_director_input", capture_build_director_input)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-move-objective-1")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go to the library"), owner_user_id=user_id
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
        assert state["player"]["location"] == "library"


def test_failed_move_does_not_progress(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="go to the library",
            action=ActionType.MOVE,
            target="library",
            confidence=0.9,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="active")
        state["player"]["location"] = "entry_hall"
        tool_result = ToolExecutionResult(
            success=False,
            applied_tools=[],
            summary="The path is blocked.",
            state_delta={},
            current_location="entry_hall",
        )
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The library remains still."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-failed-move")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="go to the library"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "active"


def test_talk_to_library_ghost_before_objective_1_cannot_skip_ordering(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="active", objective_2="locked")
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak with the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The ghost waits."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-talk-before-objective-1")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="talk to the ghost"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "active"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "locked"


def test_successful_talk_after_objective_1_advances_objective_2(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
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
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The whisper deepens."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-talk-after-objective-1")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="talk to the ghost"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "active"


@pytest.mark.parametrize(
    ("error_code", "summary"),
    [
        ("npc_not_present", "The ghost is not here."),
        ("npc_not_found", "No ghost is present."),
        ("ambiguous_npc", "Multiple ghosts are nearby."),
    ],
)
def test_failed_talk_variants_do_not_progress(monkeypatch, error_code: str, summary: str) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="completed", objective_2="active", objective_3="locked")
        tool_result = ToolExecutionResult(
            success=False,
            applied_tools=[],
            summary=summary,
            state_delta={},
            error_code=error_code,
            npc_id=None,
        )
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The ghost stays silent."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, f"story-failed-talk-{error_code}")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="talk to the ghost"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "active"


def test_take_after_objectives_1_and_2_completes_quest(monkeypatch) -> None:
    _enable_provider(monkeypatch)
    captured_director_inputs = []

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="take the old book", action=ActionType.TAKE, target="old_book", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            state_delta={},
            item_id="old_book",
        )
        return state, tool_result

    original_build_director_input = orchestrator_module.build_director_input

    def capture_build_director_input(state, *, parsed_action, tool_result, world=DEFAULT_WORLD):
        director_input = original_build_director_input(
            state,
            parsed_action=parsed_action,
            tool_result=tool_result,
            world=world,
        )
        captured_director_inputs.append(director_input)
        return director_input

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The book settles into your hands."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)
    monkeypatch.setattr(orchestrator_module, "build_director_input", capture_build_director_input)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-take-final-quest")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="take the old book"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["status"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "completed"
        assert captured_director_inputs[-1].character.progression_tracks[0].points == 2
        assert [ability.ability_id for ability in captured_director_inputs[-1].character.available_abilities] == [
            "keen_eye"
        ]
        assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
            "librarys_whisper_completion"
        ]


def test_malformed_reward_claims_roll_back_quest_completion(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="take the old book",
            action=ActionType.TAKE,
            target="old_book",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = json.loads(campaign_state)
        return state, ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            state_delta={},
            item_id="old_book",
        )

    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse
    )
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-malformed-reward-claims")
    campaign_id = "campaign_story_malformed_reward_claims"
    initial_state = _story_state(
        objective_1="completed",
        objective_2="completed",
        objective_3="active",
    )
    initial_state["player"]["progression_rewards"] = {
        "claimed_reward_ids": ["librarys_whisper_completion", 3]
    }
    _create_campaign(user_id=user_id, campaign_id=campaign_id, state=initial_state)

    with pytest.raises(HTTPException, match="Campaign state could not be processed"):
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="take the old book", campaign_id=campaign_id),
                owner_user_id=user_id,
            )
        )

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state = _load_campaign_state(campaign)
        quest = state["story"]["quests"]["librarys_whisper"]
        assert quest["status"] == "active"
        assert quest["objectives"]["acquire_old_book"] == "active"
        assert "progression" not in state["player"]
        assert state["player"]["progression_rewards"] == {
            "claimed_reward_ids": ["librarys_whisper_completion", 3]
        }


@pytest.mark.parametrize(
    ("message", "action", "tool_result"),
    [
        (
            "take the old book",
            ActionType.TAKE,
            ToolExecutionResult(success=False, applied_tools=[], summary="The book slips away.", state_delta={}, item_id="old_book"),
        ),
        (
            "old book",
            ActionType.OBSERVE,
            ToolExecutionResult(success=True, applied_tools=["observe"], summary="You glance at the old book.", state_delta={}, item_id=None),
        ),
    ],
)
def test_failed_take_or_mentioning_old_book_does_not_progress(monkeypatch, message: str, action: ActionType, tool_result: ToolExecutionResult) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text=message, action=action, target="old_book" if "old" in message else None, confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("Nothing changes."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, f"story-book-non-advancing-{action.value}")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message=message), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "active"


def test_story_progression_occurs_before_director_and_receives_post_story_state(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="go to the library", action=ActionType.MOVE, target="library", confidence=0.94, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="active")
        state["player"]["location"] = "library"
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

    def capture_build_director_input(state, *, parsed_action, tool_result, world=DEFAULT_WORLD):
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
        return original_build_director_input(
            state,
            parsed_action=parsed_action,
            tool_result=tool_result,
            world=world,
        )

    async def fake_propose(*, director_input, model=None):
        assert director_input.current_player_room_id == "library"
        return await _stub_director_response(director_input=director_input, model=model)

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module, "build_director_input", capture_build_director_input)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The library stirs."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-director-receives-post-story-state")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="go to the library"), owner_user_id=user_id)
    )

    assert response.reply == "The library stirs."
    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"


def test_director_input_contains_same_turn_post_story_progression_with_real_tool_executor(
    monkeypatch,
) -> None:
    _enable_provider(monkeypatch)
    captured_director_inputs = []

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="go east to the library",
            action=ActionType.MOVE,
            target="library",
            confidence=0.94,
            parse_status="ok",
        )

    async def fake_propose(*, director_input, model=None):
        captured_director_inputs.append(director_input)
        return await _stub_director_response(director_input=director_input, model=model)

    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse
    )
    monkeypatch.setattr(
        orchestrator_module.orchestrator.narrator_agent,
        "generate",
        lambda **kwargs: _async_stub_narrator_reply("The library stirs."),
    )
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-director-real-tool-post-state")
    campaign_id = "campaign_story_real_tool_post_state"
    state = build_fresh_campaign_state()
    state["player"]["location"] = "grand_corridor"
    _create_campaign(user_id=user_id, campaign_id=campaign_id, state=state)

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go east to the library", campaign_id=campaign_id),
            owner_user_id=user_id,
        )
    )

    assert response.reply == "The library stirs."
    assert len(captured_director_inputs) == 1
    director_input = captured_director_inputs[0]
    assert director_input.current_player_room_id == "library"
    quest = director_input.story.quests[0]
    assert quest.quest_id == "librarys_whisper"
    assert quest.completed_objective_ids == ["enter_library"]
    assert quest.active_objective is not None
    assert quest.active_objective.objective_id == "speak_to_library_ghost"

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        persisted_state = _load_campaign_state(campaign)
        objectives = persisted_state["story"]["quests"]["librarys_whisper"]["objectives"]
        assert objectives["enter_library"] == "completed"
        assert objectives["speak_to_library_ghost"] == "active"


def test_provider_disabled_mode_still_performs_story_progression(monkeypatch) -> None:
    _disable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="go to the library", action=ActionType.MOVE, target="library", confidence=0.94, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="active")
        state["player"]["location"] = "library"
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="You enter the library.",
            state_delta={"player": {"location": {"from": "entry_hall", "to": "library"}}},
            current_location="library",
            resolved_exit="library",
        )
        return state, tool_result

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The library stirs."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-provider-disabled-progression")
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="go to the library"), owner_user_id=user_id)
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"


def test_talk_with_empty_state_delta_persists_story_progress_and_reloads_on_later_turn(monkeypatch) -> None:
    _enable_provider(monkeypatch)
    seen_states = []

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = json.loads(campaign_state) if isinstance(campaign_state, str) else dict(campaign_state)
        if "story" not in state:
            state["story"] = _story_state(objective_1="completed", objective_2="active", objective_3="locked")["story"]
        if "player" not in state:
            state["player"] = {"location": "library", "inventory": []}
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

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The whisper deepens."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

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


def test_non_advancing_signal_does_not_write_a_story_only_state_update(monkeypatch) -> None:
    _enable_provider(monkeypatch)
    derived_signals = []

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="talk to the ghost",
            action=ActionType.TALK,
            target="library_ghost",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="active")
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You speak to the ghost.",
            state_delta={},
            npc_id="library_ghost",
        )
        return state, tool_result

    original_derive_story_signal = orchestrator_module.derive_story_signal

    def capture_story_signal(tool_result):
        signal = original_derive_story_signal(tool_result)
        derived_signals.append(signal)
        return signal

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", capture_story_signal)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("You speak to the ghost."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-non-advancing-signal")
    campaign_id = "campaign_story_non_advancing_signal"
    _create_campaign(user_id=user_id, campaign_id=campaign_id, state=_story_state(objective_1="active"))

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
    assert derived_signals == [NpcSpokenToSignal(npc_id="library_ghost")]


def test_completed_idempotent_replay_does_not_call_story_progression_again(monkeypatch) -> None:
    _disable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
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

    def fail_story_derivation(*args, **kwargs):
        raise AssertionError("Story derivation should not run on a replayed completed request.")

    def fail_story_progression(*args, **kwargs):
        raise AssertionError("apply_story_signal should not run on a replayed completed request.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", lambda **kwargs: _async_stub_narrator_reply("The whisper deepens."))
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-idempotent-replay")
    campaign_id = "campaign_story_idempotent_replay"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=_story_state(objective_1="completed", objective_2="active", objective_3="locked"),
    )

    first = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
            owner_user_id=user_id,
            idempotency_key="story-replay-key",
        )
    )

    monkeypatch.setattr(orchestrator_module, "derive_story_signal", fail_story_derivation)
    monkeypatch.setattr(orchestrator_module, "apply_story_signal", fail_story_progression)

    second = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
            owner_user_id=user_id,
            idempotency_key="story-replay-key",
        )
    )

    assert first.reply == second.reply
    assert first.campaign_id == second.campaign_id
    assert first.turn_id == second.turn_id


def test_director_provider_failure_rolls_story_progression_back_with_transaction(monkeypatch) -> None:
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(raw_text="talk to the ghost", action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")

    def fake_tool_execute(self, *, parsed_action, campaign_state):
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

    async def fake_propose(*, director_input, model=None):
        raise DirectorProviderError("Director provider failed.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-director-provider-failure")
    campaign_id = "campaign_story_director_provider_failure"
    _create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        state=_story_state(objective_1="completed", objective_2="active", objective_3="locked"),
    )

    with pytest.raises(HTTPException, match="Director service failed"):
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="talk to the ghost", campaign_id=campaign_id),
                owner_user_id=user_id,
            )
        )

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state = _load_campaign_state(campaign)
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "active"


def test_director_provider_failure_rolls_back_final_objective_and_reward_with_transaction(
    monkeypatch,
) -> None:
    """8F1's authored reward is applied inside the same turn that completes
    the final quest objective; a subsequent Director/provider failure must
    roll the story completion and the reward back together at the DB
    transaction boundary, not just within domain-level copying."""
    _enable_provider(monkeypatch)

    async def fake_parse(**kwargs):
        return ParsedAction(
            raw_text="take the old book",
            action=ActionType.TAKE,
            target="old_book",
            confidence=1.0,
            parse_status="ok",
        )

    def fake_tool_execute(self, *, parsed_action, campaign_state):
        state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
        tool_result = ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            state_delta={},
            item_id="old_book",
        )
        return state, tool_result

    async def fake_propose(*, director_input, model=None):
        raise DirectorProviderError("Director provider failed.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-final-objective-reward-director-failure")
    campaign_id = "campaign_story_final_objective_reward_director_failure"
    initial_state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
    _create_campaign(user_id=user_id, campaign_id=campaign_id, state=initial_state)

    with pytest.raises(HTTPException, match="Director service failed"):
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="take the old book", campaign_id=campaign_id),
                owner_user_id=user_id,
            )
        )

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state = _load_campaign_state(campaign)
        quest = state["story"]["quests"]["librarys_whisper"]
        assert quest["status"] == "active"
        assert quest["objectives"]["acquire_old_book"] == "active"
        assert "progression" not in state["player"]
        assert "progression_rewards" not in state["player"]


def test_real_tool_executor_executes_full_library_whisper_sequence(monkeypatch) -> None:
    _disable_provider(monkeypatch)

    async def fake_parse(message: str, **kwargs):
        lowered = message.lower()
        if "north" in lowered:
            return ParsedAction(raw_text=message, action=ActionType.MOVE, target="north", confidence=1.0, parse_status="ok")
        if "east" in lowered:
            return ParsedAction(raw_text=message, action=ActionType.MOVE, target="east", confidence=1.0, parse_status="ok")
        if "ghost" in lowered:
            return ParsedAction(raw_text=message, action=ActionType.TALK, target="library_ghost", confidence=1.0, parse_status="ok")
        if "book" in lowered:
            return ParsedAction(raw_text=message, action=ActionType.TAKE, target="old_book", confidence=1.0, parse_status="ok")
        raise AssertionError(f"Unexpected message: {message!r}")

    async def fake_narrator(*, payload, model=None):
        return _stub_narrator_reply(payload.player_message)

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fake_narrator)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-real-tool-executor")

    first = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="go north"), owner_user_id=user_id)
    )
    second = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go east", campaign_id=first.campaign_id),
            owner_user_id=user_id,
        )
    )
    third = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="talk to the ghost", campaign_id=second.campaign_id),
            owner_user_id=user_id,
        )
    )
    fourth = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="take the old book", campaign_id=third.campaign_id),
            owner_user_id=user_id,
        )
    )

    with session() as db:
        campaign = db.get_campaign(fourth.campaign_id)
        state = _load_campaign_state(campaign)
        assert state["player"]["location"] == "library"
        assert state["story"]["quests"]["librarys_whisper"]["status"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["speak_to_library_ghost"] == "completed"
        assert state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "completed"
