import hashlib

from sqlalchemy import Engine, text

from apps.api.edgar.client import FilingRef
from apps.api.research.embeddings import EmbeddedChunk
from apps.api.research.repair import (
    FilingAuditResult,
    RepairReport,
    SectionDiff,
    format_report,
    run_repair,
)
from apps.api.research.repository import Repository
from apps.api.research.service import ResearchService
from apps.api.tests.test_batch import MultiTickerEdgar
from apps.api.tests.test_service import HTML, FakeSummarizer, count

# Distinct from the ingest-time FakeSummarizer so a repaired row is provably
# re-summarized (new summary text, new model) rather than reusing the old one.
CORRUPT_SLIVER = "Item 1A | Risk Factors 12"


class RepairSummarizer:
    model = "repair-model"

    def summarize(self, section: str, text: str, source_url: str) -> str:
        return f"[repaired {section}] summary. Source: {source_url}"


class ExplodingOnSectionSummarizer(RepairSummarizer):
    """Raises only for one section — used to fail one filing mid-run while a
    later filing (damaged in a different section) still repairs."""

    def __init__(self, failing_section: str) -> None:
        self._failing_section = failing_section

    def summarize(self, section: str, text: str, source_url: str) -> str:
        if section == self._failing_section:
            raise RuntimeError("summarizer boom")
        return super().summarize(section, text, source_url)


class RepairEdgar:
    """fetch_document keyed by accession number; defaults to the canonical
    test HTML (the same document ingest stored, so shas match). An override
    simulates EDGAR serving a changed document (sha mismatch)."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.fetched: list[str] = []
        self._overrides = overrides or {}

    def fetch_document(self, filing: FilingRef) -> str:
        self.fetched.append(filing.accession_number)
        return self._overrides.get(filing.accession_number, HTML)


def ingest(db: Engine, tickers: list[str]) -> None:
    service = ResearchService(
        edgar=MultiTickerEdgar(tickers), summarizer=FakeSummarizer(), engine=db
    )
    for ticker in tickers:
        assert service.run(ticker).status == "ingested"


def summarise_all(db: Engine, ticker: str) -> None:
    """ADR-0014: the thesis is lazy — composed on the first GET-summary demand,
    not eagerly at ingest. Drive that demand so every section is summarised and
    the initial thesis snapshot exists, the state repair's thesis-append logic
    depends on (it appends a repaired thesis only when every section is
    non-pending). summarize_on_demand reads the DB and the summarizer only; the
    edgar here is unused."""
    service = ResearchService(
        edgar=MultiTickerEdgar([ticker]), summarizer=FakeSummarizer(), engine=db
    )
    assert service.summarize_on_demand(ticker) is not None


def filing_id_of(db: Engine, ticker: str) -> int:
    with db.connect() as conn:
        result: int = conn.execute(
            text(
                "SELECT f.id FROM filings f JOIN companies c ON c.id = f.company_id"
                " WHERE c.ticker = :ticker"
            ),
            {"ticker": ticker},
        ).scalar_one()
    return result


def corrupt(db: Engine, ticker: str, section: str, source_text: str = CORRUPT_SLIVER) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE filing_summaries SET source_text = :source_text"
                " WHERE filing_id = :filing_id AND section = :section"
            ),
            {"source_text": source_text, "filing_id": filing_id_of(db, ticker), "section": section},
        )


def seed_chunks(db: Engine, ticker: str) -> None:
    filing_id = filing_id_of(db, ticker)
    with db.begin() as conn:
        repo = Repository(conn)
        for section in ("business", "risk_factors", "mdna"):
            repo.replace_chunks(
                filing_id,
                section,
                "https://example.test/doc",
                [EmbeddedChunk(text=f"{section} chunk", embedding=[0.0] * 1024)],
                model="voyage-context-4",
                dimension=1024,
            )


def chunk_sections(db: Engine, ticker: str) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT section FROM filing_chunks WHERE filing_id = :filing_id"),
            {"filing_id": filing_id_of(db, ticker)},
        ).scalars()
        return set(rows)


def summary_row(db: Engine, ticker: str, section: str) -> tuple[str, str, str, str]:
    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT summary, source_text, source_url, model FROM filing_summaries"
                " WHERE filing_id = :filing_id AND section = :section"
            ),
            {"filing_id": filing_id_of(db, ticker), "section": section},
        ).one()
    return (row.summary, row.source_text, row.source_url, row.model)


def table_rows(db: Engine, table: str) -> list[tuple[object, ...]]:
    with db.connect() as conn:
        return [tuple(row) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))]


def test_repair_replaces_corrupted_section_deletes_chunks_leaves_rest(db: Engine) -> None:
    ingest(db, ["AAA"])
    seed_chunks(db, "AAA")
    corrupt(db, "AAA", "business")
    filings_before = table_rows(db, "filings")
    companies_before = table_rows(db, "companies")
    _, _, url_before, _ = summary_row(db, "AAA", "business")

    report = run_repair(RepairEdgar(), RepairSummarizer(), db)

    assert [r.status for r in report.results] == ["repaired"]
    assert [d.section for d in report.results[0].damaged] == ["business"]
    summary, source_text, source_url, model = summary_row(db, "AAA", "business")
    assert summary == f"[repaired business] summary. Source: {url_before}"
    assert "Revenue was $391,035 million" in source_text  # fresh extraction, not the sliver
    assert source_url == url_before  # untouched
    assert model == "repair-model"  # the current summarizer's model
    assert chunk_sections(db, "AAA") == {"risk_factors", "mdna"}  # only its chunks deleted
    assert table_rows(db, "filings") == filings_before
    assert table_rows(db, "companies") == companies_before


def test_clean_filings_and_unaffected_sections_are_byte_untouched(db: Engine) -> None:
    ingest(db, ["AAA", "BBB"])
    seed_chunks(db, "AAA")
    seed_chunks(db, "BBB")
    corrupt(db, "AAA", "mdna")
    aaa_business_before = summary_row(db, "AAA", "business")
    aaa_risks_before = summary_row(db, "AAA", "risk_factors")
    bbb_summaries_before = {
        s: summary_row(db, "BBB", s) for s in ("business", "risk_factors", "mdna")
    }
    bbb_chunks_before = [
        row for row in table_rows(db, "filing_chunks") if row[1] == filing_id_of(db, "BBB")
    ]

    report = run_repair(RepairEdgar(), RepairSummarizer(), db)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "repaired"
    assert by_ticker["BBB"].status == "clean"
    assert summary_row(db, "AAA", "business") == aaa_business_before
    assert summary_row(db, "AAA", "risk_factors") == aaa_risks_before
    assert {
        s: summary_row(db, "BBB", s) for s in ("business", "risk_factors", "mdna")
    } == bbb_summaries_before
    assert [
        row for row in table_rows(db, "filing_chunks") if row[1] == filing_id_of(db, "BBB")
    ] == bbb_chunks_before
    assert chunk_sections(db, "AAA") == {"business", "risk_factors"}  # only mdna re-queued


def test_repair_appends_thesis_snapshot_and_latest_research_serves_it(db: Engine) -> None:
    ingest(db, ["AAA"])
    summarise_all(db, "AAA")  # ADR-0014: lazy thesis — demand composes the initial snapshot
    corrupt(db, "AAA", "business")
    snapshots_before = table_rows(db, "thesis_snapshots")
    assert len(snapshots_before) == 1

    run_repair(RepairEdgar(), RepairSummarizer(), db)

    snapshots_after = table_rows(db, "thesis_snapshots")
    assert len(snapshots_after) == 2
    assert snapshots_after[0] == snapshots_before[0]  # the old snapshot is intact
    with db.connect() as conn:
        view = Repository(conn).latest_research("AAA")
    assert view is not None
    assert "[repaired business] summary." in view.thesis  # the re-summarized section
    assert "[risk_factors] summary." in view.thesis  # unaffected sections reuse stored summaries
    assert "[mdna] summary." in view.thesis
    # The appended snapshot identifies itself as a repair in the append-only
    # history; the ingest-time snapshot keeps its original title.
    assert view.thesis.startswith("# Repaired thesis snapshot:")
    assert "supersedes" in view.thesis.lower()
    old_content = str(snapshots_after[0][2])  # thesis_snapshots.content
    assert old_content.startswith("# Initial thesis snapshot:")


def test_second_run_is_idempotent_zero_repaired(db: Engine) -> None:
    ingest(db, ["AAA"])
    summarise_all(db, "AAA")  # initial thesis (1); repair can then append (→2)
    corrupt(db, "AAA", "business")
    first = run_repair(RepairEdgar(), RepairSummarizer(), db)
    assert first.repaired == 1

    second = run_repair(RepairEdgar(), RepairSummarizer(), db)

    assert second.repaired == 0
    assert [r.status for r in second.results] == ["clean"]
    assert count(db, "thesis_snapshots") == 2  # no snapshot appended by the clean rerun


def test_sha_mismatch_is_reported_and_skipped(db: Engine) -> None:
    ingest(db, ["AAA"])
    seed_chunks(db, "AAA")
    corrupt(db, "AAA", "business")
    summaries_before = table_rows(db, "filing_summaries")
    with db.connect() as conn:
        accession: str = conn.execute(text("SELECT accession_number FROM filings")).scalar_one()
    edgar = RepairEdgar(overrides={accession: HTML + "<!-- amended since ingest -->"})

    report = run_repair(edgar, RepairSummarizer(), db)

    assert [r.status for r in report.results] == ["sha_mismatch"]
    assert report.results[0].reason is not None and "content_sha256" in report.results[0].reason
    assert table_rows(db, "filing_summaries") == summaries_before  # nothing written
    assert chunk_sections(db, "AAA") == {"business", "risk_factors", "mdna"}
    # ADR-0014: ingest composes no thesis and no demand was made, so there is
    # none to begin with; the skipped filing appends none either.
    assert count(db, "thesis_snapshots") == 0


def test_unextractable_document_with_matching_sha_fails_as_parse_error(db: Engine) -> None:
    # The stored sha is made to match the bad document, so the sha guard
    # passes and extraction itself is what fails — the parse_error path.
    ingest(db, ["AAA"])
    seed_chunks(db, "AAA")
    bad_html = "<html><body><p>No items here at all.</p></body></html>"
    with db.begin() as conn:
        conn.execute(
            text("UPDATE filings SET content_sha256 = :sha"),
            {"sha": hashlib.sha256(bad_html.encode("utf-8")).hexdigest()},
        )
        accession: str = conn.execute(text("SELECT accession_number FROM filings")).scalar_one()
    summaries_before = table_rows(db, "filing_summaries")

    report = run_repair(RepairEdgar(overrides={accession: bad_html}), RepairSummarizer(), db)

    assert [r.status for r in report.results] == ["failed"]
    assert report.results[0].category == "parse_error"
    reason = report.results[0].reason
    assert reason is not None and "SectionExtractionError" in reason
    assert table_rows(db, "filing_summaries") == summaries_before  # nothing written
    assert chunk_sections(db, "AAA") == {"business", "risk_factors", "mdna"}
    # ADR-0014: lazy thesis, no demand made — none exists and the failed filing
    # appends none.
    assert count(db, "thesis_snapshots") == 0


def test_failure_mid_run_continues_and_writes_nothing_for_failed_filing(db: Engine) -> None:
    ingest(db, ["AAA", "BBB"])
    seed_chunks(db, "AAA")
    corrupt(db, "AAA", "business")  # summarizer explodes on this section
    corrupt(db, "BBB", "mdna")
    aaa_before = {s: summary_row(db, "AAA", s) for s in ("business", "risk_factors", "mdna")}

    report = run_repair(RepairEdgar(), ExplodingOnSectionSummarizer("business"), db)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "failed"
    assert by_ticker["AAA"].reason is not None and "RuntimeError" in by_ticker["AAA"].reason
    assert by_ticker["BBB"].status == "repaired"  # the filing after the failure still ran
    assert {
        s: summary_row(db, "AAA", s) for s in ("business", "risk_factors", "mdna")
    } == aaa_before
    assert chunk_sections(db, "AAA") == {"business", "risk_factors", "mdna"}  # not partially wiped
    # ADR-0014: ingest composes no thesis and no demand was made; BBB's repair
    # re-summarises only its damaged mdna while business/risk_factors stay
    # pending, so BBB is not fully summarised and its repair appends no thesis
    # either. AAA failed and wrote nothing. Net: zero snapshots.
    assert count(db, "thesis_snapshots") == 0


def test_audit_only_reports_corruption_and_writes_nothing(db: Engine) -> None:
    ingest(db, ["AAA", "BBB"])
    seed_chunks(db, "AAA")
    corrupt(db, "AAA", "risk_factors")
    before = {
        table: table_rows(db, table)
        for table in (
            "companies",
            "filings",
            "filing_summaries",
            "filing_chunks",
            "thesis_snapshots",
        )
    }

    report = run_repair(RepairEdgar(), None, db, audit_only=True)

    assert report.audit_only is True
    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "corrupted"
    assert [d.section for d in by_ticker["AAA"].damaged] == ["risk_factors"]
    diff = by_ticker["AAA"].damaged[0]
    assert diff.stored_chars == len(CORRUPT_SLIVER)
    assert diff.fresh_chars > diff.stored_chars
    assert by_ticker["BBB"].status == "clean"
    after = {
        table: table_rows(db, table)
        for table in (
            "companies",
            "filings",
            "filing_summaries",
            "filing_chunks",
            "thesis_snapshots",
        )
    }
    assert after == before  # byte-identical database


def test_report_accounts_for_every_audited_filing(db: Engine) -> None:
    ingest(db, ["AAA", "BBB", "CCC", "DDD"])
    corrupt(db, "BBB", "business")  # summarizer explodes on this section → failed
    corrupt(db, "CCC", "mdna")  # repairs
    with db.connect() as conn:
        ddd_accession: str = conn.execute(
            text(
                "SELECT f.accession_number FROM filings f"
                " JOIN companies c ON c.id = f.company_id WHERE c.ticker = 'DDD'"
            )
        ).scalar_one()
    edgar = RepairEdgar(overrides={ddd_accession: HTML + "<!-- amended -->"})

    report = run_repair(edgar, ExplodingOnSectionSummarizer("business"), db)

    assert report.audited == 4
    assert report.clean == 1
    assert report.repaired == 1
    assert report.sha_mismatches == 1
    assert len(report.failures) == 1
    assert report.clean + report.repaired + report.sha_mismatches + len(report.failures) == 4
    assert all(r.status in ("clean", "repaired", "sha_mismatch", "failed") for r in report.results)


def test_delay_is_applied_between_document_fetches_not_before_the_first(db: Engine) -> None:
    ingest(db, ["AAA", "BBB", "CCC"])
    naps: list[float] = []

    run_repair(RepairEdgar(), None, db, audit_only=True, delay_seconds=0.25, sleep=naps.append)

    assert naps == [0.25, 0.25]  # between fetches only


def test_format_report_lists_damage_and_failures() -> None:
    report = RepairReport(
        audit_only=True,
        results=(
            FilingAuditResult(ticker="AAA", accession_number="1-25-1", status="clean"),
            FilingAuditResult(
                ticker="BBB",
                accession_number="2-25-2",
                status="corrupted",
                damaged=(
                    SectionDiff(section="business", stored_chars=25, fresh_chars=1600),
                    SectionDiff(section="mdna", stored_chars=900, fresh_chars=1500),
                ),
            ),
            FilingAuditResult(
                ticker="CCC",
                accession_number="3-25-3",
                status="sha_mismatch",
                reason="stored content_sha256 does not match the fetched document",
            ),
            FilingAuditResult(
                ticker="DDD",
                accession_number="4-25-4",
                status="failed",
                category="parse_error",
                reason="SectionExtractionError: could not locate the start of 'business'",
            ),
        ),
    )
    formatted = format_report(report)
    assert "4 audited" in formatted
    assert "1 corrupted" in formatted
    assert "1 clean" in formatted
    assert "1 sha_mismatch" in formatted
    assert "1 failed" in formatted
    assert "BBB" in formatted and "business" in formatted and "1600" in formatted
    assert "CCC" in formatted and "content_sha256" in formatted
    assert "DDD" in formatted and "parse_error" in formatted
