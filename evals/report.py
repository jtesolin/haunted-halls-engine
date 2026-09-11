from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evals.schemas import ScenarioResult

# Report serialization uses an explicit SAFE allowlist for GraderResult.details
# rather than a blacklist. `GraderResult.details` is an arbitrary
# `dict[str, Any]`; a future/custom grader could otherwise attach raw
# provider/model response content under any unrecognized key (for example
# `provider_response`, `response_body`, `error`, or `payload`) and have it
# flow straight into a stable JSON report. Only bounded, known-safe
# diagnostic fields are ever copied into the serialized report; every other
# key/value pair in `details` (known or unknown) is dropped.
_SAFE_DETAIL_KEYS = frozenset(
    {
        "length",
        "actual_length",
        "expected",
        "forbidden",
        "decision",
        "actual_decision",
        "expected_decision",
        "action",
        "allowed",
        "npc_id",
        "destination",
        "legal_destinations",
        "reason",
        "error_type",
        "error_count",
        "error_locations",
    }
)


def _safe_grader_details(details: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in details.items() if key in _SAFE_DETAIL_KEYS}


def _safe_grader_result(grader_result: dict[str, Any]) -> dict[str, Any]:
    safe = dict(grader_result)
    if "details" in safe and isinstance(safe["details"], dict):
        safe["details"] = _safe_grader_details(safe["details"])
    return safe


def summarize_results(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed is True)
    failed = total - passed
    score_total = sum(result.score or 0.0 for result in results)
    max_total = sum(result.max_score or 0.0 for result in results)

    serialized_results = []
    for result in results:
        dumped = result.model_dump(mode="json", exclude={"actual_output", "fixture_output"})
        dumped["grader_results"] = [
            _safe_grader_result(grader_result) for grader_result in dumped.get("grader_results", [])
        ]
        serialized_results.append(dumped)

    return {
        "total_scenarios": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": 0.0 if total == 0 else passed / total,
        "score": 0.0 if max_total == 0 else score_total / max_total,
        "results": serialized_results,
    }


def render_report(results: Sequence[ScenarioResult]) -> str:
    summary = summarize_results(results)
    lines = [
        "Haunted Halls Eval Report",
        "========================",
        f"scenarios: {summary['total_scenarios']}",
        f"passed: {summary['passed']}",
        f"failed: {summary['failed']}",
        f"pass_rate: {summary['pass_rate']:.2%}",
        f"score: {summary['score']:.2%}",
        "",
    ]
    for result in results:
        status = "PASS" if result.passed is True else "FAIL"
        score = 0.0 if result.max_score in (None, 0) else (result.score or 0.0) / result.max_score
        model_note = ""
        target_agent = result.model_metadata.get("target_agent")
        model_id = result.model_metadata.get("model")
        if target_agent and model_id:
            model_note = f" [{target_agent}:{model_id}]"
        lines.append(f"- {result.scenario_id}: {status} ({score:.2%}){model_note}")
    return "\n".join(lines)
