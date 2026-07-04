import re

from bs4 import BeautifulSoup

SECTIONS: tuple[str, ...] = ("business", "risk_factors", "mdna")

_MIN_SECTION_CHARS = 500

_BOUNDS: dict[str, tuple[str, str]] = {
    "business": (
        r"item\s*1\s*[.:–—-]?\s*business",
        r"item\s*1a\s*[.:–—-]?\s*risk\s*factors",
    ),
    "risk_factors": (
        r"item\s*1a\s*[.:–—-]?\s*risk\s*factors",
        r"item\s*1b\s*[.:–—-]?\s*unresolved",
    ),
    "mdna": (
        r"item\s*7\s*[.:–—-]?\s*management",
        r"item\s*7a\s*[.:–—-]?\s*quantitative",
    ),
}


class SectionExtractionError(Exception):
    pass


def extract_sections(html: str) -> dict[str, str]:
    text = _html_to_text(html)
    lowered = text.lower()
    sections: dict[str, str] = {}
    for section, (start_pattern, end_pattern) in _BOUNDS.items():
        starts = [m.start() for m in re.finditer(start_pattern, lowered)]
        if not starts:
            raise SectionExtractionError(f"could not locate the start of section {section!r}")
        start = max(starts)  # last occurrence: TOC entries come first, the body last
        ends = [m.start() for m in re.finditer(end_pattern, lowered) if m.start() > start]
        if not ends:
            raise SectionExtractionError(f"could not locate the end of section {section!r}")
        content = text[start : min(ends)].strip()
        if len(content) < _MIN_SECTION_CHARS:
            raise SectionExtractionError(
                f"section {section!r} too short ({len(content)} chars) — extraction likely failed"
            )
        sections[section] = content
    return sections


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" "))
