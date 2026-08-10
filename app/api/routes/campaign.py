from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import (
    AuthenticatedUserContext,
    require_authenticated_user_context,
)
from app.orchestration.orchestrator import orchestrator
from app.db.session import session
from app.schemas.campaign import (
    CampaignCreateRequest,
    CampaignDetail,
    CampaignSummary,
    CampaignTurn,
)
from app.schemas.character import CharacterInfo, CharacterList

router = APIRouter(prefix="/api", tags=["campaign"])


@router.post("/campaign", response_model=CampaignDetail, status_code=201)
async def create_campaign(
    payload: CampaignCreateRequest,
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> CampaignDetail:
    return await orchestrator.create_campaign(
        payload, owner_user_id=_user_context.internal_user_id
    )


@router.get("/campaign/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(
    campaign_id: str,
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> CampaignDetail:
    owner_user_id = _user_context.internal_user_id
    with session() as db:
        campaign, turns, truncated = db.get_campaign_with_turns_for_owner(
            campaign_id, owner_user_id
        )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignDetail(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        description=campaign.description,
        messages=[
            CampaignTurn(
                turn_id=turn.turn_id,
                role=turn.role,
                content=turn.content,
                created_at=turn.created_at,
            )
            for turn in turns
        ],
        truncated=truncated,
    )


@router.delete("/campaign/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: str,
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> None:
    owner_user_id = _user_context.internal_user_id
    with session() as db:
        deleted = db.delete_campaign_for_owner(
            campaign_id=campaign_id, owner_user_id=owner_user_id
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Campaign not found")


@router.get("/campaigns", response_model=list[CampaignSummary])
async def list_campaigns(
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> list[CampaignSummary]:
    owner_user_id = _user_context.internal_user_id
    with session() as db:
        campaigns = db.list_campaigns_for_owner(owner_user_id)

    summaries: list[CampaignSummary] = []
    for campaign in campaigns:
        campaign_id = campaign["campaign_id"]
        name = campaign["name"]
        if campaign_id is None or name is None:
            continue
        summaries.append(
            CampaignSummary(
                campaign_id=campaign_id,
                name=name,
                last_message=campaign["last_message"],
            )
        )
    return summaries


@router.get("/character/{character_id}", response_model=CharacterInfo)
async def get_character(
    character_id: str,
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> CharacterInfo:
    owner_user_id = _user_context.internal_user_id
    with session() as db:
        character = db.get_character_for_owner(character_id, owner_user_id)
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    return CharacterInfo(character_id=character.character_id, name=character.name)


@router.get("/characters", response_model=CharacterList)
async def list_characters(
    _user_context: AuthenticatedUserContext = Depends(
        require_authenticated_user_context
    ),
) -> CharacterList:
    owner_user_id = _user_context.internal_user_id
    with session() as db:
        characters = db.list_characters_for_owner(owner_user_id)
    return CharacterList(
        characters=[
            CharacterInfo(character_id=character.character_id, name=character.name)
            for character in characters
        ]
    )
