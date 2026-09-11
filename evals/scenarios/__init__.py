from __future__ import annotations

import json
from pathlib import Path

from app.agents.narrator import NarratorAgentInput
from app.schemas.director import DirectorInput

from evals.schemas import Scenario, ScenarioTarget

_SCENARIO_DIR = Path(__file__).resolve().parent


def load_scenario(path: str | Path) -> Scenario:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    scenario = Scenario.model_validate(payload)
    _validate_target_specific_input(scenario)
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
