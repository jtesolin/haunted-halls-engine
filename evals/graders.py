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
                    details={"error": "No director output was produced."},
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
                    details={"error": str(exc)},
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

            if action.action == "spawn_npc":
                results.append(
                    GraderResult(
                        name="reject_spawn_npc",
                        passed=False,
                        score=0.0,
                        max_score=1.0,
                        details={"action": action.action},
                    )
                )
                return results

            if action.action == "move_npc":
                legal_destinations = _get_legal_destinations(scenario.authoritative_input)
                destination = getattr(action, "destination_room_id", None)
                destination_ok = bool(destination) and destination in legal_destinations
                results.append(
                    GraderResult(
                        name="move_npc_destination_valid",
                        passed=destination_ok,
                        score=1.0 if destination_ok else 0.0,
                        max_score=1.0,
                        details={
                            "destination": destination,
                            "legal_destinations": legal_destinations,
                        },
                    )
                )

                npc_id = getattr(action, "npc_id", None)
                npc_ok = isinstance(npc_id, str) and _entity_exists(
                    scenario.authoritative_input,
                    npc_id,
                    field_name="npcs",
                )
                results.append(
                    GraderResult(
                        name="referenced_npc_exists",
                        passed=npc_ok,
                        score=1.0 if npc_ok else 0.0,
                        max_score=1.0,
                        details={"npc_id": npc_id},
                    )
                )

            if getattr(action, "npc_id", None) is not None:
                npc_ok = _entity_exists(scenario.authoritative_input, getattr(action, "npc_id"), "npcs")
                if not npc_ok:
                    results.append(
                        GraderResult(
                            name="entity_in_bounded_context",
                            passed=False,
                            score=0.0,
                            max_score=1.0,
                            details={"npc_id": getattr(action, "npc_id")},
                        )
                    )
            if getattr(action, "destination_room_id", None) is not None:
                legal_destinations = _get_legal_destinations(scenario.authoritative_input)
                if legal_destinations and getattr(action, "destination_room_id") not in legal_destinations:
                    results.append(
                        GraderResult(
                            name="destination_in_bounded_context",
                            passed=False,
                            score=0.0,
                            max_score=1.0,
                            details={
                                "destination_room_id": getattr(action, "destination_room_id"),
                                "legal_destinations": legal_destinations,
                            },
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
                    details={"error": "No narrator output was produced."},
                )
            ]

        reply_text = _extract_reply_text(actual_output)
        if reply_text is None:
            return [
                GraderResult(
                    name="narrator_output_present",
                    passed=False,
                    score=0.0,
                    max_score=1.0,
                    details={"actual_output": actual_output},
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

        expected_output = scenario.fixture_output if scenario.fixture_output is not None else scenario.deterministic_expectations
        if isinstance(expected_output, dict):
            expected_contains = expected_output.get("contains")
            if expected_contains is not None:
                contains = [str(item) for item in expected_contains] if isinstance(expected_contains, list) else [str(expected_contains)]
                passed = all(item.lower() in reply_text.lower() for item in contains)
                results.append(
                    GraderResult(
                        name="expected_content",
                        passed=passed,
                        score=1.0 if passed else 0.0,
                        max_score=1.0,
                        details={"expected": contains, "actual": reply_text},
                    )
                )

            forbidden = expected_output.get("must_not_contain")
            if forbidden is not None:
                forbidden_values = [str(item) for item in forbidden] if isinstance(forbidden, list) else [str(forbidden)]
                text_lower = reply_text.lower()
                passed = not any(item.lower() in text_lower for item in forbidden_values)
                results.append(
                    GraderResult(
                        name="forbidden_content",
                        passed=passed,
                        score=1.0 if passed else 0.0,
                        max_score=1.0,
                        details={"forbidden": forbidden_values, "actual": reply_text},
                    )
                )

        elif expected_output is not None:
            expected_value = str(expected_output)
            passed = expected_value.lower() in reply_text.lower()
            results.append(
                GraderResult(
                    name="expected_content",
                    passed=passed,
                    score=1.0 if passed else 0.0,
                    max_score=1.0,
                    details={"expected": expected_value, "actual": reply_text},
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


def _get_legal_destinations(input_payload: dict[str, Any]) -> list[str]:
    if not isinstance(input_payload, dict):
        return []
    room_ids: list[str] = []
    for key in ("legal_destinations", "allowed_destinations", "available_destinations"):
        value = input_payload.get(key)
        if isinstance(value, list):
            room_ids = [str(item) for item in value if item is not None]
            if room_ids:
                return room_ids
    current_room = input_payload.get("current_player_room_id")
    if isinstance(current_room, str):
        room_ids = [current_room]
    npc_list = input_payload.get("npcs")
    if isinstance(npc_list, list):
        for npc in npc_list:
            if isinstance(npc, dict):
                loc = npc.get("location_id") or npc.get("location")
                if isinstance(loc, str):
                    room_ids.append(loc)
    return list(dict.fromkeys(room_ids))


def _entity_exists(input_payload: dict[str, Any], entity_id: Any, field_name: str) -> bool:
    if not isinstance(entity_id, str):
        return False
    entities = input_payload.get(field_name)
    if isinstance(entities, list):
        for item in entities:
            if isinstance(item, dict):
                item_id = item.get("npc_id") or item.get("id")
                if item_id == entity_id:
                    return True
        return False
    if isinstance(entities, dict):
        return entity_id in entities
    return False
