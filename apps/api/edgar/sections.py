import re

from bs4 import BeautifulSoup

SECTIONS: tuple[str, ...] = ("business", "risk_factors", "mdna")

_MIN_SECTION_CHARS = 500

# Separator class includes "|": some filers style headings as
# "ITEM 1A | Risk Factors" (e.g. AIG), and "|" survives whitespace-squashing.
_BOUNDS: dict[str, tuple[str, str]] = {
    "business": (
        r"item1[.:|–—-]?business",
        r"item1a[.:|–—-]?riskfactors",
    ),
    "risk_factors": (
        r"item1a[.:|–—-]?riskfactors",
        r"item1b[.:|–—-]?unresolved|item2[.:|–—-]?propert",
    ),
    "mdna": (
        r"item7[.:|–—-]?management",
        r"item7a[.:|–—-]?quantitative",
    ),
}


class SectionExtractionError(Exception):
    pass


_CROSS_REFERENCE_QUOTES = set("“”\"'’‘")

# Running-prose citations end in a lead-in word — "see", "contained in",
# "section of", "refer to", "competition and", "in conjunction with" — all
# reducible to their final word, optionally followed by a short page/footnote
# number ("see 14 Item 1A"). Genuine headings are preceded by page furniture
# instead: page numbers, "TABLE OF CONTENTS", a bare "PART I".
_LEAD_IN_RE = re.compile(
    r"\b(?:see|refer|in|of|to|and|with|under|within|per|also|entitled)"
    r"\s*[:,]?\s*[\"“”'’]?\s*(?:\d{1,3})?$",
    re.IGNORECASE,
)
# "Part I," / "Part 1—" immediately before the match is the prose citation
# form ("see Part I, Item 1A. Risk Factors"). The comma/dash is load-bearing:
# a genuine heading can follow a bare "PART I" divider, which must be kept.
_PART_CITATION_RE = re.compile(r"\bpart\s*(?:[ivx]+|\d+)\s*[,;–—-]\s*$", re.IGNORECASE)


def _is_cross_reference(text: str, char_pos: int) -> bool:
    # Judged against the ORIGINAL text, where word boundaries survive: in the
    # squashed shadow "competition and" is indistinguishable from a word that
    # merely ends in "and" (the licensee/see problem generalized).
    stripped = text[max(0, char_pos - 60) : char_pos].rstrip()
    if stripped and stripped[-1] in _CROSS_REFERENCE_QUOTES:
        return True
    if _PART_CITATION_RE.search(stripped):
        return True
    return bool(_LEAD_IN_RE.search(stripped))


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
        starts_all = [m.start() for m in re.finditer(start_pattern, squashed)]
        if not starts_all:
            raise SectionExtractionError(f"could not locate the start of section {section!r}")
        ends_all = [m.start() for m in re.finditer(end_pattern, squashed)]
        start, end = _best_bounds(section, text, offsets, starts_all, ends_all)
        content = text[offsets[start] : offsets[end]].strip()
        if len(content) < _MIN_SECTION_CHARS:
            raise SectionExtractionError(
                f"section {section!r} too short ({len(content)} chars) — extraction likely failed"
            )
        sections[section] = content
    return sections


def _best_bounds(
    section: str,
    text: str,
    offsets: list[int],
    starts_all: list[int],
    ends_all: list[int],
) -> tuple[int, int]:
    """The (start, end) squashed positions bounding the section's body.

    A heading pattern matches many places — the TOC row, the genuine body
    heading, running per-page headers (AIG repeats "ITEM 1A | Risk Factors"
    atop every page), and prose cross-references — so neither the first nor
    the last occurrence is reliable. Instead, pair each candidate start with
    the first candidate end after it and take the pair spanning the MOST
    text: the genuine heading is followed by the whole section body, while a
    TOC row pairs with the adjacent TOC row (tiny span), a page header pairs
    with the next section's heading (one page), and cross-references are
    filtered out up front. If filtering leaves no plausible pair (an unseen
    filing style), retry unfiltered before giving up.
    """
    starts = [p for p in starts_all if not _is_cross_reference(text, offsets[p])] or starts_all
    ends = [p for p in ends_all if not _is_cross_reference(text, offsets[p])] or ends_all
    found_pair = False
    longest = 0
    for start_candidates, end_candidates in ((starts, ends), (starts_all, ends_all)):
        best: tuple[int, int] | None = None
        best_span = 0
        for s in start_candidates:
            e = next((e for e in end_candidates if e > s), None)
            if e is None:
                continue
            found_pair = True
            span = offsets[e] - offsets[s]
            longest = max(longest, span)
            if span >= _MIN_SECTION_CHARS and span > best_span:
                best, best_span = (s, e), span
        if best is not None:
            return best
    if not found_pair:
        raise SectionExtractionError(f"could not locate the end of section {section!r}")
    raise SectionExtractionError(
        f"section {section!r} too short ({longest} chars) — extraction likely failed"
    )


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" "))
