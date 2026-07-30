from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_internal_service_auth
from app.db.session import session
from app.schemas.internal_auth import ResolveInternalUserRequest, ResolveInternalUserResponse

router = APIRouter(
    prefix="/internal/auth/users",
    tags=["internal-auth"],
    dependencies=[Depends(require_internal_service_auth)],
)


@router.post("/resolve", response_model=ResolveInternalUserResponse)
def resolve_user(payload: ResolveInternalUserRequest) -> ResolveInternalUserResponse:
    with session() as db:
        user = db.resolve_internal_user(
            identity_provider=payload.identity_provider,
            provider_issuer=payload.provider_issuer,
            provider_subject=payload.provider_subject,
            email=str(payload.email),
            email_verified=payload.email_verified,
            display_name=payload.display_name,
            avatar_url=str(payload.avatar_url) if payload.avatar_url is not None else None,
        )

    return ResolveInternalUserResponse(user_id=user.id)
