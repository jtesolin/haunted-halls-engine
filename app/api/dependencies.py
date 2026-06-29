from fastapi import Header, HTTPException

from app.core.config import settings


def require_internal_api_token(authorization: str | None = Header(None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization token.")

    token = authorization.removeprefix("Bearer ").strip()
    if not settings.INTERNAL_API_TOKEN or token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid authorization token.")
