from __future__ import annotations

import asyncio
import argparse
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.agents.director import DirectorAgent
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.core.config import settings
from app.schemas.chat import NarratorSceneContext
from app.schemas.director import DirectorInput
from evals.graders import grade_scenario
from evals.report import render_report, summarize_results
from evals.scenarios import filter_scenarios, load_scenarios
from evals.schemas import GraderResult, Scenario, ScenarioResult


class EvalRunner:
    """Simple deterministic runner for evaluation scenarios."""

    def __init__(self, *, evaluator: Callable[[Scenario], list[GraderResult]] | None = None) -> None:
        self.evaluator = evaluator or grade_scenario

    def run(self, scenario: Scenario, *, actual_output: Any | None = None) -> ScenarioResult:
        scenario_copy = scenario.model_copy(deep=True)
        if actual_output is not None:
            scenario_copy.actual_output = actual_output
        elif scenario_copy.actual_output is None:
            scenario_copy.actual_output = scenario_copy.fixture_output
        results = self.evaluator(scenario_copy)
        scenario_copy.grader_results = results
        if scenario_copy.deterministic_expectations.get("require_failure"):
            scenario_copy.passed = bool(results) and any(
                not result.passed for result in results
            )
            scenario_copy.score = 1.0 if scenario_copy.passed else 0.0
            scenario_copy.max_score = 1.0
        else:
            scenario_copy.score = _aggregate_score(results)
            scenario_copy.max_score = _aggregate_max_score(results)
            scenario_copy.passed = bool(results) and all(result.passed for result in results)
        return ScenarioResult.model_validate(scenario_copy.model_dump())

    async def run_async(
        self,
        scenario: Scenario,
        *,
        actual_output: Any | None = None,
    ) -> ScenarioResult:
        return self.run(scenario, actual_output=actual_output)


def run_scenario(
    scenario: Scenario,
    *,
    invoke: Callable[[], Any] | None = None,
    actual_output: Any | None = None,
    evaluator: Callable[[Scenario], list[GraderResult]] | None = None,
) -> ScenarioResult:
    if actual_output is None and invoke is not None:
        result = invoke()
        if asyncio.iscoroutine(result):
            raise TypeError("Use run_scenario_async when the scenario callable is async.")
        actual_output = result
    return EvalRunner(evaluator=evaluator).run(scenario, actual_output=actual_output)


async def run_scenario_async(
    scenario: Scenario,
    *,
    invoke: Callable[[], Awaitable[Any]] | None = None,
    actual_output: Any | None = None,
    evaluator: Callable[[Scenario], list[GraderResult]] | None = None,
) -> ScenarioResult:
    if actual_output is None and invoke is not None:
        actual_output = await invoke()
    return EvalRunner(evaluator=evaluator).run(scenario, actual_output=actual_output)


def run_scenarios(
    scenarios: Sequence[Scenario],
    *,
    evaluator: Callable[[Scenario], list[GraderResult]] | None = None,
) -> list[ScenarioResult]:
    runner = EvalRunner(evaluator=evaluator)
    return [runner.run(scenario) for scenario in scenarios]


def _aggregate_score(results: Sequence[GraderResult]) -> float:
    return sum(result.score for result in results)


def _aggregate_max_score(results: Sequence[GraderResult]) -> float:
    return sum(result.max_score for result in results)


async def _run_live_scenario(scenario: Scenario) -> Any:
    if not settings.AI_ENABLED or not settings.OPENAI_API_KEY:
        raise RuntimeError(
            "Live evals require AI_ENABLED=true and OPENAI_API_KEY; "
            "offline mode is the default."
        )
    if scenario.target.value == "director":
        return (
            await DirectorAgent().propose(
                director_input=DirectorInput.model_validate(scenario.authoritative_input)
            )
        ).proposal.model_dump()
    if scenario.target.value == "narrator":
        payload = scenario.authoritative_input
        return (
            await NarratorAgent().generate(
                payload=NarratorAgentInput(
                    player_message=str(payload.get("player_message", "")),
                    scene_context=NarratorSceneContext.model_validate(
                        payload.get("scene_context", {})
                    ),
                )
            )
        ).model_dump()
    raise ValueError(f"Unsupported live scenario target: {scenario.target}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Haunted Halls agent evaluations.")
    parser.add_argument("--live", action="store_true", help="Explicitly call configured agents.")
    parser.add_argument("--scenario-id")
    parser.add_argument("--tag")
    parser.add_argument("--agent", choices=("director", "narrator"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    scenarios = filter_scenarios(
        load_scenarios(),
        scenario_id=args.scenario_id,
        tag=args.tag,
        target=args.agent,
    )
    if not scenarios:
        raise SystemExit("No scenarios matched the requested filters.")

    if args.live:
        try:
            results = [
                EvalRunner().run(
                    scenario,
                    actual_output=asyncio.run(_run_live_scenario(scenario)),
                )
                for scenario in scenarios
            ]
        except RuntimeError as exc:
            print(f"live eval unavailable: {exc}", file=sys.stderr)
            return 2
    else:
        results = run_scenarios(scenarios)

    if args.json_output:
        print(json.dumps(summarize_results(results), sort_keys=True, separators=(",", ":")))
    else:
        print(render_report(results))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
