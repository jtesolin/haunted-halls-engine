"""Deterministic story/quest domain model.

Core invariant: player prose is never evidence of quest progression. Callers
must derive typed `StorySignal` values from already-authoritative gameplay
outcomes (player movement, talk resolution, item acquisition, recorded
facts) and submit them through `apply_story_signal`. This module never reads
`ParsedAction.raw_text` or any other free-text field, never calls a model,
and never persists events itself; it only returns a structured
`StoryProgressionResult` describing what (if anything) changed so a future
caller can persist/event it.

Static quest/objective *definitions* are immutable content data. Per-campaign
mutable *progress* lives only in authoritative campaign state under the
`story` namespace, kept separate from definitions so definitions are never
duplicated into every save.

A progression trigger condition (`signal_type`, `match_value`) is unique across
the static story definition collection. This enables payload-level replay
idempotence without requiring event identity; richer repeated or shared
triggers require future authoritative event identity/consumption semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Iterable

from pydantic import ValidationError

from app.schemas.story import (
    FactRecordedSignal,
    ItemAcquiredSignal,
    NpcSpokenToSignal,
    ObjectiveStatus,
    QuestStatus,
    RoomEnteredSignal,
    StoryProgressionOutcome,
    StoryProgressionResult,
    StorySignal,
    StorySignalType,
)

_VALID_OBJECTIVE_STATUSES = {status.value for status in ObjectiveStatus}
_VALID_QUEST_STATUSES = {status.value for status in QuestStatus}


@dataclass(frozen=True)
class ObjectiveDefinition:
    id: str
    order: int
    description: str
    signal_type: StorySignalType
    match_value: str


@dataclass(frozen=True)
class QuestDefinition:
    id: str
    title: str
    description: str
    objectives: tuple[ObjectiveDefinition, ...] = field(default_factory=tuple)


def build_development_quests() -> dict[str, QuestDefinition]:
    """The Library's Whisper: the Phase 8A development quest arc.

    Reuses only existing canonical content: the `library` room, the
    `library_ghost` NPC, and the `old_book` item.
    """
    library_whisper = QuestDefinition(
        id="librarys_whisper",
        title="The Library's Whisper",
        description="Something in the library wants to be heard, and read.",
        objectives=(
            ObjectiveDefinition(
                id="enter_library",
                order=0,
                description="Enter the library.",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="library",
            ),
            ObjectiveDefinition(
                id="speak_to_library_ghost",
                order=1,
                description="Speak to the library ghost.",
                signal_type=StorySignalType.NPC_SPOKEN_TO,
                match_value="library_ghost",
            ),
            ObjectiveDefinition(
                id="acquire_old_book",
                order=2,
                description="Acquire the old book.",
                signal_type=StorySignalType.ITEM_ACQUIRED,
                match_value="old_book",
            ),
        ),
    )
    return {library_whisper.id: library_whisper}


def validate_story_definitions(
    quests: dict[str, QuestDefinition] | Iterable[QuestDefinition]
) -> None:
    """Validate that progression conditions are globally unique across quests.

    A progression trigger condition (`signal_type`, `match_value`) must be unique
    across all objectives in the static story definition collection. This enables
    payload-level replay idempotence without requiring event identity; richer
    repeated or shared triggers require future authoritative event identity/consumption
    semantics.
    """
    quest_list: list[tuple[str, QuestDefinition]] = []
    if isinstance(quests, Mapping):
        for q_id, q_def in quests.items():
            quest_list.append((str(q_id), q_def))
    else:
        for q_item in quests:
            if isinstance(q_item, QuestDefinition):
                quest_list.append((q_item.id, q_item))
    seen: dict[tuple[StorySignalType, str], tuple[str, str]] = {}
    for quest_id, quest in quest_list:
        for objective in quest.objectives:
            key = (objective.signal_type, objective.match_value)
            if key in seen:
                first_quest, first_obj = seen[key]
                raise ValueError(
                    f"Duplicate story progression condition {key} in objective "
                    f"'{objective.id}' of quest '{quest_id}' (already defined in "
                    f"objective '{first_obj}' of quest '{first_quest}')."
                )
            seen[key] = (quest_id, objective.id)


STORY_QUESTS: dict[str, QuestDefinition] = build_development_quests()
validate_story_definitions(STORY_QUESTS)


def _fresh_quest_progress(quest: QuestDefinition) -> dict[str, Any]:
    objectives: dict[str, str] = {}
    for objective in quest.objectives:
        objectives[objective.id] = (
            ObjectiveStatus.ACTIVE.value
            if objective.order == 0
            else ObjectiveStatus.LOCKED.value
        )
    return {
        "status": QuestStatus.ACTIVE.value if quest.objectives else QuestStatus.INACTIVE.value,
        "objectives": objectives,
    }


def _derive_quest_status(
    quest: QuestDefinition, objective_statuses: dict[str, str]
) -> str | None:
    """Validate the completed/active/locked sequence and derive quest status.

    Returns `None` when the persisted sequence is not a valid
    completed-prefix / exactly-one-active / locked-suffix sequence (or all
    locked / all completed), signaling that the caller must fall back to a fresh,
    safe default rather than trust (and potentially grant progression from or
    get permanently stuck on) malformed data.
    """
    if not quest.objectives:
        return QuestStatus.INACTIVE.value

    phase = "completed"
    completed_count = 0
    active_count = 0
    locked_count = 0

    for objective in quest.objectives:
        status = objective_statuses.get(objective.id)
        if status not in _VALID_OBJECTIVE_STATUSES:
            return None
        if phase == "completed":
            if status == ObjectiveStatus.COMPLETED.value:
                completed_count += 1
                continue
            if status == ObjectiveStatus.ACTIVE.value:
                active_count += 1
                phase = "active"
                continue
            if status == ObjectiveStatus.LOCKED.value:
                locked_count += 1
                phase = "locked"
                continue
            return None
        if phase == "active":
            if status == ObjectiveStatus.LOCKED.value:
                locked_count += 1
                phase = "locked"
                continue
            return None
        if phase == "locked":
            if status == ObjectiveStatus.LOCKED.value:
                locked_count += 1
                continue
            return None

    if completed_count == len(quest.objectives):
        return QuestStatus.COMPLETED.value
    if active_count == 1:
        return QuestStatus.ACTIVE.value
    if completed_count == 0 and active_count == 0 and locked_count == len(quest.objectives):
        return QuestStatus.INACTIVE.value
    return None


def _normalize_quest_progress(
    quest: QuestDefinition, persisted: Any
) -> dict[str, Any]:
    fresh = _fresh_quest_progress(quest)
    if not isinstance(persisted, dict):
        return fresh
    objectives_raw = persisted.get("objectives")
    if not isinstance(objectives_raw, dict):
        return fresh
    objective_statuses: dict[str, str] = {}
    for objective in quest.objectives:
        value = objectives_raw.get(objective.id)
        if not isinstance(value, str):
            return fresh
        objective_statuses[objective.id] = value
    derived_status = _derive_quest_status(quest, objective_statuses)
    if derived_status is None:
        return fresh
    persisted_status = persisted.get("status")
    if persisted_status not in _VALID_QUEST_STATUSES:
        return fresh
    if persisted_status != derived_status:
        return fresh
    return {"status": derived_status, "objectives": objective_statuses}


def ensure_story_state(state: dict[str, Any]) -> dict[str, Any]:
    """Provide/normalize the `story` progress namespace in authoritative state.

    Preserves valid persisted progress, safely resets any single quest whose
    persisted progress is malformed or internally inconsistent back to that
    quest's fresh starting progress (never granting completion from
    unreadable data), and does not touch any other campaign-state namespace.
    Legacy campaigns with no `story` key at all are normalized the same way.
    """
    raw_story = state.get("story")
    raw_quests = raw_story.get("quests") if isinstance(raw_story, dict) else None
    normalized_quests: dict[str, dict[str, Any]] = {}
    for quest_id, quest in STORY_QUESTS.items():
        persisted = raw_quests.get(quest_id) if isinstance(raw_quests, dict) else None
        normalized_quests[quest_id] = _normalize_quest_progress(quest, persisted)
    state["story"] = {"quests": normalized_quests}
    return state["story"]


def _coerce_signal(signal: StorySignal | dict[str, Any]) -> StorySignal | None:
    if isinstance(
        signal,
        (RoomEnteredSignal, NpcSpokenToSignal, ItemAcquiredSignal, FactRecordedSignal),
    ):
        return signal
    if not isinstance(signal, dict):
        return None
    signal_type = signal.get("signal_type")
    if not isinstance(signal_type, str):
        return None
    model_by_type = {
        StorySignalType.ROOM_ENTERED.value: RoomEnteredSignal,
        StorySignalType.NPC_SPOKEN_TO.value: NpcSpokenToSignal,
        StorySignalType.ITEM_ACQUIRED.value: ItemAcquiredSignal,
        StorySignalType.FACT_RECORDED.value: FactRecordedSignal,
    }
    model = model_by_type.get(signal_type)
    if model is None:
        return None
    try:
        return model.model_validate(signal)
    except ValidationError:
        return None


def _signal_match_value(signal: StorySignal) -> str:
    if isinstance(signal, RoomEnteredSignal):
        return signal.room_id
    if isinstance(signal, NpcSpokenToSignal):
        return signal.npc_id
    if isinstance(signal, ItemAcquiredSignal):
        return signal.item_id
    return signal.fact


def _signal_type(signal: StorySignal) -> StorySignalType:
    return signal.signal_type


def _not_applicable(reason: str) -> StoryProgressionResult:
    return StoryProgressionResult(
        changed=False,
        outcome=StoryProgressionOutcome.NOT_APPLICABLE,
        reason=reason,
    )


def apply_story_signal(
    state: dict[str, Any], signal: StorySignal | dict[str, Any]
) -> StoryProgressionResult:
    """Deterministically apply one typed progression signal to `state`.

    Advances only the single currently-active objective of the first
    not-yet-completed quest whose active objective matches the signal;
    completes the quest once its final objective completes; is idempotent
    for a signal matching an already-completed objective (no mutation,
    `ALREADY_SATISFIED`); and leaves state unchanged for signals that do not
    match any current or completed objective (`NOT_APPLICABLE`) or that fail
    to parse as a known typed signal (`INVALID_SIGNAL`).
    """
    coerced = _coerce_signal(signal)
    if coerced is None:
        return StoryProgressionResult(
            changed=False,
            outcome=StoryProgressionOutcome.INVALID_SIGNAL,
            reason="Signal payload is not a recognized typed story signal.",
        )

    working_state = {"story": deepcopy(state.get("story"))}
    story = ensure_story_state(working_state)
    incoming_type = _signal_type(coerced)
    incoming_value = _signal_match_value(coerced)

    quests: dict[str, Any] = story["quests"]
    completed_match: tuple[str, QuestDefinition, dict[str, Any], ObjectiveDefinition] | None = None
    locked_match: tuple[str, QuestDefinition, dict[str, Any], ObjectiveDefinition] | None = None
    active_match: tuple[str, QuestDefinition, dict[str, Any], ObjectiveDefinition] | None = None
    for quest_id, quest in STORY_QUESTS.items():
        progress = quests[quest_id]
        objective_statuses: dict[str, str] = progress["objectives"]
        for objective in quest.objectives:
            if objective.signal_type != incoming_type:
                continue
            if objective.match_value != incoming_value:
                continue
            current_status = objective_statuses[objective.id]
            if current_status == ObjectiveStatus.COMPLETED.value:
                if completed_match is None:
                    completed_match = (quest_id, quest, progress, objective)
            elif current_status == ObjectiveStatus.LOCKED.value:
                if locked_match is None:
                    locked_match = (quest_id, quest, progress, objective)
            elif active_match is None:
                active_match = (quest_id, quest, progress, objective)

    if active_match is not None:
        quest_id, quest, progress, objective = active_match
        previous_quest_status = QuestStatus(progress["status"])
        progress["objectives"][objective.id] = ObjectiveStatus.COMPLETED.value
        next_objective = _next_objective(quest, objective.order)
        if next_objective is None:
            progress["status"] = QuestStatus.COMPLETED.value
            result = StoryProgressionResult(
                changed=True,
                outcome=StoryProgressionOutcome.QUEST_COMPLETED,
                reason="Final objective completed; quest completed.",
                quest_id=quest_id,
                objective_id=objective.id,
                previous_objective_status=ObjectiveStatus.ACTIVE,
                new_objective_status=ObjectiveStatus.COMPLETED,
                previous_quest_status=previous_quest_status,
                new_quest_status=QuestStatus.COMPLETED,
            )
        else:
            progress["objectives"][next_objective.id] = ObjectiveStatus.ACTIVE.value
            progress["status"] = QuestStatus.ACTIVE.value
            result = StoryProgressionResult(
                changed=True,
                outcome=StoryProgressionOutcome.OBJECTIVE_ADVANCED,
                reason="Objective completed; next objective activated.",
                quest_id=quest_id,
                objective_id=objective.id,
                previous_objective_status=ObjectiveStatus.ACTIVE,
                new_objective_status=ObjectiveStatus.COMPLETED,
                previous_quest_status=previous_quest_status,
                new_quest_status=QuestStatus.ACTIVE,
            )
        state["story"] = story
        return result

    if completed_match is not None:
        quest_id, _, progress, objective = completed_match
        return StoryProgressionResult(
            changed=False,
            outcome=StoryProgressionOutcome.ALREADY_SATISFIED,
            reason="Signal matches an already-completed objective.",
            quest_id=quest_id,
            objective_id=objective.id,
            previous_objective_status=ObjectiveStatus.COMPLETED,
            new_objective_status=ObjectiveStatus.COMPLETED,
            previous_quest_status=QuestStatus(progress["status"]),
            new_quest_status=QuestStatus(progress["status"]),
        )

    if locked_match is not None:
        return _not_applicable(
            "Signal matches a locked objective; prerequisites are not yet met."
        )

    return _not_applicable("Signal does not match any known story objective.")


def _next_objective(
    quest: QuestDefinition, completed_order: int
) -> ObjectiveDefinition | None:
    for objective in quest.objectives:
        if objective.order == completed_order + 1:
            return objective
    return None
