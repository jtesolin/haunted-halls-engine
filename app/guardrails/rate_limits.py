from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.config import settings
from app.db.repositories import Repository


def _daily_start() -> str:
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.isoformat()


def validate_daily_request_limit(db: Repository, player_id: str) -> None:
    request_count = db.count_player_requests_since(player_id, _daily_start())
    if request_count >= settings.MAX_DAILY_PLAYER_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Daily request limit exceeded ({settings.MAX_DAILY_PLAYER_REQUESTS} requests).",
        )


def validate_daily_token_limit(db: Repository, player_id: str, estimated_input_tokens: int) -> None:
    token_sum = db.sum_player_estimated_input_tokens_since(player_id, _daily_start())
    if token_sum + estimated_input_tokens > settings.MAX_DAILY_PLAYER_TOKENS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily token budget exceeded. "
                f"Used={token_sum}, requested={estimated_input_tokens}, limit={settings.MAX_DAILY_PLAYER_TOKENS}."
            ),
        )


def validate_campaign_turn_limit(db: Repository, player_id: str, campaign_id: str | None) -> None:
    if campaign_id is None:
        return
    turn_count = db.count_campaign_turns(player_id, campaign_id)
    if turn_count >= settings.MAX_TURNS_PER_CAMPAIGN:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Campaign turn limit exceeded for campaign {campaign_id}. "
                f"Limit is {settings.MAX_TURNS_PER_CAMPAIGN} turns."
            ),
        )
