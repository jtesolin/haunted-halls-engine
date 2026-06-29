from typing import Literal


class ModelPolicy:
    NARRATOR: Literal["gpt-3.5-turbo"] = "gpt-3.5-turbo"
    ACTION_PARSER: Literal["gpt-3.5-turbo"] = "gpt-3.5-turbo"
    SUMMARIZER: Literal["gpt-3.5-turbo"] = "gpt-3.5-turbo"
    DIRECTOR: Literal["gpt-4o"] = "gpt-4o"

    @classmethod
    def narrator_model(cls) -> str:
        return cls.NARRATOR

    @classmethod
    def action_parser_model(cls) -> str:
        return cls.ACTION_PARSER

    @classmethod
    def summarizer_model(cls) -> str:
        return cls.SUMMARIZER

    @classmethod
    def director_model(cls) -> str:
        return cls.DIRECTOR
