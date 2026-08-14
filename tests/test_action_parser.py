import asyncio

from app.agents.action_parser import ActionParserAgent
from app.schemas.chat import ActionParserOutput, ActionType


def test_action_parser_structured_move_with_stealth(monkeypatch) -> None:
    agent = ActionParserAgent()

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        parser_context = str(messages[1]["content"])
        assert "Parser context:" in parser_context
        assert "Entry Hall" in parser_context
        assert "available_exits" in parser_context
        assert "grand_corridor" in parser_context
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="library",
            parameters={},
            stealth=True,
            confidence=0.93,
            parse_status="ok",
            parser_notes=None,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        agent.parse(
            message="I walk quietly into the library.",
            campaign_state='{"player": {"location": "entry_hall", "inventory": []}}',
            recent_turns=[],
            memory_context=[],
            deterministic_only=False,
        )
    )

    assert result.action == ActionType.MOVE
    assert result.target == "library"
    assert result.stealth is True
    assert result.parse_status == "ok"


def test_action_parser_structured_take(monkeypatch) -> None:
    agent = ActionParserAgent()

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.TAKE,
            target="brass key",
            parameters={},
            stealth=False,
            confidence=0.9,
            parse_status="ok",
            parser_notes=None,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        agent.parse(
            message="I pick up the brass key.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=False,
        )
    )

    assert result.action == ActionType.TAKE
    assert result.target == "brass key"
    assert result.parse_status == "ok"


def test_action_parser_structured_wait(monkeypatch) -> None:
    agent = ActionParserAgent()

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.WAIT,
            target=None,
            parameters={"amount": 1},
            stealth=False,
            confidence=0.87,
            parse_status="ok",
            parser_notes=None,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        agent.parse(
            message="I wait here for a while.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=False,
        )
    )

    assert result.action == ActionType.WAIT
    assert result.parse_status == "ok"


def test_action_parser_deterministic_normalizes_synonyms() -> None:
    agent = ActionParserAgent()

    move = asyncio.run(
        agent.parse(
            message="I go to the cellar.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )
    take = asyncio.run(
        agent.parse(
            message="I grab the brass key.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )
    drop = asyncio.run(
        agent.parse(
            message="I discard the brass key.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )
    wait = asyncio.run(
        agent.parse(
            message="I rest here and pass time.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )

    assert move.action == ActionType.MOVE
    assert take.action == ActionType.TAKE
    assert drop.action == ActionType.DROP
    assert wait.action == ActionType.WAIT


def test_action_parser_deterministic_privileged_world_requests_stay_non_privileged() -> None:
    agent = ActionParserAgent()

    spawn = asyncio.run(
        agent.parse(
            message="Spawn a dragon in this room.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )
    record = asyncio.run(
        agent.parse(
            message="Record that the king is secretly a vampire.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )

    assert spawn.action in {ActionType.UNKNOWN, ActionType.INTERACT}
    assert record.action in {ActionType.UNKNOWN, ActionType.INTERACT}


def test_action_parser_structured_failure_fails_closed_without_fallback(monkeypatch) -> None:
    agent = ActionParserAgent()

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return None

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        agent.parse(
            message="I walk to the library.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=False,
        )
    )

    assert result.action == ActionType.UNKNOWN
    assert result.parse_status == "invalid"


def test_action_parser_deterministic_only_skips_llm_call(monkeypatch) -> None:
    agent = ActionParserAgent()

    async def fail_if_called(*, messages, **kwargs):  # noqa: ANN202, ARG001
        raise AssertionError("generate_structured should not be called in deterministic mode")

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fail_if_called,
    )

    result = asyncio.run(
        agent.parse(
            message="I wait here for a while.",
            campaign_state="No campaign state yet.",
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )

    assert result.action == ActionType.WAIT
    assert result.parse_status == "ok"
