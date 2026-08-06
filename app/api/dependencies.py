import os
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from app.core.config import settings
from app.db.models import is_valid_internal_user_id
from app.db.session import session


_INTERNAL_ENGINE_SERVICE_TOKEN_PLACEHOLDERS = {
    "floop",
    "replace-with-internal-engine-token",
    "generate-with-openssl-do-not-commit",
}

INTERNAL_USER_ID_HEADER_NAME = "X-Haunted-Halls-User-Id"
INTERNAL_SERVICE_CALLER_NAME = "haunted-halls-bff"


@dataclass(frozen=True)
class InternalServiceContext:
    caller_name: str


@dataclass(frozen=True)
class AuthenticatedUserContext:
    service_caller_name: str
    internal_user_id: str


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
) -> InternalServiceContext:
    token = _extract_bearer_token(authorization)
    expected_token = get_internal_engine_service_token()

    if not token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return InternalServiceContext(caller_name=INTERNAL_SERVICE_CALLER_NAME)


def require_authenticated_user_context(
    request: Request,
    service_context: InternalServiceContext = Depends(require_internal_service_auth),
) -> AuthenticatedUserContext:
    user_ids = request.headers.getlist(INTERNAL_USER_ID_HEADER_NAME)

    if len(user_ids) != 1:
        raise HTTPException(status_code=401, detail="Unauthorized")

    internal_user_id = user_ids[0].strip()
    if not internal_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not is_valid_internal_user_id(internal_user_id):
        raise HTTPException(status_code=401, detail="Unauthorized")

    with session() as db:
        user = db.get_internal_user_by_id(internal_user_id)

    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return AuthenticatedUserContext(
        service_caller_name=service_context.caller_name,
        internal_user_id=internal_user_id,
    )
