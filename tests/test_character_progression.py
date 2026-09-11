from __future__ import annotations

import json

from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import (
    MAX_TRACK_POINTS,
    PROGRESSION_TRACK_IDS,
    default_character_progression_state,
    ensure_character_progression_state,
    grant_progress,
    unlock_ability,
)
from app.schemas.character import CharacterInfo, CharacterList


def test_default_character_progression_state_is_deterministic() -> None:
    first = default_character_progression_state()
    second = default_character_progression_state()

    assert first == second
    assert set(first["tracks"]) == {"investigation", "resolve", "rapport", "occult"}
    assert all(points == 0 for points in first["tracks"].values())
    assert first["unlocked_abilities"] == []


def test_legacy_state_with_no_progression_namespace_normalizes_safely() -> None:
    state = {"player": {"location": "entry_hall", "inventory": []}}

    progression = ensure_character_progression_state(state)

    assert progression == default_character_progression_state()
    # Unrelated player fields are preserved untouched.
    assert state["player"]["location"] == "entry_hall"
    assert state["player"]["inventory"] == []


def test_every_supported_track_has_stable_id_and_explicit_bounds() -> None:
    assert PROGRESSION_TRACK_IDS == ("investigation", "resolve", "rapport", "occult")
    state: dict = {}
    progression = ensure_character_progression_state(state)
    for track_id in PROGRESSION_TRACK_IDS:
        assert 0 <= progression["tracks"][track_id] <= MAX_TRACK_POINTS


def test_valid_progress_grant_updates_only_requested_track() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    result = grant_progress(state, "investigation", 3)

    assert result.success is True
    assert result.changed is True
    assert result.prior_points == 0
    assert result.new_points == 3
    assert result.capped is False
    progression = state["player"]["progression"]
    assert progression["tracks"]["investigation"] == 3
    assert progression["tracks"]["resolve"] == 0
    assert progression["tracks"]["rapport"] == 0
    assert progression["tracks"]["occult"] == 0


def test_negative_or_zero_amount_is_rejected_without_mutation() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    negative_result = grant_progress(state, "investigation", -1)
    zero_result = grant_progress(state, "investigation", 0)
    non_int_result = grant_progress(state, "investigation", 1.5)  # type: ignore[arg-type]

    assert negative_result.success is False
    assert negative_result.error_code == "invalid_amount"
    assert zero_result.success is False
    assert zero_result.error_code == "invalid_amount"
    assert non_int_result.success is False
    assert non_int_result.error_code == "invalid_amount"
    assert state["player"]["progression"]["tracks"]["investigation"] == 0


def test_unknown_track_id_is_rejected_without_mutation() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    result = grant_progress(state, "strength", 5)

    assert result.success is False
    assert result.error_code == "unknown_track"
    assert "strength" not in state["player"]["progression"]["tracks"]
    assert state["player"]["progression"] == default_character_progression_state()


def test_invalid_operations_on_legacy_state_do_not_create_progression() -> None:
    state = {"player": {"location": "entry_hall", "inventory": []}}
    state_before = json.dumps(state, sort_keys=True)

    invalid_track = grant_progress(state, ["investigation"], 1)  # type: ignore[arg-type]
    invalid_amount = grant_progress(state, "investigation", 0)
    invalid_ability = unlock_ability(state, "")

    assert invalid_track.error_code == "unknown_track"
    assert invalid_amount.error_code == "invalid_amount"
    assert invalid_amount.prior_points == 0
    assert invalid_ability.error_code == "invalid_ability_id"
    assert "progression" not in state["player"]
    assert json.dumps(state, sort_keys=True) == state_before


def test_clean_capped_grant_reports_no_change_and_leaves_state_unchanged() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    first = grant_progress(state, "occult", MAX_TRACK_POINTS)
    assert first.success is True
    assert first.new_points == MAX_TRACK_POINTS
    assert first.capped is False

    state_before = json.dumps(state, sort_keys=True)
    overflow = grant_progress(state, "occult", 5)

    assert overflow.success is True
    assert overflow.changed is False
    assert overflow.capped is True
    assert overflow.new_points == MAX_TRACK_POINTS
    assert state["player"]["progression"]["tracks"]["occult"] == MAX_TRACK_POINTS
    assert json.dumps(state, sort_keys=True) == state_before


def test_capped_grant_reports_normalization_repair_as_change() -> None:
    state = {
        "player": {
            "progression": {
                "version": 999,
                "tracks": {
                    "investigation": MAX_TRACK_POINTS,
                    "resolve": "malformed",
                    "rapport": 0,
                    "occult": 0,
                },
                "unlocked_abilities": [],
            }
        }
    }

    result = grant_progress(state, "investigation", 1)

    assert result.success is True
    assert result.capped is True
    assert result.changed is True
    assert result.prior_points == MAX_TRACK_POINTS
    assert result.new_points == MAX_TRACK_POINTS
    assert state["player"]["progression"] == {
        "version": 1,
        "tracks": {
            "investigation": MAX_TRACK_POINTS,
            "resolve": 0,
            "rapport": 0,
            "occult": 0,
        },
        "unlocked_abilities": [],
    }


def test_clean_duplicate_ability_unlock_reports_no_change() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    first = unlock_ability(state, "keen_eye")
    second = unlock_ability(state, "keen_eye")

    assert first.success is True
    assert first.changed is True
    assert first.already_unlocked is False
    assert second.success is True
    assert second.changed is False
    assert second.already_unlocked is True
    assert state["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]


def test_duplicate_ability_unlock_reports_normalization_repair_as_change() -> None:
    state = {
        "player": {
            "progression": {
                "version": 999,
                "tracks": {
                    "investigation": 0,
                    "resolve": 0,
                    "rapport": "malformed",
                    "occult": 0,
                },
                "unlocked_abilities": ["keen_eye", "keen_eye", 42],
            }
        }
    }

    result = unlock_ability(state, "keen_eye")

    assert result.success is True
    assert result.already_unlocked is True
    assert result.changed is True
    assert state["player"]["progression"] == {
        "version": 1,
        "tracks": {
            "investigation": 0,
            "resolve": 0,
            "rapport": 0,
            "occult": 0,
        },
        "unlocked_abilities": ["keen_eye"],
    }


def test_invalid_ability_id_is_rejected_without_mutation() -> None:
    state: dict = {}
    ensure_character_progression_state(state)

    blank_result = unlock_ability(state, "")
    padded_result = unlock_ability(state, " keen_eye ")
    non_string_result = unlock_ability(state, 123)  # type: ignore[arg-type]

    assert blank_result.success is False
    assert blank_result.error_code == "invalid_ability_id"
    assert padded_result.success is False
    assert padded_result.error_code == "invalid_ability_id"
    assert non_string_result.success is False
    assert non_string_result.error_code == "invalid_ability_id"
    assert state["player"]["progression"]["unlocked_abilities"] == []


def test_malformed_persisted_progression_does_not_grant_elevated_ranks_or_abilities() -> None:
    state = {
        "player": {
            "progression": {
                "version": 999,
                "tracks": {
                    "investigation": 99999,
                    "resolve": -5,
                    "rapport": "ten",
                    "occult": True,
                    "arcana": 10,
                },
                "unlocked_abilities": ["read_the_room", "read_the_room", "  padded  ", 42, ""],
            }
        }
    }

    progression = ensure_character_progression_state(state)

    assert progression["version"] == 1
    assert progression["tracks"]["investigation"] == 0
    assert progression["tracks"]["resolve"] == 0
    assert progression["tracks"]["rapport"] == 0
    assert progression["tracks"]["occult"] == 0
    assert "arcana" not in progression["tracks"]
    assert progression["unlocked_abilities"] == ["read_the_room"]


def test_json_serialization_round_trip_preserves_valid_progression() -> None:
    state: dict = {}
    ensure_character_progression_state(state)
    grant_progress(state, "rapport", 4)
    unlock_ability(state, "read_the_room")

    dumped = json.dumps(state)
    reloaded = json.loads(dumped)
    progression = ensure_character_progression_state(reloaded)

    assert progression["tracks"]["rapport"] == 4
    assert progression["unlocked_abilities"] == ["read_the_room"]
    assert progression == reloaded["player"]["progression"]


def test_progression_operations_do_not_mutate_unrelated_world_state() -> None:
    state = build_fresh_campaign_state()
    items_before = json.dumps(state["items"], sort_keys=True)
    npcs_before = json.dumps(state["npcs"], sort_keys=True)
    clock_before = json.dumps(state["clock"], sort_keys=True)
    facts_before = json.dumps(state["facts"], sort_keys=True)
    inventory_before = list(state["player"]["inventory"])
    location_before = state["player"]["location"]

    grant_progress(state, "investigation", 2)
    unlock_ability(state, "keen_eye")

    assert json.dumps(state["items"], sort_keys=True) == items_before
    assert json.dumps(state["npcs"], sort_keys=True) == npcs_before
    assert json.dumps(state["clock"], sort_keys=True) == clock_before
    assert json.dumps(state["facts"], sort_keys=True) == facts_before
    assert state["player"]["inventory"] == inventory_before
    assert state["player"]["location"] == location_before


def test_character_info_and_character_list_api_dtos_are_unaffected() -> None:
    info = CharacterInfo(character_id="pc-1", name="Investigator")
    listing = CharacterList(characters=[info])

    assert info.character_id == "pc-1"
    assert info.name == "Investigator"
    assert listing.characters == [info]
