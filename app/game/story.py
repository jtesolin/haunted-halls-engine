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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


STORY_QUESTS: dict[str, QuestDefinition] = build_development_quests()


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
    completed-prefix / at-most-one-active / locked-suffix sequence, signaling
    that the caller must fall back to a fresh, safe default rather than trust
    (and potentially grant progression from) malformed data.
    """
    phase = "completed"
    any_completed = False
    any_active = False
    for objective in quest.objectives:
        status = objective_statuses.get(objective.id)
        if status not in _VALID_OBJECTIVE_STATUSES:
            return None
        if phase == "completed":
            if status == ObjectiveStatus.COMPLETED.value:
                any_completed = True
                continue
            if status == ObjectiveStatus.ACTIVE.value:
                any_active = True
                phase = "active"
                continue
            if status == ObjectiveStatus.LOCKED.value:
                phase = "locked"
                continue
            return None
        if phase == "active":
            if status == ObjectiveStatus.LOCKED.value:
                phase = "locked"
                continue
            return None
        if phase == "locked":
            if status != ObjectiveStatus.LOCKED.value:
                return None
            continue
    if not quest.objectives:
        return QuestStatus.INACTIVE.value
    if phase == "completed" and any_completed:
        return QuestStatus.COMPLETED.value
    if any_completed or any_active:
        return QuestStatus.ACTIVE.value
    return QuestStatus.INACTIVE.value


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
    story = ensure_story_state(state)
    coerced = _coerce_signal(signal)
    if coerced is None:
        return StoryProgressionResult(
            changed=False,
            outcome=StoryProgressionOutcome.INVALID_SIGNAL,
            reason="Signal payload is not a recognized typed story signal.",
        )

    incoming_type = _signal_type(coerced)
    incoming_value = _signal_match_value(coerced)

    quests: dict[str, Any] = story["quests"]
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
            if current_status == ObjectiveStatus.LOCKED.value:
                return _not_applicable(
                    "Signal matches a locked objective; prerequisites are not yet met."
                )
            # current_status == ACTIVE: this objective may advance.
            previous_quest_status = QuestStatus(progress["status"])
            objective_statuses[objective.id] = ObjectiveStatus.COMPLETED.value
            next_objective = _next_objective(quest, objective.order)
            if next_objective is None:
                progress["status"] = QuestStatus.COMPLETED.value
                return StoryProgressionResult(
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
            objective_statuses[next_objective.id] = ObjectiveStatus.ACTIVE.value
            progress["status"] = QuestStatus.ACTIVE.value
            return StoryProgressionResult(
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

    return _not_applicable("Signal does not match any known story objective.")


def _next_objective(
    quest: QuestDefinition, completed_order: int
) -> ObjectiveDefinition | None:
    for objective in quest.objectives:
        if objective.order == completed_order + 1:
            return objective
    return None
