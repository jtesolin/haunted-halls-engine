from __future__ import annotations

import asyncio
import argparse
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.agents.director import DirectorAgent
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.ai.model_client import ModelUsage
from app.core.config import settings
from app.guardrails.model_policy import ModelPolicy
from app.schemas.director import DirectorInput
from evals.graders import grade_scenario
from evals.report import render_report, summarize_results
from evals.scenarios import filter_scenarios, load_scenarios
from evals.schemas import GraderResult, Scenario, ScenarioResult, ScenarioTarget

# Sentinel distinguishing "caller omitted actual_output" (offline fallback to
# scenario.actual_output/fixture_output is allowed) from "caller explicitly
# passed actual_output=None" (a genuinely missing provider result, which must
# be graded as missing rather than silently backfilled from the fixture).
_OUTPUT_NOT_SUPPLIED = object()


class LiveEvalError(RuntimeError):
    """Sanitized failure raised when a live agent/provider call fails.

    Never embeds raw provider response, prompt content, or API response
    bodies; only a safe category/message is carried to callers.
    """


class EvalRunner:
    """Simple deterministic runner for evaluation scenarios."""

    def __init__(self, *, evaluator: Callable[[Scenario], list[GraderResult]] | None = None) -> None:
        self.evaluator = evaluator or grade_scenario

    def run(
        self,
        scenario: Scenario,
        *,
        actual_output: Any = _OUTPUT_NOT_SUPPLIED,
        model_metadata: dict[str, Any] | None = None,
    ) -> ScenarioResult:
        scenario_copy = scenario.model_copy(deep=True)
        if actual_output is not _OUTPUT_NOT_SUPPLIED:
            # Explicitly supplied, including an explicit None: preserve it
            # as-is so a genuinely missing provider result is graded as
            # missing rather than silently replaced by fixture_output.
            scenario_copy.actual_output = actual_output
        elif scenario_copy.actual_output is None:
            scenario_copy.actual_output = scenario_copy.fixture_output
        if model_metadata is not None:
            scenario_copy.model_metadata = model_metadata
        results = self.evaluator(scenario_copy)
        scenario_copy.grader_results = results
        # A scenario passes when all applicable deterministic graders pass.
        # There is no inversion for "expected failure" semantics: negative
        # scenarios must supply a safe fixture_output and are proven by
        # focused unit tests that inject the invalid output directly.
        scenario_copy.score = _aggregate_score(results)
        scenario_copy.max_score = _aggregate_max_score(results)
        scenario_copy.passed = bool(results) and all(result.passed for result in results)
        return ScenarioResult.model_validate(scenario_copy.model_dump())

    async def run_async(
        self,
        scenario: Scenario,
        *,
        actual_output: Any = _OUTPUT_NOT_SUPPLIED,
        model_metadata: dict[str, Any] | None = None,
    ) -> ScenarioResult:
        return self.run(scenario, actual_output=actual_output, model_metadata=model_metadata)


def run_scenario(
    scenario: Scenario,
    *,
    invoke: Callable[[], Any] | None = None,
    actual_output: Any = _OUTPUT_NOT_SUPPLIED,
    evaluator: Callable[[Scenario], list[GraderResult]] | None = None,
) -> ScenarioResult:
    if actual_output is _OUTPUT_NOT_SUPPLIED and invoke is not None:
        result = invoke()
        if asyncio.iscoroutine(result):
            raise TypeError("Use run_scenario_async when the scenario callable is async.")
        actual_output = result
    return EvalRunner(evaluator=evaluator).run(scenario, actual_output=actual_output)


async def run_scenario_async(
    scenario: Scenario,
    *,
    invoke: Callable[[], Awaitable[Any]] | None = None,
    actual_output: Any = _OUTPUT_NOT_SUPPLIED,
    evaluator: Callable[[Scenario], list[GraderResult]] | None = None,
) -> ScenarioResult:
    if actual_output is _OUTPUT_NOT_SUPPLIED and invoke is not None:
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


def _usage_metadata(usage: ModelUsage | None) -> dict[str, Any] | None:
    """Reduce provider usage metadata to token counters only (no raw content)."""

    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _require_live_credentials() -> None:
    # A missing, empty, OR whitespace-only key must fail closed; credentials
    # alone (even if present) never activate live execution without --live,
    # which is enforced by callers only invoking this from the --live path.
    api_key = (settings.OPENAI_API_KEY or "").strip()
    if not settings.AI_ENABLED or not api_key:
        raise RuntimeError(
            "Live evals require AI_ENABLED=true and a non-empty, non-whitespace "
            "OPENAI_API_KEY; offline mode is the default."
        )


async def _run_live_scenario(scenario: Scenario) -> tuple[Any, dict[str, Any]]:
    _require_live_credentials()

    if scenario.target == ScenarioTarget.DIRECTOR:
        director_input = DirectorInput.model_validate(scenario.authoritative_input)
        selected_model = ModelPolicy.director_model()
        try:
            result = await DirectorAgent().propose(director_input=director_input, model=selected_model)
        except Exception as exc:
            raise LiveEvalError(
                f"director agent execution failed ({type(exc).__name__})"
            ) from exc
        metadata: dict[str, Any] = {"target_agent": "director", "model": selected_model}
        usage = _usage_metadata(getattr(result, "usage", None))
        if usage is not None:
            metadata["usage"] = usage
        return result.proposal.model_dump(), metadata

    if scenario.target == ScenarioTarget.NARRATOR:
        narrator_input = NarratorAgentInput.model_validate(scenario.authoritative_input)
        selected_model = ModelPolicy.narrator_model()
        try:
            output = await NarratorAgent().generate(payload=narrator_input, model=selected_model)
        except Exception as exc:
            raise LiveEvalError(
                f"narrator agent execution failed ({type(exc).__name__})"
            ) from exc
        metadata = {"target_agent": "narrator", "model": selected_model}
        usage = _usage_metadata(
            ModelUsage(
                input_tokens=output.input_tokens,
                cached_input_tokens=output.cached_input_tokens,
                cache_write_input_tokens=output.cache_write_input_tokens,
                output_tokens=output.output_tokens,
                reasoning_output_tokens=output.reasoning_output_tokens,
                total_tokens=output.total_tokens,
            )
        )
        if usage is not None:
            metadata["usage"] = usage
        return output.model_dump(), metadata

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
            results = []
            for scenario in scenarios:
                actual_output, model_metadata = asyncio.run(_run_live_scenario(scenario))
                results.append(
                    EvalRunner().run(
                        scenario,
                        actual_output=actual_output,
                        model_metadata=model_metadata,
                    )
                )
        except RuntimeError as exc:
            # Both the credential guard and LiveEvalError are RuntimeError
            # subclasses that already carry only a safe, sanitized message;
            # neither embeds raw provider response, prompt content, or
            # secrets.
            print(f"live eval failed: {exc}", file=sys.stderr)
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
