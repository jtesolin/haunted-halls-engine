import os
import secrets

from fastapi import Header, HTTPException

from app.core.config import settings


_INTERNAL_ENGINE_SERVICE_TOKEN_PLACEHOLDERS = {
    "floop",
    "replace-with-internal-engine-token",
    "generate-with-openssl-do-not-commit",
}


def get_internal_engine_service_token() -> str:
    token = (settings.INTERNAL_ENGINE_SERVICE_TOKEN or "").strip()

    if not token:
        raise RuntimeError("INTERNAL_ENGINE_SERVICE_TOKEN is not configured")

    if token in _INTERNAL_ENGINE_SERVICE_TOKEN_PLACEHOLDERS:
        raise RuntimeError(
            "INTERNAL_ENGINE_SERVICE_TOKEN must be at least 32 bytes of random entropy"
        )

    if not os.getenv("PYTEST_CURRENT_TEST") and len(token) < 64:
        raise RuntimeError(
            "INTERNAL_ENGINE_SERVICE_TOKEN must be at least 32 bytes of random entropy"
        )

    return token


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""

    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer":
        return ""

    return token.strip()


def require_internal_service_auth(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    token = _extract_bearer_token(authorization)
    expected_token = get_internal_engine_service_token()

    if not token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
