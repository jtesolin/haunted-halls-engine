"""Tests for deterministic narrative world authority domain."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.game.campaign_state import build_fresh_campaign_state
from app.game.narrative import (
    ClueDefinition,
    InvalidNarrativeStateError,
    NARRATIVE_CLUE_ID_MAX_LENGTH,
    NARRATIVE_CLUES,
    ensure_narrative_state,
    list_revealable_clues,
    read_narrative_state_snapshot,
    validate_narrative_clue_definitions,
)
from app.game.story import apply_story_signal
from app.schemas.story import NpcSpokenToSignal, RoomEnteredSignal


def test_canonical_clue_definitions_validate() -> None:
    """Canonical clue definitions pass validation."""
    validate_narrative_clue_definitions(NARRATIVE_CLUES)


def test_duplicate_clue_ids_reject() -> None:
    """Duplicate clue IDs raise during validation."""
    clue1 = ClueDefinition(
        clue_id="duplicate_clue",
        text="First clue.",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )
    clue2 = ClueDefinition(
        clue_id="duplicate_clue",
        text="Second clue.",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )
    # Pass as iterable to ensure duplicates are detected
    clues_list = [clue1, clue2]

    with pytest.raises(ValueError, match="Duplicate clue ID"):
        validate_narrative_clue_definitions(clues_list)


def test_missing_quest_rejects() -> None:
    """Clue referencing unknown quest raises during validation."""
    clue = ClueDefinition(
        clue_id="test_clue",
        text="Test clue.",
        quest_id="unknown_quest",
        objective_id="acquire_old_book",
    )

    with pytest.raises(ValueError, match="unknown quest"):
        validate_narrative_clue_definitions({"test_clue": clue})


def test_missing_objective_rejects() -> None:
    """Clue referencing unknown objective raises during validation."""
    clue = ClueDefinition(
        clue_id="test_clue",
        text="Test clue.",
        quest_id="librarys_whisper",
        objective_id="unknown_objective",
    )

    with pytest.raises(ValueError, match="unknown objective"):
        validate_narrative_clue_definitions({"test_clue": clue})


def test_blank_clue_text_rejects() -> None:
    """Clue with blank text raises during validation."""
    clue = ClueDefinition(
        clue_id="test_clue",
        text="",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )

    with pytest.raises(ValueError, match="text must be non-empty"):
        validate_narrative_clue_definitions({"test_clue": clue})


def test_oversized_clue_text_rejects() -> None:
    """Clue with text exceeding 500 characters raises during validation."""
    clue = ClueDefinition(
        clue_id="test_clue",
        text="x" * 501,
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )

    with pytest.raises(ValueError, match="exceeds 500 characters"):
        validate_narrative_clue_definitions({"test_clue": clue})


def test_oversized_clue_id_rejects() -> None:
    """Clue ID must observe the shared narrative identifier bound."""
    clue = ClueDefinition(
        clue_id="x" * (NARRATIVE_CLUE_ID_MAX_LENGTH + 1),
        text="Test clue.",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
    )

    with pytest.raises(ValueError, match="Clue ID exceeds"):
        validate_narrative_clue_definitions({"test_clue": clue})


def test_legacy_state_without_narrative_namespace_reads_empty() -> None:
    """Legacy campaign state without narrative namespace normalizes to empty clues."""
    state = {"player": {"location": "entry_hall", "inventory": []}}
    narrative = ensure_narrative_state(state)

    assert narrative == {"revealed_clues": []}
    assert state["narrative"] == {"revealed_clues": []}


@pytest.mark.parametrize(
    "narrative_state",
    [
        None,
        {},
        {"revealed_clues": [""]},
        {"revealed_clues": ["x" * (NARRATIVE_CLUE_ID_MAX_LENGTH + 1)]},
    ],
    ids=[
        "null-narrative",
        "missing-revealed-clues",
        "empty-clue-id",
        "oversized-clue-id",
    ],
)
def test_present_malformed_narrative_state_is_not_repaired(
    narrative_state: Any,
) -> None:
    """Present malformed state raises without discarding its persisted value."""
    state = {"narrative": narrative_state}
    original = copy.deepcopy(state)

    with pytest.raises(InvalidNarrativeStateError):
        ensure_narrative_state(state)

    assert state == original


def test_read_snapshot_does_not_mutate_caller() -> None:
    """read_narrative_state_snapshot does not mutate supplied state."""
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    snapshot1 = read_narrative_state_snapshot(state)
    snapshot2 = read_narrative_state_snapshot(state)

    assert state == original
    assert snapshot1 == snapshot2


def test_read_snapshot_preserves_absent_vs_null_narrative_state() -> None:
    """Snapshot treats absent legacy state as empty and present null as malformed."""
    absent_state: dict[str, Any] = {}
    null_state = {"narrative": None}

    assert read_narrative_state_snapshot(absent_state) == {"revealed_clues": []}
    assert "narrative" not in absent_state
    with pytest.raises(InvalidNarrativeStateError):
        read_narrative_state_snapshot(null_state)
    assert null_state == {"narrative": None}


def test_malformed_present_narrative_namespace_raises_safely() -> None:
    """Malformed present narrative namespace raises InvalidNarrativeStateError safely."""
    malformed_states = [
        {"narrative": []},  # narrative is list, not dict
        {"narrative": "broken"},  # narrative is string, not dict
        {"narrative": None},  # narrative is explicitly null
        {"narrative": {}},  # revealed_clues is missing
        {"narrative": {"revealed_clues": {}}},  # revealed_clues is dict, not list
        {"narrative": {"revealed_clues": "foo"}},  # revealed_clues is string, not list
        {"narrative": {"revealed_clues": [""]}},  # clue ID is empty
    ]

    for state in malformed_states:
        with pytest.raises(InvalidNarrativeStateError):
            ensure_narrative_state(state)


def test_revealable_clue_ordering_is_deterministic() -> None:
    """revealable-clue ordering is deterministic by clue_id."""
    state = build_fresh_campaign_state()

    revealable1 = list_revealable_clues(state)
    revealable2 = list_revealable_clues(state)

    assert revealable1 == revealable2
    # Verify sorted by clue_id
    clue_ids = [c.clue_id for c in revealable1]
    assert clue_ids == sorted(clue_ids)


def test_initial_library_whisper_state_does_not_reveal_ghost_points_to_old_book() -> None:
    """At initial state, ghost_points_to_old_book is not revealable."""
    state = build_fresh_campaign_state()
    revealable = list_revealable_clues(state)

    revealable_ids = [c.clue_id for c in revealable]
    assert "ghost_points_to_old_book" not in revealable_ids


def test_real_story_progression_makes_ghost_points_to_old_book_revealable() -> None:
    """After entering library and speaking to ghost, acquire_old_book becomes active."""
    state = build_fresh_campaign_state()

    # Before progression
    revealable_before = list_revealable_clues(state)
    revealable_ids_before = [c.clue_id for c in revealable_before]
    assert "ghost_points_to_old_book" not in revealable_ids_before

    # Apply story progression
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))

    # After progression
    revealable_after = list_revealable_clues(state)
    revealable_ids_after = [c.clue_id for c in revealable_after]
    assert "ghost_points_to_old_book" in revealable_ids_after


def test_persisted_revealed_clue_not_revealable_again() -> None:
    """After persisted reveal, clue is not revealable again."""
    state = build_fresh_campaign_state()

    # Progress story to make clue revealable
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))

    # Manually set revealed clue
    if "narrative" not in state:
        state["narrative"] = {"revealed_clues": []}
    state["narrative"]["revealed_clues"] = ["ghost_points_to_old_book"]

    # Verify not revealable
    revealable = list_revealable_clues(state)
    revealable_ids = [c.clue_id for c in revealable]
    assert "ghost_points_to_old_book" not in revealable_ids
