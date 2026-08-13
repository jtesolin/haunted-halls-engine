from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import model_client
from app.ai.prompts import action_parser_prompt
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget
from app.schemas.chat import ActionParserOutput, ActionType, ParsedAction


logger = logging.getLogger(__name__)


class ActionParserError(Exception):
    pass


class ActionParseProviderError(ActionParserError):
    pass


ParseStatus = Literal["ok", "ambiguous", "invalid"]


class ParserContext(BaseModel):
    location: str | None = None
    exits: list[str] = Field(default_factory=list)
    nearby_objects: list[str] = Field(default_factory=list)
    inventory: list[str] = Field(default_factory=list)
    nearby_npcs: list[str] = Field(default_factory=list)
    status_flags: dict[str, Any] = Field(default_factory=dict)


class ActionParserAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "ActionParser"

    async def parse(
        self,
        *,
        message: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]] | None = None,
        model: str | None = None,
        deterministic_only: bool = False,
    ) -> ParsedAction:
        if deterministic_only:
            logger.info(
                "action_parser_deterministic_mode message_length=%s",
                len(message),
            )
            return self._fallback_parse(message)

        parser_context = self._build_parser_context(campaign_state)
        messages = self._build_messages(
            message=message,
            parser_context=parser_context,
            recent_turns=recent_turns,
            memory_context=memory_context or [],
        )
        try:
            parsed_output = await model_client.generate_structured(
                messages=messages,
                response_model=ActionParserOutput,
                model=model or ModelPolicy.action_parser_model(),
                max_output_tokens=TokenBudget.action_parser_max_output_tokens(),
                reasoning_effort="minimal",
                timeout=15,
            )
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
            return ParsedAction(
                raw_text=message,
                action=parsed_output.action,
                target=parsed_output.target,
                parameters=parsed_output.parameters.model_dump(exclude_none=True),
                stealth=parsed_output.stealth,
                confidence=parsed_output.confidence,
                parse_status=parsed_output.parse_status,
                parser_notes=parsed_output.parser_notes,
            )

        return ParsedAction(
            raw_text=message,
            action=ActionType.UNKNOWN,
            target=None,
            parameters={},
            stealth=False,
            confidence=0.0,
            parse_status="invalid",
            parser_notes="Action parser did not return valid structured output.",
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

    def _fallback_parse(self, message: str) -> ParsedAction:
        lower = message.lower()
        stealth = any(token in lower for token in ("quiet", "quietly", "stealth", "sneak", "silently", "hidden"))

        action = ActionType.OBSERVE
        target: str | None = None
        parse_status: ParseStatus = "ambiguous"
        confidence = 0.45
        notes = "Deterministic heuristic parser was used."

        if self._contains_any_phrase(lower, ["climb", "scale"]):
            action = ActionType.CLIMB
            parse_status = "ok"
            confidence = 0.88
            target = self._target_after_tokens(lower, ["onto", "on", "to", "the"]) or "roof"
        elif self._contains_any_phrase(lower, ["move", "go", "walk", "run", "enter"]):
            action = ActionType.MOVE
            parse_status = "ok"
            confidence = 0.82
            target = self._target_after_tokens(lower, ["to", "into", "toward", "towards", "the"])
        elif self._contains_any_phrase(lower, ["take", "grab", "pick up", "collect"]):
            action = ActionType.TAKE
            parse_status = "ok"
            confidence = 0.8
            target = self._target_after_tokens(lower, ["the", "a", "an"])
        elif self._contains_any_phrase(lower, ["drop", "remove", "discard"]):
            action = ActionType.DROP
            parse_status = "ok"
            confidence = 0.76
            target = self._target_after_tokens(lower, ["the", "a", "an"])
        elif self._contains_any_phrase(lower, ["wait", "rest", "pass time"]):
            action = ActionType.WAIT
            parse_status = "ok"
            confidence = 0.7
        elif self._contains_any_phrase(lower, ["talk", "speak", "ask", "say"]):
            action = ActionType.TALK
            parse_status = "ok"
            confidence = 0.72
            target = self._target_after_tokens(lower, ["to", "with", "the"])
        elif self._contains_any_phrase(lower, ["use", "open", "pull", "push", "interact"]):
            action = ActionType.USE
            parse_status = "ok"
            confidence = 0.7
            target = self._target_after_tokens(lower, ["the", "a", "an", "with"])
        elif self._contains_any_phrase(lower, ["attack", "hit", "strike", "fight"]):
            action = ActionType.ATTACK
            parse_status = "ok"
            confidence = 0.7
            target = self._target_after_tokens(lower, ["the", "a", "an"])
        elif self._contains_any_phrase(lower, ["spawn", "summon", "record", "record fact", "advance clock"]):
            action = ActionType.UNKNOWN
            parse_status = "ambiguous"
            confidence = 0.2
            notes = "Requested privileged world manipulation is not a player action."

        return ParsedAction(
            raw_text=message,
            action=action,
            target=target,
            parameters={},
            stealth=stealth,
            confidence=confidence,
            parse_status=parse_status,
            parser_notes=notes,
        )

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
        npcs = self._dict_value(state, "npcs")

        location_raw = player.get("location")
        location = location_raw if isinstance(location_raw, str) else None

        inventory_raw = player.get("inventory")
        inventory = [item for item in inventory_raw if isinstance(item, str)] if isinstance(inventory_raw, list) else []

        nearby_npcs: list[str] = []
        for npc_id, npc_state in npcs.items():
            if not isinstance(npc_id, str) or not isinstance(npc_state, dict):
                continue
            if location is not None and npc_state.get("room") == location:
                nearby_npcs.append(npc_id)

        rooms = self._dict_value(state, "rooms")
        current_room = self._dict_value(rooms, location) if location is not None else {}
        exits_raw = current_room.get("exits") if isinstance(current_room, dict) else None
        nearby_objects_raw = current_room.get("objects") if isinstance(current_room, dict) else None

        exits = [room for room in exits_raw if isinstance(room, str)] if isinstance(exits_raw, list) else []
        nearby_objects = [obj for obj in nearby_objects_raw if isinstance(obj, str)] if isinstance(nearby_objects_raw, list) else []

        status_flags = self._dict_value(state, "status")
        return ParserContext(
            location=location,
            exits=exits,
            nearby_objects=nearby_objects,
            inventory=inventory,
            nearby_npcs=nearby_npcs,
            status_flags=status_flags,
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

    def _target_after_tokens(self, text: str, tokens: list[str]) -> str | None:
        words = text.replace(".", " ").replace(",", " ").split()
        stop_words = {"the", "a", "an"}
        for index, word in enumerate(words):
            if word in tokens and index + 1 < len(words):
                for candidate_index in range(index + 1, len(words)):
                    candidate = words[candidate_index].strip()
                    if not candidate or candidate in stop_words:
                        continue
                    return candidate
        return None

    def _contains_any_phrase(self, text: str, phrases: list[str]) -> bool:
        for phrase in phrases:
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pattern, text):
                return True
        return False
