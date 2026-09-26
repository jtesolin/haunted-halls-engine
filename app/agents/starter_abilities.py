"""Campaign-creation generation for bounded starter ability content."""

from typing import Literal, overload

from openai.types.chat import ChatCompletionMessageParam

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.generated_abilities import (
    AbilityChannel,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    GeneratedAbilityDefinition,
    GeneratedAbilityKind,
    GeneratedAbilityMechanics,
    StarterAbilityGeneration,
)


class StarterAbilityGenerator(BaseAgent):
    @property
    def name(self) -> str:
        return "StarterAbilityGenerator"

    def build_provider_request(self) -> list[ChatCompletionMessageParam]:
        return [
            {
                "role": "developer",
                "content": (
                    "Generate exactly two modest non-combat Haunted Halls starter abilities. "
                    "Return only the schema. Include one sensory ability using sense/surroundings "
                    "and one utility ability using minor_utility/object. Set minimum_points to 0 "
                    "so both abilities are available immediately. Use range 0 or 1, no "
                    "bypasses, and only nearby or line_of_sight requirements. Do not use keen_eye "
                    "or any existing built-in ability id or display name."
                ),
            }
        ]

    def estimate_provider_input_tokens(self) -> int:
        return sum(
            estimate_tokens(str(message.get("content", "")))
            for message in self.build_provider_request()
            if isinstance(message, dict) and isinstance(message.get("content"), str)
        )

    @overload
    async def generate(
        self, *, provider_model_enabled: bool, return_usage: Literal[False] = False
    ) -> StarterAbilityGeneration: ...

    @overload
    async def generate(
        self, *, provider_model_enabled: bool, return_usage: Literal[True]
    ) -> ModelCallResult[StarterAbilityGeneration]: ...

    async def generate(
        self, *, provider_model_enabled: bool, return_usage: bool = False
    ) -> StarterAbilityGeneration | ModelCallResult[StarterAbilityGeneration]:
        if not provider_model_enabled:
            generation = self._stub_generation()
            return ModelCallResult(output=generation, usage=None) if return_usage else generation
        result = await model_client.generate_structured(
            messages=self.build_provider_request(),
            response_model=StarterAbilityGeneration,
            model=ModelPolicy.narrator_model(),
            max_output_tokens=TokenBudget.starter_ability_max_output_tokens(),
            reasoning_effort=ModelPolicy.narrator_reasoning_effort(),
            timeout=20,
            return_usage=return_usage,
        )
        if return_usage:
            if not isinstance(result, ModelCallResult):
                raise ValueError("Starter ability generator did not return model usage.")
            output = result.output
        else:
            output = result.output if isinstance(result, ModelCallResult) else result
        if not isinstance(output, StarterAbilityGeneration):
            raise ValueError("Starter ability generator did not return valid structured output.")
        return result if return_usage and isinstance(result, ModelCallResult) else output

    def _stub_generation(self) -> StarterAbilityGeneration:
        return StarterAbilityGeneration(
            abilities=[
                GeneratedAbilityDefinition(
                    ability_id="echo_sense",
                    display_name="Echo Sense",
                    description="Feel nearby spaces through faint supernatural echoes.",
                    kind=GeneratedAbilityKind.SENSORY,
                    mechanics=GeneratedAbilityMechanics(
                        effect=AbilityEffect.SENSE,
                        domain=AbilityDomain.SURROUNDINGS,
                        channel=AbilityChannel.SUPERNATURAL,
                        detail=AbilityDetail.LIMITED,
                        range=1,
                        requires=("nearby",),
                    ),
                    track=ProgressionTrackId.INVESTIGATION,
                    minimum_points=0,
                ),
                GeneratedAbilityDefinition(
                    ability_id="whispering_touch",
                    display_name="Whispering Touch",
                    description="Coax a nearby ordinary object to offer a small practical response.",
                    kind=GeneratedAbilityKind.UTILITY,
                    mechanics=GeneratedAbilityMechanics(
                        effect=AbilityEffect.MINOR_UTILITY,
                        domain=AbilityDomain.OBJECT,
                        channel=AbilityChannel.SUPERNATURAL,
                        detail=AbilityDetail.PRACTICAL,
                        range=1,
                        requires=("nearby",),
                    ),
                    track=ProgressionTrackId.OCCULT,
                    minimum_points=0,
                ),
            ]
        )
