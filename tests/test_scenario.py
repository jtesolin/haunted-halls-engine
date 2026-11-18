"""Persistent campaign scenario: module, creation, progression, endings, legacy."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents.starter_abilities import StarterAbilityGenerator
from app.ai.model_client import ModelCallResult, ModelUsage
from app.db.session import session
from app.game.campaign_state import InvalidCampaignStateError, build_fresh_campaign_state
from app.game.scenario import (
    StoryUpdate,
    apply_story_update,
    build_fallback_scenario,
    build_story_context,
    ensure_scenario,
    extract_story_update,
    read_scenario,
    scenario_from_provider,
)
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.campaign import CampaignCreateRequest
from app.schemas.chat import ChatRequest
from app.schemas.scenario import (
    ScenarioBeatProviderOutput,
    ScenarioOutcomeProviderOutput,
    ScenarioProviderOutput,
)
from tests.factories import starter_ability_generation
from tests.test_chat import _resolved_internal_user_id

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")


def _provider(**overrides) -> ScenarioProviderOutput:
    data = dict(
        premise="A bell tolls in the cellar.",
        objective="Find who rings it.",
        stakes="The bell tolls for you next.",
        hidden_truth="The sexton never left.",
        beats=[
            ScenarioBeatProviderOutput(beat_id="rope", clue="A frayed bell rope."),
            ScenarioBeatProviderOutput(beat_id="ledger", clue="A ledger of the dead."),
        ],
        outcomes=[
            ScenarioOutcomeProviderOutput(outcome_id="rest", summary="The sexton rests."),
            ScenarioOutcomeProviderOutput(outcome_id="toll", summary="The bell never stops."),
        ],
    )
    data.update(overrides)
    return ScenarioProviderOutput(**data)


class _Narrator:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.payloads = []

    async def generate(self, *, payload, model=None):
        self.payloads.append(payload)

        class R:
            reply_text = self.reply
            input_tokens = cached_input_tokens = cache_write_input_tokens = 0
            output_tokens = reasoning_output_tokens = total_tokens = 0

        return R()


@pytest.fixture
def owner() -> str:
    return _resolved_internal_user_id(TestClient(app), f"scn-{uuid4().hex}")


def _campaign(owner: str, state: dict) -> str:
    campaign_id = f"campaign_{uuid4().hex}"
    with session() as db:
        db.create_campaign(
            campaign_id=campaign_id, owner_user_id=owner, name="c",
            description="d", state=state,
        )
    return campaign_id


def _state(campaign_id: str, owner: str) -> dict:
    with session() as db:
        campaign = db.get_campaign_for_owner(campaign_id, owner)
    assert campaign is not None and campaign.state
    return json.loads(campaign.state)


def _chat(campaign_id: str, owner: str, narrator: _Narrator, monkeypatch) -> str:
    monkeypatch.setattr(orchestrator_module.orchestrator, "narrator_agent", narrator)
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="look around", campaign_id=campaign_id),
            owner_user_id=owner,
        )
    )
    return response.reply


def _scenario_state() -> dict:
    state = build_fresh_campaign_state()
    state["scenario"] = scenario_from_provider(_provider())
    return state


def test_provider_scenario_validates_and_bad_output_is_rejected() -> None:
    assert read_scenario({"scenario": scenario_from_provider(_provider())})["status"] == "active"
    for bad in (
        _provider(premise=" "),
        _provider(beats=[ScenarioBeatProviderOutput(beat_id="Bad Id", clue="x"),
                         ScenarioBeatProviderOutput(beat_id="b", clue="y")]),
        _provider(objective="x" * 301),
        _provider(outcomes=_provider().outcomes[:1]),
    ):
        with pytest.raises(ValueError):
            scenario_from_provider(bad)


def test_unusable_provider_scenario_falls_back_and_creation_persists_it(monkeypatch, owner) -> None:
    async def generate(self, return_usage=False):
        generation = starter_ability_generation()
        return ModelCallResult(output=generation, usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2))

    monkeypatch.setattr(StarterAbilityGenerator, "generate", generate)
    detail = asyncio.run(orchestrator_module.orchestrator.create_campaign(CampaignCreateRequest(), owner))
    assert _state(detail.campaign_id, owner)["scenario"] == build_fallback_scenario()


def test_generated_scenario_is_persisted_at_creation_without_extra_call_and_not_exposed(monkeypatch, owner) -> None:
    generated = starter_ability_generation().model_copy(update={"scenario": scenario_from_provider(_provider())})

    async def generate(self, return_usage=False):
        return ModelCallResult(output=generated, usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2))

    monkeypatch.setattr(StarterAbilityGenerator, "generate", generate)
    narrator = _Narrator("You hear a bell. What now?")
    monkeypatch.setattr(orchestrator_module.orchestrator, "narrator_agent", narrator)
    detail = asyncio.run(orchestrator_module.orchestrator.create_campaign(CampaignCreateRequest(), owner))

    assert _state(detail.campaign_id, owner)["scenario"]["objective"] == "Find who rings it."
    assert "Find who rings it." in narrator.payloads[0].story_context
    assert "sexton" not in detail.model_dump_json()


def test_context_hides_secrets_after_conclusion_and_is_bounded() -> None:
    state = _scenario_state()
    context = build_story_context(state)
    assert "The sexton never left." in context and "HIDDEN" in context
    apply_story_update(state, StoryUpdate(beats=["rope"], outcome="rest"))
    done = build_story_context(state)
    assert "STORY CONCLUDED (rest)" in done and "The sexton never left." not in done
    assert "HIDDEN" not in done and "A ledger of the dead." not in done
    assert len(context) < 2500


def test_marker_is_always_stripped_and_unknown_ids_ignored() -> None:
    clean, update = extract_story_update("Dust stirs.\n[[STORY beats=rope,nope outcome=bogus]]")
    assert clean == "Dust stirs." and "STORY" not in clean
    state = _scenario_state()
    assert apply_story_update(state, update)
    scenario = state["scenario"]
    assert [b["revealed"] for b in scenario["beats"]] == [True, False]
    assert scenario["status"] == "active" and scenario["outcome_id"] is None
    assert not apply_story_update(state, StoryUpdate(beats=["rope"]))


def test_successful_turn_persists_progress_and_reload_preserves_it(monkeypatch, owner) -> None:
    campaign_id = _campaign(owner, _scenario_state())
    reply = _chat(campaign_id, owner, _Narrator("A frayed rope. What next?\n[[STORY beats=rope]]"), monkeypatch)
    assert reply == "A frayed rope. What next?"
    scenario = _state(campaign_id, owner)["scenario"]
    assert [b["revealed"] for b in scenario["beats"]] == [True, False]
    # Next turn (a "reload") sees the persisted discovery in its story context.
    narrator = _Narrator("Quiet.")
    _chat(campaign_id, owner, narrator, monkeypatch)
    assert "A frayed bell rope." in narrator.payloads[0].story_context.split("HIDDEN undiscovered")[0]


@pytest.mark.parametrize("outcome", ["rest", "toll", "negotiated_exit"])
def test_alternate_and_unlisted_endings_conclude_and_persist(monkeypatch, owner, outcome) -> None:
    campaign_id = _campaign(owner, _scenario_state())
    _chat(campaign_id, owner, _Narrator(f"The end.\n[[STORY outcome={outcome}]]"), monkeypatch)
    scenario = _state(campaign_id, owner)["scenario"]
    if outcome == "negotiated_exit":
        # Unlisted ids are not trusted: the story stays open rather than guessing.
        assert scenario["status"] == "active"
        return
    assert scenario["status"] == "concluded" and scenario["outcome_id"] == outcome


def test_after_conclusion_later_markers_are_ignored_and_play_continues(monkeypatch, owner) -> None:
    state = _scenario_state()
    apply_story_update(state, StoryUpdate(outcome="rest"))
    campaign_id = _campaign(owner, state)
    narrator = _Narrator("Peace.\n[[STORY outcome=toll beats=ledger]]")
    assert _chat(campaign_id, owner, narrator, monkeypatch) == "Peace."
    scenario = _state(campaign_id, owner)["scenario"]
    assert scenario["outcome_id"] == "rest" and not scenario["beats"][1]["revealed"]
    assert "STORY CONCLUDED" in narrator.payloads[0].story_context


def test_failed_turn_does_not_advance_story(monkeypatch, owner) -> None:
    campaign_id = _campaign(owner, _scenario_state())
    before = _state(campaign_id, owner)

    class Boom(_Narrator):
        async def generate(self, *, payload, model=None):
            raise RuntimeError("provider down")

    with pytest.raises(HTTPException) as err:
        _chat(campaign_id, owner, Boom("x"), monkeypatch)
    assert err.value.status_code == 502
    assert _state(campaign_id, owner) == before


def test_legacy_campaign_gets_scenario_lazily_and_malformed_fails_closed(monkeypatch, owner) -> None:
    legacy = build_fresh_campaign_state()
    del legacy["scenario"]
    assert ensure_scenario(legacy) and legacy["scenario"] == build_fallback_scenario()
    del legacy["scenario"]
    campaign_id = _campaign(owner, legacy)
    narrator = _Narrator("The house stirs.")
    _chat(campaign_id, owner, narrator, monkeypatch)
    assert _state(campaign_id, owner)["scenario"]["status"] == "active"
    assert "Player objective" in narrator.payloads[0].story_context

    broken = build_fresh_campaign_state()
    broken["scenario"] = {"status": "active"}
    with pytest.raises(InvalidCampaignStateError):
        read_scenario(broken)
    broken_id = _campaign(owner, broken)
    with pytest.raises(HTTPException) as err:
        _chat(broken_id, owner, _Narrator("x"), monkeypatch)
    assert err.value.status_code == 500
