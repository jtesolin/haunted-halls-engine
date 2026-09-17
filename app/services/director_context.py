from __future__ import annotations

from typing import Any

from app.game.abilities import VALIDATED_ABILITY_DEFINITIONS, evaluate_ability_availability
from app.game.character_progression import PROGRESSION_TRACK_IDS, read_character_progression_state
from app.game.story import STORY_QUESTS, read_story_state_snapshot
from app.game.world import DEFAULT_WORLD, World
from app.schemas.abilities import AbilityAvailabilityStatus
from app.schemas.chat import ParsedAction, ToolExecutionResult
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.director import (
    DirectorAbilityContext,
    DirectorCharacterContext,
    DirectorInput,
    DirectorNPCContext,
    DirectorPlayerActionContext,
    DirectorProgressionTrackContext,
    DirectorStoryContext,
    DirectorStoryObjectiveContext,
    DirectorStoryQuestContext,
)
from app.schemas.story import ObjectiveStatus, QuestStatus


class InvalidDirectorContextError(ValueError):
    """Raised when authoritative state cannot be projected for the Director."""


def build_director_input(
    state: dict[str, Any],
    *,
    parsed_action: ParsedAction,
    tool_result: ToolExecutionResult,
    world: World = DEFAULT_WORLD,
) -> DirectorInput:
    """Build a deterministic, non-mutating Director projection."""
    if not isinstance(state, dict):
        raise InvalidDirectorContextError("Authoritative campaign state is malformed.")

    player = state.get("player")
    if not isinstance(player, dict):
        raise InvalidDirectorContextError("Authoritative player state is malformed.")
    player_location = player.get("location")
    if not isinstance(player_location, str) or world.get_room(player_location) is None:
        raise InvalidDirectorContextError("Authoritative player location is invalid.")

    clock = state.get("clock")
    if not isinstance(clock, dict):
        raise InvalidDirectorContextError("Authoritative clock state is malformed.")
    clock_tick = clock.get("tick")
    if isinstance(clock_tick, bool) or not isinstance(clock_tick, int) or clock_tick < 0:
        raise InvalidDirectorContextError("Authoritative clock tick is invalid.")

    facts = state.get("facts")
    if not isinstance(facts, list) or any(not isinstance(fact, str) for fact in facts):
        raise InvalidDirectorContextError("Authoritative facts state is malformed.")

    npcs = state.get("npcs")
    if not isinstance(npcs, dict):
        raise InvalidDirectorContextError("Authoritative NPC state is malformed.")

    npc_ids = list(npcs)
    if any(not isinstance(npc_id, str) or not npc_id for npc_id in npc_ids):
        raise InvalidDirectorContextError("Authoritative NPC identity is malformed.")

    npc_contexts: list[DirectorNPCContext] = []
    for npc_id in sorted(npc_ids):
        npc = npcs[npc_id]
        if not isinstance(npc, dict):
            raise InvalidDirectorContextError(
                f"Authoritative NPC '{npc_id}' entry is malformed."
            )
        location = npc.get("location")
        status = npc.get("status")
        if not isinstance(location, str) or world.get_room(location) is None:
            raise InvalidDirectorContextError(
                f"Authoritative NPC '{npc_id}' location is invalid."
            )
        if status not in {"active", "absent"}:
            raise InvalidDirectorContextError(
                f"Authoritative NPC '{npc_id}' status is invalid."
            )
        destinations = [
            exit_data["room_id"]
            for exit_data in world.available_exits(location)
        ]
        npc_contexts.append(
            DirectorNPCContext(
                npc_id=npc_id,
                location_id=location,
                status=status,
                one_hop_destination_room_ids=destinations,
            )
        )

    return DirectorInput(
        current_player_room_id=player_location,
        clock_tick=clock_tick,
        facts=list(facts),
        npcs=npc_contexts,
        player_action=DirectorPlayerActionContext(
            action=parsed_action.action,
            target=parsed_action.target,
            parse_status=parsed_action.parse_status,
            succeeded=tool_result.success,
            result_summary=tool_result.summary,
            applied_tools=list(tool_result.applied_tools),
            error_code=tool_result.error_code,
        ),
        story=_build_story_context(state),
        character=_build_character_context(state),
    )


def _build_story_context(state: dict[str, Any]) -> DirectorStoryContext:
    story = read_story_state_snapshot(state)
    raw_quests = story.get("quests")
    quests = raw_quests if isinstance(raw_quests, dict) else {}
    quest_contexts: list[DirectorStoryQuestContext] = []

    for quest_id, quest_definition in STORY_QUESTS.items():
        progress = quests.get(quest_id)
        if not isinstance(progress, dict):
            continue
        raw_objectives = progress.get("objectives")
        objectives = raw_objectives if isinstance(raw_objectives, dict) else {}
        status = QuestStatus(progress["status"])
        completed_objective_ids = [
            objective.id
            for objective in quest_definition.objectives
            if objectives.get(objective.id) == ObjectiveStatus.COMPLETED.value
        ]
        active_objective = None
        if status != QuestStatus.COMPLETED:
            for objective in quest_definition.objectives:
                if objectives.get(objective.id) == ObjectiveStatus.ACTIVE.value:
                    active_objective = DirectorStoryObjectiveContext(
                        objective_id=objective.id,
                        description=objective.description,
                    )
                    break
        quest_contexts.append(
            DirectorStoryQuestContext(
                quest_id=quest_id,
                title=quest_definition.title,
                status=status,
                completed_objective_ids=completed_objective_ids,
                active_objective=active_objective,
            )
        )

    return DirectorStoryContext(quests=quest_contexts)


def _build_character_context(state: dict[str, Any]) -> DirectorCharacterContext:
    progression = read_character_progression_state(state)
    tracks = progression["tracks"]
    progression_tracks = [
        DirectorProgressionTrackContext(
            track_id=ProgressionTrackId(track_id),
            points=tracks[track_id],
        )
        for track_id in PROGRESSION_TRACK_IDS
    ]

    available_abilities: list[DirectorAbilityContext] = []
    for definition in VALIDATED_ABILITY_DEFINITIONS:
        availability = evaluate_ability_availability(state, definition.ability_id)
        if availability.status != AbilityAvailabilityStatus.AVAILABLE:
            continue
        available_abilities.append(
            DirectorAbilityContext(
                ability_id=definition.ability_id,
                display_name=definition.display_name,
                short_description=definition.short_description,
                track_id=definition.track,
            )
        )

    return DirectorCharacterContext(
        progression_tracks=progression_tracks,
        available_abilities=available_abilities,
    )
