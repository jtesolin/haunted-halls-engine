from typing import Literal


class ModelPolicy:
    NARRATOR: Literal["gpt-4o-mini"] = "gpt-4o-mini"
    ACTION_PARSER: Literal["gpt-4o-mini"] = "gpt-4o-mini"
    SUMMARIZER: Literal["gpt-4o-mini"] = "gpt-4o-mini"
    DIRECTOR: Literal["gpt-4.1-mini"] = "gpt-4.1-mini"

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
