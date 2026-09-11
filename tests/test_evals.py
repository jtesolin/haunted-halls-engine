from __future__ import annotations

import copy
import asyncio
import json

import pytest

from evals.graders import grade_scenario
from evals.runner import EvalRunner, run_scenarios
from evals.runner import _run_live_scenario
from evals.scenarios import filter_scenarios, load_scenarios
from evals.schemas import Scenario, ScenarioTarget


def test_director_eval_accepts_no_action_proposal() -> None:
    scenario = Scenario(
        scenario_id="director-none-1",
        description="No action required.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input={
            "current_player_room_id": "entry_hall",
            "legal_destinations": ["grand_corridor", "library"],
            "npcs": {"old_caretaker": {"npc_id": "old_caretaker", "location_id": "entry_hall"}},
        },
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )

    result = grade_scenario(scenario)
    assert result[0].passed is True
    assert all(item.passed for item in result)


def test_director_eval_rejects_spawn_npc_action() -> None:
    scenario = Scenario(
        scenario_id="director-spawn-1",
        description="Spawn NPC is not allowed.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input={
            "current_player_room_id": "entry_hall",
            "legal_destinations": ["grand_corridor"],
            "npcs": {},
        },
        actual_output={"decision": "act", "world_action": {"action": "spawn_npc", "npc_id": "new_sidekick"}},
    )

    result = grade_scenario(scenario)
    assert any(item.name == "allowed_world_action_vocab" and item.passed is False for item in result)


def test_runner_sets_passed_and_score() -> None:
    scenario = Scenario(
        scenario_id="director-score-1",
        description="Scoring example.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input={
            "current_player_room_id": "entry_hall",
            "legal_destinations": ["grand_corridor"],
            "npcs": {"old_caretaker": {"npc_id": "old_caretaker", "location_id": "entry_hall"}},
        },
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )

    result = EvalRunner().run(scenario)
    assert result.passed is True
    assert result.score is not None
    assert result.max_score is not None


def test_checked_in_corpus_has_director_and_narrator_cases() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 8
    assert {scenario.target for scenario in scenarios} == {
        ScenarioTarget.DIRECTOR,
        ScenarioTarget.NARRATOR,
    }
    assert len({scenario.scenario_id for scenario in scenarios}) == len(scenarios)


def test_scenario_filters_by_id_tag_and_agent() -> None:
    scenarios = load_scenarios()
    assert [item.scenario_id for item in filter_scenarios(scenarios, scenario_id="narrator-observable-item-state")] == [
        "narrator-observable-item-state"
    ]
    assert all("grounding" in item.tags for item in filter_scenarios(scenarios, tag="grounding"))
    assert all(item.target == ScenarioTarget.DIRECTOR for item in filter_scenarios(scenarios, target="director"))


def test_duplicate_scenario_ids_fail_clearly(tmp_path) -> None:
    scenario = {
        "scenario_id": "duplicate",
        "description": "duplicate",
        "target": "director",
        "fixture_output": {"decision": "none"},
    }
    (tmp_path / "one.json").write_text(json.dumps(scenario), encoding="utf-8")
    (tmp_path / "two.json").write_text(json.dumps(scenario), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate scenario IDs"):
        from evals.scenarios import load_scenarios

        load_scenarios(tmp_path)


def test_narrator_grounding_checks_are_fixture_specific() -> None:
    scenario = Scenario(
        scenario_id="narrator-grounding",
        description="No false success claim.",
        target=ScenarioTarget.NARRATOR,
        deterministic_expectations={"must_not_contain": ["the door opens"]},
        actual_output={"reply_text": "The locked door remains shut."},
    )
    assert all(result.passed for result in grade_scenario(scenario))

    scenario.actual_output = {"reply_text": "The door opens with a click."}
    assert any(not result.passed for result in grade_scenario(scenario))


def test_offline_runner_uses_fixtures_without_provider_calls(monkeypatch) -> None:
    def fail_provider(*args, **kwargs):
        raise AssertionError("offline eval must not call a provider")

    monkeypatch.setattr("app.ai.model_client.model_client.generate_text", fail_provider)
    monkeypatch.setattr("app.ai.model_client.model_client.generate_structured", fail_provider)
    results = run_scenarios(load_scenarios())
    assert all(result.passed for result in results)


def test_runner_does_not_mutate_authoritative_fixture() -> None:
    scenario = load_scenarios()[0]
    original = copy.deepcopy(scenario.authoritative_input)
    EvalRunner().run(scenario)
    assert scenario.authoritative_input == original


def test_live_mode_requires_explicit_enablement(monkeypatch) -> None:
    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", False)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    scenario = Scenario(
        scenario_id="live-disabled",
        description="Disabled live mode.",
        target=ScenarioTarget.DIRECTOR,
    )
    with pytest.raises(RuntimeError, match="AI_ENABLED=true"):
        asyncio.run(_run_live_scenario(scenario))


def test_mocked_live_director_uses_agent_contract(monkeypatch) -> None:
    class FakeDirector:
        async def propose(self, *, director_input):
            return type("Result", (), {"proposal": type("Proposal", (), {"model_dump": lambda self: {"decision": "none"}})()})()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FakeDirector)
    scenario = Scenario(
        scenario_id="live-director",
        description="Mocked live Director.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input={
            "current_player_room_id": "entry_hall",
            "clock_tick": 0,
            "facts": [],
            "npcs": [],
            "player_action": {
                "action": "observe",
                "parse_status": "ok",
                "succeeded": True,
                "result_summary": "Observed.",
            },
        },
    )
    assert asyncio.run(_run_live_scenario(scenario)) == {"decision": "none"}


def test_mocked_live_narrator_uses_agent_contract(monkeypatch) -> None:
    class FakeNarrator:
        async def generate(self, *, payload):
            return type("Result", (), {"model_dump": lambda self: {"reply_text": "grounded"}})()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.NarratorAgent", FakeNarrator)
    scenario = Scenario(
        scenario_id="live-narrator",
        description="Mocked live Narrator.",
        target=ScenarioTarget.NARRATOR,
        authoritative_input={"scene_context": {}, "player_message": "look"},
    )
    assert asyncio.run(_run_live_scenario(scenario)) == {"reply_text": "grounded"}
