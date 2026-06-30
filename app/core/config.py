from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    AI_ENABLED: bool = False
    OPENAI_API_KEY: Optional[str] = None
    # ANTHROPIC_API_KEY: Optional[str] = None
    # DEFAULT_MODEL_PROVIDER: Optional[str] = None
    DEFAULT_MODEL_NAME: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    INTERNAL_API_TOKEN: Optional[str] = "floop"

    MAX_INPUT_CHARACTERS: int = 2000
    MAX_OUTPUT_TOKENS: int = 500
    MAX_RECENT_MESSAGES: int = 10
    MAX_DAILY_PLAYER_REQUESTS: int = 50
    MAX_DAILY_PLAYER_TOKENS: int = 4000
    MAX_CAMPAIGNS_PER_PLAYER: int = 10
    MAX_TURNS_PER_CAMPAIGN: int = 20
    MAX_DAILY_PROJECT_REQUESTS: int = 1000
    MAX_DAILY_PROJECT_TOKENS: int = 50000
    MAX_ESTIMATED_INPUT_TOKENS: int = 4000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
