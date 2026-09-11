from __future__ import annotations

import json
from pathlib import Path

from evals.schemas import Scenario

_SCENARIO_DIR = Path(__file__).resolve().parent


def load_scenario(path: str | Path) -> Scenario:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return Scenario.model_validate(payload)


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
