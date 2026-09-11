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
    validate_scenario_contract(scenario)
    return scenario


def validate_scenario_contract(scenario: Scenario) -> None:
    """The single shared scenario-contract validation entry point.

    `Scenario` is also the public PROGRAMMATIC contract: code can construct
    one directly and pass it straight to `EvalRunner.run()` without ever
    going through `load_scenario()`/a JSON fixture file. Both entry points
    must therefore enforce exactly the same rules -- target-specific
    authoritative-input validation, deterministic-expectation validation,
    and (for Narrator) the no-silently-ignored-fields invariant -- from this
    one place, rather than duplicating them between the loader and the
    runner.
    """

    _validate_target_specific_input(scenario)
    _validate_deterministic_expectations(scenario)


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
        _validate_no_ignored_narrator_fields(scenario)


def _validate_no_ignored_narrator_fields(scenario: Scenario) -> None:
    """A Narrator scenario's declared authoritative_input must equal the
    context the live Narrator would actually receive.

    NarratorAgentInput and its nested production models (NarratorSceneContext,
    NarratorItem, NearbyNPC, ToolExecutionResult, ParsedAction, ...) use
    ordinary Pydantic extra-ignore behavior (the production default), NOT
    extra="forbid" -- and that default must not change, since it is
    production behavior out of scope for this eval harness. A fixture typo
    (e.g. scene_context.nearby_npc instead of nearby_npcs) would otherwise
    validate successfully, remain present in the raw fixture JSON, and then
    be silently discarded when NarratorAgentInput is actually constructed --
    meaning the checked-in scenario would claim one context while a live
    run receives a different, silently-narrower one.

    This performs a structural diff between the ORIGINAL fixture dict and a
    JSON-safe dump of only the fields Pydantic actually accepted
    (exclude_unset=True, so unset defaults are not mistaken for "accepted"
    fields the fixture never declared). Any fixture key that does not
    survive that round trip was silently ignored, and the scenario is
    rejected with a clear error naming the offending path.
    """

    validated = NarratorAgentInput.model_validate(scenario.authoritative_input)
    accepted = validated.model_dump(mode="json", exclude_unset=True)
    ignored_paths = _find_ignored_fields(scenario.authoritative_input, accepted)
    if ignored_paths:
        raise ValueError(
            f"{scenario.scenario_id}: authoritative_input field(s) would be silently "
            f"ignored by NarratorAgentInput and never reach the live Narrator: "
            f"{', '.join(sorted(ignored_paths))}"
        )


def _find_ignored_fields(original: Any, accepted: Any, *, path: str = "") -> list[str]:
    """Recursively find keys present in `original` that do not survive in
    `accepted`. This is a generic structural diff (not a duplicate schema):
    it only needs to know how to walk dicts/lists, never the specific
    production field names/types."""

    if isinstance(original, dict):
        if not isinstance(accepted, dict):
            return [path or "<root>"]
        ignored: list[str] = []
        for key, value in original.items():
            child_path = f"{path}.{key}" if path else key
            if key not in accepted:
                ignored.append(child_path)
                continue
            ignored.extend(_find_ignored_fields(value, accepted[key], path=child_path))
        return ignored
    if isinstance(original, list):
        if not isinstance(accepted, list) or len(accepted) != len(original):
            # A length/shape mismatch here is not itself a "silently
            # ignored field"; the earlier NarratorAgentInput.model_validate
            # call above already enforces list-item shape/type validity.
            return []
        ignored = []
        for index, (original_item, accepted_item) in enumerate(zip(original, accepted)):
            ignored.extend(_find_ignored_fields(original_item, accepted_item, path=f"{path}[{index}]"))
        return ignored
    return []


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
