"""Deterministic narrative world authority domain.

Clue definitions are immutable authored content. A clue becomes revealable
based on current authoritative story progression and is revealed only through
explicit world authority actions. Revealing a clue does not advance quest
progression—that remains driven by StorySignal processing alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.game.story import STORY_QUESTS, read_story_state_snapshot
from app.schemas.world import NARRATIVE_CLUE_ID_MAX_LENGTH


class InvalidNarrativeStateError(Exception):
    """Raised when persisted narrative state is present but malformed.

    This deliberately does not include the raw persisted payload in its
    message so that malformed/corrupted state is never leaked into logs or
    error responses.
    """


@dataclass(frozen=True)
class ClueDefinition:
    """Immutable canonical clue metadata."""

    clue_id: str
    text: str
    quest_id: str
    objective_id: str


def build_development_clues() -> dict[str, ClueDefinition]:
    """Development clue corpus."""
    ghost_points_to_old_book = ClueDefinition(
        clue_id="ghost_points_to_old_book",
        text="The library ghost's attention settles on the old book.",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )
    return {ghost_points_to_old_book.clue_id: ghost_points_to_old_book}


def validate_narrative_clue_definitions(
    clues: dict[str, ClueDefinition] | Iterable[ClueDefinition] | None = None,
) -> None:
    """Validate static clue identity, text, and quest/objective references.

    Raises immediately for programmer/configuration mistakes:
    - Empty or oversized clue IDs
    - Duplicate clue IDs
    - Empty or oversized clue text
    - Referenced quest not in STORY_QUESTS
    - Referenced objective not in the quest
    """
    if clues is None:
        clues = build_development_clues()

    # Normalize to iterable of ClueDefinition objects
    clue_list: list[ClueDefinition] = []
    if isinstance(clues, dict):
        clue_list = list(clues.values())
    else:
        # Handle iterable
        clue_list = list(clues)

    seen_ids: set[str] = set()
    for clue in clue_list:
        clue_id = clue.clue_id
        if not clue_id or not isinstance(clue_id, str):
            raise ValueError("Clue ID must be a non-empty string.")
        if len(clue_id) > NARRATIVE_CLUE_ID_MAX_LENGTH:
            raise ValueError(
                f"Clue ID exceeds {NARRATIVE_CLUE_ID_MAX_LENGTH} characters."
            )
        if clue_id in seen_ids:
            raise ValueError(f"Duplicate clue ID '{clue_id}'.")
        seen_ids.add(clue_id)

        if not clue.text or not isinstance(clue.text, str):
            raise ValueError(f"Clue '{clue_id}' text must be non-empty.")
        if len(clue.text) > 500:
            raise ValueError(f"Clue '{clue_id}' text exceeds 500 characters.")

        quest_id = clue.quest_id
        if quest_id not in STORY_QUESTS:
            raise ValueError(
                f"Clue '{clue_id}' references unknown quest '{quest_id}'."
            )

        quest = STORY_QUESTS[quest_id]
        objective_ids = {obj.id for obj in quest.objectives}
        if clue.objective_id not in objective_ids:
            raise ValueError(
                f"Clue '{clue_id}' references unknown objective "
                f"'{clue.objective_id}' in quest '{quest_id}'."
            )


NARRATIVE_CLUES: dict[str, ClueDefinition] = build_development_clues()
validate_narrative_clue_definitions(NARRATIVE_CLUES)


def _normalize_revealed_clues(persisted_revealed: Any) -> list[str]:
    """Safely normalize persisted revealed_clues, or raise if malformed."""
    if not isinstance(persisted_revealed, list):
        raise InvalidNarrativeStateError(
            "Persisted revealed_clues is not an array."
        )
    result: list[str] = []
    for item in persisted_revealed:
        if not isinstance(item, str):
            raise InvalidNarrativeStateError(
                "Persisted revealed_clues contains non-string entries."
            )
        if not item:
            raise InvalidNarrativeStateError(
                "Persisted revealed_clues contains empty clue IDs."
            )
        if len(item) > NARRATIVE_CLUE_ID_MAX_LENGTH:
            raise InvalidNarrativeStateError(
                "Persisted revealed_clues contains an oversized clue ID."
            )
        result.append(item)
    return result


def ensure_narrative_state(state: dict[str, Any]) -> dict[str, Any]:
    """Provide/normalize the `narrative` namespace in authoritative state.

    Preserves valid persisted revealed clue IDs (including unknown legacy IDs
    that may have been revealed in older versions). Malformed present namespace
    raises InvalidNarrativeStateError rather than silently repairing. Legacy
    campaigns with no `narrative` key are normalized to empty revealed_clues.
    """
    if "narrative" not in state:
        state["narrative"] = {"revealed_clues": []}
        return state["narrative"]

    raw_narrative = state["narrative"]
    if not isinstance(raw_narrative, dict):
        raise InvalidNarrativeStateError(
            "Persisted narrative state is not an object."
        )

    if "revealed_clues" not in raw_narrative:
        raise InvalidNarrativeStateError(
            "Persisted narrative state is missing revealed_clues."
        )

    normalized_revealed = _normalize_revealed_clues(
        raw_narrative["revealed_clues"]
    )

    state["narrative"] = {"revealed_clues": normalized_revealed}
    return state["narrative"]


def read_narrative_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized narrative snapshot without mutating caller state.

    Absent namespace -> empty revealed-clue list.
    Valid namespace -> copied normalized representation.
    Malformed present namespace -> raises InvalidNarrativeStateError.
    """
    working_state = deepcopy(state)
    narrative = ensure_narrative_state(working_state)
    return deepcopy(narrative)


def list_revealable_clues(state: dict[str, Any]) -> list[ClueDefinition]:
    """Return canonical clue definitions that are currently eligible and unrevealed.

    Requirements:
    - returns canonical clue definitions;
    - deterministic order (by clue_id);
    - only currently eligible clues;
    - excludes already revealed clues;
    - excludes unknown persisted clue IDs;
    - does not mutate authoritative state;
    - derives eligibility from authoritative story state.
    """
    # Safely read current narrative state
    narrative_snapshot = read_narrative_state_snapshot(state)
    revealed = set(narrative_snapshot["revealed_clues"])

    # Safely read current story state
    story_snapshot = read_story_state_snapshot(state)
    quests = story_snapshot["quests"]

    revealable: list[ClueDefinition] = []

    for clue in sorted(NARRATIVE_CLUES.values(), key=lambda c: c.clue_id):
        # Skip if already revealed
        if clue.clue_id in revealed:
            continue

        # Verify quest exists and is active
        quest_progress = quests.get(clue.quest_id)
        if not quest_progress or quest_progress["status"] != "active":
            continue

        # Verify objective is active
        objective_statuses = quest_progress.get("objectives", {})
        if objective_statuses.get(clue.objective_id) != "active":
            continue

        revealable.append(clue)

    return revealable
