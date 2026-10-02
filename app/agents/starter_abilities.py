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
    AbilityObjectMotion,
    AbilitySenseFilter,
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
                    "with sense_filter presence or supernatural_presence, and one utility ability "
                    "using exactly one supported operation: move/object/toward_player, "
                    "toggle/object/open, or toggle/object/lit. Set minimum_points to 0 so both "
                    "abilities are available immediately. Sensory range may be 0 (current room) or "
                    "1 (current and directly adjacent rooms); utility range must be 0 (current room). "
                    "Use only the nearby requirement and no bypasses. Do not use keen_eye or any "
                    "existing built-in ability id or display name. Give each ability a thematic, "
                    "evocative name of one to three short words that hints at its supported effect; "
                    "never use raw taxonomy or debugging labels. The name and flavor description "
                    "must remain modest, but only the validated mechanics define what it does. For "
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
            reasoning_effort=ModelPolicy.starter_ability_reasoning_effort(),
            timeout=20,
            return_usage=return_usage,
        )
        if return_usage:
            if not isinstance(result, ModelCallResult):
                raise ValueError("Starter ability generator did not return model usage.")
            if result.output is None:
                return result
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
                    display_name="Grave Echo",
                    description="Feel supernatural presence through a faint chill in the air.",
                    kind=GeneratedAbilityKind.SENSORY,
                    mechanics=GeneratedAbilityMechanics(
                        effect=AbilityEffect.SENSE,
                        domain=AbilityDomain.SURROUNDINGS,
                        channel=AbilityChannel.SUPERNATURAL,
                        detail=AbilityDetail.LIMITED,
                        range=1,
                        requires=("nearby",),
                        sense_filter=AbilitySenseFilter.SUPERNATURAL_PRESENCE,
                    ),
                    track=ProgressionTrackId.INVESTIGATION,
                    minimum_points=0,
                ),
                GeneratedAbilityDefinition(
                    ability_id="whispering_touch",
                    display_name="Whispering Grasp",
                    description="Draw a small object near with a quiet, unseen pull.",
                    kind=GeneratedAbilityKind.UTILITY,
                    mechanics=GeneratedAbilityMechanics(
                        effect=AbilityEffect.MOVE,
                        domain=AbilityDomain.OBJECT,
                        channel=AbilityChannel.SUPERNATURAL,
                        detail=AbilityDetail.PRACTICAL,
                        range=0,
                        requires=("nearby",),
                        object_motion=AbilityObjectMotion.TOWARD_PLAYER,
                    ),
                    track=ProgressionTrackId.OCCULT,
                    minimum_points=0,
                ),
            ]
        )
