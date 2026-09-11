from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evals.schemas import ScenarioResult

# Defensive guard: keys that must never carry raw provider/model response text
# into a serialized report, even if a grader accidentally attaches one.
# `actual_output` is excluded structurally below; these are extra nested-key
# names that historically carried raw reply text in grader details, plus the
# same response-bearing keys `_extract_reply_text` understands
# (`reply_text`, `text`, `output`, `reply`), since `GraderResult.details` is
# intentionally generic and a grader could attach any of them.
_RAW_OUTPUT_KEYS = frozenset(
    {
        "actual_output",
        "actual",
        "raw_response",
        "raw_output",
        "reply_text",
        "text",
        "output",
        "reply",
    }
)


def _strip_raw_output_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_raw_output_fields(inner)
            for key, inner in value.items()
            if key not in _RAW_OUTPUT_KEYS
        }
    if isinstance(value, list):
        return [_strip_raw_output_fields(item) for item in value]
    return value


def summarize_results(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed is True)
    failed = total - passed
    score_total = sum(result.score or 0.0 for result in results)
    max_total = sum(result.max_score or 0.0 for result in results)
    return {
        "total_scenarios": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": 0.0 if total == 0 else passed / total,
        "score": 0.0 if max_total == 0 else score_total / max_total,
        "results": [
            _strip_raw_output_fields(result.model_dump(mode="json", exclude={"actual_output"}))
            for result in results
        ],
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
