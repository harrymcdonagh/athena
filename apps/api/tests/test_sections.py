import pytest

from apps.api.edgar.sections import SectionExtractionError, extract_sections

BUSINESS = "We design and sell widgets. Revenue was $391,035 million in fiscal 2025. " * 20
RISKS = "Competition may harm margins. Supply chain concentration in one region. " * 20
MDNA = "Net sales increased 2% to $391 billion driven by services growth of 13%. " * 20


def build_10k_html() -> str:
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved Staff Comments ... 45</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
        "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
        "<h2>Item 5. Market</h2><p>Common stock is listed on Nasdaq.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis of Financial Condition</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2><p>Rates.</p>"
    )
    return f"<html><body>{toc}{body}</body></html>"


def test_extracts_all_three_sections_past_the_toc() -> None:
    sections = extract_sections(build_10k_html())
    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Revenue was $391,035 million" in sections["business"]
    assert "Supply chain concentration" in sections["risk_factors"]
    assert "services growth of 13%" in sections["mdna"]
    # slices must not bleed into the next item
    assert "Unresolved Staff Comments" not in sections["risk_factors"].rstrip(" .")


def test_missing_section_raises() -> None:
    with pytest.raises(SectionExtractionError):
        extract_sections("<html><body><p>Item 1. Business</p><p>short</p></body></html>")


def test_too_short_section_raises() -> None:
    html = (
        "<html><body><h2>Item 1. Business</h2><p>tiny</p>"
        "<h2>Item 1A. Risk Factors</h2><p>tiny</p>"
        "<h2>Item 1B. Unresolved</h2>"
        "<h2>Item 7. Management's Discussion</h2><p>tiny</p>"
        "<h2>Item 7A. Quantitative</h2></body></html>"
    )
    with pytest.raises(SectionExtractionError, match="too short"):
        extract_sections(html)
