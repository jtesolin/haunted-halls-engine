from __future__ import annotations

import json
from copy import deepcopy
from typing import MutableMapping, cast

from app.game.abilities import (
    ABILITY_REGISTRY,
    AbilityDefinition,
    CANONICAL_ABILITY_DEFINITIONS,
    MAX_CHECK_DIFFICULTY,
    MIN_CHECK_DIFFICULTY,
    VALIDATED_ABILITY_DEFINITIONS,
    evaluate_ability_availability,
    resolve_ability_check,
    validate_ability_definitions,
)
from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import (
    MAX_TRACK_POINTS,
    MIN_TRACK_POINTS,
    PROGRESSION_TRACK_IDS,
    default_character_progression_state,
    ensure_character_progression_state,
    grant_progress,
    read_character_progression_state,
    unlock_ability,
)
from app.schemas.abilities import AbilityAvailabilityStatus, AbilityCheckOutcome
from app.schemas.character import CharacterInfo, CharacterList
from app.schemas.character_progression import ProgressionTrackId


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
    state["story"] = {
        "active_quest": "find_the_missing_curator",
        "clues": ["torn_letter", {"location": "west_archive", "revealed": True}],
        "flags": {"curator_trusted": False},
    }
    items_before = json.dumps(state["items"], sort_keys=True)
    npcs_before = json.dumps(state["npcs"], sort_keys=True)
    clock_before = json.dumps(state["clock"], sort_keys=True)
    facts_before = json.dumps(state["facts"], sort_keys=True)
    story_before = json.dumps(state["story"], sort_keys=True)
    inventory_before = list(state["player"]["inventory"])
    location_before = state["player"]["location"]

    grant_progress(state, "investigation", 2)
    unlock_ability(state, "keen_eye")

    assert json.dumps(state["items"], sort_keys=True) == items_before
    assert json.dumps(state["npcs"], sort_keys=True) == npcs_before
    assert json.dumps(state["clock"], sort_keys=True) == clock_before
    assert json.dumps(state["facts"], sort_keys=True) == facts_before
    assert json.dumps(state["story"], sort_keys=True) == story_before
    assert state["player"]["inventory"] == inventory_before
    assert state["player"]["location"] == location_before


def test_character_info_and_character_list_api_dtos_are_unaffected() -> None:
    info = CharacterInfo(character_id="pc-1", name="Investigator")
    listing = CharacterList(characters=[info])

    assert info.character_id == "pc-1"
    assert info.name == "Investigator"
    assert listing.characters == [info]


def test_canonical_ability_definitions_validate_and_registry_is_stable() -> None:
    assert len(CANONICAL_ABILITY_DEFINITIONS) == 4
    assert [definition.ability_id for definition in CANONICAL_ABILITY_DEFINITIONS] == [
        "keen_eye",
        "steady_nerves",
        "read_the_room",
        "occult_insight",
    ]
    assert VALIDATED_ABILITY_DEFINITIONS == CANONICAL_ABILITY_DEFINITIONS
    assert {definition.ability_id: definition.track.value for definition in CANONICAL_ABILITY_DEFINITIONS} == {
        "keen_eye": "investigation",
        "steady_nerves": "resolve",
        "read_the_room": "rapport",
        "occult_insight": "occult",
    }


def test_ability_registry_is_read_only_and_cannot_change_availability() -> None:
    mutable_registry = cast(MutableMapping[str, AbilityDefinition], ABILITY_REGISTRY)

    try:
        mutable_registry["invented"] = CANONICAL_ABILITY_DEFINITIONS[0]
    except TypeError:
        pass
    else:
        raise AssertionError("ability registry should reject additions")

    try:
        del mutable_registry["keen_eye"]
    except TypeError:
        pass
    else:
        raise AssertionError("ability registry should reject deletions")

    assert tuple(ABILITY_REGISTRY) == (
        "keen_eye",
        "steady_nerves",
        "read_the_room",
        "occult_insight",
    )
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    grant_progress(state, "investigation", 2)
    unlock_ability(state, "keen_eye")
    assert evaluate_ability_availability(state, "keen_eye").available is True
    assert evaluate_ability_availability(state, "invented").status == (
        AbilityAvailabilityStatus.UNKNOWN_ABILITY
    )


def test_ability_definition_validation_rejects_duplicates_and_invalid_values() -> None:
    duplicate = [
        CANONICAL_ABILITY_DEFINITIONS[0],
        CANONICAL_ABILITY_DEFINITIONS[0],
    ]
    try:
        validate_ability_definitions(duplicate)
    except ValueError as exc:
        assert "Duplicate ability_id 'keen_eye'" in str(exc)
    else:
        raise AssertionError("duplicate ability ids should be rejected")

    invalid_track = [
        CANONICAL_ABILITY_DEFINITIONS[0],
        CANONICAL_ABILITY_DEFINITIONS[1],
        CANONICAL_ABILITY_DEFINITIONS[2],
        CANONICAL_ABILITY_DEFINITIONS[3].__class__(
            ability_id="bad_track",
            display_name="Bad Track",
            short_description="bad",
            track=cast(ProgressionTrackId, "not_a_track"),
            minimum_points=2,
        ),
    ]
    try:
        validate_ability_definitions(invalid_track)
    except ValueError as exc:
        assert "invalid track reference" in str(exc)
    else:
        raise AssertionError("invalid track refs should be rejected")

    below_min = [
        CANONICAL_ABILITY_DEFINITIONS[0].__class__(
            ability_id="low_min",
            display_name="Low Min",
            short_description="low",
            track=CANONICAL_ABILITY_DEFINITIONS[0].track,
            minimum_points=MIN_TRACK_POINTS - 1,
        )
    ]
    try:
        validate_ability_definitions(below_min)
    except ValueError as exc:
        assert "outside 0..10" in str(exc)
    else:
        raise AssertionError("minimum below bound should be rejected")

    above_max = [
        CANONICAL_ABILITY_DEFINITIONS[0].__class__(
            ability_id="high_min",
            display_name="High Min",
            short_description="high",
            track=CANONICAL_ABILITY_DEFINITIONS[0].track,
            minimum_points=MAX_TRACK_POINTS + 1,
        )
    ]
    try:
        validate_ability_definitions(above_max)
    except ValueError as exc:
        assert "outside 0..10" in str(exc)
    else:
        raise AssertionError("minimum above bound should be rejected")

    bool_min = [
        CANONICAL_ABILITY_DEFINITIONS[0].__class__(
            ability_id="bool_min",
            display_name="Bool min",
            short_description="bool",
            track=CANONICAL_ABILITY_DEFINITIONS[0].track,
            minimum_points=True,
        )
    ]
    try:
        validate_ability_definitions(bool_min)
    except ValueError as exc:
        assert "must be an integer" in str(exc)
    else:
        raise AssertionError("bool minimum should be rejected")


def test_check_difficulty_bounds_derive_from_progression_bounds() -> None:
    assert MIN_CHECK_DIFFICULTY == MIN_TRACK_POINTS
    assert MAX_CHECK_DIFFICULTY == MAX_TRACK_POINTS


def test_ability_definition_validation_rejects_whitespace_ability_ids() -> None:
    canonical = CANONICAL_ABILITY_DEFINITIONS[0]

    for bad_ability_id in ("", "   ", " keen_eye", "keen_eye ", " keen_eye "):
        tampered = [
            canonical.__class__(
                ability_id=bad_ability_id,
                display_name=canonical.display_name,
                short_description=canonical.short_description,
                track=canonical.track,
                minimum_points=canonical.minimum_points,
            )
        ]
        try:
            validate_ability_definitions(tampered)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"ability_id {bad_ability_id!r} should be rejected by validation"
            )

    # Canonical four IDs must still validate unchanged.
    validated = validate_ability_definitions(CANONICAL_ABILITY_DEFINITIONS)
    assert tuple(definition.ability_id for definition in validated) == (
        "keen_eye",
        "steady_nerves",
        "read_the_room",
        "occult_insight",
    )


def test_ability_availability_is_deterministic_and_read_only() -> None:
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    grant_progress(state, "investigation", 2)
    unlock_ability(state, "keen_eye")

    available = evaluate_ability_availability(state, "keen_eye")
    assert available.status == AbilityAvailabilityStatus.AVAILABLE
    assert available.available is True
    assert available.owned is True
    assert available.track_id is not None
    assert available.track_id.value == "investigation"
    assert available.track_points == 2
    assert available.minimum_points == 2

    locked = evaluate_ability_availability(state, "steady_nerves")
    assert locked.status == AbilityAvailabilityStatus.LOCKED
    assert locked.available is False
    assert locked.owned is False

    insufficient = deepcopy(state)
    ensure_character_progression_state(insufficient)
    insufficient["player"]["progression"]["tracks"]["investigation"] = 1
    insufficient["player"]["progression"]["unlocked_abilities"] = ["keen_eye"]
    insufficient_result = evaluate_ability_availability(insufficient, "keen_eye")
    assert insufficient_result.status == AbilityAvailabilityStatus.INSUFFICIENT_PROGRESSION
    assert insufficient_result.available is False

    unknown = evaluate_ability_availability(state, "not_a_real_ability")
    assert unknown.status == AbilityAvailabilityStatus.UNKNOWN_ABILITY
    assert unknown.owned is False
    assert unknown.available is False

    persisted_unknown = {
        "player": {
            "progression": {
                "version": 999,
                "tracks": {
                    "investigation": 5,
                    "resolve": 0,
                    "rapport": 0,
                    "occult": 0,
                },
                "unlocked_abilities": ["mystery_ability"],
            }
        }
    }
    normalized = read_character_progression_state(persisted_unknown)
    assert normalized["unlocked_abilities"] == ["mystery_ability"]
    assert evaluate_ability_availability(persisted_unknown, "mystery_ability").status == AbilityAvailabilityStatus.UNKNOWN_ABILITY

    malformed = {
        "player": {
            "progression": {
                "version": 999,
                "tracks": {
                    "investigation": 99999,
                    "resolve": -1,
                    "rapport": "three",
                    "occult": True,
                    "arcana": 7,
                },
                "unlocked_abilities": ["keen_eye", 42, "", "   ", "read_the_room"],
            }
        }
    }
    malformed_result = evaluate_ability_availability(malformed, "keen_eye")
    assert malformed_result.status == AbilityAvailabilityStatus.INSUFFICIENT_PROGRESSION
    assert malformed_result.track_points == 0
    assert malformed_result.minimum_points == 2

    repeated = evaluate_ability_availability(state, "keen_eye")
    assert repeated == available
    assert evaluate_ability_availability(state, "keen_eye") == repeated

    deep_state = build_fresh_campaign_state()
    deep_state["story"] = {
        "active_quest": "recover_the_map",
        "notes": ["first clue"],
        "flags": {"seen_cellar": True},
    }
    deep_state["items"]["talisman"] = {"name": "talisman", "value": 1}
    deep_state["npcs"]["guard"] = {"name": "guard", "mood": "alert"}
    deep_state["clock"]["tick"] = 5
    deep_state["facts"].append({"topic": "secret_passage", "resolved": False})
    deep_state["player"]["progression"] = {
        "version": 999,
        "tracks": {"investigation": 2, "resolve": 0, "rapport": 0, "occult": 0},
        "unlocked_abilities": ["keen_eye"],
    }
    before = deepcopy(deep_state)
    _ = evaluate_ability_availability(deep_state, "keen_eye")
    assert deep_state == before
    assert deep_state["story"] == before["story"]


def test_ability_check_resolution_is_deterministic_and_pure() -> None:
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    grant_progress(state, "investigation", 5)
    unlock_ability(state, "keen_eye")
    state["story"] = {
        "active_quest": "recover_the_map",
        "notes": ["first clue"],
        "flags": {"seen_cellar": True},
    }
    state["items"]["talisman"] = {"name": "talisman", "value": 1}
    state["npcs"]["guard"] = {"name": "guard", "mood": "alert"}
    state["clock"]["tick"] = 5
    state["facts"].append({"topic": "secret_passage", "resolved": False})
    state["player"]["unrelated_state"] = {"last_safe_room": "entry_hall"}

    success = resolve_ability_check(state, "keen_eye", 3)
    assert success.outcome == AbilityCheckOutcome.SUCCESS
    assert success.resolved is True
    assert success.success is True
    assert success.margin == 2

    tied = resolve_ability_check(state, "keen_eye", 5)
    assert tied.outcome == AbilityCheckOutcome.SUCCESS
    assert tied.margin == 0
    assert tied.success is True

    failed = resolve_ability_check(state, "keen_eye", 7)
    assert failed.outcome == AbilityCheckOutcome.FAILURE
    assert failed.resolved is True
    assert failed.success is False
    assert failed.margin == -2

    insufficient = build_fresh_campaign_state()
    ensure_character_progression_state(insufficient)
    grant_progress(insufficient, "investigation", 1)
    unlock_ability(insufficient, "keen_eye")
    unavailable = resolve_ability_check(insufficient, "keen_eye", 2)
    assert unavailable.outcome == AbilityCheckOutcome.UNAVAILABLE
    assert unavailable.resolved is False
    assert unavailable.success is None

    not_owned = resolve_ability_check(state, "steady_nerves", 2)
    assert not_owned.outcome == AbilityCheckOutcome.UNAVAILABLE
    assert not_owned.resolved is False
    assert not_owned.success is None

    unknown = resolve_ability_check(state, "unknown_ability", 2)
    assert unknown.outcome == AbilityCheckOutcome.UNKNOWN_ABILITY
    assert unknown.resolved is False
    assert unknown.success is None

    invalid_difficulty_cases = [
        True,
        1.5,
        "3",
        None,
        -1,
        MAX_CHECK_DIFFICULTY + 1,
    ]
    for bad_difficulty in invalid_difficulty_cases:
        result = resolve_ability_check(state, "keen_eye", bad_difficulty)  # type: ignore[arg-type]
        assert result.outcome == AbilityCheckOutcome.INVALID_DIFFICULTY
        assert result.resolved is False
        assert result.success is None

    assert resolve_ability_check(state, "keen_eye", 0).outcome == AbilityCheckOutcome.SUCCESS

    repeated = resolve_ability_check(state, "keen_eye", 3)
    assert repeated == success
    assert resolve_ability_check(state, "keen_eye", 3) == repeated

    before = deepcopy(state)
    result = resolve_ability_check(state, "keen_eye", 3)
    assert result == success
    assert state == before
