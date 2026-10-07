from collections.abc import Iterator

import pytest
from fastapi import HTTPException

from app.core.config import Settings, settings
from app.db.repositories import Repository
from app.db.session import session
from app.guardrails.limit_errors import next_utc_reset_iso
from app.guardrails.rate_limits import (
    validate_campaign_turn_limit,
    validate_daily_request_limit,
    validate_daily_token_limit,
    validate_project_request_limit,
    validate_project_token_limit,
)


@pytest.fixture
def usage_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Repository, str]]:
    for name in (
        "MAX_DAILY_PLAYER_TOKENS",
        "MAX_DAILY_PLAYER_REQUESTS",
        "MAX_DAILY_PROJECT_TOKENS",
        "MAX_DAILY_PROJECT_REQUESTS",
        "MAX_TURNS_PER_CAMPAIGN",
    ):
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)

    with session() as db:
        user = db.resolve_internal_user(
            identity_provider="google",
            provider_issuer="https://accounts.google.com",
            provider_subject="usage-boundary-user",
            email="usage@example.com",
            email_verified=True,
            display_name="Usage User",
            avatar_url=None,
        )
        db.create_campaign(
            campaign_id="usage-campaign", owner_user_id=user.id, name="Usage Campaign"
        )
        yield db, user.id


@pytest.mark.parametrize(
    ("scope", "limit", "code", "detail"),
    [
        ("player", 300000, "daily_token_limit", "Daily token limit reached."),
        (
            "project",
            1000000,
            "daily_project_token_limit",
            "Daily project token limit reached.",
        ),
    ],
)
def test_token_limits_allow_exact_cap_and_reject_one_more_token(
    usage_repository: tuple[Repository, str],
    scope: str,
    limit: int,
    code: str,
    detail: str,
) -> None:
    db, user_id = usage_repository
    db.log_model_request(
        request_id="counted-tokens",
        owner_user_id=user_id,
        campaign_id="usage-campaign",
        turn_id="usage-turn",
        agent_name="Narrator",
        model="test-model",
        estimated_input_tokens=1,
        actual_input_tokens=limit - 500,
        actual_output_tokens=0,
        success=True,
    )

    if scope == "player":
        validate_daily_token_limit(db, user_id, 200, max_output_tokens=300)
    else:
        validate_project_token_limit(db, 200, max_output_tokens=300)

    with pytest.raises(HTTPException) as exc_info:
        if scope == "player":
            validate_daily_token_limit(db, user_id, 201, max_output_tokens=300)
        else:
            validate_project_token_limit(db, 201, max_output_tokens=300)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == {
        "detail": detail,
        "code": code,
        "retryable": False,
        "retry_at": next_utc_reset_iso(),
    }


@pytest.mark.parametrize(
    ("scope", "limit", "code", "detail"),
    [
        ("player", 100, "daily_request_limit", "Daily request limit reached."),
        (
            "project",
            1000,
            "daily_project_request_limit",
            "Daily project request limit reached.",
        ),
        (
            "campaign",
            20,
            "campaign_turn_limit",
            "This campaign has reached its turn limit.",
        ),
    ],
)
def test_request_and_campaign_limits_reject_only_at_cap(
    usage_repository: tuple[Repository, str],
    scope: str,
    limit: int,
    code: str,
    detail: str,
) -> None:
    db, user_id = usage_repository
    for index in range(limit):
        if scope == "project":
            db.log_model_request(
                request_id=f"request-{index}",
                owner_user_id=user_id,
                campaign_id="usage-campaign",
                turn_id=f"turn-{index}",
                agent_name="Narrator",
                model="test-model",
                estimated_input_tokens=0,
                actual_total_tokens=0,
                success=True,
            )
        else:
            db.create_turn("usage-campaign", f"turn-{index}", "user", "look")

        if index == limit - 2:
            if scope == "player":
                validate_daily_request_limit(db, user_id)
            elif scope == "project":
                validate_project_request_limit(db)
            else:
                validate_campaign_turn_limit(db, user_id, "usage-campaign")

    with pytest.raises(HTTPException) as exc_info:
        if scope == "player":
            validate_daily_request_limit(db, user_id)
        elif scope == "project":
            validate_project_request_limit(db)
        else:
            validate_campaign_turn_limit(db, user_id, "usage-campaign")

    expected = {"detail": detail, "code": code, "retryable": False}
    if scope != "campaign":
        expected["retry_at"] = next_utc_reset_iso()
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == expected
