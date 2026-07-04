import re

from bs4 import BeautifulSoup

SECTIONS: tuple[str, ...] = ("business", "risk_factors", "mdna")

_MIN_SECTION_CHARS = 500

_BOUNDS: dict[str, tuple[str, str]] = {
    "business": (
        r"item1[.:–—-]?business",
        r"item1a[.:–—-]?riskfactors",
    ),
    "risk_factors": (
        r"item1a[.:–—-]?riskfactors",
        r"item1b[.:–—-]?unresolved|item2[.:–—-]?propert",
    ),
    "mdna": (
        r"item7[.:–—-]?management",
        r"item7a[.:–—-]?quantitative",
    ),
}


class SectionExtractionError(Exception):
    pass


_CROSS_REFERENCE_QUOTES = set("“”\"'’‘")
_CROSS_REFERENCE_LEAD_INS = ("referto", "see", "seealso", "inconjunctionwith")


def _is_cross_reference(squashed: str, pos: int) -> bool:
    # Real headings are never quoted and are never preceded by phrases like
    # "refer to" / "see" / "see also" / "in conjunction with" — but
    # cross-references deep in the notes (e.g. `Refer to "Item 1A. Risk
    # Factors" ...`) almost always are one or the other. Treat a match as a
    # cross-reference, not a heading, when either signal is present.
    if pos > 0 and squashed[pos - 1] in _CROSS_REFERENCE_QUOTES:
        return True
    preceding = squashed[max(0, pos - 30) : pos]
    for lead_in in _CROSS_REFERENCE_LEAD_INS:
        if not preceding.endswith(lead_in):
            continue
        idx = pos - len(lead_in)
        # Require a token boundary before the lead-in itself: without this,
        # the bare "see" lead-in also matches word-internal endings like
        # "licensee"/"oversee"/"lessee", which would misclassify a genuine
        # heading as a cross-reference.
        if idx <= 0 or not squashed[idx - 1].isalnum():
            return True
    return False


def extract_sections(html: str) -> dict[str, str]:
    text = _html_to_text(html)
    lowered = text.lower()

    # Headings are sometimes styled with per-letter/small-caps spans, so
    # get_text(" ") can yield spaces inside words (e.g. "b usiness"). Match
    # against a whitespace-stripped shadow of the text, then map matched
    # positions back to the original (naturally spaced) text for slicing.
    squashed_chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(lowered):
        if not ch.isspace():
            squashed_chars.append(ch)
            offsets.append(i)
    squashed = "".join(squashed_chars)

    sections: dict[str, str] = {}
    for section, (start_pattern, end_pattern) in _BOUNDS.items():
        starts = [m.start() for m in re.finditer(start_pattern, squashed)]
        if not starts:
            raise SectionExtractionError(f"could not locate the start of section {section!r}")
        filtered_starts = [p for p in starts if not _is_cross_reference(squashed, p)] or starts
        start = max(filtered_starts)  # last occurrence: TOC entries come first, the body last
        ends = [m.start() for m in re.finditer(end_pattern, squashed) if m.start() > start]
        if not ends:
            raise SectionExtractionError(f"could not locate the end of section {section!r}")
        filtered_ends = [p for p in ends if not _is_cross_reference(squashed, p)] or ends
        end = min(filtered_ends)
        content = text[offsets[start] : offsets[end]].strip()
        if len(content) < _MIN_SECTION_CHARS:
            raise SectionExtractionError(
                f"section {section!r} too short ({len(content)} chars) — extraction likely failed"
            )
        sections[section] = content
    return sections


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" "))
