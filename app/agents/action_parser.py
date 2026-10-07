from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.ai.prompts import action_parser_prompt
from app.game.abilities import (
    canonical_ability_invocation_references,
    generated_ability_definitions,
    project_owned_abilities,
)
from app.game.items import (
    ensure_items_state,
    inventory_item_ids,
    item_candidate_identifiers,
    room_item_ids,
)
from app.game.npcs import ensure_npcs_state, nearby_npc_ids_for_room, parser_npc_projection
from app.game.world import DEFAULT_WORLD, normalize_identifier
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens
from app.schemas.chat import ActionParserOutput, ActionType, ParsedAction
from app.schemas.traversal import LocalTraversalRoute


logger = logging.getLogger(__name__)


class ActionParserError(Exception):
    pass


class ActionParseProviderError(ActionParserError):
    pass


ParseStatus = Literal["ok", "ambiguous", "invalid"]


@dataclass(frozen=True)
class AbilityReferenceOccurrence:
    canonical_reference: str
    start: int
    end: int


class ParserContext(BaseModel):
    location: str | None = None
    current_room_id: str | None = None
    current_room_name: str | None = None
    current_room_description: str | None = None
    available_exits: list[dict[str, str]] = Field(default_factory=list)
    nearby_objects: list[str] = Field(default_factory=list)
    inventory: list[str] = Field(default_factory=list)
    accessible_item_references: list[str] = Field(default_factory=list)
    traversal_routes: list[LocalTraversalRoute] = Field(default_factory=list, max_length=6)
    accessible_route_references: list[str] = Field(default_factory=list)
    nearby_npcs: list[dict[str, Any]] = Field(default_factory=list)
    status_flags: dict[str, Any] = Field(default_factory=dict)
    abilities: list[dict[str, str | bool]] = Field(default_factory=list)


class ActionParserAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "ActionParser"

    def build_provider_request(
        self,
        *,
        message: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]] | None = None,
    ) -> list[ChatCompletionMessageParam]:
        parser_context = self._build_parser_context(campaign_state)
        return self._build_messages(
            message=message,
            parser_context=parser_context,
            recent_turns=recent_turns,
            memory_context=memory_context or [],
        )

    def estimate_provider_input_tokens(
        self,
        *,
        message: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]] | None = None,
    ) -> int:
        request = self.build_provider_request(
            message=message,
            campaign_state=campaign_state,
            recent_turns=recent_turns,
            memory_context=memory_context,
        )
        return sum(
            estimate_tokens(str(item.get("content", "")))
            for item in request
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )

    async def parse(
        self,
        *,
        message: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> ParsedAction:
        parser_context = self._build_parser_context(campaign_state)
        messages = self._build_messages(
            message=message,
            parser_context=parser_context,
            recent_turns=recent_turns,
            memory_context=memory_context or [],
        )
        try:
            parsed_result = await model_client.generate_structured(
                messages=messages,
                response_model=ActionParserOutput,
                model=model or ModelPolicy.action_parser_model(),
                max_output_tokens=TokenBudget.action_parser_max_output_tokens(),
                reasoning_effort=ModelPolicy.action_parser_reasoning_effort(),
                timeout=15,
                return_usage=True,
            )
            if isinstance(parsed_result, ModelCallResult):
                parsed_output = parsed_result.output
                usage = parsed_result.usage
            else:
                parsed_output = parsed_result
                usage = None
        except Exception as exc:
            logger.error(
                "action_parser_structured_call_failed model=%s message_length=%s memory_items=%s error_type=%s error_message=%s",
                model or ModelPolicy.action_parser_model(),
                len(message),
                len(memory_context or []),
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            raise ActionParseProviderError("Action parser model call failed.") from exc

        if parsed_output is not None:
            parsed_action = ParsedAction(
                raw_text=message,
                action=parsed_output.action,
                target=parsed_output.target,
                parameters=parsed_output.parameters.model_dump(exclude_none=True),
                stealth=parsed_output.stealth,
                confidence=parsed_output.confidence,
                parse_status=parsed_output.parse_status,
                parser_notes=parsed_output.parser_notes,
                input_tokens=usage.input_tokens if usage is not None else None,
                cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
                cache_write_input_tokens=usage.cache_write_input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                reasoning_output_tokens=usage.reasoning_output_tokens if usage is not None else None,
                total_tokens=usage.total_tokens if usage is not None else None,
            )
            if parsed_action.action == ActionType.ABILITY_CHECK:
                return self._validate_model_ability_request(parsed_action, parser_context)
            return parsed_action

        return ParsedAction(
            raw_text=message,
            action=ActionType.UNKNOWN,
            target=None,
            parameters={},
            stealth=False,
            confidence=0.0,
            parse_status="invalid",
            parser_notes="Action parser did not return valid structured output.",
            input_tokens=usage.input_tokens if usage is not None else None,
            cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
            cache_write_input_tokens=usage.cache_write_input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            reasoning_output_tokens=usage.reasoning_output_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
        )

    def _build_messages(
        self,
        *,
        message: str,
        parser_context: ParserContext,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]],
    ) -> list[ChatCompletionMessageParam]:
        short_history = recent_turns[-4:]
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "developer",
                "content": action_parser_prompt,
            },
            {
                "role": "user",
                "content": (
                    "Parser context:\n"
                    f"{parser_context.model_dump_json(indent=2)}"
                ),
            },
        ]

        if memory_context:
            messages.append(
                {
                    "role": "user",
                    "content": "Relevant memory:\n" + "\n\n".join(
                        entry.get("content", "") for entry in memory_context if entry.get("content")
                    ),
                }
            )

        messages.extend(
            [
                {
                    "role": "user",
                    "content": f"Recent turns:\n{json.dumps(short_history)}",
                },
                {
                    "role": "user",
                    "content": f"Player text:\n{message}",
                },
            ]
        )
        return messages

    def _build_parser_context(self, campaign_state: str) -> ParserContext:
        if not campaign_state or campaign_state == "No campaign state yet.":
            return ParserContext()

        try:
            state = json.loads(campaign_state)
        except json.JSONDecodeError:
            return ParserContext()

        if not isinstance(state, dict):
            return ParserContext()

        player = self._dict_value(state, "player")
        npcs = ensure_npcs_state(state)

        location_raw = player.get("location")
        location = location_raw if isinstance(location_raw, str) else None

        items = ensure_items_state(state)
        inventory = inventory_item_ids(items)

        nearby_npcs: list[dict[str, Any]] = []
        if location is not None:
            for npc_id in nearby_npc_ids_for_room(npcs, location):
                nearby_npcs.append(parser_npc_projection(npc_id, npcs[npc_id]))

        current_room = DEFAULT_WORLD.get_room(location) if location is not None else None
        if current_room is not None:
            available_exits = DEFAULT_WORLD.available_exits(current_room.id)
            current_room_id = current_room.id
            current_room_name = current_room.name
            current_room_description = current_room.description
        else:
            available_exits = []
            current_room_id = None
            current_room_name = None
            current_room_description = None

        nearby_objects = room_item_ids(items, location) if isinstance(location, str) else []
        item_references: set[str] = set()
        for item_id in set(nearby_objects) | set(inventory):
            item_references.update(item_candidate_identifiers(item_id, items[item_id]))

        status_flags = self._dict_value(state, "status")
        generated_by_id = {
            ability.ability_id: ability
            for ability in generated_ability_definitions(state)
        }
        abilities = [
            {
                "ability_id": ability.ability_id,
                "name": ability.display_name,
                "description": ability.description,
                "available": True,
                "requires_target": (
                    ability.ability_id in generated_by_id
                    and generated_by_id[ability.ability_id].kind.value in {"utility", "traversal"}
                ),
                "target_kind": (
                    "route"
                    if ability.ability_id in generated_by_id
                    and generated_by_id[ability.ability_id].kind.value == "traversal"
                    else "object"
                ),
            }
            for ability in project_owned_abilities(state)
            if ability.available
        ]
        return ParserContext(
            location=location,
            current_room_id=current_room_id,
            current_room_name=current_room_name,
            current_room_description=current_room_description,
            available_exits=available_exits,
            nearby_objects=nearby_objects,
            inventory=inventory,
            accessible_item_references=sorted(item_references),
            traversal_routes=DEFAULT_WORLD.local_traversal_routes(current_room_id or ""),
            accessible_route_references=sorted({
                reference
                for route in DEFAULT_WORLD.traversal_routes
                if route.origin == current_room_id
                for reference in DEFAULT_WORLD.route_references(route)
            }),
            nearby_npcs=nearby_npcs,
            status_flags=status_flags,
            abilities=abilities,
        )

    def _validate_model_ability_request(
        self, parsed_action: ParsedAction, parser_context: ParserContext
    ) -> ParsedAction:
        parameters = parsed_action.parameters
        ability_id = parameters.get("ability_id")
        abilities = [
            ability
            for ability in parser_context.abilities
            if ability.get("available") is True
        ]
        selected = [
            ability
            for ability in abilities
            if ability.get("ability_id") == ability_id
        ]
        if parsed_action.parse_status != "ok" or len(selected) != 1:
            return self._reject_ability_request(parsed_action, "ability_not_available")

        selected_id = selected[0].get("ability_id")
        selected_name = selected[0].get("name")
        if not isinstance(selected_id, str) or not isinstance(selected_name, str):
            return self._reject_ability_request(parsed_action, "ability_not_available")

        references_by_canonical: dict[str, set[str]] = {}
        for ability in abilities:
            candidate_id = ability.get("ability_id")
            candidate_name = ability.get("name")
            if not isinstance(candidate_id, str) or not isinstance(candidate_name, str):
                continue
            for reference in canonical_ability_invocation_references(candidate_id, candidate_name):
                references_by_canonical.setdefault(reference, set()).add(candidate_id)

        selected_references = canonical_ability_invocation_references(selected_id, selected_name)
        occurrences = [
            AbilityReferenceOccurrence(reference, start, end)
            for reference in selected_references
            for start, end in self._canonical_phrase_spans(parsed_action.raw_text, reference)
        ]
        if not occurrences:
            return self._reject_ability_request(parsed_action, "ability_not_grounded")
        unique_occurrences = [
            occurrence
            for occurrence in occurrences
            if references_by_canonical[occurrence.canonical_reference] == {selected_id}
        ]
        if not unique_occurrences:
            return self._reject_ability_request(parsed_action, "ambiguous_ability_reference", "ambiguous")

        item_references = set(parser_context.accessible_item_references)
        route_spans = [
            (start, end)
            for reference in parser_context.accessible_route_references
            for start, end in self._canonical_phrase_spans(parsed_action.raw_text, reference)
        ]
        eligible_occurrences = [
            occurrence
            for occurrence in unique_occurrences
            if (
                occurrence.canonical_reference not in item_references
                and not any(
                    start < occurrence.end and occurrence.start < end
                    for start, end in route_spans
                )
            )
            or self._has_ability_namespace_qualifier(parsed_action.raw_text, occurrence)
        ]
        if not eligible_occurrences:
            return self._reject_ability_request(parsed_action, "item_ability_namespace_collision")

        requires_target = selected[0].get("requires_target") is True
        target = parsed_action.target
        if requires_target and not target:
            return self._reject_ability_request(parsed_action, "required_target_missing")
        if target:
            if (
                selected[0].get("target_kind") == "route"
                and normalize_identifier(target) not in parser_context.accessible_route_references
            ):
                return self._reject_ability_request(parsed_action, "route_target_not_local")
            grounded_target = next(
                (
                    grounded
                    for occurrence in eligible_occurrences
                    if (grounded := self._grounded_player_phrase(
                        parsed_action.raw_text,
                        target,
                        excluded_span=(occurrence.start, occurrence.end),
                    )) is not None
                ),
                None,
            )
            if grounded_target is None:
                return self._reject_ability_request(parsed_action, "target_not_grounded")
            target = grounded_target

        return parsed_action.model_copy(
            update={"target": target, "parameters": {"ability_id": selected_id}}
        )

    def _reject_ability_request(
        self,
        parsed_action: ParsedAction,
        reason: str,
        parse_status: ParseStatus = "invalid",
    ) -> ParsedAction:
        logger.warning("action_parser_ability_request_rejected reason=%s", reason)
        return parsed_action.model_copy(
            update={
                "action": ActionType.UNKNOWN,
                "target": None,
                "parameters": {},
                "confidence": 0.0,
                "parse_status": parse_status,
                "parser_notes": "The model ability request is not grounded in player text and authoritative context.",
            }
        )

    def _canonical_phrase_pattern(self, reference: str) -> re.Pattern[str] | None:
        expression = self._canonical_phrase_expression(reference)
        if expression is None:
            return None
        return re.compile(r"(?<![a-z0-9])" + expression + r"(?![a-z0-9])", re.IGNORECASE)

    def _canonical_phrase_expression(self, reference: str) -> str | None:
        normalized = normalize_identifier(reference)
        if not normalized:
            return None
        words = normalized.split()
        return r"[\W_]+".join(re.escape(word) for word in words)

    def _canonical_phrase_spans(self, text: str, reference: str) -> list[tuple[int, int]]:
        pattern = self._canonical_phrase_pattern(reference)
        if pattern is None:
            return []
        return [match.span() for match in pattern.finditer(text)]

    def _grounded_player_phrase(
        self,
        text: str,
        model_phrase: str,
        *,
        excluded_span: tuple[int, int],
    ) -> str | None:
        canonical = normalize_identifier(model_phrase)
        if not canonical:
            return None
        tokens = list(re.finditer(r"[a-z0-9]+(?:[_-][a-z0-9]+)*", text, re.IGNORECASE))
        for start in range(len(tokens)):
            for end in range(start, min(len(tokens), start + 8)):
                candidate = text[tokens[start].start():tokens[end].end()]
                candidate_span = (tokens[start].start(), tokens[end].end())
                overlaps_excluded = (
                    candidate_span[0] < excluded_span[1]
                    and excluded_span[0] < candidate_span[1]
                )
                if (
                    not overlaps_excluded
                    and len(candidate) <= 80
                    and normalize_identifier(candidate) == canonical
                ):
                    return re.sub(r"^(?:the|a|an)\s+", "", candidate, flags=re.IGNORECASE)
        return None

    def _has_ability_namespace_qualifier(
        self, text: str, occurrence: AbilityReferenceOccurrence
    ) -> bool:
        return (
            re.search(
                r"\bability[\W_]+$",
                text[:occurrence.start],
                re.IGNORECASE,
            ) is not None
            or re.match(
                r"[\W_]+ability(?![a-z0-9])",
                text[occurrence.end:],
                re.IGNORECASE,
            ) is not None
        )

    def _dict_value(self, source: dict[str, Any], key: str | None) -> dict[str, Any]:
        if key is None:
            return {}
        value = source.get(key)
        if not isinstance(value, dict):
            return {}

        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if isinstance(raw_key, str):
                normalized[raw_key] = raw_value
        return normalized
