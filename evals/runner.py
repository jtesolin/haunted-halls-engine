from __future__ import annotations

import asyncio
import argparse
import inspect
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
from evals.scenarios import filter_scenarios, load_scenarios, validate_scenario_contract
from evals.schemas import GraderResult, Scenario, ScenarioResult, ScenarioTarget

# Sentinel distinguishing "caller omitted actual_output" (offline fallback to
# scenario.actual_output/fixture_output is allowed) from "caller explicitly
# passed actual_output=None" (a genuinely missing provider result, which must
# be graded as missing rather than silently backfilled from the fixture).
_OUTPUT_NOT_SUPPLIED = object()


class LiveEvalError(RuntimeError):
    """Sanitized failure raised at the eval-harness boundary when a live
    agent/provider call fails.

    Never embeds raw provider response, prompt content, or API response
    bodies in this exception object (its message, __cause__, and
    __context__ are all limited to a safe failure-type category). This
    guarantee is scoped to the eval harness's own exception/report
    boundary; it does not alter or suppress any logging performed inside
    `DirectorAgent`/`NarratorAgent` or the model client themselves, which
    is existing, out-of-scope production behavior (see issue #55).
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
        # `Scenario` is also the public PROGRAMMATIC contract: callers can
        # construct one directly (bypassing load_scenario()/JSON fixtures
        # entirely) and hand it straight to EvalRunner.run(). The SAME
        # contract rules the loader enforces must be enforced here too, or
        # a malformed/ignored-field scenario could be graded as a false
        # green. This must run before grading.
        validate_scenario_contract(scenario_copy)
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
        # Reject an async callable BEFORE ever invoking it: calling an
        # `async def` function does not run its body, it only constructs a
        # coroutine object. If that coroutine were then discarded without
        # being awaited or closed (as a bare `raise` after the call would
        # do), Python emits a "coroutine was never awaited" RuntimeWarning,
        # which can fail callers/tests that treat warnings as errors. No
        # event loop is created in this synchronous helper; async callables
        # must go through `run_scenario_async` instead.
        if inspect.iscoroutinefunction(invoke):
            raise TypeError("Use run_scenario_async when the scenario callable is async.")
        result = invoke()
        # A nominally synchronous callable can still itself return an
        # awaitable/coroutine (e.g. `lambda: some_async_fn()`). Detect that
        # case too, and close any coroutine object that was actually
        # created before raising, so nothing is left dangling unawaited.
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
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
    # `Scenario` is also the public PROGRAMMATIC contract, and
    # `_run_live_scenario()` can be invoked directly (not only via
    # `load_scenarios()`/`EvalRunner.run()`). Validate the same shared
    # contract -- target-specific authoritative-input validation,
    # deterministic-expectation validation, and Narrator ignored-field
    # detection -- before constructing DirectorInput/NarratorAgentInput or
    # invoking an agent, so a malformed programmatic live scenario is
    # rejected before ever reaching a provider/agent call.
    validate_scenario_contract(scenario)

    if scenario.target == ScenarioTarget.DIRECTOR:
        director_input = DirectorInput.model_validate(scenario.authoritative_input)
        selected_model = ModelPolicy.director_model()
        # `raise ... from None` only sets __cause__ = None and suppresses
        # traceback *display* of the original exception; Python still
        # implicitly sets __context__ to the exception being handled while
        # inside an `except` block. To truly sever the raw provider/agent
        # exception from the sanitized LiveEvalError (so neither __cause__
        # nor __context__ retains it), only a safe category string is
        # captured inside the except block, and the LiveEvalError is raised
        # after the block has been left (no exception is being handled at
        # that point). This only sanitizes the LiveEvalError object the eval
        # harness raises; it does not change or suppress whatever
        # DirectorAgent.propose() itself already logs internally before the
        # exception reaches this boundary (existing, out-of-scope production
        # behavior per issue #55).
        failure_type: str | None = None
        try:
            result = await DirectorAgent().propose(director_input=director_input, model=selected_model)
        except Exception as exc:
            failure_type = type(exc).__name__
        if failure_type is not None:
            raise LiveEvalError(f"director agent execution failed ({failure_type})")
        metadata: dict[str, Any] = {"target_agent": "director", "model": selected_model}
        usage = _usage_metadata(getattr(result, "usage", None))
        if usage is not None:
            metadata["usage"] = usage
        return result.proposal.model_dump(), metadata

    if scenario.target == ScenarioTarget.NARRATOR:
        narrator_input = NarratorAgentInput.model_validate(scenario.authoritative_input)
        selected_model = ModelPolicy.narrator_model()
        # See the Director branch above: capture only the safe category
        # string inside the except block and raise after leaving it, so
        # neither __cause__ nor __context__ retains the raw provider/agent
        # exception. As above, this sanitizes only the LiveEvalError this
        # harness raises, not any internal logging NarratorAgent.generate()
        # itself performs (existing, out-of-scope production behavior).
        failure_type = None
        try:
            output = await NarratorAgent().generate(payload=narrator_input, model=selected_model)
        except Exception as exc:
            failure_type = type(exc).__name__
        if failure_type is not None:
            raise LiveEvalError(f"narrator agent execution failed ({failure_type})")
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


async def _run_live_scenarios(scenarios: Sequence[Scenario]) -> list[ScenarioResult]:
    """Run an entire selected scenario batch under a single event loop.

    The production model client caches an `AsyncOpenAI` client; calling
    `asyncio.run()` once per scenario would create and tear down a new event
    loop per scenario, risking reuse of a cached async client bound to a now
    closed loop. Owning the whole batch inside one `asyncio.run()` call
    avoids that hazard without changing production model-client lifecycle
    behavior.
    """

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        actual_output, model_metadata = await _run_live_scenario(scenario)
        results.append(
            EvalRunner().run(
                scenario,
                actual_output=actual_output,
                model_metadata=model_metadata,
            )
        )
    return results


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
            results = asyncio.run(_run_live_scenarios(scenarios))
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
        print(
            json.dumps(
                summarize_results(results),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    else:
        print(render_report(results))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
