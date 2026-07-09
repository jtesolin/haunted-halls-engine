from __future__ import annotations

import json
from typing import Literal, cast

from openai.types.chat import ChatCompletionMessageParam

from app.agents.base import BaseAgent
from app.ai.model_client import model_client
from app.guardrails.model_policy import ModelPolicy
from app.schemas.chat import ParsedAction


class ActionParserError(Exception):
    pass


class ActionParseProviderError(ActionParserError):
    pass


ParseStatus = Literal["ok", "ambiguous", "invalid"]


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
            return self._fallback_parse(message)

        messages = self._build_messages(
            message=message,
            campaign_state=campaign_state,
            recent_turns=recent_turns,
            memory_context=memory_context or [],
        )
        try:
            raw_reply = await model_client.generate_text(
                messages=messages,
                model=model or ModelPolicy.action_parser_model(),
                max_output_tokens=320,
                reasoning_effort="minimal",
                timeout=15,
            )
        except Exception as exc:
            raise ActionParseProviderError("Action parser model call failed.") from exc

        parsed = self._parse_model_output(message, raw_reply)
        if parsed is not None:
            return parsed
        return ParsedAction(
            raw_text=message,
            action="unknown",
            target=None,
            parameters={},
            stealth=False,
            confidence=0.0,
            parse_status="invalid",
            parser_notes="Action parser did not return valid JSON.",
        )

    def _build_messages(
        self,
        *,
        message: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]],
    ) -> list[ChatCompletionMessageParam]:
        short_history = recent_turns[-4:]
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "developer",
                "content": (
                    "Convert player text into strict JSON for tool execution. "
                    "Respond with only JSON and no markdown. "
                    "Schema: {\"action\":str,\"target\":str|null,\"parameters\":object,"
                    "\"stealth\":bool,\"confidence\":number,\"parse_status\":\"ok\"|\"ambiguous\"|\"invalid\",\"parser_notes\":str|null}."
                ),
            },
            {
                "role": "user",
                "content": f"Campaign state:\n{campaign_state}",
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

    def _parse_model_output(self, raw_text: str, model_output: str) -> ParsedAction | None:
        blob = self._extract_json_blob(model_output)
        if blob is None:
            return None
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return None

        action = str(data.get("action", "")).strip().lower()
        parse_status_raw = str(data.get("parse_status", "invalid")).strip().lower()
        parse_status: ParseStatus
        if parse_status_raw in {"ok", "ambiguous", "invalid"}:
            parse_status = cast(ParseStatus, parse_status_raw)
        else:
            parse_status = "invalid"

        confidence_value = data.get("confidence", 0.0)
        try:
            confidence = float(confidence_value)
        except (TypeError, ValueError):
            confidence = 0.0

        return ParsedAction(
            raw_text=raw_text,
            action=action or "unknown",
            target=data.get("target"),
            parameters=data.get("parameters") if isinstance(data.get("parameters"), dict) else {},
            stealth=bool(data.get("stealth", False)),
            confidence=max(0.0, min(confidence, 1.0)),
            parse_status=parse_status,
            parser_notes=data.get("parser_notes"),
        )

    def _extract_json_blob(self, text: str) -> str | None:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return stripped[start : end + 1]

    def _fallback_parse(self, message: str) -> ParsedAction:
        lower = message.lower()
        stealth = any(token in lower for token in ("quiet", "quietly", "stealth", "sneak", "silently", "hidden"))

        action = "observe"
        target: str | None = None
        parse_status: ParseStatus = "ambiguous"
        confidence = 0.45
        notes = "Heuristic parser fallback used because model output was not valid JSON."

        if any(token in lower for token in ("climb", "scale")):
            action = "climb"
            parse_status = "ok"
            confidence = 0.88
            target = self._target_after_tokens(lower, ["onto", "on", "to", "the"]) or "roof"
        elif any(token in lower for token in ("move", "go", "walk", "run", "enter")):
            action = "move"
            parse_status = "ok"
            confidence = 0.82
            target = self._target_after_tokens(lower, ["to", "into", "toward", "towards", "the"])
        elif any(token in lower for token in ("take", "grab", "pick up", "collect")):
            action = "take"
            parse_status = "ok"
            confidence = 0.8
            target = self._target_after_tokens(lower, ["the", "a", "an"])
        elif any(token in lower for token in ("drop", "remove", "discard")):
            action = "drop"
            parse_status = "ok"
            confidence = 0.76
            target = self._target_after_tokens(lower, ["the", "a", "an"])
        elif any(token in lower for token in ("spawn", "summon", "call")):
            action = "spawn_npc"
            parse_status = "ok"
            confidence = 0.72
            target = self._target_after_tokens(lower, ["spawn", "summon", "call", "the"])
        elif any(token in lower for token in ("wait", "rest", "pass time", "advance")):
            action = "advance_clock"
            parse_status = "ok"
            confidence = 0.7
        elif any(token in lower for token in ("remember", "note", "record", "fact")):
            action = "record_fact"
            parse_status = "ok"
            confidence = 0.68

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
