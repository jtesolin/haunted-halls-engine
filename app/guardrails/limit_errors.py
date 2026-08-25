from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException


def utc_day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def utc_day_start_for_accounting() -> str:
    return utc_day_start().replace(tzinfo=None).isoformat()


def next_utc_reset_iso() -> str:
    return (utc_day_start() + timedelta(days=1)).isoformat().replace("+00:00", "Z")


def usage_limit_error(
    *, code: str, detail: str, retry_at: str | None = None
) -> HTTPException:
    error: dict[str, Any] = {
        "code": code,
        "retryable": False,
    }
    if retry_at is not None:
        error["retry_at"] = retry_at
    return HTTPException(status_code=429, detail={"detail": detail, **error})