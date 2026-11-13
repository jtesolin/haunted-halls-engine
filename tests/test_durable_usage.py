"""Issue #117: durable usage, short transactions, request units and concurrency.

These tests run on SQLite by default and on PostgreSQL when TEST_DATABASE_URL is
set (the CI ``postgres-tests`` job), where the concurrency tests prove the row-lock
behavior that SQLite cannot.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.ai.model_client import model_client
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME, get_internal_engine_service_token
from app.core.config import settings
from app.db.repositories import ChatRequestIdempotencyOwnershipError, Repository
from app.db.session import get_engine, session
from app.game.campaign_state import build_fresh_campaign_state
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.chat import ChatRequest
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")

orchestrator = orchestrator_module.orchestrator


@pytest.fixture(autouse=True)
def restore_settings() -> Iterator[None]:
    original = settings.model_dump()
    yield
    for name, value in original.items():
        setattr(settings, name, value)


class Provider:
    """Wraps the conftest fake provider with failure/delay/probe injection."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fail: set[str] = set()
        self.delay: dict[str, float] = {}
        self.probes: list[Callable[[str], None]] = []
        self.blank_title = False
        self._text = model_client.generate_text
        self._structured = model_client.generate_structured
        monkeypatch.setattr(model_client, "generate_text", self._generate_text)
        monkeypatch.setattr(model_client, "generate_structured", self._generate_structured)

    async def _enter(self, label: str) -> None:
        for probe in self.probes:
            probe(label)
        if label in self.delay:
            await asyncio.sleep(self.delay[label])
        if label in self.fail:
            raise RuntimeError(f"injected {label} failure")

    async def _generate_text(self, **kwargs):
        content = str(kwargs.get("messages", [{}])[-1].get("content", "")).lower()
        label = "title" if "campaign title" in content else "narrator"
        await self._enter(label)
        result = await self._text(**kwargs)
        if label == "title" and self.blank_title:
            return result.__class__(output="  ", usage=result.usage)
        return result

    async def _generate_structured(self, **kwargs):
        name = kwargs["response_model"].__name__
        await self._enter("parser" if name == "ActionParserOutput" else "director")
        return await self._structured(**kwargs)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    return Provider(monkeypatch)


def _user(client: TestClient, subject: str) -> tuple[dict[str, str], str]:
    token = settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": subject,
            "email": f"{subject}@example.com",
            "email_verified": True,
            "display_name": "Player",
            "avatar_url": "https://example.com/a.png",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    user_id = response.json()["user_id"]
    return (
        {"Authorization": f"Bearer {token}", INTERNAL_USER_ID_HEADER_NAME: user_id},
        user_id,
    )


def _scalar(sql: str, **params):
    with session() as db:
        return db.conn.execute(text(sql), params).scalar_one()


def _usage_rows(owner: str) -> list[tuple[str, int, str]]:
    with session() as db:
        rows = db.conn.execute(
            text(
                "SELECT agent_name, success, status FROM model_requests "
                "WHERE owner_user_id = :o ORDER BY id"
            ),
            {"o": owner},
        ).all()
    return [(r[0], int(r[1]), r[2]) for r in rows]


def _requests(owner: str) -> int:
    return _scalar("SELECT COUNT(*) FROM usage_requests WHERE owner_user_id = :u", u=owner)


def _new_campaign(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["campaign_id"]


def _chat(client: TestClient, headers: dict[str, str], campaign_id: str, message="look around"):
    return client.post(
        "/api/chat", json={"message": message, "campaign_id": campaign_id}, headers=headers
    )


def _lease_token(campaign_id: str):
    return _scalar(
        "SELECT turn_lease_token FROM campaigns WHERE campaign_id = :c", c=campaign_id
    )


def _race(count: int, work: Callable[[int], object]) -> list[object]:
    barrier = threading.Barrier(count)

    def run(index: int) -> object:
        barrier.wait()
        try:
            return work(index)
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def _turn_in_thread(campaign_id: str, user_id: str, key: str | None = None):
    def turn(_: int):
        return asyncio.run(
            orchestrator.handle_chat(
                ChatRequest(message="look", campaign_id=campaign_id),
                owner_user_id=user_id,
                idempotency_key=key,
            )
        )

    return turn


# ------------------------------------------------------------------ finding 1


@pytest.mark.parametrize("failing", ["parser", "director", "narrator"])
def test_provider_failure_keeps_usage_but_not_game_state(
    provider: Provider, failing: str
) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, f"fail-{failing}")
    campaign_id = _new_campaign(client, headers)
    with session() as db:
        state_before = db.get_campaign(campaign_id).state  # type: ignore[union-attr]
        events_before = len(db.list_campaign_events(campaign_id))
    usage_before = len(_usage_rows(user_id))

    provider.fail.add(failing)
    response = _chat(client, headers, campaign_id)

    assert response.status_code == 502
    rows = _usage_rows(user_id)[usage_before:]
    assert all(status == "completed" for _, _, status in rows)
    assert sum(1 for _, success, _ in rows if success == 0) == 1
    with session() as db:
        assert db.get_campaign(campaign_id).state == state_before  # type: ignore[union-attr]
        assert db.count_campaign_turns(campaign_id, user_id) == 0
        assert len(db.list_campaign_events(campaign_id)) == events_before
    assert _lease_token(campaign_id) is None

    provider.fail.clear()
    assert _chat(client, headers, campaign_id).status_code == 200


def test_invalid_parse_is_422_with_durable_usage_and_no_turn(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "invalid-parse")
    campaign_id = _new_campaign(client, headers)
    before = len(_usage_rows(user_id))
    with session() as db:
        events_before = len(db.list_campaign_events(campaign_id))
    original = orchestrator.action_parser_agent.parse

    async def invalid_parse(**kwargs):
        result = await original(**kwargs)
        return result.model_copy(update={"parse_status": "invalid", "parser_notes": "nope"})

    monkeypatch.setattr(orchestrator.action_parser_agent, "parse", invalid_parse)

    response = _chat(client, headers, campaign_id)

    assert response.status_code == 422
    assert [n for n, _, _ in _usage_rows(user_id)[before:]] == ["ActionParser"]
    with session() as db:
        assert db.count_campaign_turns(campaign_id, user_id) == 0
        assert len(db.list_campaign_events(campaign_id)) == events_before


def test_final_commit_failure_keeps_usage_and_releases_claims(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "commit-failure")
    campaign_id = _new_campaign(client, headers)
    key = str(uuid4())
    before = len(_usage_rows(user_id))
    with session() as db:
        events_before = len(db.list_campaign_events(campaign_id))
    real_add_event = Repository.add_event

    def exploding_add_event(self, *args, **kwargs):
        if kwargs.get("type") == "narrator_response_created":
            raise RuntimeError("injected commit failure")
        return real_add_event(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "add_event", exploding_add_event)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        asyncio.run(
            orchestrator.handle_chat(
                ChatRequest(message="look", campaign_id=campaign_id),
                owner_user_id=user_id,
                idempotency_key=key,
            )
        )
    monkeypatch.setattr(Repository, "add_event", real_add_event)

    assert len(_usage_rows(user_id)) - before >= 3  # parser, director, narrator
    with session() as db:
        assert db.count_campaign_turns(campaign_id, user_id) == 0
        assert len(db.list_campaign_events(campaign_id)) == events_before
        assert db.get_chat_request_idempotency(user_id, key) is None
    assert _lease_token(campaign_id) is None
    retry = asyncio.run(
        orchestrator.handle_chat(
            ChatRequest(message="look", campaign_id=campaign_id),
            owner_user_id=user_id,
            idempotency_key=key,
        )
    )
    assert retry.reply


def test_campaign_creation_failure_keeps_usage(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "create-fail")
    provider.fail.add("narrator")

    response = client.post("/api/campaign", json={}, headers=headers)

    assert response.status_code == 502
    # starter generation succeeded and the failed opening call are both durable
    assert len(_usage_rows(user_id)) == 2
    assert _scalar("SELECT COUNT(*) FROM campaigns") == 0


# ------------------------------------------------------------------ finding 7


def test_blank_title_creates_campaign_with_fallback_and_usage(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "blank-title")
    provider.blank_title = True

    response = client.post("/api/campaign", json={}, headers=headers)

    assert response.status_code == 201
    assert response.json()["name"] == orchestrator_module.DEFAULT_CAMPAIGN_TITLE
    assert len(_usage_rows(user_id)) == 3  # starter, opening, title


# ------------------------------------------------------------------ finding 2


def test_no_connection_held_across_any_model_await(provider: Provider) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "no-held-connection")
    observed: list[tuple[str, int]] = []

    def probe(label: str) -> None:
        observed.append((label, get_engine().pool.checkedout()))  # type: ignore[attr-defined]

    provider.probes.append(probe)
    campaign_id = _new_campaign(client, headers)
    assert _chat(client, headers, campaign_id).status_code == 200

    assert {label for label, _ in observed} >= {"narrator", "title", "parser", "director"}
    assert all(checked_out == 0 for _, checked_out in observed), observed


# ------------------------------------------------------------------ finding 3, 6


def test_deleting_a_campaign_does_not_refund_request_allowance(provider: Provider) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "delete-refund")
    settings.MAX_DAILY_PLAYER_REQUESTS = 3  # one creation + two chats
    campaign_id = _new_campaign(client, headers)
    assert _chat(client, headers, campaign_id).status_code == 200
    assert _chat(client, headers, campaign_id).status_code == 200

    assert client.delete(f"/api/campaign/{campaign_id}", headers=headers).status_code == 204

    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "daily_request_limit"


def test_project_request_cap_counts_logical_requests_not_model_calls(
    provider: Provider,
) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "project-units")
    settings.MAX_DAILY_PROJECT_REQUESTS = 3
    campaign_id = _new_campaign(client, headers)  # 1 request, 3 model calls
    assert _chat(client, headers, campaign_id).status_code == 200  # 2nd request
    assert _chat(client, headers, campaign_id).status_code == 200  # 3rd request

    response = _chat(client, headers, campaign_id)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "daily_project_request_limit"


def test_failed_paid_request_still_counts_toward_the_cap(provider: Provider) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "failed-counts")
    campaign_id = _new_campaign(client, headers)
    settings.MAX_DAILY_PLAYER_REQUESTS = 3
    provider.fail.add("narrator")
    assert _chat(client, headers, campaign_id).status_code == 502
    provider.fail.clear()
    assert _chat(client, headers, campaign_id).status_code == 200

    assert _chat(client, headers, campaign_id).status_code == 429


def test_replays_and_validation_failures_do_not_consume_requests(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "no-extra-requests")
    campaign_id = _new_campaign(client, headers)
    key = str(uuid4())
    body = {"message": "look", "campaign_id": campaign_id}
    keyed = {**headers, "Idempotency-Key": key}
    assert client.post("/api/chat", json=body, headers=keyed).status_code == 200
    used = _requests(user_id)

    assert client.post("/api/chat", json=body, headers=keyed).status_code == 200
    assert client.post("/api/chat", json={"message": " "}, headers=headers).status_code == 400
    missing = {"message": "x", "campaign_id": "campaign_missing"}
    assert client.post("/api/chat", json=missing, headers=headers).status_code == 404

    assert _requests(user_id) == used


# ------------------------------------------------------------------ finding 4


def test_tool_executor_runs_off_the_event_loop_thread(
    provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "tool-thread")
    campaign_id = _new_campaign(client, headers)
    loop_threads: list[int] = []
    tool_threads: list[int] = []
    real_execute = orchestrator.tool_executor.execute
    provider.probes.append(lambda label: loop_threads.append(threading.get_ident()))

    def spy(*args, **kwargs):
        tool_threads.append(threading.get_ident())
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(orchestrator.tool_executor, "execute", spy)
    assert _chat(client, headers, campaign_id).status_code == 200

    assert tool_threads and set(tool_threads).isdisjoint(loop_threads)


# ------------------------------------------------------------------ finding 5


def test_concurrent_token_reservations_cannot_exceed_the_hard_cap() -> None:
    client = TestClient(app)
    _, user_id = _user(client, "token-race")
    pending = 100 + 100  # estimated input + max output per call
    settings.MAX_ESTIMATED_INPUT_TOKENS = 10_000
    settings.MAX_DAILY_PLAYER_TOKENS = pending * 3
    settings.MAX_DAILY_PROJECT_TOKENS = pending * 3

    def reserve(index: int):
        return orchestrator._reserve_model_call_sync(
            owner_user_id=user_id,
            campaign_id="campaign_race",
            turn_id=f"turn_{index}",
            agent_name="Narrator",
            model="m",
            estimated_input_tokens=100,
            max_output_tokens=100,
        )

    results = _race(10, reserve)

    denied = [r for r in results if isinstance(r, HTTPException)]
    assert len(results) - len(denied) == 3
    assert {d.status_code for d in denied} == {429}
    assert _scalar("SELECT COUNT(*) FROM model_requests WHERE status = 'reserved'") == 3


def test_concurrent_request_counting_cannot_exceed_the_request_cap() -> None:
    client = TestClient(app)
    _, user_id = _user(client, "request-race")
    settings.MAX_DAILY_PLAYER_REQUESTS = 4

    results = _race(12, lambda _: orchestrator._begin_campaign_creation(user_id))

    denied = [r for r in results if isinstance(r, HTTPException)]
    assert len(results) - len(denied) == 4
    assert _requests(user_id) == 4


def test_concurrent_campaign_commits_cannot_exceed_the_campaign_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    _, user_id = _user(client, "campaign-race")
    monkeypatch.setattr(orchestrator_module.UsageLimits, "MAX_CAMPAIGNS_PER_PLAYER", 2)

    def commit(index: int):
        return orchestrator._commit_campaign_creation(
            owner_user_id=user_id,
            campaign_id=f"campaign_race_{index}",
            campaign_name="n",
            initial_state=build_fresh_campaign_state(),
            assistant_turn_id=f"turn_race_{index}",
            opening="o",
        )

    results = _race(8, commit)

    denied = [r for r in results if isinstance(r, HTTPException)]
    assert len(results) - len(denied) == 2
    assert {d.detail["code"] for d in denied} == {"max_campaigns"}  # type: ignore[index]
    assert _scalar("SELECT COUNT(*) FROM campaigns WHERE owner_user_id = :u", u=user_id) == 2


def test_parallel_turns_on_one_campaign_run_one_at_a_time(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "turn-race")
    campaign_id = _new_campaign(client, headers)
    provider.delay["narrator"] = 0.4

    results = _race(4, _turn_in_thread(campaign_id, user_id))

    losers = [r for r in results if isinstance(r, HTTPException)]
    assert len(results) - len(losers) == 1
    assert {loser.status_code for loser in losers} == {503}
    with session() as db:
        assert db.count_campaign_turns(campaign_id, user_id) == 1


def test_turn_limit_is_exact_under_the_lease(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "turn-limit")
    settings.MAX_TURNS_PER_CAMPAIGN = 1
    campaign_id = _new_campaign(client, headers)
    provider.delay["narrator"] = 0.2

    _race(4, _turn_in_thread(campaign_id, user_id))

    with session() as db:
        assert db.count_campaign_turns(campaign_id, user_id) == 1


def test_expired_turn_lease_is_taken_over_and_stale_holder_is_fenced(
    provider: Provider,
) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "lease-takeover")
    campaign_id = _new_campaign(client, headers)
    with session() as db:
        assert db.acquire_turn_lease(campaign_id, user_id, "dead-worker", 300)
    assert _chat(client, headers, campaign_id).status_code == 503

    with session() as db:
        db.conn.execute(
            text(
                "UPDATE campaigns SET turn_lease_expires_at = '2000-01-01T00:00:00' "
                "WHERE campaign_id = :c"
            ),
            {"c": campaign_id},
        )
    assert _chat(client, headers, campaign_id).status_code == 200
    with session() as db:
        assert db.release_turn_lease(campaign_id, "dead-worker") is False


def test_duplicate_idempotent_requests_execute_once(provider: Provider) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "idem-race")
    campaign_id = _new_campaign(client, headers)
    provider.delay["narrator"] = 0.4
    key = str(uuid4())
    used = _requests(user_id)
    turns_before = _scalar(
        "SELECT COUNT(*) FROM turns WHERE campaign_id = :c AND role = 'user'", c=campaign_id
    )
    results = _race(4, _turn_in_thread(campaign_id, user_id, key))

    winners = [r for r in results if not isinstance(r, HTTPException)]
    assert len(winners) >= 1
    assert {w.turn_id for w in winners} == {winners[0].turn_id}  # type: ignore[union-attr]
    assert (
        _scalar(
            "SELECT COUNT(*) FROM turns WHERE campaign_id = :c AND role = 'user'", c=campaign_id
        )
        == turns_before + 1
    )
    assert _requests(user_id) == used + 1


def test_expired_idempotency_claim_is_taken_over_and_stale_owner_is_fenced(
    provider: Provider,
) -> None:
    client = TestClient(app)
    headers, user_id = _user(client, "idem-takeover")
    campaign_id = _new_campaign(client, headers)
    request = ChatRequest(message="look", campaign_id=campaign_id)
    fingerprint = orchestrator._chat_request_fingerprint(request)
    key = str(uuid4())
    with session() as db:
        claim = db.claim_chat_request_idempotency(
            owner_user_id=user_id,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            requested_campaign_id=campaign_id,
            requested_character_id=None,
            owner_token="dead-worker",
            lease_seconds=300,
        )
        assert claim.acquired
    with pytest.raises(HTTPException) as exc_info:  # live lease: told to retry
        asyncio.run(orchestrator.handle_chat(request, owner_user_id=user_id, idempotency_key=key))
    assert exc_info.value.status_code == 503

    with session() as db:
        db.conn.execute(
            text("UPDATE chat_request_idempotency SET lease_expires_at = '2000-01-01T00:00:00'")
        )
    response = asyncio.run(
        orchestrator.handle_chat(request, owner_user_id=user_id, idempotency_key=key)
    )

    assert response.reply
    with session() as db, pytest.raises(ChatRequestIdempotencyOwnershipError):
        db.complete_chat_request_idempotency(
            owner_user_id=user_id,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            reply="stale",
            campaign_id=campaign_id,
            turn_id="turn_stale",
            owner_token="dead-worker",
        )


def test_model_deadline_returns_502_releases_claims_and_keeps_reservation(
    provider: Provider,
) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "deadline")
    campaign_id = _new_campaign(client, headers)
    settings.CHAT_MODEL_DEADLINE_SECONDS = 0.2
    provider.delay["narrator"] = 5

    response = _chat(client, headers, campaign_id)

    assert response.status_code == 502
    assert _scalar("SELECT COUNT(*) FROM model_requests WHERE status = 'reserved'") == 1
    assert _lease_token(campaign_id) is None
    provider.delay.clear()
    settings.CHAT_MODEL_DEADLINE_SECONDS = 140
    assert _chat(client, headers, campaign_id).status_code == 200


def test_chat_turn_persists_ordered_turns(provider: Provider) -> None:
    client = TestClient(app)
    headers, _ = _user(client, "atomic-turn")
    campaign_id = _new_campaign(client, headers)
    assert _chat(client, headers, campaign_id).status_code == 200

    with session() as db:
        _, turns, _ = db.get_campaign_with_turns(campaign_id, limit=10)
    assert [t.role for t in turns] == ["assistant", "user", "assistant"]


# ------------------------------------------------------------------ finding 8


@pytest.mark.parametrize("token", ["test-token", "x" * 63])
def test_short_service_token_is_rejected_even_under_pytest(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    monkeypatch.setattr(settings, "INTERNAL_ENGINE_SERVICE_TOKEN", token)
    with pytest.raises(RuntimeError, match="32 bytes"):
        get_internal_engine_service_token()


def test_64_character_service_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INTERNAL_ENGINE_SERVICE_TOKEN", "x" * 64)
    assert get_internal_engine_service_token() == "x" * 64


# ------------------------------------------------------------------ migration


def test_migration_backfills_request_ledger_and_downgrades(tmp_path) -> None:
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'migration-0006.db'}"
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0005_backfill_characters")
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO internal_users (user_id, identity_provider, provider_issuer, "
                    "provider_subject, email, email_verified, created_at, updated_at, "
                    "last_login_at) VALUES ('u1','google','i','s','e@x.com',1,'t','t','t')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO campaigns (campaign_id, owner_user_id, name, created_at) "
                    "VALUES ('c1','u1','n','2026-01-01T00:00:00')"
                )
            )
            for index, role in enumerate(["assistant", "user", "assistant", "user"]):
                conn.execute(
                    text(
                        "INSERT INTO turns (turn_id, campaign_id, role, content, created_at) "
                        "VALUES (:t,'c1',:r,'x',:ts)"
                    ),
                    {"t": f"t{index}", "r": role, "ts": f"2026-01-01T00:00:0{index}"},
                )
        command.upgrade(cfg, "head")
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM usage_requests")).scalar_one() == 2
            assert conn.execute(text("SELECT n FROM usage_budget_lock WHERE id = 1")).scalar_one() == 0
        command.downgrade(cfg, "0005_backfill_characters")
        with get_engine().connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
        assert "usage_requests" not in tables and "usage_budget_lock" not in tables
    finally:
        settings.DATABASE_URL = original
