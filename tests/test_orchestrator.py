from app.orchestration.orchestrator import DEFAULT_CAMPAIGN_TITLE, ChatOrchestrator


def test_blank_campaign_title_falls_back_to_default_title() -> None:
    orchestrator = ChatOrchestrator()
    for blank in ("", " \n ", '""', "'  '"):
        assert orchestrator._normalize_campaign_title(blank) == DEFAULT_CAMPAIGN_TITLE


def test_campaign_title_uses_first_line_and_is_bounded() -> None:
    orchestrator = ChatOrchestrator()
    assert orchestrator._normalize_campaign_title("The Bell\nextra") == "The Bell"
    assert len(orchestrator._normalize_campaign_title("x" * 200)) == 80
