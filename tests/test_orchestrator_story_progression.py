from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents.director import DirectorProviderError
from app.agents.narrator import NarratorAgentInput
from app.api.routes import chat as chat_routes
from app.core.config import settings
from app.db.session import session
from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import ensure_character_progression_state, unlock_ability
from app.game.world import DEFAULT_WORLD, World
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.orchestration.orchestrator import ChatOrchestrator
from app.schemas.chat import (
    ActionParserOutput,
    ActionParserParameters,
    ActionType,
    ChatRequest,
    ParsedAction,
    ToolExecutionResult,
)
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.campaign import CampaignCreateRequest
from app.schemas.director import DirectorInput, NoActionProposal
from app.schemas.generated_abilities import AbilityObjectEffectOperation, TraversalMethod
from app.schemas.story import ItemAcquiredSignal, NpcSpokenToSignal, RoomEnteredSignal, StorySignal
from app.schemas.traversal import RouteKind, TraversalRoute
from app.services.tool_executor import ToolExecutor
from tests.factories import starter_ability_generation, traversal_ability_generation

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")


@pytest.fixture(autouse=True)
def story_progression_isolation(monkeypatch):
    original_orchestrator = orchestrator_module.orchestrator
    original_chat_orchestrator = chat_routes.orchestrator

    orchestrator_module.orchestrator = ChatOrchestrator()
    chat_routes.orchestrator = orchestrator_module.orchestrator

    try:
        yield
    finally:
        orchestrator_module.orchestrator = original_orchestrator
        chat_routes.orchestrator = original_chat_orchestrator


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


def _traversal_story_state(method: TraversalMethod) -> dict:
    state = _story_state()
    state["player"]["location"] = "grand_corridor"
    definitions = traversal_ability_generation(method).abilities
    state["player"]["generated_abilities"] = [definition.model_dump(mode="json") for definition in definitions]
    for definition in definitions:
        unlock_ability(state, definition.ability_id)
    return state


def _install_traversal_story_world(monkeypatch, method: TraversalMethod) -> None:
    """Authored test route into Library, using the same validator as demo content."""
    kind = {
        TraversalMethod.LEVITATION: "vertical",
        TraversalMethod.SPIDER_CLIMB: "surface",
        TraversalMethod.SUPERNATURAL_JUMP: "gap",
        TraversalMethod.WATER_WALKING: "water",
    }[method]
    route = TraversalRoute(
        route_id="library_crossing", name="Library Crossing",
        description="An authored crossing between stable landings.",
        origin="grand_corridor", destination="library",
        kind=RouteKind(kind), distance_metres=2,
        clear_vertical_path=kind == "vertical",
        continuous_support=kind == "surface",
        water_surface=kind == "water",
        valid_takeoff=True, valid_landing=True,
    )
    world = World(DEFAULT_WORLD.rooms, (route,))
    monkeypatch.setattr("app.agents.action_parser.DEFAULT_WORLD", world)
    orchestrator_module.orchestrator.tool_executor = ToolExecutor(world=world)


@pytest.mark.parametrize("method", list(TraversalMethod))
def test_real_traversal_turn_persists_story_final_context_and_replays_once(monkeypatch, method) -> None:
    _install_traversal_story_world(monkeypatch, method)
    state = _traversal_story_state(method)
    captured_signals = []
    captured_director = []
    captured_payloads = []
    narrator_messages = []
    original_derive = orchestrator_module.derive_story_signal
    original_generate = orchestrator_module.orchestrator.narrator_agent.generate

    async def parser_provider(**kwargs):
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK, target="Library Crossing",
            parameters=ActionParserParameters(ability_id="silver_step"),
            confidence=1, parse_status="ok",
        )

    def capture_signal(result):
        signal = original_derive(result)
        captured_signals.append(signal)
        assert result.success and result.current_location == "library"
        assert result.state_delta == {"player": {"location": {"from": "grand_corridor", "to": "library"}}}
        return signal

    async def director(*, director_input, model=None):
        captured_director.append(director_input)
        assert director_input.current_player_room_id == "library"
        quest = director_input.story.quests[0]
        assert quest.completed_objective_ids == ["enter_library"]
        assert quest.active_objective is not None
        return await _stub_director_response(director_input=director_input, model=model)

    async def narrator(*, payload, model=None):
        captured_payloads.append(payload)
        assert payload.scene_context.current_room.id == "library"
        return await original_generate(payload=payload, model=model)

    async def narrator_provider(*, messages, **kwargs):
        narrator_messages.extend(messages)
        return "You cross Library Crossing and land safely in the Library."

    monkeypatch.setattr("app.agents.action_parser.model_client.generate_structured", parser_provider)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", capture_signal)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", director)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", narrator)
    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", narrator_provider)
    user_id = _resolve_user(TestClient(app), f"traversal-story-{method.value}")
    campaign_id = _create_campaign(user_id=user_id, campaign_id=f"traversal-{method.value}", state=state)
    chat_request = ChatRequest(message="Could my Silver Step carry me along Library Crossing?", campaign_id=campaign_id)
    first = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        chat_request, owner_user_id=user_id, idempotency_key="traversal-once",
    ))
    with session() as db:
        persisted = _load_campaign_state(db.get_campaign(campaign_id))
        persisted_events = db.list_campaign_events(campaign_id)
        assert db.count_campaign_turns(campaign_id, user_id) == 1
    assert sum(event.type == "tool_executed" for event in persisted_events) == 1
    assert sum(event.type == "game_state_updated" for event in persisted_events) == 1
    assert persisted["player"]["location"] == "library"
    assert persisted["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
    assert persisted["player"]["generated_abilities"] == state["player"]["generated_abilities"]
    assert captured_signals == [RoomEnteredSignal(room_id="library")]
    request_text = "\n".join(str(message.get("content", "")) for message in narrator_messages)
    assert '"traversal_effect"' in request_text
    assert "You cross Library Crossing and land at Library." in request_text
    assert '"summary": "The attempt produces no discernible effect."' not in request_text
    replay = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        chat_request, owner_user_id=user_id, idempotency_key="traversal-once",
    ))
    assert replay == first
    assert len(captured_signals) == len(captured_director) == len(captured_payloads) == 1
    with session() as db:
        assert _load_campaign_state(db.get_campaign(campaign_id)) == persisted
        assert db.list_campaign_events(campaign_id) == persisted_events
        assert db.count_campaign_turns(campaign_id, user_id) == 1


def test_real_incompatible_traversal_turn_is_narrated_without_story_or_movement(monkeypatch) -> None:
    _install_traversal_story_world(monkeypatch, TraversalMethod.WATER_WALKING)
    state = _traversal_story_state(TraversalMethod.LEVITATION)
    signals = []
    messages_seen = []
    original_derive = orchestrator_module.derive_story_signal

    async def parser_provider(**kwargs):
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK, target="Library Crossing",
            parameters=ActionParserParameters(ability_id="silver_step"),
            parse_status="ok", confidence=1,
        )

    def capture_signal(result):
        assert result.error_code == "route_incompatible" and not result.success
        signal = original_derive(result)
        signals.append(signal)
        return signal

    async def director(*, director_input, model=None):
        assert director_input.current_player_room_id == "grand_corridor"
        assert director_input.story.quests[0].completed_objective_ids == []
        return await _stub_director_response(director_input=director_input, model=model)

    async def narrator_provider(*, messages, **kwargs):
        messages_seen.extend(messages)
        return "Silver Step cannot cross that water. You remain in the Grand Corridor."

    monkeypatch.setattr("app.agents.action_parser.model_client.generate_structured", parser_provider)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", capture_signal)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", director)
    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", narrator_provider)
    user_id = _resolve_user(TestClient(app), "traversal-incompatible")
    campaign_id = _create_campaign(user_id=user_id, campaign_id="traversal-incompatible", state=state)
    response = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        ChatRequest(message="Use my Silver Step on Library Crossing", campaign_id=campaign_id),
        owner_user_id=user_id,
    ))
    assert "remain" in response.reply
    assert signals == [None]
    with session() as db:
        assert _load_campaign_state(db.get_campaign(campaign_id)) == state
        events = db.list_campaign_events(campaign_id)
        assert sum(event.type == "tool_execution_failed" for event in events) == 1
        assert not any(event.type == "game_state_updated" for event in events)
    text = "\n".join(str(message.get("content", "")) for message in messages_seen)
    assert '"traversal_failure"' in text and "You remain where you started." in text
    assert '"traversal_effect"' not in text


@pytest.mark.parametrize("failure_stage", ["director", "narrator"])
def test_real_traversal_rollback_and_same_key_retry(monkeypatch, failure_stage) -> None:
    _install_traversal_story_world(monkeypatch, TraversalMethod.LEVITATION)
    state = _traversal_story_state(TraversalMethod.LEVITATION)
    original_derive = orchestrator_module.derive_story_signal
    signals = []
    failing = True

    async def parser_provider(**kwargs):
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK, target="Library Crossing",
            parameters=ActionParserParameters(ability_id="silver_step"),
            parse_status="ok", confidence=1,
        )

    def capture_signal(result):
        signal = original_derive(result)
        signals.append(signal)
        return signal

    async def director(*, director_input, model=None):
        assert director_input.current_player_room_id == "library"
        assert director_input.story.quests[0].completed_objective_ids == ["enter_library"]
        if failing and failure_stage == "director":
            raise DirectorProviderError("Synthetic failure")
        return await _stub_director_response(director_input=director_input, model=model)

    async def narrator_provider(**kwargs):
        if failing and failure_stage == "narrator":
            raise RuntimeError("Synthetic narrator failure")
        return "You land safely in the Library."

    monkeypatch.setattr("app.agents.action_parser.model_client.generate_structured", parser_provider)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", capture_signal)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", director)
    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", narrator_provider)
    user_id = _resolve_user(TestClient(app), f"traversal-rollback-{failure_stage}")
    campaign_id = _create_campaign(user_id=user_id, campaign_id=f"traversal-rollback-{failure_stage}", state=state)
    chat_request = ChatRequest(message="Use Silver Step on Library Crossing", campaign_id=campaign_id)
    with pytest.raises(HTTPException):
        asyncio.run(orchestrator_module.orchestrator.handle_chat(
            chat_request, owner_user_id=user_id, idempotency_key="retry-crossing",
        ))
    with session() as db:
        assert _load_campaign_state(db.get_campaign(campaign_id)) == state
        assert db.list_campaign_events(campaign_id) == []
        assert db.count_campaign_turns(campaign_id, user_id) == 0
    assert signals == [RoomEnteredSignal(room_id="library")]
    failing = False
    first = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        chat_request, owner_user_id=user_id, idempotency_key="retry-crossing",
    ))
    with session() as db:
        after = _load_campaign_state(db.get_campaign(campaign_id))
        after_events = db.list_campaign_events(campaign_id)
        assert db.count_campaign_turns(campaign_id, user_id) == 1
    assert after["player"]["location"] == "library"
    assert after["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
    replay = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        chat_request, owner_user_id=user_id, idempotency_key="retry-crossing",
    ))
    assert replay == first and len(signals) == 2
    with session() as db:
        assert _load_campaign_state(db.get_campaign(campaign_id)) == after
        assert db.list_campaign_events(campaign_id) == after_events
        assert db.count_campaign_turns(campaign_id, user_id) == 1


@pytest.mark.parametrize("method", list(TraversalMethod))
def test_new_campaign_traversal_generation_persists_without_later_regeneration(monkeypatch, method) -> None:
    calls = []
    definitions = traversal_ability_generation(method)

    async def generation(*, return_usage=False):
        calls.append(method)
        if return_usage:
            from app.ai.model_client import ModelCallResult
            return ModelCallResult(output=definitions)
        return definitions

    monkeypatch.setattr(orchestrator_module.orchestrator.starter_ability_generator, "generate", generation)
    user_id = _resolve_user(TestClient(app), f"traversal-new-{method.value}")
    campaign = asyncio.run(orchestrator_module.orchestrator.create_campaign(
        CampaignCreateRequest(), owner_user_id=user_id,
    ))
    with session() as db:
        saved = _load_campaign_state(db.get_campaign(campaign.campaign_id))
    assert saved["player"]["generated_abilities"] == definitions.model_dump(mode="json")["abilities"]
    assert len(saved["player"]["progression"]["unlocked_abilities"]) == 2
    assert calls == [method]
    asyncio.run(orchestrator_module.orchestrator.handle_chat(
        ChatRequest(message="look around", campaign_id=campaign.campaign_id), owner_user_id=user_id,
    ))
    assert calls == [method]


def test_recall_object_on_heavy_statue_reaches_gameplay_and_safe_narration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = build_fresh_campaign_state()
    generation = starter_ability_generation()
    generation.abilities[1].ability_id = "recall_object"
    generation.abilities[1].display_name = "Recall Object"
    state["player"]["generated_abilities"] = [
        ability.model_dump(mode="json") for ability in generation.abilities
    ]
    state["items"]["heavy_statue"]["portable"] = False
    ensure_character_progression_state(state)
    for ability in generation.abilities:
        assert unlock_ability(state, ability.ability_id).success
    initial_state = copy.deepcopy(state)

    captured_actions: list[ParsedAction] = []
    captured_tool_results: list[ToolExecutionResult] = []
    captured_narrator_requests: list[dict[str, object]] = []
    original_execute = orchestrator_module.ToolExecutor.execute

    async def fake_action_parser_provider(*, response_model, **kwargs):
        assert response_model is ActionParserOutput
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            target="statue",
            parameters=ActionParserParameters(ability_id="recall_object"),
            confidence=1,
            parse_status="ok",
        )

    def capture_tool_execution(self, *, parsed_action, campaign_state):
        captured_actions.append(parsed_action)
        updated_state, result = original_execute(
            self,
            parsed_action=parsed_action,
            campaign_state=campaign_state,
        )
        captured_tool_results.append(result)
        return updated_state, result

    async def fake_narrator_provider(*, messages, **kwargs):
        captured_narrator_requests.extend(messages)
        return "The Heavy Statue remains immovable."

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_action_parser_provider,
    )
    monkeypatch.setattr(
        orchestrator_module.ToolExecutor,
        "execute",
        capture_tool_execution,
    )
    monkeypatch.setattr(
        "app.agents.narrator.model_client.generate_text",
        fake_narrator_provider,
    )
    monkeypatch.setattr(
        orchestrator_module.orchestrator.director_agent,
        "propose",
        _stub_director_response,
    )

    client = TestClient(app)
    user_id = _resolve_user(client, "recall-object-heavy-statue")
    campaign_id = _create_campaign(
        user_id=user_id,
        campaign_id="campaign_recall_object_heavy_statue",
        state=state,
    )
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(
                message="recall object on the statue",
                campaign_id=campaign_id,
            ),
            owner_user_id=user_id,
        )
    )

    assert response.campaign_id == campaign_id
    assert captured_actions[0].action == ActionType.ABILITY_CHECK
    assert captured_actions[0].parameters == {"ability_id": "recall_object"}
    assert captured_actions[0].target == "statue"
    tool_result = captured_tool_results[0]
    assert tool_result.success is False
    assert tool_result.state_delta == {}
    assert tool_result.ability_result is not None
    assert tool_result.ability_result.error_code == "target_not_portable"
    narrator_request_text = "\n".join(
        str(message.get("content", "")) for message in captured_narrator_requests
    )
    assert "The attempt produces no discernible effect." in narrator_request_text
    assert "target_not_portable" not in narrator_request_text
    assert "The Heavy Statue remains immovable." == response.reply

    with session() as db:
        persisted_state = _load_campaign_state(db.get_campaign(campaign_id))
    assert persisted_state == initial_state
    assert persisted_state["items"]["heavy_statue"]["location"] == "room:entry_hall"
    assert persisted_state["items"]["heavy_statue"]["portable"] is False
    assert persisted_state["player"]["inventory"] == initial_state["player"]["inventory"]


def test_story_progression_move_into_library_advances_objective_1(monkeypatch) -> None:
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


def test_generated_retrieve_completes_quest_and_rewards_once_through_real_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
    state["player"]["location"] = "library"
    generation = starter_ability_generation()
    state["player"]["generated_abilities"] = [
        ability.model_dump(mode="json") for ability in generation.abilities
    ]
    ensure_character_progression_state(state)
    for ability in generation.abilities:
        assert unlock_ability(state, ability.ability_id).success
    captured_tools: list[ToolExecutionResult] = []
    captured_director_inputs: list[DirectorInput] = []
    captured_narrator_payloads: list[NarratorAgentInput] = []
    original_derive = orchestrator_module.derive_story_signal

    async def deterministic_parse(
        *, message: str, campaign_state: str, recent_turns: list[dict[str, str]],
        **kwargs: object,
    ) -> ParsedAction:
        return ParsedAction(
            raw_text=message,
            action=ActionType.ABILITY_CHECK,
            target="old book",
            parameters={"ability_id": "whispering_touch"},
            confidence=1,
            parse_status="ok",
        )

    def capture_signal(tool_result: ToolExecutionResult) -> StorySignal | None:
        captured_tools.append(tool_result)
        signal = original_derive(tool_result)
        if tool_result.success:
            assert tool_result.applied_tools == ["resolve_generated_ability"]
            assert tool_result.ability_result is not None
            assert tool_result.ability_result.object_effect is not None
            assert tool_result.ability_result.object_effect.operation == AbilityObjectEffectOperation.RETRIEVE
            assert signal == ItemAcquiredSignal(item_id="old_book")
        else:
            assert signal is None
        return signal

    async def capture_director(*, director_input: DirectorInput, model=None):
        captured_director_inputs.append(director_input)
        assert director_input.current_player_room_id == "library"
        quest = director_input.story.quests[0]
        assert quest.quest_id == "librarys_whisper"
        assert quest.status == "completed"
        assert quest.completed_objective_ids == [
            "enter_library", "speak_to_library_ghost", "acquire_old_book",
        ]
        assert quest.active_objective is None
        assert director_input.character.progression_tracks[0].points == 2
        assert "keen_eye" in [
            ability.ability_id for ability in director_input.character.available_abilities
        ]
        return await _stub_director_response(director_input=director_input, model=model)

    async def capture_narrator(*, payload: NarratorAgentInput, model=None):
        captured_narrator_payloads.append(payload)
        return await _async_stub_narrator_reply("The book settles into your hands.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", deterministic_parse)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", capture_signal)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", capture_director)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", capture_narrator)
    client = TestClient(app)
    user_id = _resolve_user(client, "story-generated-retrieve")
    campaign_id = _create_campaign(
        user_id=user_id, campaign_id="campaign_story_generated_retrieve", state=state,
    )
    request = ChatRequest(message="use Whispering Grasp on old book", campaign_id=campaign_id)
    first = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        request, owner_user_id=user_id, idempotency_key="generated-retrieve-completion",
    ))
    with session() as db:
        first_state = _load_campaign_state(db.get_campaign(campaign_id))
    assert first_state["items"]["old_book"]["location"] == "player:current"
    assert "old_book" in first_state["player"]["inventory"]
    assert first_state["story"]["quests"]["librarys_whisper"]["status"] == "completed"
    assert first_state["story"]["quests"]["librarys_whisper"]["objectives"]["acquire_old_book"] == "completed"
    assert first_state["player"]["progression"]["tracks"]["investigation"] == 2
    assert first_state["player"]["progression"]["unlocked_abilities"].count("keen_eye") == 1
    assert first_state["player"]["progression_rewards"]["claimed_reward_ids"] == ["librarys_whisper_completion"]
    reward = captured_narrator_payloads[0].current_turn_reward
    assert reward is not None
    assert reward.reward_id == "librarys_whisper_completion"
    assert reward.progression_grants[0].prior_points == 0
    assert reward.progression_grants[0].new_points == 2
    assert reward.unlocked_abilities[0].ability_id == "keen_eye"

    replay = asyncio.run(orchestrator_module.orchestrator.handle_chat(
        request, owner_user_id=user_id, idempotency_key="generated-retrieve-completion",
    ))
    assert replay == first
    assert len(captured_tools) == len(captured_director_inputs) == len(captured_narrator_payloads) == 1
    asyncio.run(orchestrator_module.orchestrator.handle_chat(
        request, owner_user_id=user_id, idempotency_key="generated-retrieve-again",
    ))
    assert len(captured_tools) == 2
    assert captured_tools[-1].success is False
    assert captured_narrator_payloads[-1].current_turn_reward is None
    with session() as db:
        final_state = _load_campaign_state(db.get_campaign(campaign_id))
    assert final_state == first_state


def test_final_objective_completion_forwards_authoritative_reward_to_narrator(monkeypatch) -> None:
    """Prove the orchestration boundary itself forwards the narrow authoritative
    8F1 reward earned this turn to the Narrator, and nothing broader."""
    captured_narrator_payloads: list[NarratorAgentInput] = []

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

    async def capture_narrator_generate(*, payload, model=None):
        captured_narrator_payloads.append(payload)
        return await _async_stub_narrator_reply("The book settles into your hands.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", capture_narrator_generate)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-final-objective-narrator-reward")
    asyncio.run(
        orchestrator_module.orchestrator.handle_chat(ChatRequest(message="take the old book"), owner_user_id=user_id)
    )

    assert len(captured_narrator_payloads) == 1
    reward = captured_narrator_payloads[0].current_turn_reward
    assert reward is not None
    assert reward.reward_id == "librarys_whisper_completion"
    assert len(reward.progression_grants) == 1
    assert reward.progression_grants[0].track_id == ProgressionTrackId.INVESTIGATION
    assert reward.progression_grants[0].prior_points == 0
    assert reward.progression_grants[0].new_points == 2
    assert len(reward.unlocked_abilities) == 1
    assert reward.unlocked_abilities[0].ability_id == "keen_eye"
    assert reward.unlocked_abilities[0].display_name == "Keen Eye"


def test_idempotent_replay_does_not_regrant_or_renarrate_final_objective_reward(
    monkeypatch,
) -> None:
    """Issue #76 requires that a completed idempotent chat replay neither
    grants nor narrates the authored quest-completion reward again. This
    exercises the actual `librarys_whisper` final-objective/reward turn
    through the real orchestration/idempotency path, rather than a generic
    replay scenario, so it proves the reward-specific acceptance
    requirement directly."""
    captured_narrator_payloads: list[NarratorAgentInput] = []

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

    async def capture_narrator_generate(*, payload, model=None):
        captured_narrator_payloads.append(payload)
        return await _async_stub_narrator_reply("The book settles into your hands.")

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fake_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fake_tool_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", capture_narrator_generate)
    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", _stub_director_response)

    client = TestClient(app)
    user_id = _resolve_user(client, "story-idempotent-final-objective-reward")
    campaign_id = "campaign_story_idempotent_final_objective_reward"
    initial_state = _story_state(objective_1="completed", objective_2="completed", objective_3="active")
    _create_campaign(user_id=user_id, campaign_id=campaign_id, state=initial_state)
    idempotency_key = "final-objective-reward-replay-key"

    first = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="take the old book", campaign_id=campaign_id),
            owner_user_id=user_id,
            idempotency_key=idempotency_key,
        )
    )

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state_after_first = _load_campaign_state(campaign)
    quest_after_first = state_after_first["story"]["quests"]["librarys_whisper"]
    assert quest_after_first["status"] == "completed"
    assert quest_after_first["objectives"]["acquire_old_book"] == "completed"
    assert state_after_first["player"]["progression"]["tracks"]["investigation"] == 2
    assert state_after_first["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state_after_first["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]

    assert len(captured_narrator_payloads) == 1
    first_reward = captured_narrator_payloads[0].current_turn_reward
    assert first_reward is not None
    assert first_reward.reward_id == "librarys_whisper_completion"
    assert first_reward.progression_grants[0].prior_points == 0
    assert first_reward.progression_grants[0].new_points == 2
    assert first_reward.unlocked_abilities[0].ability_id == "keen_eye"

    def fail_if_called_parse(**kwargs):
        raise AssertionError("Action parser must not run again on completed replay.")

    def fail_if_called_execute(*args, **kwargs):
        raise AssertionError("Tool executor must not run again on completed replay.")

    async def fail_if_called_generate(*args, **kwargs):
        raise AssertionError("Narrator must not run again on completed replay.")

    def fail_story_derivation(*args, **kwargs):
        raise AssertionError("Story derivation should not run on a replayed completed request.")

    def fail_story_progression(*args, **kwargs):
        raise AssertionError("apply_story_signal should not run on a replayed completed request.")

    def fail_reward_application(*args, **kwargs):
        raise AssertionError(
            "apply_quest_completion_rewards should not run on a replayed completed request."
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.action_parser_agent, "parse", fail_if_called_parse)
    monkeypatch.setattr(orchestrator_module.ToolExecutor, "execute", fail_if_called_execute)
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", fail_if_called_generate)
    monkeypatch.setattr(orchestrator_module, "derive_story_signal", fail_story_derivation)
    monkeypatch.setattr(orchestrator_module, "apply_story_signal", fail_story_progression)
    monkeypatch.setattr(
        orchestrator_module, "apply_quest_completion_rewards", fail_reward_application
    )

    second = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="take the old book", campaign_id=campaign_id),
            owner_user_id=user_id,
            idempotency_key=idempotency_key,
        )
    )

    assert second.reply == first.reply
    assert second.campaign_id == first.campaign_id
    assert second.turn_id == first.turn_id
    assert len(captured_narrator_payloads) == 1

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        state_after_replay = _load_campaign_state(campaign)
    assert state_after_replay == state_after_first
    quest_after_replay = state_after_replay["story"]["quests"]["librarys_whisper"]
    assert quest_after_replay["status"] == "completed"
    assert state_after_replay["player"]["progression"]["tracks"]["investigation"] == 2
    assert state_after_replay["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state_after_replay["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]


def test_malformed_reward_claims_roll_back_quest_completion(monkeypatch) -> None:
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


def test_story_progression_runs_with_explicit_model_agent_doubles(monkeypatch) -> None:
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
