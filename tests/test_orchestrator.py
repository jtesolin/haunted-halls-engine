import pytest

from app.orchestration.orchestrator import ChatOrchestrator


def test_campaign_title_generation_does_not_fabricate_a_stub_title() -> None:
    with pytest.raises(ValueError, match="Campaign title generation returned empty output"):
        ChatOrchestrator()._normalize_campaign_title(" \n ")
