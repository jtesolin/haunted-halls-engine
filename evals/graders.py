from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.schemas.director import DirectorProposal, NoActionProposal, WorldActionProposal
from app.schemas.world import WorldActionType

from evals.schemas import GraderResult, Scenario, ScenarioTarget

_DIRECTOR_PROPOSAL_ADAPTER = TypeAdapter(DirectorProposal)


class DeterministicGrader:
    """Shared deterministic grading contract for eval scenarios."""

    name = "deterministic"

    def grade(self, scenario: Scenario) -> list[GraderResult]:
        raise NotImplementedError


class DirectorDeterministicGrader(DeterministicGrader):
    name = "director-deterministic"

    def grade(self, scenario: Scenario) -> list[GraderResult]:
        actual_output = scenario.actual_output
        if actual_output is None:
            return [
                GraderResult(
                    name="proposal_contract",
                    passed=False,
                    score=0.0,
                    max_score=1.0,
                    details={"reason": "no_director_output_produced"},
                )
            ]

        results: list[GraderResult] = []
        proposal: DirectorProposal | None = None
        try:
            proposal = _DIRECTOR_PROPOSAL_ADAPTER.validate_python(actual_output)
        except (TypeError, ValidationError, ValueError) as exc:
            raw_world_action = _raw_world_action(actual_output)
            if raw_world_action is not None:
                allowed_actions = {member.value for member in WorldActionType}
                results.append(
                    GraderResult(
                        name="allowed_world_action_vocab",
                        passed=raw_world_action in allowed_actions,
                        score=1.0 if raw_world_action in allowed_actions else 0.0,
                        max_score=1.0,
                        details={
                            "action": raw_world_action,
                            "allowed": sorted(allowed_actions),
                        },
                    )
                )
            results.append(
                GraderResult(
                    name="proposal_contract",
                    passed=False,
                    score=0.0,
                    max_score=1.0,
                    details=_sanitize_error(exc),
                )
            )
            return results

        results.append(
            GraderResult(
                name="proposal_contract",
                passed=True,
                score=1.0,
                max_score=1.0,
                details={"decision": proposal.decision},
            )
        )

        expected_none = bool(scenario.deterministic_expectations.get("require_none"))
        if expected_none:
            results.append(
                GraderResult(
                    name="require_none",
                    passed=isinstance(proposal, NoActionProposal),
                    score=1.0 if isinstance(proposal, NoActionProposal) else 0.0,
                    max_score=1.0,
                    details={"expected_decision": "none", "actual_decision": proposal.decision},
                )
            )

        if isinstance(proposal, WorldActionProposal):
            action = proposal.world_action
            allowed_actions = {member.value for member in WorldActionType}
            is_allowed = action.action in allowed_actions
            results.append(
                GraderResult(
                    name="allowed_world_action_vocab",
                    passed=is_allowed,
                    score=1.0 if is_allowed else 0.0,
                    max_score=1.0,
                    details={"action": action.action, "allowed": sorted(allowed_actions)},
                )
            )
            if not is_allowed:
                return results

            if action.action == "move_npc":
                npc_id = getattr(action, "npc_id", None)
                npc_context = _find_npc_context(scenario.authoritative_input, npc_id)
                npc_ok = npc_context is not None
                results.append(
                    GraderResult(
                        name="referenced_npc_exists",
                        passed=npc_ok,
                        score=1.0 if npc_ok else 0.0,
                        max_score=1.0,
                        details={"npc_id": npc_id},
                    )
                )

                # Legality is scoped strictly to the proposed NPC's own bounded
                # one-hop destinations. A destination that is legal for a
                # different NPC (or the player's current room) must never be
                # accepted here.
                legal_destinations = (
                    _npc_one_hop_destinations(npc_context) if npc_context is not None else []
                )
                destination = getattr(action, "destination_room_id", None)
                destination_ok = (
                    npc_ok and isinstance(destination, str) and destination in legal_destinations
                )
                results.append(
                    GraderResult(
                        name="move_npc_destination_valid",
                        passed=destination_ok,
                        score=1.0 if destination_ok else 0.0,
                        max_score=1.0,
                        details={
                            "npc_id": npc_id,
                            "destination": destination,
                            "legal_destinations": legal_destinations,
                        },
                    )
                )
            elif getattr(action, "npc_id", None) is not None:
                npc_id = getattr(action, "npc_id")
                npc_ok = _find_npc_context(scenario.authoritative_input, npc_id) is not None
                results.append(
                    GraderResult(
                        name="entity_in_bounded_context",
                        passed=npc_ok,
                        score=1.0 if npc_ok else 0.0,
                        max_score=1.0,
                        details={"npc_id": npc_id},
                    )
                )

        return results


class NarratorDeterministicGrader(DeterministicGrader):
    name = "narrator-deterministic"

    def grade(self, scenario: Scenario) -> list[GraderResult]:
        actual_output = scenario.actual_output
        if actual_output is None:
            return [
                GraderResult(
                    name="narrator_output_present",
                    passed=False,
                    score=0.0,
                    max_score=1.0,
                    details={"reason": "no_output_produced"},
                )
            ]

        reply_text = _extract_reply_text(actual_output)
        if reply_text is None or not reply_text.strip():
            return [
                GraderResult(
                    name="narrator_output_present",
                    passed=False,
                    score=0.0,
                    max_score=1.0,
                    details={
                        "reason": "missing_or_blank_reply_text",
                        "length": 0 if reply_text is None else len(reply_text),
                    },
                )
            ]

        results: list[GraderResult] = [
            GraderResult(
                name="narrator_output_present",
                passed=True,
                score=1.0,
                max_score=1.0,
                details={"length": len(reply_text)},
            )
        ]

        # Grading expectations always come from the scenario's declared
        # deterministic_expectations. fixture_output is the offline actual
        # output (what the grader is evaluating), never the source of truth
        # for what "should" be true.
        expectations = scenario.deterministic_expectations
        text_lower = reply_text.lower()

        expected_contains = expectations.get("contains")
        if expected_contains is not None:
            contains = (
                [str(item) for item in expected_contains]
                if isinstance(expected_contains, list)
                else [str(expected_contains)]
            )
            passed = all(item.lower() in text_lower for item in contains)
            results.append(
                GraderResult(
                    name="expected_content",
                    passed=passed,
                    score=1.0 if passed else 0.0,
                    max_score=1.0,
                    details={"expected": contains, "actual_length": len(reply_text)},
                )
            )

        forbidden = expectations.get("must_not_contain")
        if forbidden is not None:
            forbidden_values = (
                [str(item) for item in forbidden] if isinstance(forbidden, list) else [str(forbidden)]
            )
            passed = not any(item.lower() in text_lower for item in forbidden_values)
            results.append(
                GraderResult(
                    name="forbidden_content",
                    passed=passed,
                    score=1.0 if passed else 0.0,
                    max_score=1.0,
                    details={"forbidden": forbidden_values, "actual_length": len(reply_text)},
                )
            )

        return results


def grade_director_output(scenario: Scenario) -> list[GraderResult]:
    return DirectorDeterministicGrader().grade(scenario)


def grade_narrator_output(scenario: Scenario) -> list[GraderResult]:
    return NarratorDeterministicGrader().grade(scenario)


def grade_scenario(scenario: Scenario) -> list[GraderResult]:
    if scenario.target == ScenarioTarget.DIRECTOR:
        return grade_director_output(scenario)
    if scenario.target == ScenarioTarget.NARRATOR:
        return grade_narrator_output(scenario)
    raise ValueError(f"Unsupported scenario target: {scenario.target}")


def _raw_world_action(actual_output: Any) -> str | None:
    if not isinstance(actual_output, dict):
        return None
    world_action = actual_output.get("world_action")
    if isinstance(world_action, dict):
        action = world_action.get("action")
        if isinstance(action, str):
            return action
    return None


def _extract_reply_text(actual_output: Any) -> str | None:
    if isinstance(actual_output, str):
        return actual_output
    if isinstance(actual_output, dict):
        for key in ("reply_text", "text", "output", "reply"):
            value = actual_output.get(key)
            if isinstance(value, str):
                return value
    if hasattr(actual_output, "reply_text") and isinstance(actual_output.reply_text, str):
        return actual_output.reply_text
    return None


def _find_npc_context(input_payload: dict[str, Any], npc_id: Any) -> dict[str, Any] | None:
    """Resolve the authoritative DirectorNPCContext-shaped record for npc_id.

    Only the production `npcs: list[DirectorNPCContext]` shape is honored;
    there is no fallback to eval-only dict-keyed or top-level shapes.
    """

    if not isinstance(input_payload, dict) or not isinstance(npc_id, str):
        return None
    npcs = input_payload.get("npcs")
    if not isinstance(npcs, list):
        return None
    for npc in npcs:
        if isinstance(npc, dict) and npc.get("npc_id") == npc_id:
            return npc
    return None


def _npc_one_hop_destinations(npc_context: dict[str, Any]) -> list[str]:
    destinations = npc_context.get("one_hop_destination_room_ids")
    if not isinstance(destinations, list):
        return []
    return [str(item) for item in destinations if isinstance(item, str)]


def _sanitize_error(exc: Exception) -> dict[str, Any]:
    """Summarize a validation error without embedding raw provider content.

    Pydantic's default ValidationError string representation can include the
    invalid input values verbatim, which for a live agent call could be raw
    provider output. Only structural details (error locations/count) are
    reported.
    """

    if isinstance(exc, ValidationError):
        return {
            "error_type": "validation_error",
            "error_count": exc.error_count(),
            "error_locations": sorted(
                {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
            ),
        }
    return {"error_type": type(exc).__name__}
