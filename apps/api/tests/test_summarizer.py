from apps.api.research.summarizer import ClaudeSummarizer, Summarizer, build_prompt


def test_prompt_contains_section_title_source_url_and_text() -> None:
    prompt = build_prompt("risk_factors", "Competition may reduce margins.", "https://sec.gov/x")
    assert "Risk Factors" in prompt
    assert "https://sec.gov/x" in prompt
    assert "Competition may reduce margins." in prompt


def test_prompt_forbids_recommendations() -> None:
    prompt = build_prompt("business", "text", "https://sec.gov/x")
    assert "recommendation" in prompt.lower()


def test_claude_summarizer_satisfies_protocol() -> None:
    summarizer: Summarizer = ClaudeSummarizer(api_key="test")
    assert summarizer.model == "claude-sonnet-5"
