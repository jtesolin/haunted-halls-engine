from fastapi import HTTPException

from app.core.config import settings


def estimate_tokens(text: str) -> int:
    return max(1, int(round(len(text) / 4)))


def validate_request_budget(estimated_input_tokens: int, max_output_tokens: int) -> None:
    if estimated_input_tokens + max_output_tokens > settings.MAX_ESTIMATED_INPUT_TOKENS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Request exceeds per-request token budget. "
                f"estimated_input_tokens={estimated_input_tokens}, max_output_tokens={max_output_tokens}, "
                f"limit={settings.MAX_ESTIMATED_INPUT_TOKENS}."
            ),
        )
