from __future__ import annotations

import copy
import asyncio
import json
import sys
import traceback
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agents.narrator import NarratorAgentInput
from app.db.schema import campaigns, game_events, model_requests, turns
from app.db.session import get_engine, session
from app.game.items import PLAYER_INVENTORY_LOCATION
from app.schemas.director import DirectorInput

from evals.graders import grade_scenario
from evals.report import render_report, summarize_results
from evals.runner import EvalRunner, run_scenarios
from evals.runner import LiveEvalError, _run_live_scenario, _run_live_scenarios
import evals.runner as runner_module
from evals.scenarios import filter_scenarios, load_scenarios
from evals.schemas import GraderResult, Scenario, ScenarioResult, ScenarioTarget

_DIRECTOR_FIXTURE = {
    "current_player_room_id": "eval_foyer",
    "clock_tick": 0,
    "facts": [],
    "npcs": [
        {
            "npc_id": "eval_npc_a",
            "location_id": "eval_foyer",
            "status": "active",
            "one_hop_destination_room_ids": ["eval_gallery"],
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
    # Otherwise-valid authoritative_input so target-specific DirectorInput
    # validation does not mask the duplicate-ID check this test exercises.
    scenario = {
        "scenario_id": "duplicate",
        "description": "duplicate",
        "target": "director",
        "authoritative_input": _DIRECTOR_FIXTURE,
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


def test_malformed_director_authoritative_input_is_rejected_at_load(tmp_path) -> None:
    """A structurally valid generic Scenario with an invalid DirectorInput
    payload must fail to load before grading/filtering/live execution."""

    malformed = {
        "scenario_id": "malformed-director-input",
        "description": "Missing required player_action and forbidden extra field.",
        "target": "director",
        "authoritative_input": {
            "current_player_room_id": "eval_foyer",
            "clock_tick": 0,
            "facts": [],
            "npcs": [],
            "legal_destinations": ["eval_gallery"],
        },
    }
    (tmp_path / "malformed_director.json").write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scenarios(tmp_path)


def test_malformed_narrator_authoritative_input_is_rejected_at_load(tmp_path) -> None:
    """A structurally valid generic Scenario with an invalid
    NarratorAgentInput payload must fail to load before grading/filtering/live
    execution."""

    malformed = {
        "scenario_id": "malformed-narrator-input",
        "description": "Missing required scene_context.",
        "target": "narrator",
        "authoritative_input": {"player_message": "look around"},
    }
    (tmp_path / "malformed_narrator.json").write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scenarios(tmp_path)


def _write_scenario_file(tmp_path, scenario_id: str, target: str, expectations: dict) -> None:
    payload = {
        "scenario_id": scenario_id,
        "description": "Deterministic expectation contract validation fixture.",
        "target": target,
        "authoritative_input": _DIRECTOR_FIXTURE if target == "director" else {
            "player_message": "look around",
            "scene_context": {
                "current_room": {"id": "eval_foyer", "name": "Evaluation Foyer", "description": "d"},
                "nearby_npcs": [],
            },
        },
        "deterministic_expectations": expectations,
    }
    (tmp_path / f"{scenario_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_valid_director_deterministic_expectations_load(tmp_path) -> None:
    _write_scenario_file(tmp_path, "director-valid-expectations", "director", {"require_none": True})
    scenarios = load_scenarios(tmp_path)
    assert scenarios[0].deterministic_expectations == {"require_none": True}


def test_valid_narrator_deterministic_expectations_load(tmp_path) -> None:
    _write_scenario_file(
        tmp_path,
        "narrator-valid-expectations",
        "narrator",
        {"contains": ["evaluation lantern"], "must_not_contain": ["forbidden"]},
    )
    scenarios = load_scenarios(tmp_path)
    assert scenarios[0].deterministic_expectations["contains"] == ["evaluation lantern"]


def test_unknown_director_expectation_key_is_rejected_at_load(tmp_path) -> None:
    _write_scenario_file(
        tmp_path, "director-unknown-expectation", "director", {"require_success": True}
    )
    with pytest.raises(ValueError, match="unsupported Director deterministic_expectations"):
        load_scenarios(tmp_path)


def test_unknown_narrator_expectation_key_is_rejected_at_load(tmp_path) -> None:
    """A typo such as must_not_include must never silently pass through the
    grader unchecked; it must fail scenario loading."""

    _write_scenario_file(
        tmp_path, "narrator-unknown-expectation", "narrator", {"must_not_include": ["ghost"]}
    )
    with pytest.raises(ValueError, match="unsupported Narrator deterministic_expectations"):
        load_scenarios(tmp_path)


def test_director_require_none_string_value_is_rejected_at_load(tmp_path) -> None:
    _write_scenario_file(
        tmp_path, "director-require-none-string", "director", {"require_none": "true"}
    )
    with pytest.raises(ValueError, match="require_none must be a bool"):
        load_scenarios(tmp_path)


@pytest.mark.parametrize(
    "expectations",
    [
        {"contains": True},
        {"contains": 42},
        {"contains": {"nested": "dict"}},
        {"contains": [1, 2]},
        {"contains": ""},
        {"contains": []},
        {"contains": ["ok", ""]},
        {"must_not_contain": False},
        {"contains": "   "},
        {"must_not_contain": "   "},
        {"contains": ["valid", "   "]},
    ],
)
def test_malformed_narrator_expectation_values_are_rejected_at_load(tmp_path, expectations) -> None:
    _write_scenario_file(tmp_path, "narrator-malformed-expectation", "narrator", expectations)
    with pytest.raises(ValueError):
        load_scenarios(tmp_path)


def test_narrator_whitespace_only_scalar_expectation_is_rejected_at_load(tmp_path) -> None:
    """A whitespace-only string ("   ") is not caught by an empty-string
    check but is just as meaningless for a substring contains/
    must_not_contain grader; it must fail scenario loading, not be silently
    stripped and accepted."""

    _write_scenario_file(tmp_path, "narrator-whitespace-scalar", "narrator", {"contains": "   "})
    with pytest.raises(ValueError, match="must not be empty or whitespace-only"):
        load_scenarios(tmp_path)


def test_narrator_whitespace_only_list_item_expectation_is_rejected_at_load(tmp_path) -> None:
    _write_scenario_file(
        tmp_path, "narrator-whitespace-list-item", "narrator", {"contains": ["valid", "   "]}
    )
    with pytest.raises(ValueError):
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
    scenario.actual_output = {"reply_text": "The steward is here, and the steward waves warmly."}
    results = grade_scenario(scenario)
    assert any(not result.passed for result in results)


def test_observable_item_scenario_rejects_unlit_contradiction() -> None:
    """The positive expectation must be specific enough that "unlit" (which
    contains "lit" as a substring) is not mistakenly accepted as satisfying
    it."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["narrator-observable-item-state"].model_copy(deep=True)
    scenario.actual_output = {"reply_text": "The evaluation lantern is unlit."}
    results = grade_scenario(scenario)
    assert any(result.name == "expected_content" and result.passed is False for result in results)


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
        "current_player_room_id": "eval_foyer",
        "clock_tick": 0,
        "facts": [],
        "npcs": [
            {
                "npc_id": "npc_a",
                "location_id": "eval_foyer",
                "status": "active",
                "one_hop_destination_room_ids": ["eval_gallery"],
            },
            {
                "npc_id": "npc_b",
                "location_id": "eval_vault",
                "status": "active",
                "one_hop_destination_room_ids": ["eval_archive"],
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
            "world_action": {"action": "move_npc", "npc_id": "npc_a", "destination_room_id": "eval_archive"},
        },
    )
    results = grade_scenario(scenario)
    destination_result = next(r for r in results if r.name == "move_npc_destination_valid")
    assert destination_result.passed is False


def test_move_npc_destination_legal_for_own_npc_passes() -> None:
    authoritative_input = {
        "current_player_room_id": "eval_foyer",
        "clock_tick": 0,
        "facts": [],
        "npcs": [
            {
                "npc_id": "npc_a",
                "location_id": "eval_foyer",
                "status": "active",
                "one_hop_destination_room_ids": ["eval_gallery"],
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
                "destination_room_id": "eval_gallery",
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
                "destination_room_id": "eval_gallery",
            },
        },
    )
    results = grade_scenario(scenario)
    npc_result = next(r for r in results if r.name == "referenced_npc_exists")
    assert npc_result.passed is False


def test_checked_in_illegal_move_scenario_rejects_injected_illegal_destination() -> None:
    """The checked-in negative scenario's fixture_output is safe
    (decision=none); the cross-NPC-leak regression is proven here by
    injecting an illegal move_npc proposal directly."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["director-illegal-non-one-hop-move"].model_copy(deep=True)
    scenario.actual_output = {
        "decision": "act",
        "world_action": {
            "action": "move_npc",
            "npc_id": "eval_npc_a",
            "destination_room_id": "eval_vault",
        },
    }
    results = grade_scenario(scenario)
    destination_result = next(r for r in results if r.name == "move_npc_destination_valid")
    assert destination_result.passed is False


def test_checked_in_unsupported_action_scenario_rejects_injected_spawn_npc() -> None:
    """The checked-in negative scenario's fixture_output is safe
    (decision=none); rejection of an injected spawn_npc proposal is proven
    here directly against its authoritative_input."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["director-unsupported-spawn-action"].model_copy(deep=True)
    scenario.actual_output = {
        "decision": "act",
        "world_action": {"action": "spawn_npc", "npc_id": "invented_npc"},
    }
    results = grade_scenario(scenario)
    assert any(
        result.name == "allowed_world_action_vocab" and result.passed is False for result in results
    )
    assert any(result.name == "proposal_contract" and result.passed is False for result in results)


def test_missing_output_no_longer_passes_via_require_failure_inversion() -> None:
    """A scenario must not pass merely because an arbitrary grader failed
    (the removed require_failure inversion). Missing output must fail."""

    scenario = Scenario(
        scenario_id="no-inversion-missing-output",
        description="Missing output must fail, not pass via inverted grader failure.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
    )
    result = EvalRunner().run(scenario, actual_output=None)
    assert result.passed is False


def test_safe_decision_none_passes_illegal_move_and_unsupported_action_scenarios() -> None:
    """The checked-in negative-context scenarios represent expected current
    safe behavior (decision=none) and pass as part of the normal corpus."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    for scenario_id in (
        "director-illegal-non-one-hop-move",
        "director-unsupported-spawn-action",
    ):
        result = EvalRunner().run(scenarios[scenario_id])
        assert result.passed is True


def test_illegal_move_scenario_no_longer_requires_blanket_no_op() -> None:
    """The illegal-move scenario only tests that a destination legal for
    NPC B is not treated as legal for NPC A; a live Director choosing a
    DIFFERENT valid bounded proposal (here, NPC B's own legal one-hop move)
    must not fail solely because it isn't decision=none."""

    scenarios = {s.scenario_id: s for s in load_scenarios()}
    scenario = scenarios["director-illegal-non-one-hop-move"].model_copy(deep=True)
    assert "require_none" not in scenario.deterministic_expectations

    npc_b_context = next(
        npc for npc in scenario.authoritative_input["npcs"] if npc["npc_id"] == "eval_npc_b"
    )
    legal_destination = npc_b_context["one_hop_destination_room_ids"][0]
    scenario.actual_output = {
        "decision": "act",
        "world_action": {
            "action": "move_npc",
            "npc_id": "eval_npc_b",
            "destination_room_id": legal_destination,
        },
    }
    results = grade_scenario(scenario)
    assert all(result.passed for result in results)


def test_explicit_none_actual_output_does_not_fall_back_to_fixture_output() -> None:
    """An explicitly supplied actual_output=None must be graded as missing
    output, never silently backfilled from fixture_output."""

    scenario = Scenario(
        scenario_id="explicit-none-no-fallback",
        description="Explicit actual_output=None must not use fixture_output.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        fixture_output={"decision": "none"},
    )
    result = EvalRunner().run(scenario, actual_output=None)
    assert result.actual_output is None
    assert result.passed is False


def test_omitted_actual_output_falls_back_to_fixture_output_offline() -> None:
    """Omitting actual_output entirely preserves normal offline behavior:
    falling back to scenario.actual_output, then fixture_output."""

    scenario = Scenario(
        scenario_id="omitted-output-uses-fixture",
        description="Omitted actual_output should use fixture_output offline.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        fixture_output={"decision": "none"},
    )
    result = EvalRunner().run(scenario)
    assert result.actual_output == {"decision": "none"}
    assert result.passed is True


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


def test_mocked_live_director_failure_is_sanitized(monkeypatch) -> None:
    """A Director provider/agent failure must escape as a sanitized
    LiveEvalError, never a raw traceback exposing provider content."""

    secret_marker = "RAW_PROVIDER_RESPONSE_SECRET_MARKER"

    class FailingDirector:
        async def propose(self, *, director_input, model=None):
            raise RuntimeError(f"provider blew up with body: {secret_marker}")

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FailingDirector)
    scenario = Scenario(
        scenario_id="live-director-failure",
        description="Director provider failure must be sanitized.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
    )
    with pytest.raises(LiveEvalError) as exc_info:
        asyncio.run(_run_live_scenario(scenario))
    assert secret_marker not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret_marker not in "".join(
        traceback.format_exception(
            type(exc_info.value), exc_info.value, exc_info.value.__traceback__
        )
    )


_NARRATOR_FIXTURE = {
    "player_message": "look",
    "scene_context": {
        "current_room": {"id": "eval_foyer", "name": "Eval Foyer", "description": "A synthetic test room."},
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
        reply_text = "The spirit is not nearby, so your words go unanswered."
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
    assert forwarded_payload.tool_result.summary == "The spirit is not nearby to respond."
    assert forwarded_payload.parsed_action is not None
    assert forwarded_payload.parsed_action.action == "talk"


def test_mocked_live_narrator_failure_is_sanitized(monkeypatch) -> None:
    """A Narrator/model-client provider failure must escape as a sanitized
    LiveEvalError, never a raw traceback exposing provider content."""

    secret_marker = "RAW_PROVIDER_RESPONSE_SECRET_MARKER"

    class FailingNarrator:
        async def generate(self, *, payload, model=None):
            raise RuntimeError(f"model client failed with body: {secret_marker}")

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.NarratorAgent", FailingNarrator)
    scenario = Scenario(
        scenario_id="live-narrator-failure",
        description="Narrator provider failure must be sanitized.",
        target=ScenarioTarget.NARRATOR,
        authoritative_input=_NARRATOR_FIXTURE,
    )
    with pytest.raises(LiveEvalError) as exc_info:
        asyncio.run(_run_live_scenario(scenario))
    assert secret_marker not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret_marker not in "".join(
        traceback.format_exception(
            type(exc_info.value), exc_info.value, exc_info.value.__traceback__
        )
    )


def test_mocked_live_batch_runs_multiple_scenarios_in_one_event_loop(monkeypatch) -> None:
    """The production model client caches an AsyncOpenAI client; running one
    asyncio.run() per scenario would create/tear down a new event loop per
    scenario and risk reusing a cached async client bound to a closed loop.
    _run_live_scenarios() must own the whole batch inside a single event
    loop, invoked via exactly one asyncio.run() call by the caller."""

    loop_ids: list[int] = []

    class FakeDirectorProposal:
        def model_dump(self):
            return {"decision": "none"}

    class FakeDirectorResult:
        proposal = FakeDirectorProposal()
        usage = None

    class FakeDirector:
        async def propose(self, *, director_input, model=None):
            loop_ids.append(id(asyncio.get_running_loop()))
            return FakeDirectorResult()

    class FakeNarratorOutput:
        reply_text = "grounded"
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
            loop_ids.append(id(asyncio.get_running_loop()))
            return FakeNarratorOutput()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FakeDirector)
    monkeypatch.setattr("evals.runner.NarratorAgent", FakeNarrator)

    director_scenario = Scenario(
        scenario_id="live-batch-director",
        description="First scenario in the batch.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
    )
    narrator_scenario = Scenario(
        scenario_id="live-batch-narrator",
        description="Second scenario in the batch.",
        target=ScenarioTarget.NARRATOR,
        authoritative_input=_NARRATOR_FIXTURE,
    )

    results = asyncio.run(_run_live_scenarios([director_scenario, narrator_scenario]))

    assert len(results) == 2
    assert {result.scenario_id for result in results} == {"live-batch-director", "live-batch-narrator"}
    # Both scenario calls ran under the exact same running event loop.
    assert len(loop_ids) == 2
    assert loop_ids[0] == loop_ids[1]


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


def test_report_excludes_raw_fixture_output_field() -> None:
    """fixture_output is arbitrary scenario output and may itself be a bare
    raw response string, which recursive key-stripping alone cannot sanitize
    since there is no nested key to strip. It must be excluded structurally,
    just like actual_output."""

    marker = "UNIQUE_RAW_FIXTURE_OUTPUT_MARKER_2X8"
    scenario = Scenario(
        scenario_id="report-fixture-output-exclusion",
        description="Top-level fixture_output must not be serialized.",
        target=ScenarioTarget.NARRATOR,
        fixture_output=marker,
    )
    result = EvalRunner().run(scenario, actual_output={"reply_text": "safe narration"})
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


def test_report_strips_response_bearing_detail_keys_recursively() -> None:
    """GraderResult.details is intentionally generic; response-bearing keys
    understood by _extract_reply_text (reply_text/text/output/reply) must be
    stripped recursively, even when manually attached by a grader."""

    marker = "UNIQUE_SECRET_MARKER_4Z7Q"
    result = ScenarioResult(
        scenario_id="report-response-bearing-keys",
        description="Response-bearing detail keys must be stripped.",
        target=ScenarioTarget.NARRATOR,
        grader_results=[
            GraderResult(
                name="fake-grader",
                passed=True,
                details={
                    "reply_text": marker,
                    "expected": ["safe term"],
                    "nested": {"text": marker, "output": marker, "reply": marker},
                },
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )
    serialized = json.dumps(summarize_results([result]))
    assert marker not in serialized
    assert "safe term" in serialized


def test_report_allowlists_grader_details_dropping_unknown_keys() -> None:
    """`GraderResult.details` is an arbitrary `dict[str, Any]`; a grader
    could attach a raw provider/model response body under any unrecognized
    key. The report boundary must use an explicit allowlist so unknown keys
    are dropped by default, rather than relying on an ever-expanding
    blacklist of specific known-bad key names."""

    marker = "UNIQUE_RAW_PROVIDER_RESPONSE_MARKER_5K2P"
    result = ScenarioResult(
        scenario_id="report-allowlist-unknown-key",
        description="Unknown grader detail keys must not survive report serialization.",
        target=ScenarioTarget.NARRATOR,
        grader_results=[
            GraderResult(
                name="fake-grader",
                passed=True,
                details={"provider_response": marker, "length": 42},
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )
    summary = summarize_results([result])
    details = summary["results"][0]["grader_results"][0]["details"]
    assert marker not in json.dumps(summary)
    assert "provider_response" not in details
    assert details["length"] == 42


def test_report_drops_arbitrary_nested_mapping_under_an_allowed_detail_key() -> None:
    """An allowed key name only bounds the KEY; nothing prevents a grader
    from attaching an arbitrary nested mapping as that key's VALUE (e.g.
    details={"expected": {"reply_text": ...}}). The report boundary must
    restrict values to bounded scalar/list shapes, not just filter keys."""

    marker = "UNIQUE_RAW_PROVIDER_MARKER"
    result = ScenarioResult(
        scenario_id="report-nested-detail-value",
        description="Nested mappings under an allowed key must not survive report serialization.",
        target=ScenarioTarget.NARRATOR,
        grader_results=[
            GraderResult(
                name="fake-grader",
                passed=True,
                details={"expected": {"reply_text": marker}, "length": 42},
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )
    summary = summarize_results([result])
    details = summary["results"][0]["grader_results"][0]["details"]
    assert marker not in json.dumps(summary)
    assert "expected" not in details
    assert details["length"] == 42


def test_report_sanitizes_model_metadata_dropping_unknown_fields() -> None:
    """Scenario.model_metadata is an arbitrary caller-supplied dict; the
    report boundary must retain only target_agent/model/known usage
    counters and drop everything else, in both the JSON and human-readable
    report."""

    marker = "UNIQUE_RAW_PROVIDER_MARKER"
    scenario = Scenario(
        scenario_id="report-model-metadata-sanitization",
        description="Unknown model_metadata fields must not survive report serialization.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )
    result = EvalRunner().run(
        scenario,
        model_metadata={
            "target_agent": "director",
            "model": "gpt-test",
            "provider_response": marker,
            "usage": {"total_tokens": 12, "response_body": marker},
        },
    )
    summary = summarize_results([result])
    serialized_metadata = summary["results"][0]["model_metadata"]
    assert serialized_metadata["target_agent"] == "director"
    assert serialized_metadata["model"] == "gpt-test"
    assert serialized_metadata["usage"] == {"total_tokens": 12}
    assert "provider_response" not in serialized_metadata
    assert "response_body" not in serialized_metadata["usage"]
    assert marker not in json.dumps(summary)
    assert marker not in render_report([result])


class _NonSerializableMarker:
    """A deliberately non-JSON-serializable object standing in for whatever
    arbitrary content a caller might attach under an unrecognized key."""

    def __repr__(self) -> str:
        return "<NonSerializableMarker>"


def test_report_drops_non_serializable_object_under_unknown_grader_detail_key() -> None:
    """Sanitization must happen BEFORE any JSON-mode encoding of the
    ScenarioResult. An unknown grader-detail key can hold an arbitrary
    non-JSON-serializable object; summarize_results() must not raise, must
    drop the unknown field, and must still produce output json.dumps() can
    encode."""

    result = ScenarioResult(
        scenario_id="report-non-serializable-detail",
        description="A non-serializable object under an unknown key must not crash the report.",
        target=ScenarioTarget.NARRATOR,
        grader_results=[
            GraderResult(
                name="fake-grader",
                passed=True,
                details={"unknown_object_field": _NonSerializableMarker(), "length": 7},
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )

    summary = summarize_results([result])
    details = summary["results"][0]["grader_results"][0]["details"]
    assert "unknown_object_field" not in details
    assert details["length"] == 7
    assert json.dumps(summary)  # must not raise


def test_report_drops_non_serializable_object_under_unknown_model_metadata_key() -> None:
    """The equivalent regression for model_metadata: an unknown field
    holding a non-JSON-serializable object must be dropped rather than
    crashing report serialization."""

    result = ScenarioResult(
        scenario_id="report-non-serializable-metadata",
        description="A non-serializable object under an unknown model_metadata key must not crash the report.",
        target=ScenarioTarget.DIRECTOR,
        passed=True,
        score=1.0,
        max_score=1.0,
        model_metadata={
            "target_agent": "director",
            "model": "gpt-test",
            "unknown_object_field": _NonSerializableMarker(),
        },
    )

    summary = summarize_results([result])
    metadata = summary["results"][0]["model_metadata"]
    assert "unknown_object_field" not in metadata
    assert metadata["target_agent"] == "director"
    assert metadata["model"] == "gpt-test"
    assert json.dumps(summary)  # must not raise


@pytest.mark.parametrize(
    "model_metadata",
    [
        {"target_agent": []},
        {"target_agent": {}},
        {"model": []},
        {"usage": []},
        {"usage": {"total_tokens": object()}},
        {"usage": {"total_tokens": True}},
    ],
)
def test_report_sanitizes_malformed_model_metadata_types_without_raising(model_metadata) -> None:
    """`_safe_model_metadata()` must fail closed (drop the malformed field)
    rather than raise, for values whose TYPE is wrong even though the key
    name is recognized (e.g. target_agent as a list is unhashable and would
    otherwise crash a naive `in frozenset` membership check; a bool must
    never be accepted as a numeric token count)."""

    result = ScenarioResult(
        scenario_id="report-malformed-model-metadata",
        description="Malformed model_metadata value types must not crash report serialization.",
        target=ScenarioTarget.DIRECTOR,
        passed=True,
        score=1.0,
        max_score=1.0,
        model_metadata=model_metadata,
    )

    summary = summarize_results([result])
    assert summary["results"][0]["model_metadata"] == {}
    assert json.dumps(summary)  # must not raise


def test_render_report_names_failed_graders_for_a_failing_scenario() -> None:
    """Issue #55 requires the human-readable report to identify which
    scenario AND which grader/check failed, not just a bare status/score."""

    scenario = Scenario(
        scenario_id="report-failed-grader-names",
        description="A scenario that fails both proposal_contract and require_none.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "act", "world_action": {"action": "spawn_npc", "npc_id": "eval_x"}},
    )
    result = EvalRunner().run(scenario)
    assert result.passed is False
    failed_names = {gr.name for gr in result.grader_results if not gr.passed}
    assert failed_names  # sanity: this scenario is expected to have failing graders

    report_text = render_report([result])
    assert "FAIL" in report_text
    assert "failed:" in report_text
    for name in failed_names:
        assert name in report_text


def test_render_report_passing_scenario_stays_concise_without_a_failed_line() -> None:
    scenario = Scenario(
        scenario_id="report-passing-concise",
        description="A passing scenario must not print a failed-graders line.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        actual_output={"decision": "none"},
    )
    result = EvalRunner().run(scenario)
    assert result.passed is True

    report_text = render_report([result])
    assert "PASS" in report_text
    scenario_lines = [line for line in report_text.splitlines() if "report-passing-concise" in line]
    assert scenario_lines == ["- report-passing-concise: PASS (100.00%)"]
    assert not any(line.strip().startswith("failed:") for line in scenario_lines)


def test_report_omits_provider_controlled_action_npc_id_destination_from_details() -> None:
    """`action`, `npc_id`, and `destination` are derived from actual
    (potentially unvalidated/provider-controlled) Director output and must
    never be persisted into a stable report, even though other bounded
    diagnostics from the same grader survive."""

    marker = "UNIQUE_PROVIDER_MARKER"
    result = ScenarioResult(
        scenario_id="report-no-provider-leak",
        description="Provider-controlled action/npc_id/destination must be dropped from stable details.",
        target=ScenarioTarget.DIRECTOR,
        grader_results=[
            GraderResult(
                name="move_npc_destination_valid",
                passed=False,
                details={
                    "action": marker,
                    "npc_id": marker,
                    "destination": marker,
                    "allowed": ["move_npc"],
                },
            )
        ],
        passed=False,
        score=0.0,
        max_score=1.0,
    )

    summary = summarize_results([result])
    report_json = json.dumps(summary)
    assert marker not in report_json

    details = summary["results"][0]["grader_results"][0]["details"]
    assert "action" not in details
    assert "npc_id" not in details
    assert "destination" not in details
    assert details["allowed"] == ["move_npc"]

    report_text = render_report([result])
    assert marker not in report_text


def test_report_bounds_oversized_diagnostic_list_but_keeps_small_ones() -> None:
    small_list_result = ScenarioResult(
        scenario_id="report-small-list",
        description="A small allowed list must survive report serialization.",
        target=ScenarioTarget.DIRECTOR,
        grader_results=[
            GraderResult(
                name="move_npc_destination_valid",
                passed=True,
                details={"allowed": ["eval_room_a", "eval_room_b"]},
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )
    oversized_list_result = ScenarioResult(
        scenario_id="report-oversized-list",
        description="An oversized allowed list must not survive report serialization in full.",
        target=ScenarioTarget.DIRECTOR,
        grader_results=[
            GraderResult(
                name="move_npc_destination_valid",
                passed=True,
                details={
                    "allowed": [f"eval_room_{i}" for i in range(50)],
                    "length": 50,
                },
            )
        ],
        passed=True,
        score=1.0,
        max_score=1.0,
    )

    summary = summarize_results([small_list_result, oversized_list_result])
    assert json.dumps(summary)  # must remain JSON serializable

    small_details = summary["results"][0]["grader_results"][0]["details"]
    assert small_details["allowed"] == ["eval_room_a", "eval_room_b"]

    oversized_details = summary["results"][1]["grader_results"][0]["details"]
    assert "allowed" not in oversized_details
    # Other safe diagnostics on the same grader result are unaffected.
    assert oversized_details["length"] == 50


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


def _persistence_row_snapshot() -> dict[str, list[dict[str, object]]]:
    """Snapshot full row content (not just counts) for every eval-relevant
    table, sorted by primary key, so update-in-place is detected as reliably
    as row creation/deletion."""

    engine = get_engine()
    with engine.connect() as connection:
        snapshot: dict[str, list[dict[str, object]]] = {}
        for name, table, order_column in (
            ("campaigns", campaigns, campaigns.c.id),
            ("turns", turns, turns.c.id),
            ("game_events", game_events, game_events.c.id),
            ("model_requests", model_requests, model_requests.c.id),
        ):
            rows = connection.execute(select(table).order_by(order_column)).mappings().all()
            snapshot[name] = [dict(row) for row in rows]
        return snapshot


def _seed_persistence_rows() -> None:
    """Seed one representative row per eval-relevant table so nonmutation
    tests can detect an in-place update, not just row creation/deletion:
    counts alone stay equal even if an existing row's content is silently
    rewritten."""

    with session() as db:
        user = db.resolve_internal_user(
            identity_provider="google",
            provider_issuer="https://accounts.google.com",
            provider_subject="eval-nonmutation-user",
            email="eval-nonmutation@example.com",
            email_verified=True,
            display_name=None,
            avatar_url=None,
        )
        db.create_campaign(
            campaign_id="eval_nonmutation_campaign",
            owner_user_id=user.id,
            name="Eval nonmutation campaign",
        )
        db.create_turn(
            campaign_id="eval_nonmutation_campaign",
            turn_id="eval_nonmutation_turn",
            role="user",
            content="seeded content that must remain untouched",
        )
        db.add_event(
            event_id="eval_nonmutation_event",
            campaign_id="eval_nonmutation_campaign",
            turn_id="eval_nonmutation_turn",
            type="player_message_received",
        )
        db.log_model_request(
            request_id="eval_nonmutation_request",
            owner_user_id=user.id,
            campaign_id="eval_nonmutation_campaign",
            turn_id="eval_nonmutation_turn",
            agent_name="narrator",
            model="seeded-model",
            estimated_input_tokens=1,
            success=True,
        )


def test_offline_eval_execution_does_not_mutate_campaign_or_telemetry_persistence() -> None:
    """Issue #55 requires proof that eval execution does not mutate campaign
    or database state. Running the whole checked-in offline corpus must not
    create, delete, or update (in place) any campaign, turn, game-event, or
    model-request row. A pre-seeded representative row per table makes the
    update-in-place case meaningful, since an empty table trivially proves
    only creation/deletion protection."""

    _seed_persistence_rows()
    before = _persistence_row_snapshot()
    results = run_scenarios(load_scenarios())
    assert len(results) == 8
    after = _persistence_row_snapshot()
    assert after == before


def test_mocked_live_eval_execution_does_not_mutate_campaign_or_telemetry_persistence(
    monkeypatch,
) -> None:
    """The live harness invokes the real Director/Narrator agent
    abstractions; even a mocked live run must not touch persistence,
    including in-place updates to a pre-seeded row."""

    class FakeProposal:
        def model_dump(self):
            return {"decision": "none"}

    class FakeResult:
        proposal = FakeProposal()
        usage = None

    class FakeDirector:
        async def propose(self, *, director_input, model=None):
            return FakeResult()

    monkeypatch.setattr("evals.runner.settings.AI_ENABLED", True)
    monkeypatch.setattr("evals.runner.settings.OPENAI_API_KEY", "present")
    monkeypatch.setattr("evals.runner.DirectorAgent", FakeDirector)

    scenario = Scenario(
        scenario_id="live-no-persistence",
        description="Mocked live Director run must not touch persistence.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
    )

    _seed_persistence_rows()
    before = _persistence_row_snapshot()
    output, model_metadata = asyncio.run(_run_live_scenario(scenario))
    EvalRunner().run(scenario, actual_output=output, model_metadata=model_metadata)
    after = _persistence_row_snapshot()
    assert after == before


def test_readme_documented_scenarios_and_tags_exist_in_corpus() -> None:
    scenarios = load_scenarios()
    ids = {scenario.scenario_id for scenario in scenarios}
    assert "narrator-observable-item-state" in ids
    assert any(
        "movement" in scenario.tags and scenario.target == ScenarioTarget.DIRECTOR
        for scenario in scenarios
    )


def _assert_eval_namespaced(value: str, *, where: str) -> None:
    assert value.startswith("eval_"), f"{where} must use the synthetic eval_ namespace, got {value!r}"


def _assert_director_input_is_synthetic(authoritative_input: dict, *, scenario_id: str) -> None:
    _assert_eval_namespaced(
        authoritative_input["current_player_room_id"],
        where=f"{scenario_id}.authoritative_input.current_player_room_id",
    )
    for npc in authoritative_input.get("npcs", []):
        _assert_eval_namespaced(npc["npc_id"], where=f"{scenario_id}.npcs[].npc_id")
        _assert_eval_namespaced(npc["location_id"], where=f"{scenario_id}.npcs[].location_id")
        for destination in npc.get("one_hop_destination_room_ids", []):
            _assert_eval_namespaced(
                destination, where=f"{scenario_id}.npcs[].one_hop_destination_room_ids[]"
            )


def _assert_narrator_input_is_synthetic(authoritative_input: dict, *, scenario_id: str) -> None:
    """Walk every entity-ID-bearing field of a Narrator authoritative_input
    and require the synthetic eval_ namespace. Free-form player/parser text
    such as ParsedAction.target or ToolExecutionResult.requested_target is
    deliberately excluded: it names something in prose (e.g. "spirit") but
    is not itself an entity ID, so it must not be required to use eval_."""

    scene_context = authoritative_input.get("scene_context", {})
    current_room = scene_context.get("current_room")
    if current_room is not None:
        _assert_eval_namespaced(
            current_room["id"], where=f"{scenario_id}.scene_context.current_room.id"
        )
    for npc in scene_context.get("nearby_npcs", []):
        _assert_eval_namespaced(npc["id"], where=f"{scenario_id}.scene_context.nearby_npcs[].id")
    for item in scene_context.get("nearby_items", []):
        _assert_eval_namespaced(item["id"], where=f"{scenario_id}.scene_context.nearby_items[].id")
    for item in scene_context.get("inventory_items", []):
        _assert_eval_namespaced(item["id"], where=f"{scenario_id}.scene_context.inventory_items[].id")
    for exit_entry in scene_context.get("available_exits", []):
        # Production `World.available_exits()` returns dicts shaped like
        # {"direction": ..., "room_id": ..., "room_name": ...}; the
        # destination room ID is carried under "room_id", not the generic
        # dict[str, str] key names implied by the (intentionally loose)
        # NarratorSceneContext schema type.
        destination_room_id = exit_entry.get("room_id")
        if destination_room_id is not None:
            _assert_eval_namespaced(
                destination_room_id, where=f"{scenario_id}.scene_context.available_exits[].room_id"
            )

    tool_result = authoritative_input.get("tool_result")
    if isinstance(tool_result, dict):
        # These ToolExecutionResult fields are genuinely entity/location IDs
        # in production (see app/services/tool_executor.py); requested_target
        # is free-form player-facing text and is deliberately excluded.
        for field_name in (
            "previous_location",
            "current_location",
            "moved_from",
            "moved_to",
            "npc_id",
            "item_id",
            "with_item_id",
            "resolved_exit",
        ):
            value = tool_result.get(field_name)
            if value is not None:
                _assert_eval_namespaced(value, where=f"{scenario_id}.tool_result.{field_name}")

        # ToolExecutionResult.available_exits reuses the same production
        # {"direction", "room_id", "room_name"} shape as
        # World.available_exits(); the destination room ID is "room_id".
        for exit_entry in tool_result.get("available_exits", []):
            destination_room_id = exit_entry.get("room_id") if isinstance(exit_entry, dict) else None
            if destination_room_id is not None:
                _assert_eval_namespaced(
                    destination_room_id, where=f"{scenario_id}.tool_result.available_exits[].room_id"
                )

        # ToolExecutionResult.available_items reuses
        # app.game.items.available_items_for_room()'s {"id", "name"} shape.
        for item_entry in tool_result.get("available_items", []):
            item_id = item_entry.get("id") if isinstance(item_entry, dict) else None
            if item_id is not None:
                _assert_eval_namespaced(
                    item_id, where=f"{scenario_id}.tool_result.available_items[].id"
                )

        # Unlike NarratorSceneContext.inventory_items (list[NarratorItem]),
        # ToolExecutionResult.inventory_items is a bare list[str] of item IDs
        # directly (see app.game.items.inventory_item_ids()).
        for item_id in tool_result.get("inventory_items", []):
            if isinstance(item_id, str):
                _assert_eval_namespaced(
                    item_id, where=f"{scenario_id}.tool_result.inventory_items[]"
                )

        for npc in tool_result.get("nearby_npcs", []):
            npc_id = npc.get("id") if isinstance(npc, dict) else None
            if npc_id is not None:
                _assert_eval_namespaced(npc_id, where=f"{scenario_id}.tool_result.nearby_npcs[].id")

        _assert_state_delta_entity_ids_are_synthetic(
            tool_result.get("state_delta"), scenario_id=scenario_id
        )


def _assert_state_delta_entity_ids_are_synthetic(state_delta: Any, *, scenario_id: str) -> None:
    """Cover the known current ToolExecutionResult.state_delta shapes that
    carry authoritative entity IDs (see app/services/tool_executor.py).
    This is intentionally NOT a generic recursive walk: state_delta also
    carries free-form property names/values (e.g. `properties.<name>`),
    booleans, and counts that are not entity IDs and must not be forced
    into the eval_ namespace."""

    if not isinstance(state_delta, dict):
        return

    # state_delta["items"] is keyed by item ID (see take_item/drop_item/
    # set_item_property in tool_executor.py, e.g.
    # state_delta={"items": {item_id: {...}}}).
    items_delta = state_delta.get("items")
    if isinstance(items_delta, dict):
        for item_id, item_change in items_delta.items():
            _assert_eval_namespaced(item_id, where=f"{scenario_id}.tool_result.state_delta.items{{key}}")
            if not isinstance(item_change, dict):
                continue
            location_change = item_change.get("location")
            if isinstance(location_change, dict):
                for direction in ("from", "to"):
                    _assert_state_delta_location_value(
                        location_change.get(direction),
                        where=f"{scenario_id}.tool_result.state_delta.items[{item_id}].location.{direction}",
                    )
            # item_change["properties"][<name>]["from"/"to"] holds arbitrary
            # property values (e.g. lit: false -> true), not entity IDs, and
            # is deliberately not walked here.

    # state_delta["player"]["location"] carries room-ID transitions (see
    # move_player in tool_executor.py:
    # state_delta={"player": {"location": {"from": ..., "to": ...}}}).
    player_delta = state_delta.get("player")
    if isinstance(player_delta, dict):
        location_change = player_delta.get("location")
        if isinstance(location_change, dict):
            for direction in ("from", "to"):
                _assert_state_delta_location_value(
                    location_change.get(direction),
                    where=f"{scenario_id}.tool_result.state_delta.player.location.{direction}",
                )
        # state_delta["player"]["inventory"] carries bare item-ID list
        # transitions (see take_item/drop_item: state_delta={"player":
        # {"inventory": {"from": [...], "to": [...]}}}).
        inventory_change = player_delta.get("inventory")
        if isinstance(inventory_change, dict):
            for direction in ("from", "to"):
                for item_id in inventory_change.get(direction) or []:
                    if isinstance(item_id, str):
                        _assert_eval_namespaced(
                            item_id,
                            where=f"{scenario_id}.tool_result.state_delta.player.inventory.{direction}[]",
                        )


def _assert_state_delta_location_value(value: Any, *, where: str) -> None:
    if value is None or value == PLAYER_INVENTORY_LOCATION:
        # PLAYER_INVENTORY_LOCATION ("player:current") is a fixed production
        # sentinel, not a room ID, and must not be required to use eval_.
        return
    if isinstance(value, str):
        _assert_eval_namespaced(value, where=where)


def _assert_director_world_action_is_synthetic(fixture_output, *, scenario_id: str) -> None:
    if not isinstance(fixture_output, dict) or fixture_output.get("decision") != "act":
        return
    world_action = fixture_output.get("world_action", {})
    npc_id = world_action.get("npc_id")
    if npc_id is not None:
        _assert_eval_namespaced(npc_id, where=f"{scenario_id}.fixture_output.world_action.npc_id")
    destination_room_id = world_action.get("destination_room_id")
    if destination_room_id is not None:
        _assert_eval_namespaced(
            destination_room_id,
            where=f"{scenario_id}.fixture_output.world_action.destination_room_id",
        )


def test_checked_in_corpus_uses_synthetic_identifiers_not_production_world_ids() -> None:
    """Issue #55 excludes production identifiers from eval fixtures. Rather
    than checking the corpus against an inherently incomplete blacklist of
    specific production strings, structurally walk every checked-in
    scenario's known entity-ID-bearing fields (room/NPC/item IDs, one-hop
    destinations, and any proposed move's NPC/destination) and require the
    synthetic eval_ namespace, without importing or coupling to any
    production world/NPC/item registry."""

    for scenario in load_scenarios():
        authoritative_input = scenario.authoritative_input
        if scenario.target == ScenarioTarget.DIRECTOR:
            _assert_director_input_is_synthetic(authoritative_input, scenario_id=scenario.scenario_id)
            _assert_director_world_action_is_synthetic(
                scenario.fixture_output, scenario_id=scenario.scenario_id
            )
        else:
            _assert_narrator_input_is_synthetic(authoritative_input, scenario_id=scenario.scenario_id)

    # Production schema validation must still succeed against the synthetic
    # corpus (already exercised by load_scenarios() at import/collection
    # time, and re-asserted here for clarity).
    for scenario in load_scenarios():
        if scenario.target == ScenarioTarget.DIRECTOR:
            DirectorInput.model_validate(scenario.authoritative_input)
        else:
            NarratorAgentInput.model_validate(scenario.authoritative_input)


_NARRATOR_EXITS_AND_INVENTORY_FIXTURE = {
    "player_message": "check the way north and my pack",
    "scene_context": {
        "current_room": {"id": "eval_foyer", "name": "Evaluation Foyer", "description": "A dusty synthetic foyer."},
        "available_exits": [
            {"direction": "north", "room_id": "eval_gallery", "room_name": "Evaluation Gallery"}
        ],
        "inventory_items": [
            {"id": "eval_brass_key", "name": "Evaluation Brass Key", "description": "A synthetic test key."}
        ],
        "nearby_npcs": [],
    },
}


def test_narrator_available_exits_and_inventory_items_synthetic_ids_pass() -> None:
    """A synthetic-namespaced available_exits destination room ID and
    inventory_items item ID must validate against both the production
    NarratorAgentInput contract and the structural eval_ helper."""

    NarratorAgentInput.model_validate(_NARRATOR_EXITS_AND_INVENTORY_FIXTURE)
    _assert_narrator_input_is_synthetic(
        _NARRATOR_EXITS_AND_INVENTORY_FIXTURE, scenario_id="synthetic-exits-and-inventory"
    )


def test_narrator_available_exits_non_eval_destination_is_rejected() -> None:
    fixture = copy.deepcopy(_NARRATOR_EXITS_AND_INVENTORY_FIXTURE)
    fixture["scene_context"]["available_exits"][0]["room_id"] = "entry_hall"
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-exit")


def test_narrator_inventory_items_non_eval_id_is_rejected() -> None:
    fixture = copy.deepcopy(_NARRATOR_EXITS_AND_INVENTORY_FIXTURE)
    fixture["scene_context"]["inventory_items"][0]["id"] = "brass_key"
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-inventory-item")


_NARRATOR_TOOL_RESULT_COLLECTIONS_FIXTURE = {
    "player_message": "look around and check my pack",
    "scene_context": {
        "current_room": {"id": "eval_foyer", "name": "Evaluation Foyer", "description": "A dusty synthetic foyer."},
        "nearby_npcs": [],
    },
    "tool_result": {
        "success": True,
        "applied_tools": ["observe_room"],
        "summary": "The player takes stock of the room.",
        "available_exits": [
            {"direction": "north", "room_id": "eval_gallery", "room_name": "Evaluation Gallery"}
        ],
        "available_items": [{"id": "eval_lantern", "name": "Evaluation Lantern"}],
        "inventory_items": ["eval_brass_key"],
        "nearby_npcs": [{"id": "eval_npc_a", "name": "Evaluation Steward"}],
    },
}


@pytest.mark.parametrize(
    ("field_path", "bad_value"),
    [
        ("available_exits", [{"direction": "north", "room_id": "entry_hall", "room_name": "Entry Hall"}]),
        ("available_items", [{"id": "brass_key", "name": "Brass Key"}]),
        ("inventory_items", ["brass_key"]),
        ("nearby_npcs", [{"id": "steward", "name": "Steward"}]),
    ],
)
def test_tool_result_collection_synthetic_ids_pass_and_reject_production_looking_values(
    field_path: str, bad_value
) -> None:
    """Every entity-ID-bearing ToolExecutionResult collection field must be
    validated against the synthetic eval_ namespace: an eval_-prefixed
    fixture passes, and a production-looking value (no eval_ prefix) in the
    same collection is rejected."""

    NarratorAgentInput.model_validate(_NARRATOR_TOOL_RESULT_COLLECTIONS_FIXTURE)
    _assert_narrator_input_is_synthetic(
        _NARRATOR_TOOL_RESULT_COLLECTIONS_FIXTURE, scenario_id="synthetic-tool-result-collections"
    )

    fixture = copy.deepcopy(_NARRATOR_TOOL_RESULT_COLLECTIONS_FIXTURE)
    fixture["tool_result"][field_path] = bad_value
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-tool-result-collection")


def _narrator_input_with_state_delta(state_delta: dict) -> dict:
    fixture = copy.deepcopy(_NARRATOR_TOOL_RESULT_COLLECTIONS_FIXTURE)
    fixture["tool_result"]["state_delta"] = state_delta
    return fixture


def test_state_delta_item_property_change_with_synthetic_id_passes() -> None:
    """`state_delta["items"]` is keyed by item ID (see
    app/services/tool_executor.py's set_item_property/take_item/drop_item);
    a synthetic eval_-namespaced item ID with an arbitrary (non-entity-ID)
    property change must pass."""

    fixture = _narrator_input_with_state_delta({"items": {"eval_lantern": {"lit": True}}})
    NarratorAgentInput.model_validate(fixture)
    _assert_narrator_input_is_synthetic(fixture, scenario_id="synthetic-state-delta-items")


def test_state_delta_item_key_with_production_looking_id_is_rejected() -> None:
    fixture = _narrator_input_with_state_delta({"items": {"brass_key": {"lit": True}}})
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-state-delta-item-key")


def test_state_delta_player_location_transition_with_synthetic_ids_passes() -> None:
    """`state_delta["player"]["location"]["from"/"to"]` carries room-ID
    transitions (see move_player in app/services/tool_executor.py)."""

    fixture = _narrator_input_with_state_delta(
        {"player": {"location": {"from": "eval_foyer", "to": "eval_gallery"}}}
    )
    NarratorAgentInput.model_validate(fixture)
    _assert_narrator_input_is_synthetic(fixture, scenario_id="synthetic-state-delta-player-location")


@pytest.mark.parametrize("direction", ["from", "to"])
def test_state_delta_player_location_transition_with_production_looking_id_is_rejected(direction: str) -> None:
    location_change = {"from": "eval_foyer", "to": "eval_gallery"}
    location_change[direction] = "entry_hall"
    fixture = _narrator_input_with_state_delta({"player": {"location": location_change}})
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-state-delta-player-location")


def test_state_delta_item_location_transition_allows_inventory_sentinel() -> None:
    """PLAYER_INVENTORY_LOCATION ("player:current") is a fixed production
    sentinel value used for take_item/drop_item transitions, not a room ID,
    and must not be required to use the eval_ namespace."""

    fixture = _narrator_input_with_state_delta(
        {"items": {"eval_lantern": {"location": {"from": "eval_gallery", "to": PLAYER_INVENTORY_LOCATION}}}}
    )
    NarratorAgentInput.model_validate(fixture)
    _assert_narrator_input_is_synthetic(fixture, scenario_id="synthetic-state-delta-item-location")


def test_state_delta_player_inventory_transition_rejects_production_looking_id() -> None:
    """`state_delta["player"]["inventory"]["from"/"to"]` carries bare
    item-ID list transitions (see take_item/drop_item)."""

    fixture = _narrator_input_with_state_delta(
        {"player": {"inventory": {"from": [], "to": ["brass_key"]}}}
    )
    NarratorAgentInput.model_validate(fixture)
    with pytest.raises(AssertionError):
        _assert_narrator_input_is_synthetic(fixture, scenario_id="non-eval-state-delta-inventory")


def _run_main_with_argv(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["evals.runner", *argv])
    return runner_module.main()


def test_cli_main_offline_json_scenario_id_filter_succeeds(monkeypatch, capsys) -> None:
    exit_code = _run_main_with_argv(
        monkeypatch, ["--scenario-id", "director-baseline-noop", "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_scenarios"] == 1
    assert payload["results"][0]["scenario_id"] == "director-baseline-noop"


def test_cli_main_offline_json_tag_filter_succeeds(monkeypatch, capsys) -> None:
    exit_code = _run_main_with_argv(monkeypatch, ["--tag", "movement", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_scenarios"] >= 1
    assert all("movement" in result["tags"] for result in payload["results"])


def test_cli_main_offline_json_agent_filter_succeeds(monkeypatch, capsys) -> None:
    exit_code = _run_main_with_argv(monkeypatch, ["--agent", "narrator", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_scenarios"] >= 1
    assert all(result["target"] == "narrator" for result in payload["results"])


def test_cli_main_no_match_raises_system_exit(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["evals.runner", "--scenario-id", "does-not-exist"])
    with pytest.raises(SystemExit):
        runner_module.main()


def test_cli_main_offline_grader_failure_returns_exit_code_1(monkeypatch, capsys) -> None:
    """main() must surface a failing offline scenario as exit code 1, not
    silently succeed."""

    failing_scenario = Scenario(
        scenario_id="cli-forced-failure",
        description="Deliberately violates its own deterministic expectations.",
        target=ScenarioTarget.DIRECTOR,
        authoritative_input=_DIRECTOR_FIXTURE,
        deterministic_expectations={"require_none": True},
        fixture_output={"decision": "act", "world_action": {"action": "advance_clock", "ticks": 1}},
    )
    monkeypatch.setattr(runner_module, "load_scenarios", lambda: [failing_scenario])
    exit_code = _run_main_with_argv(monkeypatch, ["--json"])
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1


def test_cli_main_live_without_credentials_fails_closed(monkeypatch, capsys) -> None:
    """--live without configured credentials must fail closed with a
    sanitized stderr message and exit code 2, never a raw traceback and
    never a real provider call."""

    monkeypatch.setattr(runner_module.settings, "AI_ENABLED", False)
    monkeypatch.setattr(runner_module.settings, "OPENAI_API_KEY", "")
    exit_code = _run_main_with_argv(
        monkeypatch, ["--scenario-id", "director-baseline-noop", "--live"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "live eval failed" in captured.err
    assert "Traceback" not in captured.err
