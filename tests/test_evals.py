from __future__ import annotations

import copy
import asyncio
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agents.narrator import NarratorAgentInput
from app.db.schema import campaigns, game_events, model_requests, turns
from app.db.session import get_engine
from app.schemas.director import DirectorInput

from evals.graders import grade_scenario
from evals.report import summarize_results
from evals.runner import EvalRunner, run_scenarios
from evals.runner import LiveEvalError, _run_live_scenario, _run_live_scenarios
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


def _persistence_row_counts() -> dict[str, int]:
    engine = get_engine()
    with engine.connect() as connection:
        return {
            "campaigns": connection.execute(select(func.count()).select_from(campaigns)).scalar_one(),
            "turns": connection.execute(select(func.count()).select_from(turns)).scalar_one(),
            "game_events": connection.execute(select(func.count()).select_from(game_events)).scalar_one(),
            "model_requests": connection.execute(
                select(func.count()).select_from(model_requests)
            ).scalar_one(),
        }


def test_offline_eval_execution_does_not_mutate_campaign_or_telemetry_persistence() -> None:
    """Issue #55 requires proof that eval execution does not mutate campaign
    or database state. Running the whole checked-in offline corpus must not
    create/update any campaign, turn, game-event, or model-request row."""

    before = _persistence_row_counts()
    results = run_scenarios(load_scenarios())
    assert len(results) == 8
    after = _persistence_row_counts()
    assert after == before


def test_mocked_live_eval_execution_does_not_mutate_campaign_or_telemetry_persistence(
    monkeypatch,
) -> None:
    """The live harness invokes the real Director/Narrator agent
    abstractions; even a mocked live run must not touch persistence."""

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

    before = _persistence_row_counts()
    output, model_metadata = asyncio.run(_run_live_scenario(scenario))
    EvalRunner().run(scenario, actual_output=output, model_metadata=model_metadata)
    after = _persistence_row_counts()
    assert after == before


def test_readme_documented_scenarios_and_tags_exist_in_corpus() -> None:
    scenarios = load_scenarios()
    ids = {scenario.scenario_id for scenario in scenarios}
    assert "narrator-observable-item-state" in ids
    assert any(
        "movement" in scenario.tags and scenario.target == ScenarioTarget.DIRECTOR
        for scenario in scenarios
    )


_PRODUCTION_WORLD_IDENTIFIERS = (
    "entry_hall",
    "grand_corridor",
    "library_ghost",
    "old_caretaker",
    "brass_key",
    "\"library\"",
)


def test_checked_in_corpus_uses_synthetic_identifiers_not_production_world_ids() -> None:
    """Issue #55 excludes production identifiers from eval fixtures. The
    checked-in corpus must reference fixture-local synthetic IDs (for
    example eval_foyer/eval_npc_a) rather than shipped campaign content, and
    must still validate against the real production schemas."""

    for path in sorted((_evals_scenarios_dir()).glob("*.json")):
        raw_text = path.read_text(encoding="utf-8")
        for identifier in _PRODUCTION_WORLD_IDENTIFIERS:
            assert identifier not in raw_text, f"{path.name} references production identifier {identifier!r}"

    # Production schema validation must still succeed against the synthetic
    # corpus (already exercised by load_scenarios() at import/collection
    # time, and re-asserted here for clarity).
    for scenario in load_scenarios():
        if scenario.target == ScenarioTarget.DIRECTOR:
            DirectorInput.model_validate(scenario.authoritative_input)
        else:
            NarratorAgentInput.model_validate(scenario.authoritative_input)


def _evals_scenarios_dir():
    from pathlib import Path

    import evals.scenarios as scenarios_module

    return Path(scenarios_module.__file__).resolve().parent
