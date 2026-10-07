"""Campaign-creation generation for bounded starter ability content."""

from typing import Literal, overload

from openai.types.chat import ChatCompletionMessageParam

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.game.abilities import validate_starter_ability_definitions
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
    TraversalMethod,
)
from app.schemas.starter_ability_provider import (
    StarterAbilityProviderGeneration,
    StarterAbilitySensoryProviderOutput,
    StarterAbilityUtilityProviderOutput,
    StarterAbilityTraversalProviderOutput,
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
                    "the schema's first_ability and second_ability fields. Choose two mechanically "
                    "distinct choices from the whole pool: presence or supernatural presence sensing "
                    "(range 0 or 1), retrieve, toggle_open, toggle_lit, levitation, spider_climb, "
                    "supernatural_jump, water_walking. Do not require one sensory plus one utility. "
                    "Names or flavor differences alone do not make two abilities distinct. "
                    "The engine determines "
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
                    "bypass darkness or invisibility, unlock objects, move entities other than the "
                    "player on an authored traversal route, damage or attack, "
                    "or arbitrarily change quest or world state. Traversal performs one crossing on a nearby "
                    "authored route and ends at a stable landing: levitation only clear vertical "
                    "ascent/descent; spider climb only continuous supporting surfaces; jump only "
                    "authored gaps/elevations within three metres; water walking only authored "
                    "water surfaces between landings. No free flight, ongoing effects, swimming, "
                    "underwater access, hazard immunity, teleportation, or phasing."
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
        abilities = [
            self._to_domain_ability(choice)
            for choice in (provider_output.first_ability, provider_output.second_ability)
        ]
        validate_starter_ability_definitions(abilities)
        return StarterAbilityGeneration(abilities=abilities)

    def _to_domain_ability(
        self,
        choice: (
            StarterAbilitySensoryProviderOutput
            | StarterAbilityUtilityProviderOutput
            | StarterAbilityTraversalProviderOutput
        ),
    ) -> GeneratedAbilityDefinition:
        if isinstance(choice, StarterAbilitySensoryProviderOutput):
            kind = GeneratedAbilityKind.SENSORY
            mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.SENSE,
                domain=AbilityDomain.SURROUNDINGS,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.LIMITED,
                range=choice.range,
                requires=("nearby",),
                sense_filter=choice.sense_filter,
            )
        elif isinstance(choice, StarterAbilityTraversalProviderOutput):
            kind = GeneratedAbilityKind.TRAVERSAL
            mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.TRAVERSE,
                domain=AbilityDomain.ROUTE,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.PRACTICAL,
                range=0,
                requires=("nearby",),
                traversal_method=choice.traversal_method,
                jump_reach_metres=(
                    3 if choice.traversal_method == TraversalMethod.SUPERNATURAL_JUMP else None
                ),
            )
        elif choice.operation == StarterUtilityOperation.RETRIEVE:
            kind = GeneratedAbilityKind.UTILITY
            mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.MOVE,
                domain=AbilityDomain.OBJECT,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.PRACTICAL,
                range=0,
                requires=("nearby",),
                object_motion=AbilityObjectMotion.TOWARD_PLAYER,
            )
        else:
            kind = GeneratedAbilityKind.UTILITY
            object_state = (
                AbilityObjectState.OPEN
                if choice.operation == StarterUtilityOperation.TOGGLE_OPEN
                else AbilityObjectState.LIT
            )
            mechanics = GeneratedAbilityMechanics(
                effect=AbilityEffect.TOGGLE,
                domain=AbilityDomain.OBJECT,
                channel=AbilityChannel.SUPERNATURAL,
                detail=AbilityDetail.PRACTICAL,
                range=0,
                requires=("nearby",),
                object_state=object_state,
            )
        return GeneratedAbilityDefinition(
            ability_id=choice.ability_id,
            display_name=choice.display_name,
            description=choice.description,
            kind=kind,
            mechanics=mechanics,
            track=choice.track,
            minimum_points=0,
        )
