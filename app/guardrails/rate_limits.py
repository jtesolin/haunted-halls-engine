from app.core.config import settings
from app.db.repositories import Repository
from app.guardrails.limit_errors import (
    next_utc_reset_iso,
    usage_limit_error,
    utc_day_start_for_accounting,
)


def _daily_start() -> str:
    return utc_day_start_for_accounting()


def validate_daily_request_limit(db: Repository, owner_user_id: str) -> None:
    request_count = db.count_user_requests_since(owner_user_id, _daily_start())
    if request_count >= settings.MAX_DAILY_PLAYER_REQUESTS:
        raise usage_limit_error(
            code="daily_request_limit",
            detail="Daily request limit reached.",
            retry_at=next_utc_reset_iso(),
        )


def validate_project_request_limit(db: Repository) -> None:
    request_count = db.count_project_requests_since(_daily_start())
    if request_count >= settings.MAX_DAILY_PROJECT_REQUESTS:
        raise usage_limit_error(
            code="daily_project_request_limit",
            detail="Daily project request limit reached.",
            retry_at=next_utc_reset_iso(),
        )


def validate_daily_token_limit(
    db: Repository, owner_user_id: str, estimated_input_tokens: int
) -> None:
    token_sum = db.sum_user_model_tokens_since(owner_user_id, _daily_start())
    if token_sum + estimated_input_tokens > settings.MAX_DAILY_PLAYER_TOKENS:
        raise usage_limit_error(
            code="daily_token_limit",
            detail="Daily token limit reached.",
            retry_at=next_utc_reset_iso(),
        )


def validate_project_token_limit(
    db: Repository, estimated_tokens: int
) -> None:
    token_sum = db.sum_project_model_tokens_since(_daily_start())
    if token_sum + estimated_tokens > settings.MAX_DAILY_PROJECT_TOKENS:
        raise usage_limit_error(
            code="daily_project_token_limit",
            detail="Daily project token limit reached.",
            retry_at=next_utc_reset_iso(),
        )


def validate_campaign_turn_limit(
    db: Repository, owner_user_id: str, campaign_id: str | None
) -> None:
    if campaign_id is None:
        return
    turn_count = db.count_campaign_turns(campaign_id, owner_user_id)
    if turn_count >= settings.MAX_TURNS_PER_CAMPAIGN:
        raise usage_limit_error(
            code="campaign_turn_limit",
            detail="This campaign has reached its turn limit.",
        )
