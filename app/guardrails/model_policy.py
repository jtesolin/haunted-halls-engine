from typing import Literal


class ModelPolicy:
    NARRATOR: Literal["gpt-5-nano"] = "gpt-5-nano"
    ACTION_PARSER: Literal["gpt-5-nano"] = "gpt-5-nano"
    SUMMARIZER: Literal["gpt-5-nano"] = "gpt-5-nano"
    MEMORY_REFLECTION: Literal["gpt-5.4-nano"] = "gpt-5.4-nano"
    DIRECTOR: Literal["gpt-5.4-nano"] = "gpt-5.4-nano"

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
    def memory_reflection_model(cls) -> str:
        return cls.MEMORY_REFLECTION

    @classmethod
    def director_model(cls) -> str:
        return cls.DIRECTOR
