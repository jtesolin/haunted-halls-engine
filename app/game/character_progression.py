"""Persistent character-progression domain state and deterministic helpers.

Phase 8B establishes the smallest coherent, deterministic character-growth
model needed to eventually power Phase 8C ability/check resolution. It is
intentionally independent of story/quest logic (owned by Phase 8A) and does
not integrate with the Director or `ChatOrchestrator` yet.

Design invariants:

* Character growth is authoritative deterministic game state; nothing here
  is randomized, and AI narration must never call these helpers directly to
  grant progression or abilities.
* Only the small, fixed set of `ProgressionTrackId` tracks may hold points.
  Arbitrary/unknown track ids are never created, whether from a caller
  request or from malformed persisted state.
* Progression is bounded; grants are clamped at an explicit cap rather than
  allowed to exceed it.
* Ability unlocks are idempotent: unlocking an already-owned ability is a
  successful no-op, not an error.
* Malformed persisted progression data (wrong types, out-of-range points,
  unknown tracks, non-string ability ids) is normalized down to safe
  defaults rather than trusted, so it can never escalate privileges.
* These helpers only ever read/write `state["player"]["progression"]`; they
  do not mutate room, item, NPC, clock, or fact state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.schemas.character_progression import (
    AbilityUnlockResult,
    ProgressionGrantResult,
    ProgressionTrackId,
)

# Current normalized progression document shape. Bumping this is reserved for
# a future breaking change to the persisted progression shape; today's
# normalization always re-derives this value rather than trusting a
# persisted one.
PROGRESSION_STATE_VERSION = 1

# Deterministic bounds shared by every current track. All tracks start at the
# same modest baseline with no unlocked advanced abilities.
MIN_TRACK_POINTS = 0
MAX_TRACK_POINTS = 10
DEFAULT_TRACK_POINTS = 0

PROGRESSION_TRACK_IDS: tuple[str, ...] = tuple(
    track_id.value for track_id in ProgressionTrackId
)


def default_character_progression_state() -> dict[str, Any]:
    """Build the deterministic default progression document."""
    return {
        "version": PROGRESSION_STATE_VERSION,
        "tracks": {track_id: DEFAULT_TRACK_POINTS for track_id in PROGRESSION_TRACK_IDS},
        "unlocked_abilities": [],
    }


def ensure_character_progression_state(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize `state["player"]["progression"]` in place and return it.

    Safe to call on brand-new state (no progression namespace yet), on
    already-normalized state (idempotent, no unrelated mutation), and on
    legacy/malformed persisted state (falls back to deterministic defaults
    for any field that cannot be trusted). Only `state["player"]` is
    touched; item/npc/clock/fact/world state is left untouched.
    """
    player = state.setdefault("player", {})
    if not isinstance(player, dict):
        player = {}
        state["player"] = player

    raw_progression = player.get("progression")
    normalized = _normalize_progression(raw_progression)
    player["progression"] = normalized
    return normalized


def grant_progress(
    state: dict[str, Any], track_id: str | ProgressionTrackId, amount: int
) -> ProgressionGrantResult:
    """Deterministically grant `amount` points to `track_id`.

    Rejects unknown track ids and non-positive/non-integer amounts without
    mutating any state. Valid grants are clamped at `MAX_TRACK_POINTS`; a
    grant that would exceed the cap still succeeds but reports `capped=True`
    and `changed=False` once the track is already at its cap.
    """
    if isinstance(track_id, ProgressionTrackId):
        resolved_track_id = track_id.value
    elif isinstance(track_id, str):
        resolved_track_id = track_id
    else:
        track_id_str = str(track_id)
        return ProgressionGrantResult(
            success=False,
            changed=False,
            track_id=track_id_str,
            prior_points=0,
            new_points=0,
            capped=False,
            error_code="unknown_track",
            reason=f"'{track_id_str}' is not a supported progression track.",
        )

    if resolved_track_id not in PROGRESSION_TRACK_IDS:
        return ProgressionGrantResult(
            success=False,
            changed=False,
            track_id=resolved_track_id,
            prior_points=0,
            new_points=0,
            capped=False,
            error_code="unknown_track",
            reason=f"'{resolved_track_id}' is not a supported progression track.",
        )

    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        prior_points = _read_normalized_progression(state)["tracks"][resolved_track_id]
        return ProgressionGrantResult(
            success=False,
            changed=False,
            track_id=resolved_track_id,
            prior_points=prior_points,
            new_points=prior_points,
            capped=False,
            error_code="invalid_amount",
            reason="Progression grants must be a positive integer amount.",
        )

    progression, normalization_changed = _normalize_progression_for_operation(state)
    tracks = progression["tracks"]
    prior_points = tracks[resolved_track_id]
    uncapped_points = prior_points + amount
    new_points = min(uncapped_points, MAX_TRACK_POINTS)
    capped = uncapped_points > MAX_TRACK_POINTS
    changed = normalization_changed or new_points != prior_points

    if changed:
        tracks[resolved_track_id] = new_points

    return ProgressionGrantResult(
        success=True,
        changed=changed,
        track_id=resolved_track_id,
        prior_points=prior_points,
        new_points=new_points,
        capped=capped,
        error_code=None,
        reason=None,
    )


def unlock_ability(state: dict[str, Any], ability_id: str) -> AbilityUnlockResult:
    """Deterministically record ownership of `ability_id`.

    Duplicate unlocks are idempotent successes. Invalid (non-string/blank)
    ability ids are rejected without mutation. Ability *resolution* rules
    belong to Phase 8C; this only tracks ownership metadata.
    """
    if not isinstance(ability_id, str) or ability_id.strip() != ability_id or not ability_id:
        ability_id_str = ability_id if isinstance(ability_id, str) else str(ability_id)
        return AbilityUnlockResult(
            success=False,
            changed=False,
            ability_id=ability_id_str,
            already_unlocked=False,
            error_code="invalid_ability_id",
            reason="Ability ids must be non-empty strings with no surrounding whitespace.",
        )

    progression, normalization_changed = _normalize_progression_for_operation(state)
    unlocked: list[str] = progression["unlocked_abilities"]

    if ability_id in unlocked:
        return AbilityUnlockResult(
            success=True,
            changed=normalization_changed,
            ability_id=ability_id,
            already_unlocked=True,
            error_code=None,
            reason=None,
        )

    unlocked.append(ability_id)
    return AbilityUnlockResult(
        success=True,
        changed=True,
        ability_id=ability_id,
        already_unlocked=False,
        error_code=None,
        reason=None,
    )


def read_character_progression_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized progression snapshot without mutating caller state."""
    player = state.get("player")
    raw_progression = player.get("progression") if isinstance(player, dict) else None
    normalized = _normalize_progression(raw_progression)
    return deepcopy(normalized)


def get_character_progression_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Alias for read-only normalized progression snapshots used by 8C."""
    return read_character_progression_state(state)


def _normalize_progression(raw_progression: Any) -> dict[str, Any]:
    raw_tracks = raw_progression.get("tracks") if isinstance(raw_progression, dict) else None
    tracks: dict[str, int] = {}
    for track_id in PROGRESSION_TRACK_IDS:
        raw_value = raw_tracks.get(track_id) if isinstance(raw_tracks, dict) else None
        tracks[track_id] = _normalize_track_points(raw_value)

    raw_abilities = (
        raw_progression.get("unlocked_abilities") if isinstance(raw_progression, dict) else None
    )
    unlocked_abilities = _normalize_unlocked_abilities(raw_abilities)

    return {
        "version": PROGRESSION_STATE_VERSION,
        "tracks": tracks,
        "unlocked_abilities": unlocked_abilities,
    }


def _read_normalized_progression(state: dict[str, Any]) -> dict[str, Any]:
    player = state.get("player")
    raw_progression = player.get("progression") if isinstance(player, dict) else None
    return _normalize_progression(raw_progression)


def _normalize_progression_for_operation(
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    player = state.get("player")
    raw_progression = player.get("progression") if isinstance(player, dict) else None
    progression = ensure_character_progression_state(state)
    return progression, raw_progression != progression


def _normalize_track_points(raw_value: Any) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        return DEFAULT_TRACK_POINTS
    if not MIN_TRACK_POINTS <= raw_value <= MAX_TRACK_POINTS:
        return DEFAULT_TRACK_POINTS
    return raw_value


def _normalize_unlocked_abilities(raw_abilities: Any) -> list[str]:
    if not isinstance(raw_abilities, list):
        return []
    normalized: list[str] = []
    for entry in raw_abilities:
        if not isinstance(entry, str) or entry.strip() != entry or not entry:
            continue
        if entry not in normalized:
            normalized.append(entry)
    return normalized
