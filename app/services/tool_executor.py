from __future__ import annotations

import copy
import json
import logging
from typing import Any

from app.game.items import (
    PLAYER_INVENTORY_LOCATION,
    available_items_for_room,
    default_items_state,
    ensure_items_state,
    inventory_item_ids,
    random_starting_inventory_items,
    resolve_item_ids,
    room_location,
    sync_inventory_projection,
)
from app.core.config import settings
from app.game.world import DEFAULT_WORLD, World
from app.schemas.chat import ActionType, ParsedAction, ToolExecutionResult
from app.tools.mcp_client import build_mcp_client
from app.tools.registry import RegistryTransportError, ToolRegistry

DEFAULT_CAMPAIGN_STATE: dict[str, Any] = {
    "player": {
        "location": "entry_hall",
        "inventory": [],
    },
    "items": default_items_state(),
    "npcs": {},
    "clock": {
        "tick": 0,
    },
    "facts": [],
}


logger = logging.getLogger(__name__)


class InvalidCampaignStateError(ValueError):
    """Raised when persisted campaign state is present but cannot be safely loaded."""


class ToolExecutor:
    def __init__(self, registry: ToolRegistry | None = None, world: World | None = None) -> None:
        self.registry = registry or self._build_registry()
        self.world = world or DEFAULT_WORLD

    def _build_registry(self) -> ToolRegistry:
        mcp_client = build_mcp_client() if settings.TOOL_REGISTRY_TRANSPORT in {"mcp", "hybrid"} else None
        registry = ToolRegistry(mode=settings.TOOL_REGISTRY_TRANSPORT, mcp_client=mcp_client)
        registry.register("move_player", self.move_player)
        registry.register("take_item", self.take_item)
        registry.register("drop_item", self.drop_item)
        registry.register("spawn_npc", self.spawn_npc)
        registry.register("advance_clock", self.advance_clock)
        registry.register("record_fact", self.record_fact)
        registry.register_mcp("spawn_npc", "create_npc")
        registry.register_mcp("advance_clock", "advance_time")
        registry.register_mcp("record_fact", "search_lore")
        return registry

    def execute(self, *, parsed_action: ParsedAction, campaign_state: str) -> tuple[dict[str, Any], ToolExecutionResult]:
        state = self._state_from_text(campaign_state)
        previous_state = copy.deepcopy(state)

        action = parsed_action.action
        target = parsed_action.target

        result = ToolExecutionResult(
            success=False,
            summary="No executable tool matched this action.",
        )

        if parsed_action.parse_status != "ok":
            result.summary = "Action parse was not confident enough for tool execution."
            return state, result

        if action in {ActionType.MOVE, ActionType.CLIMB}:
            requested_target = target or parsed_action.parameters.get("direction") or parsed_action.parameters.get("destination") or ""
            movement_result, dispatch_error = self._dispatch_tool_with_value(
                "move_player",
                state,
                str(requested_target),
            )
            if dispatch_error is not None:
                return state, dispatch_error
            if isinstance(movement_result, ToolExecutionResult):
                result = movement_result
            else:
                result = ToolExecutionResult(
                    success=False,
                    summary="Movement result was not structured correctly.",
                    errors=["invalid_movement_result"],
                )

        elif action == ActionType.TAKE:
            item = target or parsed_action.parameters.get("item")
            result = self.take_item(state, item)

        elif action == ActionType.DROP:
            item = target or parsed_action.parameters.get("item")
            result = self.drop_item(state, item)

        elif action == ActionType.OBSERVE:
            result = self.observe(state)

        elif action == ActionType.WAIT:
            amount = parsed_action.parameters.get("amount", 1)
            try:
                ticks = max(1, int(amount))
            except (TypeError, ValueError):
                ticks = 1
            dispatch_error = self._dispatch_tool("advance_clock", state, ticks)
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["advance_clock"],
                summary=f"Advanced clock by {ticks} tick(s).",
            )

        if state != previous_state and not result.state_delta:
            result.state_delta = self._compute_state_delta(previous_state, state)
        return state, result

    def observe(self, state: dict[str, Any]) -> ToolExecutionResult:
        player = state.setdefault("player", {})
        current_location = player.get("location")
        room = self.world.get_room(current_location) if isinstance(current_location, str) else None

        if room is None:
            return ToolExecutionResult(
                success=False,
                summary="Player location is not set to a valid room.",
                errors=["invalid_current_location"],
                error_code="invalid_current_location",
            )

        items = ensure_items_state(state)
        return ToolExecutionResult(
            success=True,
            applied_tools=["observe"],
            summary=f"You take stock of {room.name}.",
            current_location=room.id,
            current_room_name=room.name,
            current_room_description=room.description,
            available_exits=self.world.available_exits(room.id),
            available_items=available_items_for_room(items, room.id),
            inventory_items=inventory_item_ids(items),
        )

    def move_player(self, state: dict[str, Any], requested_target: str) -> ToolExecutionResult:
        player = state.setdefault("player", {})
        previous_location = player.get("location")
        previous_room = self.world.get_room(previous_location) if isinstance(previous_location, str) else None

        if not isinstance(previous_location, str) or previous_room is None:
            return ToolExecutionResult(
                success=False,
                summary="Player location is not set to a valid room.",
                errors=["invalid_exit", "reason:invalid_current_location"],
                error_code="invalid_exit",
                previous_location=previous_location if isinstance(previous_location, str) else None,
                current_location=previous_location if isinstance(previous_location, str) else None,
                requested_target=requested_target or None,
                previous_room_name=previous_room.name if previous_room is not None else None,
                current_room_name=previous_room.name if previous_room is not None else None,
                current_room_description=previous_room.description if previous_room is not None else None,
                available_exits=self.world.available_exits(previous_location) if isinstance(previous_location, str) else [],
            )

        destination = self.world.resolve_exit(previous_location, requested_target)
        items = ensure_items_state(state)
        if destination is None:
            available_exits = self.world.available_exits(previous_location)
            return ToolExecutionResult(
                success=False,
                summary=f"No exit from {previous_room.name} leads to {requested_target or 'that destination'}.",
                errors=[
                    "invalid_exit",
                    f"requested_target:{requested_target or ''}",
                    f"current_room:{previous_room.id}",
                ],
                error_code="invalid_exit",
                previous_location=previous_location,
                current_location=previous_location,
                requested_target=requested_target or None,
                previous_room_name=previous_room.name,
                current_room_name=previous_room.name,
                current_room_description=previous_room.description,
                available_exits=available_exits,
                available_items=available_items_for_room(items, previous_location),
            )

        player["location"] = destination.id
        available_exits = self.world.available_exits(destination.id)
        return ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary=f"Moved from {previous_room.name} to {destination.name}.",
            state_delta={
                "player": {
                    "location": {
                        "from": previous_location,
                        "to": destination.id,
                    }
                }
            },
            previous_location=previous_location,
            current_location=destination.id,
            requested_target=requested_target or None,
            resolved_exit=destination.id,
            previous_room_name=previous_room.name,
            current_room_name=destination.name,
            current_room_description=destination.description,
            available_exits=available_exits,
            available_items=available_items_for_room(items, destination.id),
        )

    def take_item(self, state: dict[str, Any], requested_target: str | None) -> ToolExecutionResult:
        items = ensure_items_state(state)
        player = state.setdefault("player", {})
        current_room = player.get("location")

        if not isinstance(current_room, str) or self.world.get_room(current_room) is None:
            return ToolExecutionResult(
                success=False,
                summary="Player location is not set to a valid room.",
                errors=["invalid_current_location"],
                error_code="invalid_current_location",
                requested_target=requested_target,
            )

        matches, ambiguous = self._resolve_item_in_scope(
            items, requested_target, room_location(current_room)
        )
        if ambiguous:
            return ToolExecutionResult(
                success=False,
                summary=f"'{requested_target or ''}' could match multiple items.",
                errors=["ambiguous_item"],
                error_code="ambiguous_item",
                requested_target=requested_target,
                current_location=current_room,
                available_items=available_items_for_room(items, current_room),
            )
        if not matches:
            return ToolExecutionResult(
                success=False,
                summary=f"No item matched '{requested_target or ''}'.",
                errors=["item_not_found"],
                error_code="item_not_found",
                requested_target=requested_target,
                current_location=current_room,
                available_items=available_items_for_room(items, current_room),
            )

        item_id = matches[0]
        item = items[item_id]
        current_location = item.get("location")
        if current_location == PLAYER_INVENTORY_LOCATION:
            return ToolExecutionResult(
                success=False,
                summary=f"{item.get('name', item_id)} is already in your inventory.",
                errors=["item_already_owned"],
                error_code="item_already_owned",
                requested_target=requested_target,
                item_id=item_id,
                item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
                current_location=current_room,
                available_items=available_items_for_room(items, current_room),
            )

        expected_room_location = room_location(current_room)
        if current_location != expected_room_location:
            return ToolExecutionResult(
                success=False,
                summary=f"{item.get('name', item_id)} is not in this room.",
                errors=["item_not_in_room"],
                error_code="item_not_in_room",
                requested_target=requested_target,
                item_id=item_id,
                item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
                current_location=current_room,
                available_items=available_items_for_room(items, current_room),
            )

        if not bool(item.get("portable", True)):
            return ToolExecutionResult(
                success=False,
                summary=f"{item.get('name', item_id)} cannot be carried.",
                errors=["item_not_portable"],
                error_code="item_not_portable",
                requested_target=requested_target,
                item_id=item_id,
                item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
                current_location=current_room,
                available_items=available_items_for_room(items, current_room),
            )

        previous_inventory = list(player.get("inventory", [])) if isinstance(player.get("inventory"), list) else []
        moved_from = expected_room_location
        item["location"] = PLAYER_INVENTORY_LOCATION
        sync_inventory_projection(state, items)
        next_inventory = list(player.get("inventory", [])) if isinstance(player.get("inventory"), list) else []

        return ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary=f"You take {item.get('name', item_id)}.",
            state_delta={
                "items": {
                    item_id: {
                        "location": {
                            "from": moved_from,
                            "to": PLAYER_INVENTORY_LOCATION,
                        }
                    }
                },
                "player": {
                    "inventory": {
                        "from": previous_inventory,
                        "to": next_inventory,
                    }
                },
            },
            requested_target=requested_target,
            item_id=item_id,
            item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
            moved_from=moved_from,
            moved_to=PLAYER_INVENTORY_LOCATION,
            current_location=current_room,
            available_items=available_items_for_room(items, current_room),
            inventory_items=next_inventory,
        )

    def drop_item(self, state: dict[str, Any], requested_target: str | None) -> ToolExecutionResult:
        items = ensure_items_state(state)
        player = state.setdefault("player", {})
        current_room = player.get("location")

        if not isinstance(current_room, str) or self.world.get_room(current_room) is None:
            return ToolExecutionResult(
                success=False,
                summary="Player location is not set to a valid room.",
                errors=["invalid_current_location"],
                error_code="invalid_current_location",
                requested_target=requested_target,
            )

        matches, ambiguous = self._resolve_item_in_scope(
            items, requested_target, PLAYER_INVENTORY_LOCATION
        )
        if ambiguous:
            return ToolExecutionResult(
                success=False,
                summary=f"'{requested_target or ''}' could match multiple items.",
                errors=["ambiguous_item"],
                error_code="ambiguous_item",
                requested_target=requested_target,
                current_location=current_room,
            )
        if not matches:
            return ToolExecutionResult(
                success=False,
                summary=f"No item matched '{requested_target or ''}'.",
                errors=["item_not_found"],
                error_code="item_not_found",
                requested_target=requested_target,
                current_location=current_room,
            )

        item_id = matches[0]
        item = items[item_id]
        if item.get("location") != PLAYER_INVENTORY_LOCATION:
            return ToolExecutionResult(
                success=False,
                summary=f"{item.get('name', item_id)} is not in your inventory.",
                errors=["item_not_in_inventory"],
                error_code="item_not_in_inventory",
                requested_target=requested_target,
                item_id=item_id,
                item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
                current_location=current_room,
            )

        previous_inventory = list(player.get("inventory", [])) if isinstance(player.get("inventory"), list) else []
        next_location = room_location(current_room)
        item["location"] = next_location
        sync_inventory_projection(state, items)
        next_inventory = list(player.get("inventory", [])) if isinstance(player.get("inventory"), list) else []

        return ToolExecutionResult(
            success=True,
            applied_tools=["drop_item"],
            summary=f"You drop {item.get('name', item_id)}.",
            state_delta={
                "items": {
                    item_id: {
                        "location": {
                            "from": PLAYER_INVENTORY_LOCATION,
                            "to": next_location,
                        }
                    }
                },
                "player": {
                    "inventory": {
                        "from": previous_inventory,
                        "to": next_inventory,
                    }
                },
            },
            requested_target=requested_target,
            item_id=item_id,
            item_name=item.get("name") if isinstance(item.get("name"), str) else item_id,
            moved_from=PLAYER_INVENTORY_LOCATION,
            moved_to=next_location,
            current_location=current_room,
            available_items=available_items_for_room(items, current_room),
            inventory_items=next_inventory,
        )

    def spawn_npc(self, state: dict[str, Any], npc_id: str, room_id: str) -> None:
        npcs = state.setdefault("npcs", {})
        npcs[npc_id] = {
            "room": room_id,
        }

    def advance_clock(self, state: dict[str, Any], amount: int) -> None:
        clock = state.setdefault("clock", {})
        clock["tick"] = int(clock.get("tick", 0)) + amount

    def record_fact(self, state: dict[str, Any], fact: str) -> None:
        facts = state.setdefault("facts", [])
        facts.append(fact)

    def _resolve_item_in_scope(
        self,
        items: dict[str, dict[str, Any]],
        requested_target: str | None,
        scope_location: str,
    ) -> tuple[list[str], bool]:
        """Resolve a name/alias/tag match against items in scope first, falling back to a
        global lookup only to produce a precise not-found/wrong-location error, never to
        widen ambiguity beyond what the player can actually see or reach."""
        scoped_items = {
            item_id: item
            for item_id, item in items.items()
            if item.get("location") == scope_location
        }
        scoped_matches = resolve_item_ids(scoped_items, requested_target)
        if len(scoped_matches) > 1:
            return [], True
        if scoped_matches:
            return scoped_matches, False

        global_matches = resolve_item_ids(items, requested_target)
        if len(global_matches) == 1:
            return global_matches, False
        return [], False

    def _state_from_text(self, campaign_state: str) -> dict[str, Any]:
        if not campaign_state or campaign_state == "No campaign state yet.":
            return self._build_fresh_campaign_state()
        try:
            value = json.loads(campaign_state)
        except json.JSONDecodeError as exc:
            logger.error("persisted_campaign_state_json_decode_failed")
            raise InvalidCampaignStateError(
                "Persisted campaign state contains invalid JSON."
            ) from exc
        if not isinstance(value, dict):
            logger.error(
                "persisted_campaign_state_invalid_type type=%s",
                type(value).__name__,
            )
            raise InvalidCampaignStateError(
                "Persisted campaign state must be a JSON object."
            )
        ensure_items_state(value)
        return value

    def _build_fresh_campaign_state(self) -> dict[str, Any]:
        state = copy.deepcopy(DEFAULT_CAMPAIGN_STATE)
        items = ensure_items_state(state)
        items.update(random_starting_inventory_items())
        sync_inventory_projection(state, items)
        return state

    def _compute_state_delta(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        for key in set(before.keys()) | set(after.keys()):
            if before.get(key) != after.get(key):
                delta[key] = after.get(key)
        return delta

    def _dispatch_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> ToolExecutionResult | None:
        try:
            result = self.registry.execute(tool_name, *args, **kwargs)
            self._apply_remote_state_result(args, result)
            return None
        except (KeyError, RegistryTransportError) as exc:
            return self._build_dispatch_error(tool_name, exc)

    def _dispatch_tool_with_value(
        self,
        tool_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, ToolExecutionResult | None]:
        try:
            result = self.registry.execute(tool_name, *args, **kwargs)
            self._apply_remote_state_result(args, result)
            return result, None
        except (KeyError, RegistryTransportError) as exc:
            return None, self._build_dispatch_error(tool_name, exc)

    def _build_dispatch_error(self, tool_name: str, exc: Exception) -> ToolExecutionResult:
        return ToolExecutionResult(
            success=False,
            summary=f"Tool dispatch failed for {tool_name}.",
            errors=[
                "tool_dispatch_failed",
                f"tool:{tool_name}",
                f"reason:{exc}",
            ],
        )

    def _apply_remote_state_result(self, args: tuple[Any, ...], result: Any) -> None:
        if not args:
            return
        state = args[0]
        if not isinstance(state, dict):
            return
        if not isinstance(result, dict):
            return

        structured_content = result.get("structured_content")
        if not isinstance(structured_content, dict):
            return

        next_state = structured_content.get("state")
        if not isinstance(next_state, dict):
            return

        state.clear()
        state.update(next_state)
