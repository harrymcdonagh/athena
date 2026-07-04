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


def test_extracts_sections_when_item_1b_is_omitted() -> None:
    # Some companies omit "Item 1B. Unresolved Staff Comments" entirely; Item 1A
    # runs directly into Item 2. Properties.
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 2. Properties ... 40</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
        "<h2>Item 2. Properties</h2><p>We lease office space.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis of Financial Condition</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2><p>Rates.</p>"
    )
    html = f"<html><body>{toc}{body}</body></html>"

    sections = extract_sections(html)

    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Revenue was $391,035 million" in sections["business"]
    assert "Supply chain concentration" in sections["risk_factors"]
    assert "services growth of 13%" in sections["mdna"]
    # risk_factors must stop at Item 2 and not bleed into Properties
    assert "lease office space" not in sections["risk_factors"]


def test_missing_section_raises() -> None:
    with pytest.raises(SectionExtractionError):
        extract_sections("<html><body><p>Item 1. Business</p><p>short</p></body></html>")


def test_extracts_sections_with_letter_split_headings() -> None:
    # Some filings style headings with per-letter/small-caps spans, so
    # BeautifulSoup's get_text(" ") yields spaces inside words, e.g.
    # "Item 1. B usiness" instead of "Item 1. Business".
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved Staff Comments ... 45</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. B<span>usiness</span></h2><p>{BUSINESS}</p>"
        f"<h2>Item 1A. R<span>isk</span> F<span>actors</span></h2><p>{RISKS}</p>"
        "<h2>Item 1B. U<span>nresolved</span> S<span>taff</span> "
        "C<span>omments</span></h2><p>None.</p>"
        "<h2>Item 5. Market</h2><p>Common stock is listed on Nasdaq.</p>"
        f"<h2>Item 7. M<span>anagement</span>'s Discussion and Analysis</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Q<span>uantitative</span> and Qualitative "
        "Disclosures</h2><p>Rates.</p>"
    )
    html = f"<html><body>{toc}{body}</body></html>"

    sections = extract_sections(html)

    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Revenue was $391,035 million" in sections["business"]
    assert "Supply chain concentration" in sections["risk_factors"]
    assert "services growth of 13%" in sections["mdna"]
    assert "Unresolved Staff Comments" not in sections["risk_factors"].rstrip(" .")


def test_cross_references_are_not_treated_as_headings() -> None:
    # Real 10-Ks contain cross-references deep in the notes, e.g.
    # `Refer to "Item 1A. Risk Factors" ...` — these must not be mistaken
    # for the actual heading (which would make the filter look for a
    # section end that never comes after it) or for a section end (which
    # would silently truncate an earlier section).
    trailing = (
        "<p>Refer to “Item 1A. Risk Factors” for additional information. "
        "This discussion should be read in conjunction with "
        "“Item 1A. Risk Factors,” our financial statements and the "
        "related notes. " + ("Additional cross-reference context. " * 20) + "</p>"
    )
    assert len(trailing) > 600
    html = build_10k_html().replace("</body></html>", trailing + "</body></html>")

    sections = extract_sections(html)

    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert sections["risk_factors"].startswith("Item 1A. Risk Factors")
    assert "Competition may harm margins" in sections["risk_factors"]
    assert "Additional cross-reference context" not in sections["business"]
    assert "Additional cross-reference context" not in sections["risk_factors"]
    assert "Additional cross-reference context" not in sections["mdna"]


def test_cross_reference_filter_falls_back_when_all_matches_filtered() -> None:
    # If every occurrence of a heading happens to be quoted (an unseen
    # filing style), the filter would remove all candidates. The
    # `filtered or positions` fallback must recover the genuine headings
    # instead of raising.
    body = (
        f"<p>“Item 1. Business”</p><p>{BUSINESS}</p>"
        f"<p>“Item 1A. Risk Factors”</p><p>{RISKS}</p>"
        "<p>“Item 1B. Unresolved Staff Comments”</p><p>None.</p>"
        f"<p>“Item 7. Management's Discussion and Analysis”</p><p>{MDNA}</p>"
        "<p>“Item 7A. Quantitative and Qualitative Disclosures”</p><p>Rates.</p>"
    )
    html = f"<html><body>{body}</body></html>"

    sections = extract_sections(html)

    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Revenue was $391,035 million" in sections["business"]
    assert "Supply chain concentration" in sections["risk_factors"]
    assert "services growth of 13%" in sections["mdna"]


def test_word_ending_in_see_does_not_mask_real_heading() -> None:
    # A body paragraph legitimately ends with a word like "licensee" right
    # before the real Item 1A heading. The bare "see" lead-in must not match
    # inside "licensee" (a token-boundary check is required) — otherwise the
    # genuine heading is misclassified as a cross-reference and filtered
    # out, leaving only the (unfiltered) TOC candidate to win.
    business_body = BUSINESS + " We operate certain retail stores as the licensee"
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved Staff Comments ... 45</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. Business</h2><p>{business_body}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
        "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
        "<h2>Item 5. Market</h2><p>Common stock is listed on Nasdaq.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis of Financial Condition</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2><p>Rates.</p>"
    )
    html = f"<html><body>{toc}{body}</body></html>"

    sections = extract_sections(html)

    assert sections["risk_factors"].startswith("Item 1A. Risk Factors")
    assert "Competition may harm margins" in sections["risk_factors"]
    # If "licensee" were mistaken for the "see" lead-in, the genuine heading
    # would be dropped as a cross-reference and risk_factors would instead
    # start at the earlier TOC line, bleeding the entire business section in.
    assert "Revenue was $391,035 million" not in sections["risk_factors"]


def test_mid_section_cross_reference_does_not_truncate_section() -> None:
    # The NVDA bug: a cross-reference to the next section's heading can sit
    # *inside* the current section's own body (not just in trailing notes).
    # The nearest END match being a cross-reference must not truncate the
    # section early — extraction must keep scanning to the real heading.
    post_cross_ref = "Additional business detail after the cross-reference. " * 20
    assert len(post_cross_ref) >= 600
    business_body = (
        BUSINESS + "Refer to “Item 1A. Risk Factors” for more information. " + post_cross_ref
    )
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved Staff Comments ... 45</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. Business</h2><p>{business_body}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
        "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
        "<h2>Item 5. Market</h2><p>Common stock is listed on Nasdaq.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis of Financial Condition</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2><p>Rates.</p>"
    )
    html = f"<html><body>{toc}{body}</body></html>"

    sections = extract_sections(html)

    assert "Revenue was $391,035 million" in sections["business"]
    assert "Additional business detail after the cross-reference" in sections["business"]
    assert sections["risk_factors"].startswith("Item 1A. Risk Factors")
    assert "Competition may harm margins" in sections["risk_factors"]


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
