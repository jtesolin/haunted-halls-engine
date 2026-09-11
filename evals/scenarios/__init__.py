from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agents.narrator import NarratorAgentInput
from app.schemas.director import DirectorInput

from evals.schemas import Scenario, ScenarioTarget

_SCENARIO_DIR = Path(__file__).resolve().parent

# The keys and value shapes actually implemented by the deterministic
# graders in evals/graders.py. deterministic_expectations is intentionally a
# generic dict[str, Any] on Scenario, but an unrecognized or malformed key
# (e.g. a typo such as "must_not_include") must fail scenario loading rather
# than being silently ignored by the grader and producing a false-green
# scenario.
_DIRECTOR_EXPECTATION_KEYS = frozenset({"require_none"})
_NARRATOR_EXPECTATION_KEYS = frozenset({"contains", "must_not_contain"})


def load_scenario(path: str | Path) -> Scenario:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    scenario = Scenario.model_validate(payload)
    _validate_target_specific_input(scenario)
    _validate_deterministic_expectations(scenario)
    return scenario


def _validate_target_specific_input(scenario: Scenario) -> None:
    """Validate authoritative_input against the real production contract.

    Scenario.authoritative_input stays a generic dict in the harness-wide
    Scenario model, but a malformed target-specific fixture must fail at
    load time, before offline grading, filtering/execution, or live
    execution. Reuses the existing production classes rather than a
    duplicate eval-only input schema.
    """

    if scenario.target == ScenarioTarget.DIRECTOR:
        DirectorInput.model_validate(scenario.authoritative_input)
    elif scenario.target == ScenarioTarget.NARRATOR:
        NarratorAgentInput.model_validate(scenario.authoritative_input)


def _validate_deterministic_expectations(scenario: Scenario) -> None:
    """Reject a scenario whose deterministic_expectations contract is not
    one the current grader for scenario.target actually implements.

    This is deliberately narrow: it validates only the specific
    key/value shapes evals/graders.py reads today, not a general
    expression/expectation language.
    """

    expectations = scenario.deterministic_expectations
    if scenario.target == ScenarioTarget.DIRECTOR:
        _validate_director_expectations(expectations, scenario_id=scenario.scenario_id)
    elif scenario.target == ScenarioTarget.NARRATOR:
        _validate_narrator_expectations(expectations, scenario_id=scenario.scenario_id)


def _validate_director_expectations(expectations: dict[str, Any], *, scenario_id: str) -> None:
    unknown_keys = set(expectations) - _DIRECTOR_EXPECTATION_KEYS
    if unknown_keys:
        raise ValueError(
            f"{scenario_id}: unsupported Director deterministic_expectations key(s): "
            f"{sorted(unknown_keys)}; only {sorted(_DIRECTOR_EXPECTATION_KEYS)} are implemented"
        )
    if "require_none" in expectations and not isinstance(expectations["require_none"], bool):
        raise ValueError(
            f"{scenario_id}: deterministic_expectations.require_none must be a bool, "
            f"got {type(expectations['require_none']).__name__}"
        )


def _validate_narrator_expectations(expectations: dict[str, Any], *, scenario_id: str) -> None:
    unknown_keys = set(expectations) - _NARRATOR_EXPECTATION_KEYS
    if unknown_keys:
        raise ValueError(
            f"{scenario_id}: unsupported Narrator deterministic_expectations key(s): "
            f"{sorted(unknown_keys)}; only {sorted(_NARRATOR_EXPECTATION_KEYS)} are implemented"
        )
    for key in _NARRATOR_EXPECTATION_KEYS:
        if key in expectations:
            _validate_narrator_expectation_value(expectations[key], key=key, scenario_id=scenario_id)


def _validate_narrator_expectation_value(value: Any, *, key: str, scenario_id: str) -> None:
    # bool is a subclass of int, so isinstance(value, str) is checked first
    # and neither bool nor int/float/dict is coerced with str(...) merely to
    # make it gradeable; a non-string/non-list-of-strings value fails load.
    if isinstance(value, str):
        if not value.strip():
            # A whitespace-only string (" ") is not caught by `not value`
            # but is just as meaningless for a substring `contains`/
            # `must_not_contain` check; reject it explicitly at load time
            # rather than letting it silently pass through and grade
            # against something misleading. The original (unstripped)
            # value is retained for grading when it IS meaningful.
            raise ValueError(f"{scenario_id}: deterministic_expectations.{key} must not be empty or whitespace-only")
        return
    if isinstance(value, list):
        if not value:
            raise ValueError(f"{scenario_id}: deterministic_expectations.{key} must not be empty")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"{scenario_id}: deterministic_expectations.{key} must be a non-empty, "
                    f"non-whitespace-only string or a list of such strings; got {item!r} in the list"
                )
        return
    raise ValueError(
        f"{scenario_id}: deterministic_expectations.{key} must be a non-empty string or a list "
        f"of non-empty strings, got {type(value).__name__}"
    )


def load_scenarios(directory: str | Path = _SCENARIO_DIR) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for file_path in sorted(Path(directory).glob("*.json")):
        scenarios.append(load_scenario(file_path))
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    duplicates = sorted(
        scenario_id
        for scenario_id in set(scenario_ids)
        if scenario_ids.count(scenario_id) > 1
    )
    if duplicates:
        raise ValueError(f"Duplicate scenario IDs: {', '.join(duplicates)}")
    return scenarios


def filter_scenarios(
    scenarios: list[Scenario],
    *,
    scenario_id: str | None = None,
    tag: str | None = None,
    target: str | None = None,
) -> list[Scenario]:
    return [
        scenario
        for scenario in scenarios
        if (scenario_id is None or scenario.scenario_id == scenario_id)
        and (tag is None or tag in scenario.tags)
        and (target is None or scenario.target.value == target)
    ]
