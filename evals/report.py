from __future__ import annotations

import json
import math
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
        "allowed",
        "legal_destinations",
        "error_type",
        "error_count",
    }
)
# NOTE: `action`, `npc_id`, and `destination` are intentionally NOT on this
# allowlist. Those keys, when populated by a grader, are typically derived
# directly from provider-controlled Director output (the proposed action
# type, NPC identifier, and destination room ID) BEFORE full schema
# validation has necessarily succeeded. A grader's internal `GraderResult`
# may still carry them for in-process diagnostics, but the stable report
# boundary must never persist arbitrary model-controlled identifiers/action
# strings, even when they are short enough to pass the scalar-length bound
# below.
#
# NOTE: `reason` is also intentionally NOT on this allowlist. `details` is an
# arbitrary `dict[str, Any]`, and a future/custom grader could attach raw
# provider/model content under `reason` and have it serialized simply
# because it happens to be a short string. A grader may still set `reason`
# on its in-process `GraderResult` for diagnostics; it just never crosses
# the stable JSON/human report boundary.
#
# NOTE: `error_locations` is also intentionally NOT on this allowlist.
# `_sanitize_error()` derives it from `ValidationError.errors()[].loc`, and
# for malformed provider/model output an extra/unexpected field NAME is
# itself provider-controlled and can appear in that location path. Keeping
# only `error_type`/`error_count` provides sufficient bounded diagnostics for
# 8E1 while guaranteeing no provider-controlled field name can escape into a
# stable report. `_sanitize_error()` may still compute `error_locations`
# internally if useful; the report projection below simply never emits it.

# A key on the allowlist above only bounds the KEY NAME; nothing prevents a
# grader from attaching an arbitrary nested mapping (or other unbounded
# shape) as that key's VALUE, e.g. details={"expected": {"reply_text": ...}}.
# Safe report values are therefore further restricted to bounded diagnostic
# shapes: None, bool, int, float, a length-bounded string, or a list/tuple of
# such safe scalars. Anything else (arbitrary mappings, unbounded strings,
# nested containers of containers, ...) is dropped rather than serialized.
_MAX_SAFE_STRING_LENGTH = 200
# A grader could otherwise attach an arbitrarily large list (for example, an
# unbounded `legal_destinations` collection copied from a large or malicious
# world) even though each individual scalar item is short/safe. Bound the
# ITEM COUNT of any list/tuple diagnostic as well, so no huge payload can be
# copied into a stable report just because its elements are individually
# safe.
MAX_SAFE_DETAIL_LIST_ITEMS = 20


def _is_safe_scalar(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        # NaN/Infinity are not standard JSON; Python's json module will
        # nonetheless silently emit them as bare NaN/Infinity/-Infinity
        # tokens unless explicitly rejected. A grader-supplied non-finite
        # diagnostic value must never reach a stable report.
        return math.isfinite(value)
    if isinstance(value, str):
        return "\n" not in value and len(value) <= _MAX_SAFE_STRING_LENGTH
    return False


_DROP = object()


def _safe_detail_value(value: Any) -> Any:
    if _is_safe_scalar(value):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_SAFE_DETAIL_LIST_ITEMS:
            # Prefer dropping an oversized collection outright over silently
            # truncating it: a truncated `legal_destinations` (for example)
            # could otherwise be misread as a complete/authoritative list.
            return _DROP
        if all(_is_safe_scalar(item) for item in value):
            return list(value)
        return _DROP
    # Arbitrary mappings (and any other unbounded/nested shape) are never
    # serialized wholesale, even under an allowed key name.
    return _DROP


def _safe_grader_details(details: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        safe_value = _safe_detail_value(value)
        if safe_value is _DROP:
            continue
        safe[key] = safe_value
    return safe


def _safe_grader_result(grader_result: dict[str, Any]) -> dict[str, Any]:
    safe = dict(grader_result)
    if "details" in safe and isinstance(safe["details"], dict):
        safe["details"] = _safe_grader_details(safe["details"])
    return safe


# model_metadata is also an arbitrary caller-supplied dict[str, Any]
# (EvalRunner.run() accepts any model_metadata). A small explicit projection
# keeps only the fields the report is meant to expose, ignoring anything
# else (including unknown usage sub-keys), so caller-supplied metadata can
# never smuggle raw provider content into either the JSON or human report.
_SAFE_TARGET_AGENTS = frozenset({"director", "narrator"})
_SAFE_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }
)


def _is_safe_usage_value(value: Any) -> bool:
    # bool is a subclass of int; a token count must be a genuine numeric
    # value (or None), never a bool.
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _safe_model_metadata(model_metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}

    target_agent = model_metadata.get("target_agent")
    # Guard the type before membership testing: an unhashable value (e.g. a
    # list or dict) would otherwise raise TypeError from `in a frozenset`
    # and crash report generation instead of failing closed by dropping it.
    if isinstance(target_agent, str) and target_agent in _SAFE_TARGET_AGENTS:
        safe["target_agent"] = target_agent

    model = model_metadata.get("model")
    if isinstance(model, str) and "\n" not in model and 0 < len(model) <= _MAX_SAFE_STRING_LENGTH:
        safe["model"] = model

    usage = model_metadata.get("usage")
    if isinstance(usage, dict):
        safe_usage = {
            key: value
            for key, value in usage.items()
            if key in _SAFE_USAGE_KEYS and _is_safe_usage_value(value)
        }
        if safe_usage:
            safe["usage"] = safe_usage

    return safe


def summarize_results(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed is True)
    failed = total - passed
    score_total = sum(result.score or 0.0 for result in results)
    max_total = sum(result.max_score or 0.0 for result in results)

    serialized_results = []
    for result in results:
        # Build the stable report record from an explicit safe-field
        # ALLOWLIST rather than a broad model dump. `Scenario`/
        # `ScenarioResult` are the harness-wide contract and also carry
        # `authoritative_input` and `deterministic_expectations` -- arbitrary
        # caller/fixture payload fields that issue #55 does not require in
        # every report and that could otherwise smuggle a
        # non-JSON-serializable object or raw provider-shaped content into a
        # stable report merely because they happen to round-trip through
        # `model_dump()`. `actual_output`/`fixture_output` are excluded for
        # the same reason (see prior rounds). `grader_results` and
        # `model_metadata` are still routed through their existing safe
        # projections below.
        dumped = {
            "scenario_id": result.scenario_id,
            "description": result.description,
            "target": result.target,
            "tags": list(result.tags),
            "grader_results": [
                _safe_grader_result(grader_result.model_dump(mode="python"))
                for grader_result in result.grader_results
            ],
            "passed": result.passed,
            "score": result.score,
            "max_score": result.max_score,
            "model_metadata": _safe_model_metadata(result.model_metadata),
        }
        serialized_results.append(dumped)

    summary = {
        "total_scenarios": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": 0.0 if total == 0 else passed / total,
        "score": 0.0 if max_total == 0 else score_total / max_total,
        "results": serialized_results,
    }
    # Belt-and-suspenders: prove the sanitized summary is actually encodable
    # as STRICT JSON (no bare NaN/Infinity/-Infinity tokens, which Python's
    # json module would otherwise silently emit since they are not valid
    # JSON) before returning it, rather than trusting the projections above
    # to have covered every path. If this ever raises, that is a bug in the
    # sanitizer (an unbounded/unsafe/non-finite value escaped it), not
    # something to paper over by stringifying unknown content.
    json.dumps(summary, allow_nan=False)
    return summary


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
    for result, serialized in zip(results, summary["results"]):
        status = "PASS" if result.passed is True else "FAIL"
        score = 0.0 if result.max_score in (None, 0) else (result.score or 0.0) / result.max_score
        model_note = ""
        # Route through the same safe projection used for the JSON report so
        # caller-supplied model_metadata cannot bypass sanitization through
        # the human-readable render path.
        safe_metadata = serialized["model_metadata"]
        target_agent = safe_metadata.get("target_agent")
        model_id = safe_metadata.get("model")
        if target_agent and model_id:
            model_note = f" [{target_agent}:{model_id}]"
        lines.append(f"- {result.scenario_id}: {status} ({score:.2%}){model_note}")
        if status == "FAIL":
            # Identify which grader(s) failed (issue #55 requires the report
            # to name the failing check), using only the grader `name`
            # values already present in the sanitized JSON representation.
            # Never print raw grader `details` here.
            failed_grader_names = [
                grader_result["name"]
                for grader_result in serialized["grader_results"]
                if grader_result.get("passed") is not True
            ]
            if failed_grader_names:
                lines.append(f"  failed: {', '.join(failed_grader_names)}")
    return "\n".join(lines)
