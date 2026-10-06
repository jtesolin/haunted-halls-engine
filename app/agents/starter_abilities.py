"""Campaign-creation generation for bounded starter ability content."""

from typing import Literal, overload

from openai.types.chat import ChatCompletionMessageParam

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens
from app.schemas.generated_abilities import (
    AbilityChannel,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    AbilityObjectMotion,
    AbilityObjectState,
    GeneratedAbilityDefinition,
    GeneratedAbilityKind,
    GeneratedAbilityMechanics,
    StarterAbilityGeneration,
)
from app.schemas.starter_ability_provider import (
    StarterAbilityProviderGeneration,
    StarterUtilityOperation,
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
                    "Generate exactly two modest non-combat Haunted Halls starter abilities using "
                    "the schema's sensory_ability and utility_ability fields. Choose a bounded "
                    "sense_filter and range for the sensory ability, and exactly one supported "
                    "utility operation: retrieve, toggle_open, or toggle_lit. The engine determines "
                    "all mechanics from these choices; do not add or imply other operations. "
                    "Sensory range may be 0 (current room) or 1 (current and directly adjacent "
                    "rooms). Both abilities are available immediately. Do not use keen_eye or any "
                    "existing built-in ability id or display name. Give each ability a thematic, "
                    "evocative name of one to three short words that hints at its supported effect; "
                    "never use raw taxonomy or debugging labels. The name and flavor description "
                    "must remain modest, but only the engine-defined operation determines what it "
                    "does. For "
                    "presence sensing, reveal only active presence (and whether it is supernatural), "
                    "not identity or biography. Pull only a small portable ordinary item from the "
                    "current room. Toggle only the existing state of a canonically openable or "
                    "lightable nearby item. Do not imply perfect knowledge, read minds, see remotely, "
                    "bypass darkness or invisibility, unlock objects, move entities, damage or attack, "
                    "or change quest or world state."
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
        self, *, return_usage: Literal[False] = False
    ) -> StarterAbilityGeneration: ...

    @overload
    async def generate(
        self, *, return_usage: Literal[True]
    ) -> ModelCallResult[StarterAbilityGeneration]: ...

    async def generate(
        self, *, return_usage: bool = False
    ) -> StarterAbilityGeneration | ModelCallResult[StarterAbilityGeneration]:
        result = await model_client.generate_structured(
            messages=self.build_provider_request(),
            response_model=StarterAbilityProviderGeneration,
            model=ModelPolicy.narrator_model(),
            max_output_tokens=TokenBudget.starter_ability_max_output_tokens(),
            reasoning_effort=ModelPolicy.starter_ability_reasoning_effort(),
            timeout=20,
            return_usage=return_usage,
        )
        if return_usage:
            if not isinstance(result, ModelCallResult):
                raise ValueError("Starter ability generator did not return model usage.")
            if result.output is None:
                return ModelCallResult(
                    output=None,
                    usage=result.usage,
                    status=result.status,
                    incomplete_details_reason=result.incomplete_details_reason,
                )
            provider_output = result.output
        else:
            provider_output = result.output if isinstance(result, ModelCallResult) else result
        if not isinstance(provider_output, StarterAbilityProviderGeneration):
            raise ValueError("Starter ability generator did not return valid structured output.")
        output = self._to_domain_generation(provider_output)
        if return_usage:
            if not isinstance(result, ModelCallResult):
                raise ValueError("Starter ability generator did not return model usage.")
            return ModelCallResult(
                output=output,
                usage=result.usage,
                status=result.status,
                incomplete_details_reason=result.incomplete_details_reason,
            )
        return output

    def _to_domain_generation(
        self, provider_output: StarterAbilityProviderGeneration
    ) -> StarterAbilityGeneration:
        sensory = provider_output.sensory_ability
        utility = provider_output.utility_ability
        if utility.operation == StarterUtilityOperation.RETRIEVE:
            utility_mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.MOVE,
                domain=AbilityDomain.OBJECT,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.PRACTICAL,
                range=0,
                requires=("nearby",),
                object_motion=AbilityObjectMotion.TOWARD_PLAYER,
            )
        else:
            object_state = (
                AbilityObjectState.OPEN
                if utility.operation == StarterUtilityOperation.TOGGLE_OPEN
                else AbilityObjectState.LIT
            )
            utility_mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.TOGGLE,
                domain=AbilityDomain.OBJECT,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.PRACTICAL,
                range=0,
                requires=("nearby",),
                object_state=object_state,
            )
        return StarterAbilityGeneration(
            abilities=[
                GeneratedAbilityDefinition(
                    ability_id=sensory.ability_id,
                    display_name=sensory.display_name,
                    description=sensory.description,
                    kind=GeneratedAbilityKind.SENSORY,
                    mechanics=GeneratedAbilityMechanics(
                        effect=AbilityEffect.SENSE,
                        domain=AbilityDomain.SURROUNDINGS,
                        channel=AbilityChannel.SUPERNATURAL,
                        detail=AbilityDetail.LIMITED,
                        range=sensory.range,
                        requires=("nearby",),
                        sense_filter=sensory.sense_filter,
                    ),
                    track=sensory.track,
                    minimum_points=0,
                ),
                GeneratedAbilityDefinition(
                    ability_id=utility.ability_id,
                    display_name=utility.display_name,
                    description=utility.description,
                    kind=GeneratedAbilityKind.UTILITY,
                    mechanics=utility_mechanics,
                    track=utility.track,
                    minimum_points=0,
                ),
            ]
        )
