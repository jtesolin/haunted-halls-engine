from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, ModelUsage, model_client
from app.ai.prompts import director_prompt
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens
from app.schemas.director import DirectorInput, DirectorProposal
from app.schemas.world import (
    AdvanceClockWorldAction,
    MoveNpcWorldAction,
    RecordFactWorldAction,
    SetNpcStatusWorldAction,
)


logger = logging.getLogger(__name__)

_DIRECTOR_PROPOSAL_ADAPTER = TypeAdapter(DirectorProposal)


class DirectorProviderWorldAction(BaseModel):
    """Provider-facing flattened action DTO for Structured Outputs compatibility."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["move_npc", "set_npc_status", "advance_clock", "record_fact"]
    npc_id: str | None = None
    destination_room_id: str | None = None
    status: Literal["active", "absent"] | None = None
    ticks: int | None = None
    fact: str | None = None

    def to_domain_action(
        self,
    ) -> (
        MoveNpcWorldAction
        | SetNpcStatusWorldAction
        | AdvanceClockWorldAction
        | RecordFactWorldAction
    ):
        if self.action == "move_npc":
            self._require_only("npc_id", "destination_room_id")
            return MoveNpcWorldAction(
                npc_id=self._require_string("npc_id"),
                destination_room_id=self._require_string("destination_room_id"),
            )
        if self.action == "set_npc_status":
            self._require_only("npc_id", "status")
            status = self.status
            if status is None:
                raise ValueError("set_npc_status requires status.")
            return SetNpcStatusWorldAction(
                npc_id=self._require_string("npc_id"),
                status=status,
            )
        if self.action == "advance_clock":
            self._require_only("ticks")
            ticks = self.ticks
            if ticks is None:
                raise ValueError("advance_clock requires ticks.")
            return AdvanceClockWorldAction(ticks=ticks)
        if self.action == "record_fact":
            self._require_only("fact")
            return RecordFactWorldAction(fact=self._require_string("fact"))
        raise ValueError(f"Unsupported Director provider world action: {self.action}")

    def _require_only(self, *allowed_fields: str) -> None:
        allowed = set(allowed_fields)
        provided = {
            field_name
            for field_name in (
                "npc_id",
                "destination_room_id",
                "status",
                "ticks",
                "fact",
            )
            if getattr(self, field_name) is not None
        }
        unexpected = sorted(provided - allowed)
        if unexpected:
            raise ValueError(
                f"{self.action} received fields for another action: "
                f"{', '.join(unexpected)}."
            )

    def _require_string(self, field_name: str) -> str:
        value = getattr(self, field_name)
        if not isinstance(value, str):
            raise ValueError(f"{self.action} requires {field_name}.")
        return value


class DirectorProviderProposal(BaseModel):
    """Provider-facing proposal DTO adapted into the authoritative domain union."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["none", "act"]
    world_action: DirectorProviderWorldAction | None = None

    @model_validator(mode="after")
    def validate_decision_payload(self) -> "DirectorProviderProposal":
        if self.decision == "none" and self.world_action is not None:
            raise ValueError("decision='none' must not include a world action.")
        if self.decision == "act" and self.world_action is None:
            raise ValueError("decision='act' requires a world action.")
        return self

    def to_domain_proposal(self) -> DirectorProposal:
        if self.decision == "none":
            return _DIRECTOR_PROPOSAL_ADAPTER.validate_python({"decision": "none"})

        if self.world_action is None:
            raise ValueError("decision='act' requires a world action.")

        return _DIRECTOR_PROPOSAL_ADAPTER.validate_python(
            {
                "decision": "act",
                "world_action": self.world_action.to_domain_action().model_dump(),
            }
        )


class DirectorProposalResponse(BaseModel):
    """Strict provider wrapper that avoids schema composition unsupported upstream."""

    model_config = ConfigDict(extra="forbid")

    proposal: DirectorProviderProposal

    def to_domain_proposal(self) -> DirectorProposal:
        return self.proposal.to_domain_proposal()


@dataclass(frozen=True)
class DirectorAgentResult:
    """A validated proposal and the usage metadata from its provider call."""

    proposal: DirectorProposal
    usage: ModelUsage | None


class DirectorError(Exception):
    """Base error raised while generating a Director proposal."""


class DirectorProviderError(DirectorError):
    """Raised when the structured-output provider call fails."""


class DirectorProposalOutputError(DirectorError):
    """Raised when the provider does not return a valid Director proposal."""


class DirectorAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "Director"

    def build_provider_request(
        self,
        *,
        director_input: DirectorInput,
    ) -> list[ChatCompletionMessageParam]:
        """Build the bounded request sent to the Director model."""
        self._require_director_input(director_input)
        return [
            {
                "role": "developer",
                "content": director_prompt,
            },
            {
                "role": "user",
                "content": (
                    "Authoritative Director input:\n"
                    f"{director_input.model_dump_json(exclude_none=True, indent=2)}"
                ),
            },
        ]

    def estimate_provider_input_tokens(
        self,
        *,
        director_input: DirectorInput,
    ) -> int:
        """Estimate input tokens from the exact request that will be provided."""
        request = self.build_provider_request(director_input=director_input)
        return self._estimate_provider_request_tokens(request)

    @staticmethod
    def _estimate_provider_request_tokens(
        request: list[ChatCompletionMessageParam],
    ) -> int:
        return sum(
            estimate_tokens(str(message.get("content", "")))
            for message in request
            if isinstance(message, dict) and isinstance(message.get("content"), str)
        )

    async def propose(
        self,
        *,
        director_input: DirectorInput,
        model: str | None = None,
    ) -> DirectorAgentResult:
        """Generate one advisory proposal without executing or persisting it."""
        request = self.build_provider_request(director_input=director_input)
        selected_model = model or ModelPolicy.director_model()
        estimated_input_tokens = self._estimate_provider_request_tokens(request)

        try:
            generated = await model_client.generate_structured(
                messages=request,
                response_model=DirectorProposalResponse,
                model=selected_model,
                max_output_tokens=TokenBudget.director_max_output_tokens(),
                reasoning_effort="minimal",
                timeout=15,
                return_usage=True,
            )
        except Exception as exc:
            logger.error(
                "director_structured_call_failed model=%s estimated_input_tokens=%s "
                "error_type=%s error_message=%s",
                selected_model,
                estimated_input_tokens,
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            raise DirectorProviderError("Director model call failed.") from exc

        if isinstance(generated, ModelCallResult):
            provider_output = generated.output
            usage = generated.usage
        else:
            provider_output = generated
            usage = None

        if provider_output is None:
            logger.warning(
                "director_structured_output_missing model=%s estimated_input_tokens=%s",
                selected_model,
                estimated_input_tokens,
            )
            raise DirectorProposalOutputError(
                "Director did not return valid structured output."
            )

        try:
            proposal = self._adapt_provider_output(provider_output)
        except (TypeError, ValueError, ValidationError) as exc:
            logger.error(
                "director_structured_output_invalid model=%s estimated_input_tokens=%s "
                "error_type=%s error_message=%s",
                selected_model,
                estimated_input_tokens,
                type(exc).__name__,
                str(exc),
                exc_info=True,
            )
            raise DirectorProposalOutputError(
                "Director returned invalid structured output."
            ) from exc

        return DirectorAgentResult(proposal=proposal, usage=usage)

    @staticmethod
    def _adapt_provider_output(provider_output: DirectorProposalResponse) -> DirectorProposal:
        if not isinstance(provider_output, DirectorProposalResponse):
            raise TypeError("Director provider output has an unexpected type.")
        return provider_output.to_domain_proposal()

    @staticmethod
    def _require_director_input(director_input: DirectorInput) -> None:
        if not isinstance(director_input, DirectorInput):
            raise TypeError("Director proposals require a typed DirectorInput.")
