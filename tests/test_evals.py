from __future__ import annotations

import copy
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.agents.narrator import NarratorAgentInput
from app.schemas.director import DirectorInput

from evals.graders import grade_scenario
from evals.report import summarize_results
from evals.runner import EvalRunner, run_scenarios
from evals.runner import _run_live_scenario
from evals.scenarios import filter_scenarios, load_scenarios
from evals.schemas import Scenario, ScenarioTarget

_DIRECTOR_FIXTURE = {
    "current_player_room_id": "entry_hall",
    "clock_tick": 0,
    "facts": [],
    "npcs": [
        {
            "npc_id": "old_caretaker",
            "location_id": "entry_hall",
            "status": "active",
            "one_hop_destination_room_ids": ["grand_corridor"],
        }
    ],
    "player_action": {
        "action": "observe",
        "parse_status": "ok",
        "succeeded": True,
        "result_summary": "The player looks around.",
    },
}


def test_director_eval_accepts_no_action_proposal() -> None:
    scenario = Scenario(
        scenario_id="director-none-1",
        description="No action required.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
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
        authoritative_input=_DIRECTOR_FIXTURE,
        actual_output={"decision": "act", "world_action": {"action": "spawn_npc", "npc_id": "new_sidekick"}},
    )

    result = grade_scenario(scenario)
    assert any(item.name == "allowed_world_action_vocab" and item.passed is False for item in result)


def test_runner_sets_passed_and_score() -> None:
    scenario = Scenario(
        scenario_id="director-score-1",
        description="Scoring example.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
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
        load_scenarios(tmp_path)


def test_malformed_scenario_file_is_rejected(tmp_path) -> None:
    # Missing the required "description" field.
    malformed = {"scenario_id": "missing-description", "target": "director"}
    (tmp_path / "malformed.json").write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scenarios(tmp_path)


def test_invalid_target_agent_is_rejected(tmp_path) -> None:
    invalid_target = {
        "scenario_id": "invalid-target",
        "description": "Not a real agent.",
        "target": "wizard",
    }
    (tmp_path / "invalid_target.json").write_text(json.dumps(invalid_target), encoding="utf-8")
    with pytest.raises(ValidationError):
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


def test_narrator_grading_never_reads_expectations_from_fixture_output() -> None:
    """fixture_output is the offline ACTUAL output, never the expectations source."""

    scenario = Scenario(
        scenario_id="narrator-fixture-not-expectations",
        description="fixture_output must not be treated as deterministic_expectations.",
        target=ScenarioTarget.NARRATOR,
        deterministic_expectations={"must_not_contain": ["forbidden phrase"]},
        fixture_output={"reply_text": "This text includes the forbidden phrase anyway."},
        actual_output={"reply_text": "This text includes the forbidden phrase anyway."},
    )
    results = grade_scenario(scenario)
    assert any(
        result.name == "forbidden_content" and result.passed is False for result in results
    )


def test_checked_in_narrator_scenario_fails_grading_on_violated_expectations() -> None:
    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["narrator-absent-npc-grounding"].model_copy(deep=True)
    scenario.actual_output = {"reply_text": "Old caretaker is here, and the caretaker waves warmly."}
    results = grade_scenario(scenario)
    assert any(not result.passed for result in results)


def test_narrator_output_rejects_empty_and_whitespace_only_text() -> None:
    for reply_text in ("", "   ", "\n\t"):
        scenario = Scenario(
            scenario_id="narrator-empty-output",
            description="Empty/whitespace output must be rejected.",
            target=ScenarioTarget.NARRATOR,
            actual_output={"reply_text": reply_text},
        )
        results = grade_scenario(scenario)
        assert results[0].name == "narrator_output_present"
        assert results[0].passed is False


def test_checked_in_director_scenarios_validate_as_director_input() -> None:
    for scenario in load_scenarios():
        if scenario.target != ScenarioTarget.DIRECTOR:
            continue
        DirectorInput.model_validate(scenario.authoritative_input)


def test_checked_in_narrator_scenarios_validate_as_narrator_agent_input() -> None:
    for scenario in load_scenarios():
        if scenario.target != ScenarioTarget.NARRATOR:
            continue
        NarratorAgentInput.model_validate(scenario.authoritative_input)


def test_move_npc_destination_legal_for_other_npc_is_still_rejected() -> None:
    """A destination legal for NPC B must not make it legal for NPC A."""

    authoritative_input = {
        "current_player_room_id": "entry_hall",
        "clock_tick": 0,
        "facts": [],
        "npcs": [
            {
                "npc_id": "npc_a",
                "location_id": "entry_hall",
                "status": "active",
                "one_hop_destination_room_ids": ["grand_corridor"],
            },
            {
                "npc_id": "npc_b",
                "location_id": "library",
                "status": "active",
                "one_hop_destination_room_ids": ["cellar"],
            },
        ],
        "player_action": {
            "action": "wait",
            "parse_status": "ok",
            "succeeded": True,
            "result_summary": "The player waits.",
        },
    }
    DirectorInput.model_validate(authoritative_input)

    scenario = Scenario(
        scenario_id="director-cross-npc-destination-leak",
        description="npc_a proposes a destination that is only legal for npc_b.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=authoritative_input,
        actual_output={
            "decision": "act",
            "world_action": {"action": "move_npc", "npc_id": "npc_a", "destination_room_id": "cellar"},
        },
    )
    results = grade_scenario(scenario)
    destination_result = next(r for r in results if r.name == "move_npc_destination_valid")
    assert destination_result.passed is False


def test_move_npc_destination_legal_for_own_npc_passes() -> None:
    authoritative_input = {
        "current_player_room_id": "entry_hall",
        "clock_tick": 0,
        "facts": [],
        "npcs": [
            {
                "npc_id": "npc_a",
                "location_id": "entry_hall",
                "status": "active",
                "one_hop_destination_room_ids": ["grand_corridor"],
            }
        ],
        "player_action": {
            "action": "wait",
            "parse_status": "ok",
            "succeeded": True,
            "result_summary": "The player waits.",
        },
    }
    scenario = Scenario(
        scenario_id="director-own-npc-destination-ok",
        description="npc_a proposes its own bounded destination.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=authoritative_input,
        actual_output={
            "decision": "act",
            "world_action": {
                "action": "move_npc",
                "npc_id": "npc_a",
                "destination_room_id": "grand_corridor",
            },
        },
    )
    results = grade_scenario(scenario)
    destination_result = next(r for r in results if r.name == "move_npc_destination_valid")
    assert destination_result.passed is True


def test_move_npc_rejects_unknown_npc_id() -> None:
    authoritative_input = dict(_DIRECTOR_FIXTURE)
    scenario = Scenario(
        scenario_id="director-unknown-npc",
        description="A move proposal referencing an NPC not in authoritative_input fails.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=authoritative_input,
        actual_output={
            "decision": "act",
            "world_action": {
                "action": "move_npc",
                "npc_id": "ghost_not_in_context",
                "destination_room_id": "grand_corridor",
            },
        },
    )
    results = grade_scenario(scenario)
    npc_result = next(r for r in results if r.name == "referenced_npc_exists")
    assert npc_result.passed is False


def test_offline_runner_uses_fixtures_without_provider_calls(monkeypatch) -> None:
    def fail_provider(*args, **kwargs):
        raise AssertionError("offline eval must not call a provider")

    monkeypatch.setattr("app.ai.model_client.model_client.generate_text", fail_provider)
    monkeypatch.setattr("app.ai.model_client.model_client.generate_structured", fail_provider)
    results = run_scenarios(load_scenarios())
    assert all(result.passed for result in results)
    assert len(results) == 8


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


def test_live_mode_rejects_whitespace_only_api_key(monkeypatch) -> None:
    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "   \t  ")
    scenario = Scenario(
        scenario_id="live-whitespace-key",
        description="Whitespace-only API key must fail closed.",
        target=ScenarioTarget.DIRECTOR,
    )
    with pytest.raises(RuntimeError, match="non-empty"):
        asyncio.run(_run_live_scenario(scenario))


def test_live_mode_rejects_missing_api_key(monkeypatch) -> None:
    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", None)
    scenario = Scenario(
        scenario_id="live-missing-key",
        description="Missing API key must fail closed.",
        target=ScenarioTarget.DIRECTOR,
    )
    with pytest.raises(RuntimeError, match="non-empty"):
        asyncio.run(_run_live_scenario(scenario))


def test_mocked_live_director_uses_agent_contract_and_model_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeUsage:
        input_tokens = 12
        cached_input_tokens = 0
        cache_write_input_tokens = 0
        output_tokens = 4
        reasoning_output_tokens = 0
        total_tokens = 16

    class FakeProposal:
        def model_dump(self):
            return {"decision": "none"}

    class FakeResult:
        proposal = FakeProposal()
        usage = FakeUsage()

    class FakeDirector:
        async def propose(self, *, director_input, model=None):
            captured["director_input"] = director_input
            captured["model"] = model
            return FakeResult()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FakeDirector)
    scenario = Scenario(
        scenario_id="live-director",
        description="Mocked live Director.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
    )
    output, model_metadata = asyncio.run(_run_live_scenario(scenario))
    assert output == {"decision": "none"}
    assert isinstance(captured["director_input"], DirectorInput)
    assert model_metadata["target_agent"] == "director"
    from app.guardrails.model_policy import ModelPolicy

    assert model_metadata["model"] == ModelPolicy.director_model()
    assert captured["model"] == ModelPolicy.director_model()
    assert model_metadata["usage"]["total_tokens"] == 16


_NARRATOR_FIXTURE = {
    "player_message": "look",
    "scene_context": {
        "current_room": {"id": "entry_hall", "name": "Entry Hall", "description": "A dusty hall."},
        "nearby_npcs": [],
    },
}


def test_mocked_live_narrator_uses_agent_contract_and_model_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeOutput:
        reply_text = "grounded"
        input_tokens = 5
        cached_input_tokens = 0
        cache_write_input_tokens = 0
        output_tokens = 3
        reasoning_output_tokens = 0
        total_tokens = 8

        def model_dump(self):
            return {"reply_text": self.reply_text}

    class FakeNarrator:
        async def generate(self, *, payload, model=None):
            captured["payload"] = payload
            captured["model"] = model
            return FakeOutput()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.NarratorAgent", FakeNarrator)
    scenario = Scenario(
        scenario_id="live-narrator",
        description="Mocked live Narrator.",
        target=ScenarioTarget.NARRATOR,
        authoritative_input=_NARRATOR_FIXTURE,
    )
    output, model_metadata = asyncio.run(_run_live_scenario(scenario))
    assert output == {"reply_text": "grounded"}
    assert isinstance(captured["payload"], NarratorAgentInput)
    assert model_metadata["target_agent"] == "narrator"
    from app.guardrails.model_policy import ModelPolicy

    assert model_metadata["model"] == ModelPolicy.narrator_model()
    assert model_metadata["usage"]["total_tokens"] == 8


def test_mocked_live_narrator_forwards_failed_tool_result_intact(monkeypatch) -> None:
    """The failed-action grounding scenario must forward its authoritative
    tool_result to NarratorAgent.generate() unchanged."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["narrator-failed-action-grounding"]

    captured: dict[str, object] = {}

    class FakeOutput:
        reply_text = "The ghost is not nearby, so your call goes unanswered."
        input_tokens = None
        cached_input_tokens = None
        cache_write_input_tokens = None
        output_tokens = None
        reasoning_output_tokens = None
        total_tokens = None

        def model_dump(self):
            return {"reply_text": self.reply_text}

    class FakeNarrator:
        async def generate(self, *, payload, model=None):
            captured["payload"] = payload
            return FakeOutput()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.NarratorAgent", FakeNarrator)

    asyncio.run(_run_live_scenario(scenario))

    forwarded_payload = captured["payload"]
    assert isinstance(forwarded_payload, NarratorAgentInput)
    assert forwarded_payload.tool_result is not None
    assert forwarded_payload.tool_result.success is False
    assert forwarded_payload.tool_result.error_code == "npc_not_nearby"
    assert forwarded_payload.tool_result.summary == "The ghost is not nearby to respond."
    assert forwarded_payload.parsed_action is not None
    assert forwarded_payload.parsed_action.action == "talk"


def test_report_excludes_raw_actual_output_field() -> None:
    scenario = Scenario(
        scenario_id="report-actual-output-exclusion",
        description="Top-level actual_output must not be serialized.",
        target=ScenarioTarget.NARRATOR,
    )
    marker = "UNIQUE_RAW_OUTPUT_MARKER_7Q1"
    result = EvalRunner().run(scenario, actual_output={"reply_text": f"Contains {marker}."})
    summary = summarize_results([result])
    serialized = json.dumps(summary)
    assert marker not in serialized


def test_report_strips_raw_reply_text_from_grader_details() -> None:
    marker = "UNIQUE_LIVE_MARKER_9F3A"
    scenario = Scenario(
        scenario_id="report-sanitize-details",
        description="Grader details must never embed raw reply text.",
        target=ScenarioTarget.NARRATOR,
    )
    result = EvalRunner().run(scenario, actual_output={"reply_text": f"Some narration containing {marker}."})
    summary = summarize_results([result])
    serialized = json.dumps(summary)
    assert marker not in serialized


def test_report_summary_has_stable_json_fields() -> None:
    scenario = Scenario(
        scenario_id="report-stable-fields",
        description="Stable JSON summary fields.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )
    result = EvalRunner().run(scenario)
    summary = summarize_results([result])
    assert set(summary.keys()) == {
        "total_scenarios",
        "passed",
        "failed",
        "pass_rate",
        "score",
        "results",
    }
    assert summary["total_scenarios"] == 1
    assert summary["passed"] == 1
    assert summary["failed"] == 0
    assert summary["pass_rate"] == 1.0
    assert summary["score"] == 1.0


def test_report_aggregate_counts_pass_fail_and_score() -> None:
    passing = Scenario(
        scenario_id="agg-pass",
        description="Passing scenario.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )
    failing = Scenario(
        scenario_id="agg-fail",
        description="Failing scenario.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "act", "world_action": {"action": "advance_clock", "ticks": 1}},
    )
    results = [EvalRunner().run(passing), EvalRunner().run(failing)]
    summary = summarize_results(results)
    assert summary["total_scenarios"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert 0.0 < summary["score"] < 1.0


def test_model_metadata_present_for_mocked_live_runs(monkeypatch) -> None:
    class FakeUsage:
        input_tokens = 1
        cached_input_tokens = 0
        cache_write_input_tokens = 0
        output_tokens = 1
        reasoning_output_tokens = 0
        total_tokens = 2

    class FakeProposal:
        def model_dump(self):
            return {"decision": "none"}

    class FakeResult:
        proposal = FakeProposal()
        usage = FakeUsage()

    class FakeDirector:
        async def propose(self, *, director_input, model=None):
            return FakeResult()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FakeDirector)
    scenario = Scenario(
        scenario_id="live-metadata",
        description="Mocked live Director metadata.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
    )
    output, model_metadata = asyncio.run(_run_live_scenario(scenario))
    result = EvalRunner().run(scenario, actual_output=output, model_metadata=model_metadata)
    assert result.model_metadata.get("target_agent") == "director"
    assert result.model_metadata.get("model")

    summary = summarize_results([result])
    assert summary["results"][0]["model_metadata"]["target_agent"] == "director"


def test_offline_results_have_empty_model_metadata() -> None:
    scenario = load_scenarios()[0]
    result = EvalRunner().run(scenario)
    assert result.model_metadata == {}


def test_readme_documented_scenarios_and_tags_exist_in_corpus() -> None:
    scenarios = load_scenarios()
    ids = {scenario.scenario_id for scenario in scenarios}
    assert "narrator-observable-item-state" in ids
    assert any(
        "movement" in scenario.tags and scenario.target == ScenarioTarget.DIRECTOR
        for scenario in scenarios
    )
