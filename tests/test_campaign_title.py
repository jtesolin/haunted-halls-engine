import asyncio

from app.agents import campaign_title as title_module
from app.agents.campaign_title import CampaignTitleGenerator, CampaignTitleInput
from app.ai.model_client import ModelCallResult, ModelUsage
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget


def test_title_generator_request_shape_and_usage(monkeypatch) -> None:
    captured: dict = {}

    async def fake_generate_text(**kwargs):
        captured.update(kwargs)
        return ModelCallResult(
            output="The Bell",
            usage=ModelUsage(input_tokens=90, output_tokens=8, total_tokens=98),
        )

    monkeypatch.setattr(title_module.model_client, "generate_text", fake_generate_text)

    output = asyncio.run(
        CampaignTitleGenerator().generate(payload=CampaignTitleInput(opening="A bell tolls."))
    )

    assert output.title == "The Bell"
    assert (output.input_tokens, output.output_tokens, output.total_tokens) == (90, 8, 98)
    assert captured["model"] == ModelPolicy.campaign_title_model()
    assert captured["reasoning_effort"] == ModelPolicy.campaign_title_reasoning_effort()
    assert captured["max_output_tokens"] == TokenBudget.campaign_title_max_output_tokens()
    assert [m["role"] for m in captured["messages"]] == ["developer", "user"]
    assert "A bell tolls." in captured["messages"][1]["content"]
    assert CampaignTitleGenerator().name == "CampaignTitleGenerator"


def test_title_generator_blank_output_keeps_usage(monkeypatch) -> None:
    async def fake_generate_text(**kwargs):
        return ModelCallResult(output=None, usage=ModelUsage(input_tokens=5, total_tokens=5))

    monkeypatch.setattr(title_module.model_client, "generate_text", fake_generate_text)

    output = asyncio.run(
        CampaignTitleGenerator().generate(payload=CampaignTitleInput(opening="x"))
    )

    assert output.title == ""
    assert output.total_tokens == 5


def test_title_prompt_includes_premise_and_objective_and_avoids_room_naming() -> None:
    messages = CampaignTitleGenerator()._build_messages(
        CampaignTitleInput(opening="An Entry Hall.", premise="A bell tolls.", objective="Find who rings it.")
    )
    assert "Premise: A bell tolls." in messages[1]["content"]
    assert "Objective: Find who rings it." in messages[1]["content"]
    assert "not the starting room" in messages[0]["content"]
    bare = CampaignTitleGenerator()._build_messages(CampaignTitleInput(opening="x"))
    assert "Premise" not in bare[1]["content"]
