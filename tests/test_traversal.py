from __future__ import annotations

import asyncio
import copy
import json
from itertools import combinations

import pytest

from app.agents.action_parser import ActionParserAgent
from app.agents.starter_abilities import StarterAbilityGenerator
from app.game.abilities import (
    evaluate_ability_availability,
    generated_ability_definitions,
    generated_mechanical_signature,
    project_owned_abilities,
    validate_starter_ability_definitions,
)
from app.game.campaign_state import build_fresh_campaign_state, load_authoritative_campaign_state
from app.game.character_progression import unlock_ability
from app.game.narrator_scene import build_narrator_scene_context
from app.game.story import derive_story_signal
from app.game.traversal import validate_route_capability
from app.game.world import DEFAULT_WORLD, Room, World
from app.schemas.chat import ActionParserOutput, ActionParserParameters, ActionType, ParsedAction
from app.schemas.generated_abilities import (
    AbilityGameplayStatus,
    GeneratedAbilityMechanics,
    TraversalMethod,
)
from app.schemas.story import RoomEnteredSignal
from app.schemas.traversal import TraversalRoute
from app.schemas.starter_ability_provider import (
    StarterAbilityProviderGeneration,
    StarterAbilitySensoryProviderOutput,
    StarterAbilityUtilityProviderOutput,
    StarterAbilityTraversalProviderOutput,
    StarterUtilityOperation,
)
from app.schemas.generated_abilities import AbilitySenseFilter
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.world import MoveNpcWorldAction
from app.services.tool_executor import ToolExecutor
from app.services.world_authority import WorldAuthorityExecutor
from tests.factories import starter_ability_generation, traversal_ability_generation


def traversal_state(method: TraversalMethod, location: str = "rain_court") -> dict:
    state = build_fresh_campaign_state()
    definitions = traversal_ability_generation(method).abilities
    state["player"]["generated_abilities"] = [definition.model_dump(mode="json") for definition in definitions]
    state["player"]["location"] = location
    for definition in definitions:
        unlock_ability(state, definition.ability_id)
    return state


def request(target: str, ability_id: str = "silver_step") -> ParsedAction:
    return ParsedAction(
        raw_text=f"I call on my ability {ability_id} to cross {target}.",
        action=ActionType.ABILITY_CHECK,
        target=target,
        parameters={"ability_id": ability_id},
        parse_status="ok",
        confidence=1,
    )


@pytest.mark.parametrize(
    ("method", "target", "destination"),
    [
        (TraversalMethod.LEVITATION, "Gallery Ascent", "upper_gallery"),
        (TraversalMethod.SPIDER_CLIMB, "Ivy Wall", "upper_gallery"),
        (TraversalMethod.SUPERNATURAL_JUMP, "Channel Gap", "far_bank"),
        (TraversalMethod.WATER_WALKING, "Water Crossing", "far_bank"),
    ],
)
def test_conversion_reload_availability_summary_and_real_crossing(
    method: TraversalMethod, target: str, destination: str
) -> None:
    initial = traversal_state(method)
    # Round-trip the actual persisted campaign JSON through the integrity boundary.
    reloaded = load_authoritative_campaign_state(json.dumps(initial))
    definitions = generated_ability_definitions(reloaded)
    assert len(definitions) == 2
    assert definitions[0].mechanics.traversal_method == method
    assert evaluate_ability_availability(reloaded, "silver_step").available
    projection = project_owned_abilities(reloaded)[0]
    assert "Purely cosmetic" not in projection.description
    assert "authored" in projection.description
    if method == TraversalMethod.SUPERNATURAL_JUMP:
        assert "three metres" in projection.description

    updated, result = ToolExecutor().execute(parsed_action=request(target), campaign_state=json.dumps(reloaded))
    assert result.success
    assert result.ability_result is not None
    assert result.ability_result.status == AbilityGameplayStatus.RESOLVED
    effect = result.ability_result.traversal_effect
    assert effect is not None and effect.method == method and effect.destination == destination
    assert updated["player"]["location"] == destination
    assert result.previous_location == "rain_court"
    assert result.current_location == result.resolved_exit == destination
    assert result.state_delta == {"player": {"location": {"from": "rain_court", "to": destination}}}
    assert derive_story_signal(result) == RoomEnteredSignal(room_id=destination)
    assert reloaded == initial  # Executor does not mutate caller state.
    scene = build_narrator_scene_context(json.dumps(updated))
    assert scene.current_room.id == destination
    assert not scene.traversal_routes  # No remote graph leak.
    assert "generated_abilities" in updated["player"]
    assert not any(key in updated["player"] for key in ("flying", "climbing", "duration"))


@pytest.mark.parametrize("method", list(TraversalMethod))
@pytest.mark.parametrize("route_index", range(5))
def test_finite_demo_capability_matrix(method: TraversalMethod, route_index: int) -> None:
    route = DEFAULT_WORLD.traversal_routes[route_index]
    allowed = {
        "gallery_ascent": {TraversalMethod.LEVITATION, TraversalMethod.SPIDER_CLIMB, TraversalMethod.SUPERNATURAL_JUMP},
        "ivy_wall": {TraversalMethod.SPIDER_CLIMB},
        "channel_gap": {TraversalMethod.SUPERNATURAL_JUMP},
        "water_crossing": {TraversalMethod.WATER_WALKING},
        "wide_gap": set(),
    }
    state = traversal_state(method)
    updated, result = ToolExecutor().execute(parsed_action=request(route.route_id), campaign_state=json.dumps(state))
    assert result.success == (method in allowed[route.route_id])
    if result.success:
        assert updated["player"]["location"] == route.destination
    else:
        assert updated == state
        expected_error = (
            "route_out_of_reach"
            if route.route_id == "wide_gap" and method == TraversalMethod.SUPERNATURAL_JUMP
            else "route_incompatible"
        )
        assert result.error_code == expected_error
        assert result.state_delta == {}
        assert derive_story_signal(result) is None


@pytest.mark.parametrize(
    ("method", "route_index", "changes", "error"),
    [
        (TraversalMethod.LEVITATION, 0, {"clear_vertical_path": False}, "route_incompatible"),
        (TraversalMethod.SPIDER_CLIMB, 1, {"continuous_support": False}, "route_incompatible"),
        (TraversalMethod.SUPERNATURAL_JUMP, 2, {"distance_metres": 4}, "route_out_of_reach"),
        (TraversalMethod.SUPERNATURAL_JUMP, 2, {"valid_takeoff": False}, "route_unstable_endpoint"),
        (TraversalMethod.WATER_WALKING, 3, {"water_surface": False}, "route_incompatible"),
        (TraversalMethod.WATER_WALKING, 3, {"valid_landing": False}, "route_unstable_endpoint"),
    ],
)
def test_shared_constraint_failures_are_atomic(
    method: TraversalMethod, route_index: int, changes: dict, error: str
) -> None:
    route = TraversalRoute.model_validate(DEFAULT_WORLD.traversal_routes[route_index].model_dump() | changes)
    world = World(DEFAULT_WORLD.rooms, (route,))
    state = traversal_state(method)
    mechanics = generated_ability_definitions(state)[0].mechanics
    assert validate_route_capability(route, mechanics)[0] == error
    updated, result = ToolExecutor(world=world).execute(parsed_action=request(route.name), campaign_state=json.dumps(state))
    assert updated == state and not result.success and result.error_code == error
    assert derive_story_signal(result) is None


@pytest.mark.parametrize(
    ("target", "location", "ability", "error"),
    [
        ("unknown path", "rain_court", "silver_step", "route_unknown"),
        ("Upper Gallery", "rain_court", "silver_step", "route_ambiguous"),
        ("Gallery Ascent", "entry_hall", "silver_step", "route_not_local"),
        ("Gallery Ascent", "rain_court", "keen_eye", "not_owned"),
        ("Gallery Ascent", "rain_court", "unknown_ability", "unknown_ability"),
    ],
)
def test_unknown_ambiguous_remote_unowned_requests(
    target: str, location: str, ability: str, error: str
) -> None:
    state = traversal_state(TraversalMethod.LEVITATION, location)
    updated, result = ToolExecutor().execute(parsed_action=request(target, ability), campaign_state=json.dumps(state))
    assert not result.success and result.error_code == error
    assert updated == state and result.state_delta == {}
    assert derive_story_signal(result) is None


def test_known_traversal_ability_must_be_owned() -> None:
    state = traversal_state(TraversalMethod.LEVITATION)
    state["player"]["progression"]["unlocked_abilities"].remove("silver_step")
    updated, result = ToolExecutor().execute(parsed_action=request("Gallery Ascent"), campaign_state=json.dumps(state))
    assert result.error_code == "not_owned" and not result.success and updated == state


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "missing"}, {"destination": "missing"},
        {"destination": "rain_court"}, {"distance_metres": 0},
        {"distance_metres": 13}, {"distance_metres": True},
        {"kind": "teleport"}, {"name": " "}, {"valid_landing": "yes"},
        {"kind": "gap"},  # Contradictory vertical path/support.
    ],
)
def test_malformed_authored_routes_and_bad_endpoints(changes: dict) -> None:
    with pytest.raises(ValueError):
        route = TraversalRoute.model_validate(DEFAULT_WORLD.traversal_routes[0].model_dump() | changes)
        World(DEFAULT_WORLD.rooms, (route,))


def test_duplicate_and_unbounded_local_route_content_rejected() -> None:
    route = DEFAULT_WORLD.traversal_routes[0]
    with pytest.raises(ValueError, match="unique"):
        World(DEFAULT_WORLD.rooms, (route, route))
    with pytest.raises(ValueError, match="six"):
        World(DEFAULT_WORLD.rooms, tuple(route.model_copy(update={"route_id": f"route_{i}"}) for i in range(7)))


def test_new_pairs_distinct_cosmetics_ignored_old_definitions_not_rewritten() -> None:
    definitions = traversal_ability_generation(TraversalMethod.LEVITATION).abilities
    assert len(validate_starter_ability_definitions(definitions)) == 2  # Both traversal.
    duplicate = definitions[0].model_copy(update={
        "ability_id": "other_name", "display_name": "Other Name",
        "description": "Different flavor", "track": definitions[1].track,
    })
    assert generated_mechanical_signature(definitions[0]) == generated_mechanical_signature(duplicate)
    with pytest.raises(ValueError, match="mechanically distinct"):
        validate_starter_ability_definitions([definitions[0], duplicate])
    # Compatible loading does not impose a new generation rule on persisted definitions.
    state = traversal_state(TraversalMethod.LEVITATION)
    state["player"]["generated_abilities"] = [
        definitions[0].model_dump(mode="json"), duplicate.model_dump(mode="json"),
    ]
    assert len(generated_ability_definitions(state)) == 2
    legacy = starter_ability_generation().model_dump(mode="json")
    for definition in legacy["abilities"]:
        definition["mechanics"].pop("traversal_method")
        definition["mechanics"].pop("jump_reach_metres")
    state["player"]["generated_abilities"] = legacy["abilities"]
    before = copy.deepcopy(state)
    assert len(generated_ability_definitions(state)) == 2
    assert state == before


def test_combined_pool_all_distinct_pairs_without_rigid_kind_pairing() -> None:
    choices = []
    common = {
        "display_name": "Silent Gift",
        "description": "Cosmetic flavor only.",
        "track": ProgressionTrackId.OCCULT,
    }
    for sense_filter in AbilitySenseFilter:
        for sense_range in (0, 1):
            choices.append(StarterAbilitySensoryProviderOutput(
                **common, ability_id=f"gift_{len(choices)}",
                sense_filter=sense_filter, range=sense_range,
            ))
    for operation in StarterUtilityOperation:
        choices.append(StarterAbilityUtilityProviderOutput(
            **common, ability_id=f"gift_{len(choices)}", operation=operation,
        ))
    for method in TraversalMethod:
        choices.append(StarterAbilityTraversalProviderOutput(
            **common, ability_id=f"gift_{len(choices)}", traversal_method=method,
        ))
    assert len(choices) == 11
    generator = StarterAbilityGenerator()
    for first, second in combinations(choices, 2):
        second = second.model_copy(update={"display_name": "Quiet Gift"})
        result = generator._to_domain_generation(StarterAbilityProviderGeneration(
            first_ability=first, second_ability=second,
        ))
        assert len(result.abilities) == 2
        assert generated_mechanical_signature(result.abilities[0]) != generated_mechanical_signature(result.abilities[1])
    # Every variant rejects its own duplicate, even with a fresh ID/name/track.
    for choice in choices:
        duplicate = choice.model_copy(update={
            "ability_id": "other_gift", "display_name": "Quiet Gift",
            "track": ProgressionTrackId.INVESTIGATION,
        })
        with pytest.raises(ValueError, match="mechanically distinct"):
            generator._to_domain_generation(StarterAbilityProviderGeneration(
                first_ability=choice, second_ability=duplicate,
            ))


@pytest.mark.parametrize("field", ["valid_takeoff", "valid_landing"])
def test_authored_endpoint_declarations_are_required(field: str) -> None:
    raw = DEFAULT_WORLD.traversal_routes[0].model_dump()
    raw.pop(field)
    with pytest.raises(ValueError):
        TraversalRoute.model_validate(raw)


def test_levitation_descends_one_clear_authored_route() -> None:
    route = DEFAULT_WORLD.traversal_routes[0].model_copy(update={
        "route_id": "gallery_descent", "name": "Gallery Descent",
        "origin": "upper_gallery", "destination": "rain_court",
    })
    state = traversal_state(TraversalMethod.LEVITATION, "upper_gallery")
    updated, result = ToolExecutor(world=World(DEFAULT_WORLD.rooms, (route,))).execute(
        parsed_action=request("Gallery Descent"), campaign_state=json.dumps(state),
    )
    assert result.success and updated["player"]["location"] == "rain_court"
    assert derive_story_signal(result) == RoomEnteredSignal(room_id="rain_court")


def test_traversal_mechanics_are_finite_and_do_not_infect_legacy_primitives() -> None:
    mechanics = traversal_ability_generation(TraversalMethod.SUPERNATURAL_JUMP).abilities[0].mechanics.model_dump()
    for changes in (
        {"jump_reach_metres": 4}, {"jump_reach_metres": None},
        {"sense_filter": "presence"}, {"range": 1}, {"bypasses": ["gravity"]},
        {"traversal_method": "teleport"},
    ):
        with pytest.raises(ValueError):
            GeneratedAbilityMechanics.model_validate(mechanics | changes)
    old = starter_ability_generation().abilities[0].mechanics.model_dump()
    with pytest.raises(ValueError):
        GeneratedAbilityMechanics.model_validate(old | {"traversal_method": "levitation"})


def test_demo_ordinary_alternatives_and_special_routes_not_walkable() -> None:
    world = DEFAULT_WORLD
    court = world.resolve_exit("dining_room", "south")
    assert court is not None and court.id == "rain_court"
    for route in world.traversal_routes:
        assert not world.can_move(route.origin, route.route_id)
        assert not world.can_move(route.origin, route.name)
        assert world.can_move(route.origin, route.destination)
        assert world.can_move(route.destination, route.origin)
    assert world.can_move("grand_corridor", "library")
    assert world.can_move("library", "west")
    state = traversal_state(TraversalMethod.LEVITATION)
    action = ParsedAction(raw_text="walk Gallery Ascent", action=ActionType.MOVE, target="Gallery Ascent", parse_status="ok")
    updated, result = ToolExecutor().execute(parsed_action=action, campaign_state=json.dumps(state))
    assert not result.success and updated == state


def test_special_only_adjacency_not_available_to_walking_npcs_or_sensing() -> None:
    rooms = {
        "entry_hall": Room("entry_hall", "Entry Hall", "A room."),
        "library": Room("library", "Library", "A remote landing."),
    }
    route = DEFAULT_WORLD.traversal_routes[0].model_copy(update={"origin": "entry_hall", "destination": "library"})
    world = World(rooms, (route,))
    assert not world.can_move("entry_hall", "library")
    state = build_fresh_campaign_state()
    definitions = starter_ability_generation().abilities
    state["player"]["generated_abilities"] = [definition.model_dump(mode="json") for definition in definitions]
    for definition in definitions:
        unlock_ability(state, definition.ability_id)
    # A supernatural NPC in a special-route-only destination is not sensed.
    state["npcs"]["library_ghost"]["location"] = "library"
    _, sensed = ToolExecutor(world=world).execute(parsed_action=request("", "echo_sense").model_copy(update={"target": None}), campaign_state=json.dumps(state))
    assert sensed.ability_result is not None
    assert sensed.ability_result.presence_effect is not None
    assert sensed.ability_result.presence_effect.adjacent_room_count == 0
    state["npcs"]["library_ghost"]["location"] = "entry_hall"
    _, moved = WorldAuthorityExecutor(world=world).execute(
        action=MoveNpcWorldAction(npc_id="library_ghost", destination_room_id="library"),
        state=state,
    )
    assert not moved.success


@pytest.mark.parametrize(
    ("method", "target", "success"),
    [
        (TraversalMethod.LEVITATION, "Gallery Ascent", True),
        (TraversalMethod.LEVITATION, "Water Crossing", False),
        (TraversalMethod.SPIDER_CLIMB, "Ivy Wall", True),
        (TraversalMethod.SUPERNATURAL_JUMP, "Channel Gap", True),
        (TraversalMethod.WATER_WALKING, "Water Crossing", True),
    ],
)
def test_natural_language_model_grounding_reaches_real_executor(
    monkeypatch: pytest.MonkeyPatch, method: TraversalMethod, target: str, success: bool
) -> None:
    state = traversal_state(method)
    text = f"Could my Silver Step carry me along the {target}?"

    async def provider(**kwargs):
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK, target=target.upper().replace(" ", "_"),
            parameters=ActionParserParameters(ability_id="silver_step"),
            parse_status="ok", confidence=1,
        )

    monkeypatch.setattr("app.agents.action_parser.model_client.generate_structured", provider)
    parser = ActionParserAgent()
    context = parser._build_parser_context(json.dumps(state))
    assert len(context.traversal_routes) == 5
    assert context.available_exits != context.traversal_routes
    parsed = asyncio.run(parser.parse(message=text, campaign_state=json.dumps(state), recent_turns=[]))
    assert parsed.action == ActionType.ABILITY_CHECK and parsed.target == target
    updated, result = ToolExecutor().execute(parsed_action=parsed, campaign_state=json.dumps(state))
    assert result.success == success
    if not success:
        assert result.error_code == "route_incompatible" and updated == state


@pytest.mark.parametrize(
    ("text", "target", "accepted"),
    [
        ("Use Gallery Ascent", "Gallery Ascent", False),
        ("My ability Gallery Ascent crosses Gallery Ascent", "Gallery Ascent", True),
        ("Gallery Ascent crosses Gallery Ascent", "Gallery Ascent", False),
        ("My ability Gallery Ascent crosses Ivy Wall", "Ivy Wall", True),
        ("Gallery Ascent crosses Ivy Wall", "Ivy Wall", False),
        ("My ability Gallery Ascent crosses invented path", "invented path", False),
        ("My ability Gallery Ascent crosses Gallery Ascent then Gallery Ascent", "Gallery Ascent", True),
    ],
)
def test_route_ability_collision_is_occurrence_aware(text: str, target: str, accepted: bool) -> None:
    state = traversal_state(TraversalMethod.LEVITATION)
    state["player"]["generated_abilities"][0]["display_name"] = "Gallery Ascent"
    parser = ActionParserAgent()
    parsed = parser._validate_model_ability_request(
        request(target).model_copy(update={"raw_text": text}),
        parser._build_parser_context(json.dumps(state)),
    )
    assert (parsed.action == ActionType.ABILITY_CHECK) == accepted


def test_destination_ambiguity_stays_typed_and_routes_are_locally_projected() -> None:
    state = traversal_state(TraversalMethod.LEVITATION)
    parser = ActionParserAgent()
    parsed = parser._validate_model_ability_request(
        request("Upper Gallery"), parser._build_parser_context(json.dumps(state)),
    )
    assert parsed.action == ActionType.ABILITY_CHECK
    _, result = ToolExecutor().execute(parsed_action=parsed, campaign_state=json.dumps(state))
    assert result.error_code == "route_ambiguous"
    for location in ("entry_hall", "upper_gallery", "far_bank"):
        state["player"]["location"] = location
        assert parser._build_parser_context(json.dumps(state)).traversal_routes == []
        assert build_narrator_scene_context(json.dumps(state)).traversal_routes == []


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("Take Gallery Ascent using Water Crossing", False),
        ("Use my ability Gallery along Water Crossing", True),
        ("Upper Gallery, then Water Crossing", False),
        ("Use Gallery ability along Water Crossing", True),
    ],
)
def test_ability_reference_inside_route_or_destination_is_not_an_invocation(
    text: str, accepted: bool
) -> None:
    state = traversal_state(TraversalMethod.LEVITATION)
    state["player"]["generated_abilities"][0]["display_name"] = "Gallery"
    parser = ActionParserAgent()
    parsed = parser._validate_model_ability_request(
        request("Water Crossing").model_copy(update={"raw_text": text}),
        parser._build_parser_context(json.dumps(state)),
    )
    assert (parsed.action == ActionType.ABILITY_CHECK) == accepted
