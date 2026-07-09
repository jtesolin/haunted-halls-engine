from fastapi import HTTPException

from app.core.config import settings


class TokenBudget:
    NARRATOR_MAX_OUTPUT_TOKENS = 500
    ACTION_PARSER_MAX_OUTPUT_TOKENS = 320
    SUMMARIZER_MAX_OUTPUT_TOKENS = 180
    MEMORY_REFLECTION_MAX_OUTPUT_TOKENS = 180

    @classmethod
    def narrator_max_output_tokens(cls) -> int:
        return cls.NARRATOR_MAX_OUTPUT_TOKENS

    @classmethod
    def action_parser_max_output_tokens(cls) -> int:
        return cls.ACTION_PARSER_MAX_OUTPUT_TOKENS

    @classmethod
    def summarizer_max_output_tokens(cls) -> int:
        return cls.SUMMARIZER_MAX_OUTPUT_TOKENS

    @classmethod
    def memory_reflection_max_output_tokens(cls) -> int:
        return cls.MEMORY_REFLECTION_MAX_OUTPUT_TOKENS


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
