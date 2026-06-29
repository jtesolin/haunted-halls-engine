import json

from fastapi import HTTPException

from app.db.repositories import Repository
from app.guardrails.usage_limits import UsageLimits
from app.schemas.chat import ChatRequest


def _parse_campaign_state(state: str | None) -> dict[str, object] | None:
    if not state:
        return None
    try:
        return json.loads(state)
    except json.JSONDecodeError:
        return None


def validate_chat_request(db: Repository, request: ChatRequest) -> None:
    candidate = request.player_id.strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="player_id is required")
    if candidate.lower() == "anonymous":
        raise HTTPException(status_code=422, detail="player_id cannot be 'anonymous'")

    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    if len(request.message) > UsageLimits.MAX_INPUT_CHARACTERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"message length exceeds {UsageLimits.MAX_INPUT_CHARACTERS} characters "
                f"({len(request.message)} received)."
            ),
        )

    if request.campaign_id:
        campaign = db.get_player_campaign(candidate, request.campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=404,
                detail="Campaign not found for player_id and campaign_id combination.",
            )

        state = _parse_campaign_state(campaign.state)
        if state and state.get("archived") is True:
            raise HTTPException(status_code=400, detail="Campaign is archived.")
    else:
        player_campaign_count = db.count_player_campaigns(candidate)
        if player_campaign_count >= UsageLimits.MAX_CAMPAIGNS_PER_PLAYER:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Player has reached the maximum number of campaigns ({UsageLimits.MAX_CAMPAIGNS_PER_PLAYER})."
                ),
            )
