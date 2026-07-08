from collections.abc import Sequence
from datetime import date

import anthropic

# anthropic SDK exception types are bound to real httpx; do not use httpx2 here.
import httpx
import pytest
from sqlalchemy import Engine, text

from apps.api.edgar.client import CompanyRef, FilingNotFoundError, FilingRef
from apps.api.research.service import (
    PLAUSIBLE_SECTION_MIN_CHARS,
    PLAUSIBLE_SECTION_MIN_FRACTION,
    ResearchOutcome,
    ResearchService,
    UnsupportedFilingTypeError,
    UpstreamError,
    check_section_plausibility,
    compose_thesis,
)

COMPANY = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
FILING = FilingRef(
    accession_number="0000320193-25-000123",
    form_type="10-K",
    filing_date=date(2025, 11, 1),
    period_end_date=date(2025, 9, 27),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/aapl-10k.htm",
)
PRIOR_FILING = FilingRef(
    accession_number="0000320193-24-000100",
    form_type="10-K",
    filing_date=date(2024, 11, 1),
    period_end_date=date(2024, 9, 28),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/old-10k.htm",
)
EIGHT_K = FilingRef(
    accession_number="0000320193-26-000001",
    form_type="8-K",
    filing_date=date(2026, 1, 5),
    period_end_date=date(2026, 1, 5),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/a8k.htm",
)

BUSINESS = "We design and sell widgets. Revenue was $391,035 million in fiscal 2025. " * 20
RISKS = "Competition may harm margins. Supply chain concentration in one region. " * 20
MDNA = "Net sales increased 2% to $391 billion driven by services growth of 13%. " * 20

HTML = (
    "<html><body>"
    "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
    "<p>Item 1B. Unresolved ... 45</p><p>Item 7. Management's Discussion ... 50</p>"
    "<p>Item 7A. Quantitative ... 80</p>"
    f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
    f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
    "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
    f"<h2>Item 7. Management's Discussion and Analysis</h2><p>{MDNA}</p>"
    "<h2>Item 7A. Quantitative and Qualitative Disclosures</h2><p>Rates.</p>"
    "</body></html>"
)


def make_html(business: str, risks: str, mdna: str, padding: str = "") -> str:
    """The standard fixture layout with controllable section/document sizes.
    `padding` lands after Item 7A, inflating the document without entering any
    section — how a real filing's financial statements dwarf its items."""
    return (
        "<html><body>"
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved ... 45</p><p>Item 7. Management's Discussion ... 50</p>"
        "<p>Item 7A. Quantitative ... 80</p>"
        f"<h2>Item 1. Business</h2><p>{business}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{risks}</p>"
        "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis</h2><p>{mdna}</p>"
        f"<h2>Item 7A. Quantitative and Qualitative Disclosures</h2><p>Rates. {padding}</p>"
        "</body></html>"
    )


class FakeEdgar:
    def __init__(self, filings: Sequence[FilingRef] = (FILING,)) -> None:
        self._filings = list(filings)  # newest first, like EDGAR's recent window

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        return COMPANY

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        for filing in self._filings:
            if filing.form_type == "10-K":
                return filing
        raise FilingNotFoundError(f"no 10-K filing found for CIK {company.cik}")

    def get_filing(self, company: CompanyRef, accession_number: str) -> FilingRef:
        # Exact match only: dash-insensitive normalization is EdgarClient's job
        # and is pinned by test_edgar_client; callers here pass canonical form.
        for filing in self._filings:
            if filing.accession_number == accession_number:
                return filing
        raise FilingNotFoundError(f"accession {accession_number!r} not found")

    def fetch_document(self, filing: FilingRef) -> str:
        return HTML


class FixedDocumentEdgar(FakeEdgar):
    def __init__(self, html: str, filings: Sequence[FilingRef] = (FILING,)) -> None:
        super().__init__(filings)
        self._html = html

    def fetch_document(self, filing: FilingRef) -> str:
        return self._html


class FakeSummarizer:
    model = "fake-model"

    def summarize(self, section: str, text: str, source_url: str) -> str:
        return f"[{section}] summary. Source: {source_url}"


class CountingSummarizer(FakeSummarizer):
    """Records every summarize() call so a test can assert ingest makes ZERO
    calls (ADR-0014 §1) and compute-on-read makes exactly one per pending
    section (§3)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def summarize(self, section: str, text: str, source_url: str) -> str:
        self.calls.append(section)
        return super().summarize(section, text, source_url)


class ExplodingSummarizer(FakeSummarizer):
    def summarize(self, section: str, text: str, source_url: str) -> str:
        raise UpstreamError("anthropic", "boom")


class AnthropicErrorSummarizer(FakeSummarizer):
    def summarize(self, section: str, text: str, source_url: str) -> str:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.APIConnectionError(request=request)


def count(db: Engine, table: str) -> int:
    with db.connect() as conn:
        result: int = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    return result


def test_ingest_persists_source_text_eagerly_and_leaves_summaries_pending(db: Engine) -> None:
    # ADR-0014 §1/§2: ingest writes source_text for all 3 sections (retrieval
    # reads it) but computes NO summaries and composes NO thesis. This is the
    # proof the bleeding stopped — the single most important assertion is the
    # ZERO summarizer calls (see test_ingest_makes_zero_summarizer_calls).
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    outcome = service.run("AAPL")

    assert outcome.status == "ingested"
    assert outcome.summaries == {}  # nothing computed at ingest; sections pending
    assert outcome.thesis_snapshot_id is None  # thesis composed lazily on demand
    assert count(db, "companies") == 1
    assert count(db, "filings") == 1
    assert count(db, "filing_summaries") == 3
    assert count(db, "thesis_snapshots") == 0
    with db.connect() as conn:
        stored_model = conn.execute(
            text("SELECT DISTINCT model FROM filing_summaries")
        ).scalar_one()
        source_text = conn.execute(
            text("SELECT source_text FROM filing_summaries WHERE section = 'business'")
        ).scalar_one()
        pending = conn.execute(
            text("SELECT count(*) FROM filing_summaries WHERE summary IS NULL")
        ).scalar_one()
    assert stored_model == "fake-model"  # the model that WILL summarise, stamped eagerly
    assert "Revenue was $391,035 million" in source_text
    assert pending == 3  # all three sections pending until demanded


def test_ingest_makes_zero_summarizer_calls(db: Engine) -> None:
    summarizer = CountingSummarizer()
    service = ResearchService(edgar=FakeEdgar(), summarizer=summarizer, engine=db)

    service.run("AAPL")

    assert summarizer.calls == []  # the whole point of ADR-0014: no eager spend


def test_summarize_on_demand_computes_once_then_caches(db: Engine) -> None:
    summarizer = CountingSummarizer()
    service = ResearchService(edgar=FakeEdgar(), summarizer=summarizer, engine=db)
    service.run("AAPL")
    assert count(db, "thesis_snapshots") == 0

    view = service.summarize_on_demand("AAPL")

    assert view is not None
    assert set(view.summaries) == {"business", "risk_factors", "mdna"}
    assert sorted(summarizer.calls) == ["business", "mdna", "risk_factors"]  # one per section
    # UPDATE-in-place, not INSERT: still exactly 3 rows, now all non-pending.
    assert count(db, "filing_summaries") == 3
    with db.connect() as conn:
        pending = conn.execute(
            text("SELECT count(*) FROM filing_summaries WHERE summary IS NULL")
        ).scalar_one()
    assert pending == 0
    assert count(db, "thesis_snapshots") == 1  # thesis composed on first demand

    second = service.summarize_on_demand("AAPL")

    assert second is not None
    assert second.summaries == view.summaries
    assert len(summarizer.calls) == 3  # cache hit: NO further summarizer calls
    assert count(db, "thesis_snapshots") == 1  # append-only, not recomposed


def test_summarize_on_demand_unknown_ticker_returns_none(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    assert service.summarize_on_demand("ZZZZ") is None


def test_rerun_same_filing_is_skipped_noop(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    first = service.run("AAPL")

    second = service.run("AAPL")

    assert second.status == "skipped"
    assert second.filing_id == first.filing_id
    assert second.company_id == first.company_id
    assert second.accession_number == first.accession_number
    assert second.summaries == {}
    assert second.thesis_snapshot_id is None
    assert count(db, "filings") == 1
    assert count(db, "filing_summaries") == 3
    # ADR-0014: ingest composes NO thesis; no GET summary demand was made, so
    # the thesis stays lazy (0), not eagerly written at ingest.
    assert count(db, "thesis_snapshots") == 0


def test_run_with_accession_ingests_prior_year_filing(db: Engine) -> None:
    edgar = FakeEdgar(filings=[FILING, PRIOR_FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    service.run("AAPL")

    outcome = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    assert outcome.status == "ingested"
    assert outcome.accession_number == PRIOR_FILING.accession_number
    assert count(db, "companies") == 1
    assert count(db, "filings") == 2
    assert count(db, "filing_summaries") == 6
    # ADR-0014: both filings ingested with summaries pending and no thesis; a
    # thesis composes only on a GET summary demand, which this test never makes.
    assert count(db, "thesis_snapshots") == 0
    with db.connect() as conn:
        source_urls = (
            conn.execute(
                text(
                    "SELECT DISTINCT s.source_url FROM filing_summaries s"
                    " JOIN filings f ON f.id = s.filing_id"
                    " WHERE f.accession_number = :acc"
                ),
                {"acc": PRIOR_FILING.accession_number},
            )
            .scalars()
            .all()
        )
        period = conn.execute(
            text("SELECT period_end_date FROM filings WHERE accession_number = :acc"),
            {"acc": PRIOR_FILING.accession_number},
        ).scalar_one()
    assert source_urls == [PRIOR_FILING.filing_url]
    assert period == PRIOR_FILING.period_end_date


def test_rerun_prior_year_accession_is_skipped_noop(db: Engine) -> None:
    edgar = FakeEdgar(filings=[FILING, PRIOR_FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    service.run("AAPL")
    first = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    second = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    assert second.status == "skipped"
    assert second.filing_id == first.filing_id
    assert second.thesis_snapshot_id is None
    assert count(db, "filings") == 2
    assert count(db, "filing_summaries") == 6
    # ADR-0014: lazy thesis — no demand made, so no snapshot at ingest/skip.
    assert count(db, "thesis_snapshots") == 0


def test_run_with_unknown_accession_raises_filing_not_found(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    with pytest.raises(FilingNotFoundError):
        service.run("AAPL", accession_number="0000320193-99-999999")
    assert count(db, "filings") == 0


def test_run_rejects_non_10k_target(db: Engine) -> None:
    edgar = FakeEdgar(filings=[EIGHT_K, FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    with pytest.raises(UnsupportedFilingTypeError, match="8-K"):
        service.run("AAPL", accession_number=EIGHT_K.accession_number)
    assert count(db, "filings") == 0
    assert count(db, "filing_summaries") == 0


def test_run_rejects_missing_period_end_date(db: Engine) -> None:
    undated = FilingRef(
        accession_number="0000320193-24-000999",
        form_type="10-K",
        filing_date=date(2024, 11, 1),
        period_end_date=None,
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/undated.htm",
    )
    service = ResearchService(
        edgar=FakeEdgar(filings=[undated]), summarizer=FakeSummarizer(), engine=db
    )
    with pytest.raises(UpstreamError) as exc:
        service.run("AAPL")
    assert exc.value.source == "sec-edgar"
    assert count(db, "filings") == 0


def test_summarize_on_demand_failure_leaves_sections_pending(db: Engine) -> None:
    # Ingest makes ZERO summarizer calls, so it succeeds even with an exploding
    # summarizer; the failure surfaces on demand and writes nothing — pending
    # stays pending (honest absence), no thesis composed.
    service = ResearchService(edgar=FakeEdgar(), summarizer=ExplodingSummarizer(), engine=db)
    service.run("AAPL")
    assert count(db, "filing_summaries") == 3

    with pytest.raises(UpstreamError):
        service.summarize_on_demand("AAPL")

    with db.connect() as conn:
        pending = conn.execute(
            text("SELECT count(*) FROM filing_summaries WHERE summary IS NULL")
        ).scalar_one()
    assert pending == 3
    assert count(db, "thesis_snapshots") == 0


def test_summarize_on_demand_wraps_anthropic_error_as_upstream(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=AnthropicErrorSummarizer(), engine=db)
    service.run("AAPL")
    with pytest.raises(UpstreamError) as exc:
        service.summarize_on_demand("AAPL")
    assert exc.value.source == "anthropic"


# --- section-plausibility guard (flag-not-block) ---

# Fraction-tripping layout: healthy-sized business/risk sections, a small (but
# above-floor) mdna, and enough padding that mdna is an implausible fraction of
# the document — the shape of the hijacked-sliver corruption class.
FRACTION_TRIP_HTML = make_html(
    business="b" * 30_000, risks="r" * 30_000, mdna="m" * 1_500, padding="p" * 90_000
)
# Floor-tripping layout: a small document, so the tiny mdna is a healthy
# fraction of it but is broken in absolute terms.
FLOOR_TRIP_HTML = make_html(business="b" * 5_000, risks="r" * 5_000, mdna="m" * 800)
BOTH_TRIP_HTML = make_html(
    business="b" * 30_000, risks="r" * 30_000, mdna="m" * 700, padding="p" * 90_000
)
# SPG's measured corruption (calibration 2026-07-07): stored risk_factors was
# 2,179 chars of an ~689k-char document text (0.32%) — the worst real case.
SPG_SHAPED_HTML = make_html(
    business="b" * 30_571, risks="r" * 2_179, mdna="m" * 87_294, padding="p" * 560_000
)


def run_with_document(db: Engine, html: str) -> ResearchOutcome:
    service = ResearchService(
        edgar=FixedDocumentEdgar(html), summarizer=FakeSummarizer(), engine=db
    )
    return service.run("AAPL")


def test_low_fraction_section_warns_and_is_still_stored(db: Engine) -> None:
    outcome = run_with_document(db, FRACTION_TRIP_HTML)

    assert outcome.status == "ingested"
    assert [w.section for w in outcome.section_warnings] == ["mdna"]
    warning = outcome.section_warnings[0]
    assert warning.checks == ("fraction_of_document",)
    assert warning.fraction < PLAUSIBLE_SECTION_MIN_FRACTION
    assert warning.section_chars >= PLAUSIBLE_SECTION_MIN_CHARS  # floor did NOT fire
    # Flag-not-block: the flagged section is stored and summarized normally.
    assert count(db, "filing_summaries") == 3
    with db.connect() as conn:
        stored = conn.execute(
            text("SELECT source_text FROM filing_summaries WHERE section = 'mdna'")
        ).scalar_one()
    assert "m" * 100 in stored
    assert count(db, "thesis_snapshots") == 0  # ADR-0014: thesis composed on demand, not ingest


def test_section_under_absolute_floor_warns_and_is_still_stored(db: Engine) -> None:
    outcome = run_with_document(db, FLOOR_TRIP_HTML)

    assert outcome.status == "ingested"
    assert [w.section for w in outcome.section_warnings] == ["mdna"]
    warning = outcome.section_warnings[0]
    assert warning.checks == ("absolute_floor",)
    assert warning.fraction >= PLAUSIBLE_SECTION_MIN_FRACTION  # fraction did NOT fire
    assert count(db, "filing_summaries") == 3


def test_healthy_sections_produce_no_warnings(db: Engine) -> None:
    outcome = run_with_document(db, HTML)  # the standard healthy fixture

    assert outcome.status == "ingested"
    assert outcome.section_warnings == ()


def test_spg_shaped_corruption_fires_the_fraction_check(db: Engine) -> None:
    # Reconstructs the real signal that went unflagged: had this guard existed,
    # SPG's hijacked risk_factors would have warned at first ingestion.
    outcome = run_with_document(db, SPG_SHAPED_HTML)

    assert [w.section for w in outcome.section_warnings] == ["risk_factors"]
    warning = outcome.section_warnings[0]
    assert "fraction_of_document" in warning.checks
    assert warning.fraction == pytest.approx(0.0032, rel=0.2)


def test_both_checks_firing_are_both_reported(db: Engine) -> None:
    outcome = run_with_document(db, BOTH_TRIP_HTML)

    assert [w.section for w in outcome.section_warnings] == ["mdna"]
    assert outcome.section_warnings[0].checks == ("fraction_of_document", "absolute_floor")


def test_plausibility_warning_is_logged_on_single_ticker_ingest(
    db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="apps.api.research.service"):
        run_with_document(db, FRACTION_TRIP_HTML)

    assert "AAPL" in caplog.text
    assert FILING.accession_number in caplog.text
    assert "mdna" in caplog.text
    assert "fraction_of_document" in caplog.text


def test_plausibility_thresholds_are_exclusive_boundaries() -> None:
    # A document with no tags or whitespace has a known exact text length.
    document = "<p>" + "x" * 100_000 + "</p>"
    at_fraction = "y" * int(100_000 * PLAUSIBLE_SECTION_MIN_FRACTION)
    at_floor_doc = "<p>" + "x" * 10_000 + "</p>"

    assert check_section_plausibility(document, {"mdna": at_fraction}) == ()
    assert check_section_plausibility(document, {"mdna": at_fraction[:-1]}) != ()
    assert check_section_plausibility(at_floor_doc, {"mdna": "y" * 1_000}) == ()
    floored = check_section_plausibility(at_floor_doc, {"mdna": "y" * 999})
    assert [w.checks for w in floored] == [("absolute_floor",)]


def test_compose_thesis_cites_filing() -> None:
    thesis = compose_thesis(COMPANY, FILING, {"business": "b", "risk_factors": "r", "mdna": "m"})
    assert "Apple Inc." in thesis
    assert FILING.filing_url in thesis
    assert FILING.accession_number in thesis
    assert "recommendation" in thesis.lower()  # the no-advice disclaimer is embedded
