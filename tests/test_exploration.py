import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.orchestration.orchestrator as orchestrator_module
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import get_engine
from app.game.campaign_state import (
    InvalidCampaignStateError,
    build_fresh_campaign_state,
)
from app.game.exploration import project_exploration, record_exploration
from app.main import app
from app.schemas.chat import ChatRequest
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")
TOKEN = "test-internal-engine-service-token-0000000000000000000000000000000000000000000000"


def _headers(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": subject,
            "email": f"{subject}@example.com",
            "email_verified": True,
            "display_name": "Explorer",
            "avatar_url": None,
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    return {
        "Authorization": f"Bearer {TOKEN}",
        INTERNAL_USER_ID_HEADER_NAME: response.json()["user_id"],
    }


def _client() -> TestClient:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = TOKEN
    return TestClient(app)


def _move(state: dict, room: str) -> None:
    state["player"]["location"] = room
    record_exploration(state)


def _view(state: dict):
    return project_exploration(json.dumps(state))


def _edges(view) -> dict:
    return {e.id: e for e in view.edges}


def test_new_campaign_knows_only_start_room_with_anonymous_frontier() -> None:
    state = build_fresh_campaign_state()
    view = _view(state)
    assert [n.id for n in view.nodes if n.kind == "room"] == ["entry_hall"]
    frontier = [n for n in view.nodes if n.kind == "frontier"]
    assert len(frontier) == 1 and frontier[0].name is None
    assert "Grand Corridor" not in view.model_dump_json()
    assert "grand_corridor" not in view.model_dump_json()
    assert view.current_location.id == "entry_hall"


def test_revisits_reuse_ids_and_links_do_not_duplicate_with_cycles() -> None:
    state = build_fresh_campaign_state()
    for room in ["grand_corridor", "entry_hall", "grand_corridor", "library", "grand_corridor"]:
        _move(state, room)
    ids = [n.id for n in _view(state).nodes if n.kind == "room"]
    assert ids == ["entry_hall", "grand_corridor", "library"]
    edges = state["exploration"]["edges"]
    assert len(edges) == len(set(edges))
    # Both directions of the corridor<->hall cycle exist exactly once, resolved.
    e = _edges(_view(state))
    assert e["entry_hall/exit/north"].target == "grand_corridor"
    assert e["grand_corridor/exit/south"].target == "entry_hall"
    assert e["entry_hall/exit/north"].traversed and e["grand_corridor/exit/south"].traversed
    before = json.dumps(state["exploration"], sort_keys=True)
    assert record_exploration(state) is False
    assert json.dumps(state["exploration"], sort_keys=True) == before


def test_unvisited_destinations_stay_hidden_and_seen_exit_to_known_room_resolves() -> None:
    state = build_fresh_campaign_state()
    _move(state, "grand_corridor")
    view = _view(state)
    dumped = view.model_dump_json()
    for secret in ("Library", "library", "Dining", "dining_room", "Staircase", "staircase"):
        assert secret not in dumped
    e = _edges(view)
    assert e["grand_corridor/exit/east"].target.startswith("frontier-")
    # Seen from the visited side, the way back is not anonymous.
    assert e["grand_corridor/exit/south"].target == "entry_hall"
    assert not e["grand_corridor/exit/south"].traversed


def test_frontier_ids_are_stable_and_distinct() -> None:
    state = build_fresh_campaign_state()
    _move(state, "grand_corridor")
    first = _edges(_view(state))
    second = _edges(_view(state))
    targets = [e.target for e in first.values() if e.target.startswith("frontier-")]
    assert len(targets) == len(set(targets)) == 3
    assert {k: v.target for k, v in first.items()} == {k: v.target for k, v in second.items()}


def test_one_way_and_unusual_movement_are_directed_edges() -> None:
    state = build_fresh_campaign_state()
    _move(state, "crypt")  # no authored exit from entry_hall: teleport/phasing
    e = _edges(_view(state))
    passage = e["entry_hall/passage/crypt"]
    assert (passage.kind, passage.target, passage.traversed) == ("passage", "crypt", True)
    assert "crypt/passage/entry_hall" not in e  # one-way: no reverse link invented


def test_traversal_route_edge_distinct_from_walking_exit() -> None:
    state = build_fresh_campaign_state()
    for room in ["grand_corridor", "dining_room", "rain_court"]:
        _move(state, room)
    e = _edges(_view(state))
    assert e["rain_court/route/channel_gap"].kind == "route"
    assert e["rain_court/route/channel_gap"].target.startswith("frontier-")
    assert "Far Bank" not in _view(state).model_dump_json()


def test_observations_track_world_changes_without_duplicates() -> None:
    state = build_fresh_campaign_state()
    _move(state, "grand_corridor")
    _move(state, "library")
    obs = _view(state).observations
    assert any(o.startswith("Old Book:") and o.endswith("It is closed.") for o in obs)
    assert any(o.startswith("Brass Key:") for o in obs)
    assert any(o.startswith("Library Ghost:") for o in obs)
    assert len(obs) == len(set(obs))
    state["items"]["old_book"]["properties"]["is_open"] = True  # state changes
    state["items"]["brass_key"]["location"] = "player:current"  # taken
    state["npcs"]["library_ghost"]["status"] = "absent"
    obs = _view(state).observations
    assert any(o.startswith("Old Book:") and o.endswith("It is open.") for o in obs)
    assert not any("closed" in o for o in obs if o.startswith("Old Book"))
    assert not any(o.startswith(("Brass Key", "Library Ghost")) for o in obs)
    # Ordinary exits are not duplicated as details.
    assert not any(o.startswith("west") for o in obs)


def test_route_observation_is_neutral_exit_feature() -> None:
    state = build_fresh_campaign_state()
    for room in ["grand_corridor", "dining_room", "rain_court"]:
        _move(state, room)
    obs = _view(state).observations
    assert "Channel Gap: a 3-metre gap crossing." in obs
    assert "Far Bank" not in " ".join(obs) and "Upper Gallery" not in " ".join(obs)


def test_legacy_state_projects_without_persisting_and_malformed_fails_closed() -> None:
    state = build_fresh_campaign_state()
    del state["exploration"]
    snapshot = json.dumps(state)
    assert _view(state).current_location.id == "entry_hall"
    assert json.dumps(state) == snapshot
    state["exploration"] = {"rooms": ["nowhere"], "current": "nowhere", "edges": {}}
    with pytest.raises(InvalidCampaignStateError):
        _view(state)
    with pytest.raises(InvalidCampaignStateError):
        record_exploration(state)


def _campaign(client: TestClient, headers) -> str:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201
    return response.json()["campaign_id"]


def _persisted(campaign_id: str) -> dict:
    with get_engine().connect() as conn:
        return json.loads(
            conn.execute(
                text("SELECT state FROM campaigns WHERE campaign_id = :c"),
                {"c": campaign_id},
            ).scalar_one()
        )


def test_api_requires_auth_and_is_owner_scoped() -> None:
    client = _client()
    owner = _headers(client, "explore-owner")
    other = _headers(client, "explore-other")
    campaign_id = _campaign(client, owner)
    url = f"/api/campaign/{campaign_id}/exploration"

    assert client.get(url).status_code in {401, 403}
    assert client.get(url, headers=other).status_code == 404
    assert client.get("/api/campaign/missing/exploration", headers=owner).status_code == 404
    response = client.get(url, headers=owner)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"current_location", "nodes", "edges", "observations"}
    assert body["current_location"]["id"] == "entry_hall"
    assert any(o.startswith("Old Caretaker:") for o in body["observations"])


def test_api_campaigns_are_isolated() -> None:
    client = _client()
    headers = _headers(client, "explore-iso")
    first, second = _campaign(client, headers), _campaign(client, headers)
    with get_engine().begin() as conn:
        state = _persisted(first)
        _move(state, "grand_corridor")
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": json.dumps(state), "c": first},
        )
    assert client.get(f"/api/campaign/{first}/exploration", headers=headers).json()["current_location"]["id"] == "grand_corridor"
    assert client.get(f"/api/campaign/{second}/exploration", headers=headers).json()["current_location"]["id"] == "entry_hall"


def test_api_malformed_state_returns_500_without_leak() -> None:
    client = _client()
    headers = _headers(client, "explore-bad")
    campaign_id = _campaign(client, headers)
    state = _persisted(campaign_id)
    state["exploration"] = {"rooms": "x"}
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": json.dumps(state), "c": campaign_id},
        )
    response = client.get(f"/api/campaign/{campaign_id}/exploration", headers=headers)
    assert response.status_code == 500
    assert response.json() == {"detail": "Exploration state is unavailable"}


def test_turn_move_persists_graph_and_failed_turn_persists_nothing(monkeypatch) -> None:
    client = _client()
    headers = _headers(client, "explore-turn")
    user_id = headers[INTERNAL_USER_ID_HEADER_NAME]
    campaign_id = _campaign(client, headers)
    before = _persisted(campaign_id)

    async def broken(**kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("narrator down")

    real = orchestrator_module.orchestrator.narrator_agent.generate
    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", broken)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="go north", campaign_id=campaign_id), owner_user_id=user_id,
        ))
    assert exc.value.status_code == 502
    assert _persisted(campaign_id) == before  # no location, node or edge written

    monkeypatch.setattr(orchestrator_module.orchestrator.narrator_agent, "generate", real)
    asyncio.run(orchestrator_module.orchestrator.handle_chat(
        ChatRequest(message="go north", campaign_id=campaign_id), owner_user_id=user_id,
    ))
    body = client.get(f"/api/campaign/{campaign_id}/exploration", headers=headers).json()
    assert body["current_location"]["id"] == "grand_corridor"
    assert [n["id"] for n in body["nodes"] if n["kind"] == "room"] == ["entry_hall", "grand_corridor"]
    edge = next(e for e in body["edges"] if e["id"] == "entry_hall/exit/north")
    assert edge["traversed"] and edge["target"] == "grand_corridor"


def test_ability_traversal_records_route_edge_and_reveals_landing() -> None:
    from app.schemas.chat import ToolExecutionResult
    from app.schemas.generated_abilities import (
        AbilityGameplayResult,
        AbilityGameplayStatus,
        AbilityTraversalEffect,
        TraversalMethod,
    )

    state = build_fresh_campaign_state()
    for room in ["grand_corridor", "dining_room", "rain_court"]:
        _move(state, room)
    state["player"]["location"] = "far_bank"
    result = ToolExecutionResult(
        success=True, summary="crossed",
        ability_result=AbilityGameplayResult(
            ability_id="x", status=AbilityGameplayStatus.RESOLVED,
            traversal_effect=AbilityTraversalEffect(
                method=TraversalMethod.LEVITATION, route_id="channel_gap",
                route_name="Channel Gap", origin="rain_court",
                destination="far_bank", destination_name="Far Bank",
            ),
        ),
    )
    assert record_exploration(state, tool_result=result)
    e = _edges(_view(state))
    assert (e["rain_court/route/channel_gap"].target, e["rain_court/route/channel_gap"].traversed) == ("far_bank", True)
    assert "rain_court/passage/far_bank" not in e


@pytest.mark.parametrize("legacy", [True, False])
def test_failed_move_records_no_exploration_facts(legacy: bool) -> None:
    client = _client()
    headers = _headers(client, f"explore-failed-move-{legacy}")
    user_id = headers[INTERNAL_USER_ID_HEADER_NAME]
    campaign_id = _campaign(client, headers)
    state = _persisted(campaign_id)
    if legacy:
        del state["exploration"]
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": json.dumps(state), "c": campaign_id},
        )
    asyncio.run(orchestrator_module.orchestrator.handle_chat(
        ChatRequest(message="go west", campaign_id=campaign_id), owner_user_id=user_id,
    ))  # entry_hall has no west exit: tool fails, turn still narrated
    after = _persisted(campaign_id)
    assert after.get("exploration") == state.get("exploration")
    assert after["player"]["location"] == "entry_hall"


def test_legacy_campaign_first_move_keeps_origin_room() -> None:
    client = _client()
    headers = _headers(client, "explore-legacy-move")
    user_id = headers[INTERNAL_USER_ID_HEADER_NAME]
    campaign_id = _campaign(client, headers)
    state = _persisted(campaign_id)
    del state["exploration"]
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": json.dumps(state), "c": campaign_id},
        )
    asyncio.run(orchestrator_module.orchestrator.handle_chat(
        ChatRequest(message="go north", campaign_id=campaign_id), owner_user_id=user_id,
    ))
    body = client.get(f"/api/campaign/{campaign_id}/exploration", headers=headers).json()
    assert [n["id"] for n in body["nodes"] if n["kind"] == "room"] == ["entry_hall", "grand_corridor"]
    edges = {e["id"]: e for e in body["edges"]}
    assert edges["entry_hall/exit/north"]["traversed"]
    assert edges["grand_corridor/exit/south"]["target"] == "entry_hall"


def test_observations_are_deduplicated() -> None:
    state = build_fresh_campaign_state()
    _move(state, "library")
    twin = dict(state["items"]["brass_key"])
    state["items"]["brass_key_twin"] = {**twin, "id": "brass_key_twin"}
    state["items"]["twin_of_room"] = {
        **twin, "id": "twin_of_room", "name": "A",
        "description": "", "location": "room:library",
    }
    obs = _view(state).observations
    assert len(obs) == len(set(obs))
    assert sum(o.startswith("Brass Key:") for o in obs) == 1


def test_projection_rejects_graph_that_disagrees_with_player_location() -> None:
    state = build_fresh_campaign_state()
    state["player"]["location"] = "grand_corridor"  # graph still ends at entry_hall
    snapshot = json.dumps(state)
    with pytest.raises(InvalidCampaignStateError):
        _view(state)
    assert json.dumps(state) == snapshot
    # The turn path still records real movement.
    assert record_exploration(state) is True
    assert _view(state).current_location.id == "grand_corridor"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["entry_hall/exit/north"].update(label="crypt"),
        lambda e: e["entry_hall/exit/north"].update(to=None, traversed=True),
        lambda e: e.__setitem__("entry_hall/exit/secret_vault", e["entry_hall/exit/north"]),
        lambda e: e["entry_hall/exit/north"].update(**{"from": "entry_hall", "to": "entry_hall"}),
    ],
)
def test_projection_rejects_inconsistent_edges_so_nothing_unrecorded_is_echoed(mutate) -> None:
    state = build_fresh_campaign_state()
    mutate(state["exploration"]["edges"])
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


def _rain_court_state() -> dict:
    state = build_fresh_campaign_state()
    for room in ["grand_corridor", "dining_room", "rain_court"]:
        _move(state, room)
    return state


def _add(state: dict, edge_id: str, **edge) -> None:
    state["exploration"]["edges"][edge_id] = {
        "from": "rain_court", "to": None, "kind": "route", "label": "Channel Gap",
        "traversed": False, **edge,
    }


@pytest.mark.parametrize(
    ("edge_id", "edge"),
    [
        ("rain_court/route/hidden_tunnel", {}),  # route key not authored
        ("rain_court/route/channel_gap", {"label": "Far Bank"}),  # wrong label
        ("rain_court/passage/far_bank", {"kind": "passage", "label": "unmapped passage"}),  # to None
        ("rain_court/passage/far_bank", {"kind": "passage", "label": "unmapped passage", "to": "dining_room"}),  # key != to
        ("rain_court/passage/dining_room", {"kind": "passage", "label": "Far Bank", "to": "dining_room"}),  # wrong label
        ("rain_court/exit/channel_gap", {}),  # id kind segment disagrees with kind
        ("dining_room/route/channel_gap", {}),  # id prefix disagrees with from
        ("rain_court/route/channel_gap", {"traversed": True}),  # traversed without target
        ("rain_court/route/channel_gap", {"to": "rain_court"}),  # self loop
    ],
)
def test_projection_rejects_forged_route_and_passage_edges(edge_id: str, edge: dict) -> None:
    state = _rain_court_state()
    del state["exploration"]["edges"]["rain_court/route/channel_gap"]
    _add(state, edge_id, **edge)
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


def test_route_and_passage_edges_written_by_the_engine_round_trip() -> None:
    state = _rain_court_state()
    _move(state, "crypt")  # unmapped passage
    view = _view(state)
    e = _edges(view)
    assert e["rain_court/passage/crypt"].label == "unmapped passage"
    assert e["rain_court/route/channel_gap"].label == "Channel Gap"
    assert record_exploration(state) is False


@pytest.mark.parametrize(
    ("edge_id", "edge"),
    [
        ("north", {"from": "entry_hall", "kind": "exit", "label": "north"}),
        ("crypt", {"from": "rain_court", "kind": "passage", "label": "unmapped passage", "to": "crypt", "traversed": True}),
    ],
)
def test_projection_rejects_bare_edge_ids_without_the_from_kind_prefix(edge_id: str, edge: dict) -> None:
    state = _rain_court_state()
    state["exploration"]["rooms"].append("crypt")
    _add(state, edge_id, **edge)
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


@pytest.mark.parametrize(
    ("edge_id", "edge"),
    [
        ("entry_hall/exit/north", {"from": "entry_hall", "kind": "exit", "label": "north", "to": "library"}),
        ("rain_court/route/channel_gap", {"to": "dining_room"}),
    ],
)
def test_projection_rejects_edges_retargeted_away_from_authored_destination(edge_id: str, edge: dict) -> None:
    state = _rain_court_state()
    state["exploration"]["rooms"].append("library")
    state["exploration"]["edges"].pop(edge_id, None)
    _add(state, edge_id, **edge)
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


def test_projection_rejects_untraversed_passage_and_unreached_rooms() -> None:
    state = _rain_court_state()
    _move(state, "crypt")
    state["exploration"]["edges"]["rain_court/passage/crypt"]["traversed"] = False
    with pytest.raises(InvalidCampaignStateError):
        _view(state)
    state = _rain_court_state()
    state["exploration"]["rooms"].append("crypt")  # never reached by any traversed edge
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


def test_projection_rejects_rooms_connected_only_to_each_other() -> None:
    state = build_fresh_campaign_state()
    ex = state["exploration"]
    ex["rooms"] += ["grand_corridor", "library"]
    ex["current"] = "library"
    state["player"]["location"] = "library"
    ex["edges"]["grand_corridor/exit/east"] = {
        "from": "grand_corridor", "to": "library", "kind": "exit", "label": "east", "traversed": True,
    }
    ex["edges"]["library/exit/west"] = {
        "from": "library", "to": "grand_corridor", "kind": "exit", "label": "west", "traversed": True,
    }
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


# --- #132: persisted room notes and cross-room corrections -------------------


def _notes(state: dict, room: str) -> list[str]:
    from app.game.exploration import project_room_notes

    result = project_room_notes(json.dumps(state), room)
    assert result is not None
    return result.notes


def test_notes_persist_for_visited_room_and_ignore_unobserved_changes() -> None:
    state = build_fresh_campaign_state()
    before = _notes(state, "entry_hall")
    assert any(n.startswith("Old Caretaker:") for n in before)
    _move(state, "grand_corridor")
    assert _notes(state, "entry_hall") == before
    # World changes the player did not see must not alter or leak into old notes.
    state["items"]["old_book"] = {**state["items"]["old_book"], "location": "room:entry_hall"}
    state["npcs"]["old_caretaker"]["description"] = "Changed while away."
    state["npcs"]["old_caretaker"]["status"] = "absent"
    assert _notes(state, "entry_hall") == before
    assert not any("Changed while away" in n for n in _notes(state, "entry_hall"))


def test_seeing_npc_elsewhere_corrects_old_note_without_duplicates() -> None:
    state = build_fresh_campaign_state()
    _move(state, "grand_corridor")
    state["npcs"]["old_caretaker"]["location"] = "grand_corridor"  # seen here now
    record_exploration(state)
    old = _notes(state, "entry_hall")
    assert "Old Caretaker: last seen here; since seen in Grand Corridor." in old
    assert not any(n.startswith("Old Caretaker: ") and "last seen" not in n for n in old)
    here = _notes(state, "grand_corridor")
    assert sum(n.startswith("Old Caretaker:") for n in here) == 1
    record_exploration(state)  # idempotent
    assert _notes(state, "entry_hall") == old
    # Moves again: every older correction follows the latest sighting.
    _move(state, "library")
    state["npcs"]["old_caretaker"]["location"] = "library"
    record_exploration(state)
    for room in ("entry_hall", "grand_corridor"):
        assert "Old Caretaker: last seen here; since seen in Library." in _notes(state, room)


def test_revisit_updates_notes_and_drops_observed_absence() -> None:
    state = build_fresh_campaign_state()
    _move(state, "grand_corridor")
    state["npcs"]["old_caretaker"]["location"] = "grand_corridor"
    record_exploration(state)
    state["npcs"]["old_caretaker"]["location"] = "library"  # unseen while away
    state["items"]["brass_key"]["location"] = "player:current"
    _move(state, "entry_hall")
    notes = _notes(state, "entry_hall")
    # The correction survives (still the last sighting); no stale "here" note.
    assert [n for n in notes if "Old Caretaker" in n] == [
        "Old Caretaker: last seen here; since seen in Grand Corridor."
    ]
    assert state["exploration"]["notes"]["entry_hall"][-1]["seen_in"] == "grand_corridor"
    assert len(notes) == len(set(notes))
    # Never reveals the unvisited library the caretaker walked to.
    assert "Library" not in json.dumps(notes)


def test_malformed_notes_fail_closed() -> None:
    state = build_fresh_campaign_state()
    state["exploration"]["notes"] = {"library": [{"key": "room", "text": "x"}]}
    with pytest.raises(InvalidCampaignStateError):
        _view(state)
    state["exploration"]["notes"] = {"entry_hall": [{"key": "room", "text": "x", "seen_in": "entry_hall"}]}
    with pytest.raises(InvalidCampaignStateError):
        _view(state)
    state["exploration"]["notes"] = {"entry_hall": [{"key": "a", "text": "x"}, {"key": "a", "text": "y"}]}
    with pytest.raises(InvalidCampaignStateError):
        _view(state)


def test_room_notes_read_does_not_persist_legacy_state() -> None:
    state = build_fresh_campaign_state()
    del state["exploration"]
    snapshot = json.dumps(state)
    assert any(n.startswith("Old Caretaker:") for n in _notes(state, "entry_hall"))
    assert json.dumps(state) == snapshot


def test_room_notes_api_rejects_undiscovered_foreign_and_unauthenticated() -> None:
    client = _client()
    owner = _headers(client, "notes-owner")
    other = _headers(client, "notes-other")
    campaign_id = _campaign(client, owner)
    base = f"/api/campaign/{campaign_id}/exploration/rooms"

    assert client.get(f"{base}/entry_hall").status_code in {401, 403}
    assert client.get(f"{base}/entry_hall", headers=other).status_code == 404
    ok = client.get(f"{base}/entry_hall", headers=owner)
    assert ok.status_code == 200 and ok.json()["current"] is True
    assert any(n.startswith("Old Caretaker:") for n in ok.json()["notes"])
    # Unvisited and nonexistent rooms are indistinguishable.
    unvisited = client.get(f"{base}/library", headers=owner)
    unknown = client.get(f"{base}/no_such_room", headers=owner)
    assert unvisited.status_code == unknown.status_code == 404
    assert unvisited.json() == {"detail": "Room not found"} == unknown.json()
    assert (
        client.get("/api/campaign/missing/exploration/rooms/entry_hall", headers=owner).status_code
        == 404
    )


def test_room_notes_api_returns_frozen_notes_for_previous_room() -> None:
    client = _client()
    owner = _headers(client, "notes-prev")
    campaign_id = _campaign(client, owner)
    from app.db.session import get_engine as _engine

    state = _persisted(campaign_id)
    _move(state, "grand_corridor")
    with _engine().begin() as conn:
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": json.dumps(state), "c": campaign_id},
        )
    body = client.get(
        f"/api/campaign/{campaign_id}/exploration/rooms/entry_hall", headers=owner
    ).json()
    assert body["current"] is False
    assert any(n.startswith("Old Caretaker:") for n in body["notes"])
