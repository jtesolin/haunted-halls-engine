from typing import Literal

from openai.types.shared.reasoning_effort import ReasoningEffort


class ModelPolicy:
    NARRATOR: Literal["gpt-5-nano"] = "gpt-5-nano"
    ACTION_PARSER: Literal["gpt-5-nano"] = "gpt-5-nano"
    SUMMARIZER: Literal["gpt-5-nano"] = "gpt-5-nano"
    MEMORY_REFLECTION: Literal["gpt-5.4-nano"] = "gpt-5.4-nano"
    DIRECTOR: Literal["gpt-5.4-nano"] = "gpt-5.4-nano"

    NARRATOR_REASONING_EFFORT: ReasoningEffort = "medium"
    ACTION_PARSER_REASONING_EFFORT: ReasoningEffort = "minimal"
    SUMMARIZER_REASONING_EFFORT: ReasoningEffort = "minimal"
    MEMORY_REFLECTION_REASONING_EFFORT: ReasoningEffort = "none"
    DIRECTOR_REASONING_EFFORT: ReasoningEffort = "none"

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

    @classmethod
    def narrator_reasoning_effort(cls) -> ReasoningEffort:
        return cls.NARRATOR_REASONING_EFFORT

    @classmethod
    def action_parser_reasoning_effort(cls) -> ReasoningEffort:
        return cls.ACTION_PARSER_REASONING_EFFORT

    @classmethod
    def summarizer_reasoning_effort(cls) -> ReasoningEffort:
        return cls.SUMMARIZER_REASONING_EFFORT

    @classmethod
    def memory_reflection_reasoning_effort(cls) -> ReasoningEffort:
        return cls.MEMORY_REFLECTION_REASONING_EFFORT

    @classmethod
    def director_reasoning_effort(cls) -> ReasoningEffort:
        return cls.DIRECTOR_REASONING_EFFORT
