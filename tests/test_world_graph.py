from __future__ import annotations

from app.game.world import DEFAULT_WORLD


def test_world_resolves_directional_and_named_adjacent_exits() -> None:
    entry_hall = DEFAULT_WORLD.get_room("entry_hall")
    grand_corridor = DEFAULT_WORLD.get_room("grand_corridor")
    library = DEFAULT_WORLD.get_room("library")

    assert entry_hall is not None
    assert grand_corridor is not None
    assert library is not None

    assert DEFAULT_WORLD.resolve_exit("entry_hall", "north") == grand_corridor
    assert DEFAULT_WORLD.resolve_exit("grand_corridor", "library") == library
    assert DEFAULT_WORLD.resolve_exit("grand_corridor", "the library") == library
    assert DEFAULT_WORLD.can_move("entry_hall", "north") is True


def test_world_rejects_non_adjacent_and_unknown_targets() -> None:
    assert DEFAULT_WORLD.resolve_exit("entry_hall", "crypt") is None
    assert DEFAULT_WORLD.resolve_exit("entry_hall", "west") is None
    assert DEFAULT_WORLD.resolve_exit("entry_hall", "observatory") is None
    assert DEFAULT_WORLD.can_move("entry_hall", "crypt") is False


def test_world_exposes_explicit_reverse_connections_only_when_defined() -> None:
    entry_hall = DEFAULT_WORLD.resolve_exit("grand_corridor", "south")
    staircase = DEFAULT_WORLD.resolve_exit("crypt", "south")

    assert entry_hall is not None
    assert entry_hall.id == "entry_hall"
    assert staircase is not None
    assert staircase.id == "staircase"
    assert DEFAULT_WORLD.resolve_exit("entry_hall", "south") is None


def test_world_exposes_sorted_available_exits_for_parser_context() -> None:
    exits = DEFAULT_WORLD.available_exits("grand_corridor")

    assert [exit_data["direction"] for exit_data in exits] == ["east", "north", "south", "west"]
    assert exits[0]["room_name"] == "Library"