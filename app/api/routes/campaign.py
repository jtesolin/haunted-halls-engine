from fastapi import APIRouter, HTTPException

from app.db.session import session
from app.schemas.campaign import CampaignDetail, CampaignSummary, CampaignTurn
from app.schemas.character import CharacterInfo, CharacterList

router = APIRouter(prefix="/api", tags=["campaign"])


def _validate_player_id(player_id: str) -> str:
    candidate = player_id.strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="player_id is required")
    if candidate.lower() == "anonymous":
        raise HTTPException(status_code=422, detail="player_id cannot be 'anonymous'")
    return candidate


@router.get("/campaign/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(campaign_id: str) -> CampaignDetail:
    with session() as repo:
        campaign, turns, truncated = repo.get_campaign_with_turns(campaign_id)

    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return CampaignDetail(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        description=campaign.description,
        player_id=campaign.player_id,
        messages=[
            CampaignTurn(
                turn_id=turn.turn_id,
                player_message=turn.player_message,
                ai_reply=turn.ai_reply,
                created_at=turn.created_at,
            )
            for turn in turns
        ],
        truncated=truncated,
    )


@router.get("/campaigns/{player_id}", response_model=list[CampaignSummary])
async def list_campaigns(player_id: str) -> list[CampaignSummary]:
    player_id = _validate_player_id(player_id)

    with session() as repo:
        campaigns = repo.list_campaign_summaries_for_player(player_id)

    summaries: list[CampaignSummary] = []
    for campaign in campaigns:
        campaign_id = campaign["campaign_id"]
        title = campaign["title"]
        if campaign_id is None or title is None:
            continue

        summaries.append(
            CampaignSummary(
                campaign_id=campaign_id,
                title=title,
                last_message=campaign["last_message"],
            )
        )

    return summaries


@router.get("/character/{character_id}", response_model=CharacterInfo)
async def get_character(character_id: str) -> CharacterInfo:
    with session() as repo:
        character = repo.get_character(character_id)

    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")

    return CharacterInfo(character_id=character.character_id, name=character.name)


@router.get("/characters/{player_id}", response_model=CharacterList)
async def list_characters(player_id: str) -> CharacterList:
    player_id = _validate_player_id(player_id)

    with session() as repo:
        characters = repo.list_characters_for_player(player_id)

    return CharacterList(
        characters=[
            CharacterInfo(character_id=character.character_id, name=character.name)
            for character in characters
        ]
    )
