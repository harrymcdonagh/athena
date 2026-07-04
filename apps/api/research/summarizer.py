from typing import Protocol

import anthropic

_MODEL = "claude-sonnet-5"

_SYSTEM_PROMPT = (
    "You are Athena, an investment research assistant. You summarize SEC filings for a "
    "personal research file.\n"
    "Rules:\n"
    "- Summarize only what the filing states. Preserve exact figures (revenue, margins, "
    "unit counts, dates, percentages) exactly as written.\n"
    "- NEVER give buy/sell/hold recommendations, price targets, or investment advice of "
    "any kind. You summarize and cite; conclusions are the reader's responsibility.\n"
    "- Write clear markdown. End with a line `Source: <filing URL>`."
)

_SECTION_TITLES = {
    "business": "Item 1 — Business",
    "risk_factors": "Item 1A — Risk Factors",
    "mdna": "Item 7 — Management's Discussion and Analysis",
}


class SummarizationError(Exception):
    pass


class Summarizer(Protocol):
    @property
    def model(self) -> str: ...

    def summarize(self, section: str, text: str, source_url: str) -> str: ...


def build_prompt(section: str, text: str, source_url: str) -> str:
    title = _SECTION_TITLES[section]
    return (
        f"Summarize the {title} section of this 10-K filing.\n"
        f"Source filing URL: {source_url}\n\n"
        "Requirements:\n"
        "- 300-500 words of markdown, organised with short headings or bullets.\n"
        "- Preserve exact figures as stated in the filing.\n"
        "- Focus on thesis-relevant facts: what the business does, how it makes money, "
        "material risks, and drivers of results.\n"
        "- No investment recommendation or opinion of any kind.\n"
        f"- End with the line: Source: {source_url}\n\n"
        f"<section>\n{text}\n</section>"
    )


class ClaudeSummarizer:
    model = _MODEL

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def summarize(self, section: str, text: str, source_url: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(section, text, source_url)}],
        )
        parts = [block.text for block in response.content if block.type == "text"]
        if not parts:
            raise SummarizationError(
                f"model returned no text for section {section!r} (stop_reason="
                f"{response.stop_reason!r})"
            )
        return "\n".join(parts).strip()
