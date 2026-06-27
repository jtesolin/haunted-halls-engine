from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    MODEL_PROVIDER: Optional[str] = None
    MODEL_NAME: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    INTERNAL_API_TOKEN: Optional[str] = None

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
